"""Pure-memory XY/servo controller using the same intention/event interface.

Acknowledgement only accepts an operation. A later simulator iteration applies
the operation, and a separate final observation completes the virtual job.
Nothing here asserts or measures completion on a physical machine.
"""
from collections import deque
import json
import math
import queue
import threading
import time
import uuid
from .ir import Move, Pen, compile_paths
from .profiles import get_profile
from .transport import MemoryTransport
from .base import validate_job_context, validate_motion_lease


class ServoEncoder:
    """Alternate wire model: explicit servo commands, never a fabricated Z axis."""
    @staticmethod
    def encode(operation):
        if isinstance(operation,Pen):
            command=dict(servo='down' if operation.down else 'up')
        elif isinstance(operation,Move):
            command=dict(move_xy=[operation.x,operation.y],feed_mm_min=operation.feed)
        else:
            raise ValueError('Unsupported servo operation')
        return (json.dumps(command,separators=(',',':'))+'\n').encode('ascii')


class SimulatedController:
    def __init__(self, *, profile_id='simulator_servo', clock=time.monotonic,
                 timeout=10, motion_delay=0, **kwargs):
        if kwargs: raise TypeError('Unknown simulator options: '+', '.join(kwargs))
        self.device_profile=get_profile(profile_id)
        if not self.device_profile.capabilities.simulation:
            raise ValueError('Simulator requires a virtual profile')
        if not math.isfinite(timeout) or timeout<=0 or not math.isfinite(motion_delay) or motion_delay<0:
            raise ValueError('Invalid simulator timing')
        self.clock,self.timeout,self.motion_delay=clock,timeout,motion_delay
        self.commands=queue.Queue(); self.events=queue.Queue()
        self._lock=threading.RLock(); self._quit=threading.Event(); self._thread=None; self._listeners=[]
        self.transport=None; self.serial=None; self.state='disconnected'; self.connected=False
        self.session_id=None; self.geometry_id=None; self.current_job_id=None; self.mapping=None; self.profile=None; self.authorization_id=None
        self.wpos=(0.,0.); self.pen_state='up'; self.failure=''
        self.operations=deque(); self.pending=None; self.motion=None
        self.sent=self.acked=self.total=0; self.job=False; self._completion_pending=False
        self.work_bounds=(0.,0.,210.,297.); self.write_feed=3000.; self.travel_feed=4000.

    def start(self):
        if self._thread is None:
            self._thread=threading.Thread(target=self._run,name='MaliangVirtualDevice',daemon=True)
            self._thread.start()
        return self

    def close(self):
        self._quit.set()
        if self._thread: self._thread.join(2)
        with self._lock: self._disconnect()

    def submit(self,action,**kwargs): self.commands.put((action,kwargs))

    def subscribe_events(self,listener):
        with self._lock: self._listeners.append(listener)
        def unsubscribe():
            with self._lock:
                if listener in self._listeners: self._listeners.remove(listener)
        return unsubscribe

    def _emit(self,kind,text='',**fields):
        event=dict(kind=kind,text=text,simulation=True,job_id=fields.pop('job_id',self.current_job_id),**fields)
        self.events.put(event)
        for listener in tuple(self._listeners):
            try: listener(dict(event))
            except Exception: pass

    def drain_events(self):
        result=[]
        while True:
            try: result.append(self.events.get_nowait())
            except queue.Empty: return result

    def snapshot(self):
        with self._lock:
            return dict(connection=self.connected,state=self.state,session_id=self.session_id,
                geometry_id=self.geometry_id,job_id=self.current_job_id,simulation=True,device_profile=self.device_profile.id,
                capabilities=self.device_profile.capabilities.to_dict(),position_source='virtual_model',
                wpos=self.wpos if self.connected else None,wco=(0.,0.) if self.connected else None,
                page_to_machine=self.mapping,calibrated=self.mapping is not None,authorized=self.profile is not None,authorization_id=self.authorization_id,
                pen_state=self.pen_state,pen_up=None,pen_down=None,pen_touch=None,
                progress=(self.acked,self.total),sent=self.sent,failure=self.failure,
                pending=self.pending is not None or self.motion is not None,status_pending=False,
                queued_commands=self.commands.qsize(),
                coordinate_age=0 if self.connected else math.inf,points_count=0,anchor_available=False,
                clearance=None,clearance_bounds=None,clearance_remaining=None,idle=self.state in {'ready','complete'},
                mapping_preset=None,anchor_preset_source=None)

    def _clear_job(self):
        self.operations.clear(); self.pending=self.motion=None
        self.job=False; self._completion_pending=False

    def _invalidate(self): self.mapping=None; self.profile=None; self.authorization_id=None

    def _disconnect(self):
        self._clear_job(); self._invalidate(); self.connected=False; self.session_id=None
        self.state='disconnected'
        if self.transport: self.transport.close()
        self.transport=None

    def _prepare(self,kw):
        self.profile=None; self.authorization_id=None
        authorization_id=kw.get('authorization_id')
        if authorization_id is not None and (not isinstance(authorization_id,str) or not authorization_id.strip() or len(authorization_id)>128):
            raise ValueError('Invalid authorization identity')
        if self.job or self.pending or self.motion or self.operations: raise ValueError('Stop virtual job before preparing')
        geometry=kw.get('geometry_id',self.geometry_id)
        if not isinstance(geometry,str) or not geometry.strip():
            raise ValueError('Current nonempty page geometry identity required')
        bounds=tuple(kw.get('work_bounds',self.work_bounds))
        write=float(kw.get('write_feed',self.write_feed)); travel=float(kw.get('travel_feed',self.travel_feed))
        # Reuse generic compilation validation without accepting any job here.
        if len(bounds)!=4 or not all(math.isfinite(v) for v in bounds) or bounds[2]<=bounds[0] or bounds[3]<=bounds[1]:
            raise ValueError('Invalid virtual work bounds')
        if not all(math.isfinite(v) and 0<v<=10000 for v in (write,travel)):
            raise ValueError('Invalid virtual feed')
        self.geometry_id=geometry; self.work_bounds=bounds; self.write_feed=write; self.travel_feed=travel
        self.mapping=(1.,0.,0.,1.,0.,0.); self.profile=dict(virtual=True,geometry_id=geometry,work_bounds=bounds)
        self.authorization_id=authorization_id
        self.state='ready'; self.failure=''
        self._emit('authorized',authorization_id=authorization_id)
        self._emit('prepared','Explicit virtual identity mapping and servo authorization')

    def _action(self,action,kw):
        if action=='connect':
            if self.connected: raise ValueError('Already connected')
            self.transport=MemoryTransport(); self.connected=True; self.session_id=uuid.uuid4().hex; self.current_job_id=None
            self.state='ready'; self.failure=''; self.wpos=(0.,0.); self.pen_state='up'; self._invalidate()
            # Connection establishes virtual axes only; document authorization is explicit.
            self.mapping=(1.,0.,0.,1.,0.,0.)
            if kw.get('geometry_id'): self._prepare(kw)
            self._emit('connected','Offline XY/servo simulator; no physical device')
            return
        if action=='disconnect': return self._disconnect()
        if not self.connected: raise ValueError('Disconnected')
        if action=='geometry_changed':
            if self.job or self.pending or self.motion or self.operations:
                self._clear_job(); self.state='stopped'
                self._emit('fault','Page geometry changed during virtual job')
            self.geometry_id=kw['geometry_id']; self._invalidate(); return
        if action in {'prepare_virtual','authorize'}: return self._prepare(kw)
        if action=='manual_reposition':
            if self.job or self.pending or self.motion or self.operations: raise ValueError('Stop virtual job before manual reposition')
            self._invalidate(); self._emit('warning','Virtual mapping invalidated; prepare_virtual required'); return
        if action in {'stop','cancel','confirm_reset'}:
            self._clear_job(); self._invalidate(); self.state='stopped'; self._emit('cancelled','Virtual queue stopped'); return
        if action=='pause':
            if not self.job or self.state!='running': raise ValueError('Only running jobs can pause')
            self.state='paused'; self._emit('paused'); return
        if action=='resume':
            validate_motion_lease(kw)
            if self.state!='paused': raise ValueError('Only confirmed Hold can resume')
            self.state='running'; self._emit('resumed'); return
        if action=='poll': return
        if action=='start_job':
            validate_job_context(self,kw)
            if self.state not in {'ready','complete'} or self.job or self.pending or self.motion:
                raise ValueError('Virtual controller is busy')
            if self.profile is None or kw['geometry_id']!=self.geometry_id:
                raise ValueError('Current runtime authorization required')
            operations=compile_paths(kw['polylines'],mapping=self.mapping,work_bounds=self.work_bounds,
                write_feed=self.write_feed,travel_feed=self.travel_feed,preflight_passed=kw.get('preflight_passed',False))
            validate_motion_lease(kw)
            self.operations=deque(operations); self.sent=self.acked=0; self.total=len(operations)
            self.current_job_id=kw.get('job_id')
            self.job=True; self.state='running'; self._completion_pending=False; self._emit('started'); return
        if self.state not in {'ready','complete'} or self.pending or self.motion:
            raise ValueError('Virtual controller is busy')
        if action in {'lift_pen','lower_pen'}:
            if self.mapping is None: raise ValueError('Explicit virtual preparation required')
            self.operations.append(Pen(action=='lower_pen')); return
        if action=='jog':
            axis=kw['axis'].upper(); distance=float(kw['distance']); feed=float(kw.get('feed',60))
            if axis not in {'X','Y'} or not math.isfinite(distance) or not 0<abs(distance)<=1 or not math.isfinite(feed) or not 0<feed<=300:
                raise ValueError('Invalid bounded XY simulator jog')
            x,y=self.wpos; target=(x+distance,y) if axis=='X' else (x,y+distance)
            l,t,r,b=self.work_bounds
            if not l<=target[0]<=r or not t<=target[1]<=b: raise ValueError('Jog exceeds virtual work bounds')
            self.operations.append(Move(*target,feed)); return
        raise ValueError('Unsupported simulator action '+action)

    def tick(self):
        with self._lock:
            while True:
                try: action,kw=self.commands.get_nowait()
                except queue.Empty: break
                try: self._action(action,kw)
                except (ValueError,TypeError,KeyError) as exc: self._emit('rejected',str(exc),action=action,job_id=kw.get('job_id',self.current_job_id))
            if not self.connected or self.state in {'paused','stopped'}: return
            if self._completion_pending:
                self._completion_pending=False; self.job=False; self.state='complete'
                self._emit('complete','Virtual completion observed; no physical completion claim'); return
            if self.motion is not None:
                operation,due=self.motion
                if self.clock()<due: return
                if isinstance(operation,Move): self.wpos=(operation.x,operation.y)
                else: self.pen_state='down' if operation.down else 'up'
                self.motion=None
                if self.job and not self.operations:
                    self.state='waiting_idle'; self._completion_pending=True
                return
            if self.pending is not None:
                operation=self.pending; self.pending=None
                if self.job: self.acked+=1
                self._emit('ack','Operation accepted; virtual motion may still be pending',progress=(self.acked,self.total))
                self.motion=(operation,self.clock()+self.motion_delay); return
            if self.operations:
                operation=self.operations.popleft(); payload=ServoEncoder.encode(operation)
                self.transport.write(payload); self.pending=operation
                if self.job: self.sent+=1
                self._emit('tx',payload.decode().rstrip())

    poll=tick

    def _run(self):
        while not self._quit.is_set():
            self.tick(); self._quit.wait(.01)

    @staticmethod
    def device_list(): return []
