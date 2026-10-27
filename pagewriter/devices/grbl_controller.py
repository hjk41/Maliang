"""Exclusive background Grbl controller. Importing this module never opens hardware.

Only the worker thread accesses Serial; callers submit intentions and consume events.
Runtime coordinate evidence is never restored from an imported profile.
"""
from __future__ import annotations
import math
import queue
import re
import threading
import time
import uuid
from collections import deque
from .profiles import get_profile
from .transport import SerialTransport
from .base import validate_job_context, validate_motion_lease, PEN_CALIBRATION_STROKE_MM
from .grbl_protocol import LineParser, JobSession, MachineProfile, compile_machine_job


def fit_affine(points):
    if len(points) != 3:
        raise ValueError('Exactly three independently positioned points required')
    if not all(math.isfinite(float(v)) for p, q in points for v in (*p, *q)):
        raise ValueError('Nonfinite calibration')
    (p0,q0),(p1,q1),(p2,q2)=points
    x1,y1=p1[0]-p0[0],p1[1]-p0[1]
    x2,y2=p2[0]-p0[0],p2[1]-p0[1]
    det=x1*y2-x2*y1
    if abs(det)<1 or min(math.dist(p0,p1), math.dist(p0,p2),math.dist(p1,p2))<2:
        raise ValueError('Degenerate page triangle')
    u1,v1=q1[0]-q0[0],q1[1]-q0[1]
    u2,v2=q2[0]-q0[0],q2[1]-q0[1]
    a,c=(u1*y2-u2*y1)/det,(x1*u2-x2*u1)/det
    b,d=(v1*y2-v2*y1)/det,(x1*v2-x2*v1)/det
    if not (0.2<=math.hypot(a,b)<=5 and 0.2<=math.hypot(c,d)<=5) or abs(a*d-b*c)<0.04:
        raise ValueError('Degenerate or unreasonable machine mapping')
    return a,b,c,d,q0[0]-a*p0[0]-c*p0[1],q0[1]-b*p0[0]-d*p0[1]


class GrblController:
    def __init__(self, serial_factory=None, clock=time.monotonic, timeout=10, profile_id='songboshi_2024'):
        self.device_profile=get_profile(profile_id)
        if self.device_profile.driver!='grbl': raise ValueError('Use create_controller for this device profile')
        self.transport=None; self._listeners=[]
        self.serial_factory=serial_factory
        if not math.isfinite(timeout) or timeout<=0: raise ValueError('Finite positive timeout required')
        self.clock,self.timeout=clock,timeout
        self.commands=queue.Queue(); self.events=queue.Queue(); self._deferred=deque()
        self._lock=threading.RLock(); self._quit=threading.Event(); self._thread=None
        self.serial=None; self.parser=LineParser(); self.pending=None; self.lines=deque()
        self.state='disconnected'; self.session_id=None; self.geometry_id=None; self.current_job_id=None
        self.wco=None; self.wpos=None; self.coordinate_time=-math.inf
        self.status_state=None; self.status_wait=False; self.status_deadline=None
        self.mapping=None; self.points=[]; self.pen_up=None; self.pen_down=None; self.pen_touch=None
        self.profile=None; self.authorization_id=None; self.anchor_mapping=None; self.clearance=None
        self.mapping_preset=None; self.anchor_preset_source=None
        self.travel=[0.,0.,0.]; self.sent=0; self.acked=0; self.total=0
        self.job=False; self.barrier=False; self.final_query=False; self.failure=''
        self.units_mm=False; self.last_poll=-math.inf; self.jog_target=None; self.clearance_center=None
        self.motion_finish=clock(); self.planned_position=None; self.planned_feed=None
        self.pause_started=None; self.hold_deadline=None
        self.completion_deadline=None; self.next_final_poll=0.; self.final_poll_interval=.25
        self.pen_calibration=None

    def _command_deadline(self,line,now):
        """Budget accepted but unfinished motion, never merely the next serial ACK.

        Generated jobs use G21/G90/G94/G1; jog uses explicit G21/G91/F.
        Nominal queued duration drains with wall time. A 2x execution allowance
        plus the communication timeout covers planner backpressure and barriers.
        """
        words={key:float(value) for key,value in re.findall(r'([XYZF])\s*([-+]?(?:\d+(?:\.\d*)?|\.\d+))',line)}
        is_jog=line.startswith('$J=')
        is_move=is_jog or bool(re.search(r'\bG0?1(?:\s|$)',line))
        if 'F' in words and not is_jog: self.planned_feed=words['F']
        if is_move:
            origin=self.wpos if is_jog else self.planned_position
            if origin is None: origin=self.wpos
            if origin is None: raise ValueError('Motion time budget needs known starting position')
            target=tuple((origin[i]+words.get(axis,0)) if is_jog else words.get(axis,origin[i]) for i,axis in enumerate('XYZ'))
            distance=math.dist(origin,target); feed=words.get('F',self.planned_feed)
            if feed is None or not math.isfinite(feed) or feed<=0: raise ValueError('Motion needs finite positive feed')
            duration=distance*60/feed
            if not math.isfinite(duration): raise ValueError('Nonfinite motion duration')
            self.motion_finish=max(now,self.motion_finish)+duration+.25
            if not is_jog: self.planned_position=target
        remaining=max(0,self.motion_finish-now)
        return now+self.timeout+2*remaining

    def start(self):
        if not self._thread:
            self._thread=threading.Thread(target=self._run, name='PageWriterSerial',daemon=True); self._thread.start()
        return self

    def close(self):
        self.submit('disconnect'); self._quit.set()
        if self._thread: self._thread.join(2)
        else:
            with self._lock: self._disconnect()

    def submit(self, action, **kwargs):
        with self._lock:
            if action=='authorize':
                # A pending candidate can never inherit a previous wider scope.
                self.profile=None; self.authorization_id=None
            self.commands.put((action,kwargs))

    def drain_events(self):
        result=[]
        while True:
            try: result.append(self.events.get_nowait())
            except queue.Empty: return result

    def snapshot(self):
        with self._lock:
            return dict(simulation=False,device_profile=self.device_profile.id,
                capabilities=self.device_profile.capabilities.to_dict(),position_source='firmware_work_coordinates',
                connection=self.serial is not None,state=self.state,session_id=self.session_id,job_id=self.current_job_id,
                wpos=self.wpos,wco=self.wco,calibrated=self.mapping is not None,
                authorized=self.profile is not None,authorization_id=self.authorization_id,pen_up=self.pen_up,pen_down=self.pen_down,pen_touch=self.pen_touch,
                progress=(self.acked,self.total),failure=self.failure,geometry_id=self.geometry_id,
                pending=self.pending is not None,status_pending=self.status_wait,coordinate_age=self.clock()-self.coordinate_time,
                queued_commands=self.commands.qsize()+len(self._deferred),
                points_count=len(self.points),anchor_available=self.anchor_mapping is not None,clearance=self.clearance,idle=self.status_state=='Idle',
                page_to_machine=self.mapping,clearance_bounds=self._clearance_bounds(),
                clearance_remaining=self._clearance_remaining(),mapping_preset=self.mapping_preset,
                anchor_preset_source=self.anchor_preset_source,
                pen_calibration=({key:self.pen_calibration[key] for key in ('id','phase','up','down')}
                                 if self.pen_calibration else None))

    def _clearance_bounds(self):
        if self.clearance_center is None or self.clearance is None: return None
        xy,z=self.clearance
        return tuple((p-r,p+r) for p,r in zip(self.clearance_center,(xy,xy,z)))

    def _clearance_remaining(self):
        bounds=self._clearance_bounds()
        if bounds is None or self.wpos is None: return None
        return tuple((p-lo,hi-p) for p,(lo,hi) in zip(self.wpos,bounds))

    def subscribe_events(self,listener):
        """Worker-thread notifications; consumers must marshal UI work themselves."""
        with self._lock: self._listeners.append(listener)
        def unsubscribe():
            with self._lock:
                if listener in self._listeners: self._listeners.remove(listener)
        return unsubscribe

    def _emit(self,kind,text='',**fields):
        event=dict(kind=kind,text=text,job_id=fields.pop('job_id',self.current_job_id),**fields)
        self.events.put(event)
        for listener in tuple(self._listeners):
            try: listener(dict(event))
            except Exception: pass  # Observers cannot interrupt the transport FSM.

    def _invalidate(self, preserve_axes=False):
        self.pen_calibration=None
        if preserve_axes:
            self.anchor_preset_source=self.mapping_preset if self.mapping is not None else self.anchor_preset_source
        else:
            self.anchor_preset_source=None
        self.mapping_preset=None
        self.anchor_mapping=(self.mapping or self.anchor_mapping) if preserve_axes else None
        self.mapping=None; self.points=[]; self.profile=None; self.authorization_id=None; self.pen_up=self.pen_down=self.pen_touch=None

    def _write(self,data):
        if self.serial is None: raise RuntimeError('Disconnected')
        if self.transport.write(data)!=len(data): raise IOError('Partial serial write')
        self._emit('tx',data.decode('ascii',errors='backslashreplace'))

    def _fault(self,reason):
        self.failure=str(reason); self.lines.clear(); self.job=False; self.barrier=False; self.final_query=False
        self.state='unknown'; self._invalidate(); self.pending=None
        self.pause_started=self.hold_deadline=None
        self.completion_deadline=None
        try:
            if self.serial: self._write(b'!')
        except Exception: pass
        self._emit('fault',self.failure)

    def _fresh(self):
        if not self.units_mm or self.wpos is None or self.wco is None or self.clock()-self.coordinate_time>2:
            raise ValueError('Fresh millimetre work coordinates and WCO required')
        if self.status_state!='Idle' or self.pending or self.lines or self.status_wait or self.parser.buffer or self.jog_target is not None:
            raise ValueError('Fresh Idle and empty command/status queues required')
        if self.state not in {'ready','complete'}: raise ValueError('Controller is not ready')
        return self.wpos

    def _query(self):
        if not self.status_wait:
            self._write(b'?'); self.status_wait=True; self.status_deadline=self.clock()+self.timeout

    def _connect(self,port='COM3'):
        if self.serial is not None: raise ValueError('Already connected')
        self.transport=SerialTransport(port,self,self.serial_factory,self.device_profile.serial_baud or 115200)
        s=self.transport.serial
        self.serial=s; self.parser=LineParser(); self.pending=None; self.lines=deque(['$I','$G','$$','$#'])
        self.session_id=uuid.uuid4().hex; self.current_job_id=None; self._invalidate(); self.wco=self.wpos=None
        self.units_mm=False; self.coordinate_time=-math.inf; self.status_wait=False
        self.state='connecting'; self.failure=''; self.travel=[0.,0.,0.]; self.clearance=None; self.clearance_center=None; self.jog_target=None
        self.motion_finish=self.clock(); self.planned_position=None; self.planned_feed=None; self.pause_started=self.hold_deadline=None
        self.completion_deadline=None; self.next_final_poll=0.
        self._query()

    def _disconnect(self):
        if self.serial:
            if self.job or self.pending or self.status_state in {'Run','Jog','Hold'}:
                try: self._write(b'!')
                except Exception: pass
                self._emit('warning','Disconnected after Hold request; physical stop unconfirmed')
            self.transport.close(); self.transport=None; self.serial=None
        self.pending=None; self.lines.clear(); self._invalidate(); self.session_id=None
        self.wco=self.wpos=None; self.job=False; self.status_wait=False; self.state='disconnected'
        self.pause_started=self.hold_deadline=None
        self.completion_deadline=None; self.final_query=False

    def _action(self,action,kw):
        if action=='cancel': action='stop'
        if action in {'connect','disconnect','stop','confirm_reset','pause'}:
            self._deferred.clear()  # never run a waiting motion after these
        if action=='connect': return self._connect(**kw)
        if action=='disconnect': return self._disconnect()
        if not self.serial: raise ValueError('Disconnected')
        if action=='poll':
            if self.state=='calibrating' or (self.job and self.state not in {'paused','pausing'}): return
            return self._query()
        if action=='stop':
            self.pause_started=self.hold_deadline=None
            self.completion_deadline=None; self.final_query=False
            self.lines.clear(); self.job=False; self.state='stopped'; self._invalidate(); return self._write(b'!')
        if action=='confirm_reset':
            self.pause_started=self.hold_deadline=None
            self.completion_deadline=None; self.final_query=False
            self.lines.clear(); self.pending=None; self.job=False; self._invalidate(); self.wco=self.wpos=None
            self.state='unknown'; return self._write(b'\x18')
        if action=='pause':
            if self.state!='running': raise ValueError('Only running jobs can pause')
            self.pause_started=self.clock(); self.hold_deadline=self.clock()+self.timeout
            self.state='pausing'; self._write(b'!'); self._query(); return
        if action=='resume':
            validate_motion_lease(kw)
            if self.state!='paused': raise ValueError('Only confirmed Hold can resume')
            elapsed=self.clock()-self.pause_started
            if self.pending: self.pending=(self.pending[0],self.pending[1]+elapsed)
            if self.completion_deadline is not None: self.completion_deadline+=elapsed
            self.motion_finish+=elapsed; self.pause_started=self.hold_deadline=None
            self._write(b'~'); self.state='running'; return
        if action=='geometry_changed':
            if self.state=='calibrating': self._fault('Page geometry changed during pen calibration')
            if self.job: self._fault('Page geometry changed during job')
            self.geometry_id=kw['geometry_id']; self._invalidate(); return
        if action=='manual_reposition':
            if self.state=='calibrating' or self.job or self.pending or self.lines: raise ValueError('Drain commands and stop job before manual reposition')
            self._invalidate(preserve_axes=True); self.clearance=self.clearance_center=None
            self._emit('warning','Manual movement is not measured by the controller; inspect and declare clearance again'); return
        pos=self._fresh()
        if self.pen_calibration and (
                (self.pen_calibration['phase']=='inspect' and action not in {'calibrate_pen','confirm_pen_calibration'})
                or action in {'capture_pen','set_pen_stroke','set_pen_touch'}):
            self.pen_calibration=None
        if action=='calibrate_pen':
            direction=kw.get('direction'); calibration_id=kw.get('calibration_id')
            if (kw.get('confirmed') is not True or type(direction) is not int or direction not in (-1,1)
                    or not isinstance(calibration_id,str) or not calibration_id.strip() or len(calibration_id)>128):
                raise ValueError('Confirmed 5 mm pen calibration and direction +/-1 required')
            if self.pen_calibration and self.pen_calibration['id']==calibration_id:
                raise ValueError('Pen calibration request already used')
            if not self.clearance: raise ValueError('Declare inspected clearance first')
            distance=direction*PEN_CALIBRATION_STROKE_MM
            target=(pos[0],pos[1],pos[2]+distance)
            if any(not lo<=v<=hi for v,(lo,hi) in zip(target,self._clearance_bounds())):
                raise ValueError('Pen calibration exceeds inspected Z envelope')
            self.pen_up=self.pen_down=self.pen_touch=None
            self.profile=None; self.authorization_id=None
            self.pen_calibration=dict(id=calibration_id,phase='lowering',up=pos[2],down=target[2],origin=tuple(pos))
            self.state='calibrating'; self.jog_target=target; self.coordinate_time=-math.inf
            self.next_final_poll=0.
            self.travel=[v-c for v,c in zip(target,self.clearance_center)]
            self.lines.append(f'$J=G21 G91 Z{distance:.4f} F30.000')
            self._emit('pen_calibration_progress',calibration_id=calibration_id,phase='lowering'); return
        if action=='confirm_pen_calibration':
            calibration=self.pen_calibration
            if (not calibration or calibration['phase']!='inspect' or
                    kw.get('calibration_id')!=calibration['id'] or
                    any(abs(a-b)>.05 for a,b in zip(pos,calibration['origin']))):
                raise ValueError('Pen calibration result is stale; run calibration again')
            if kw.get('confirmed') is not True:
                raise ValueError('Pen calibration requires visual confirmation of the dot and intact paper')
            self.pen_up,self.pen_down=calibration['up'],calibration['down']
            self.pen_touch=None  # No contact sensor: never claim a measured surface.
            self.profile=None; self.authorization_id=None; calibration['phase']='confirmed'
            self._emit('pen_calibration_confirmed',calibration_id=calibration['id']); return
        if action=='declare_clearance':
            xy,z=float(kw['xy_mm']),float(kw['z_mm'])
            if not (0<xy<=10 and 0<z<=5): raise ValueError('Inspected clearance maximum XY 10mm, Z 5mm')
            if self.clearance is not None and kw.get('confirmed_new_range') is not True:
                raise ValueError('New physical range needs explicit confirmation; calibration will be invalidated')
            self.clearance=(xy,z); self.clearance_center=tuple(pos); self.travel=[0.,0.,0.]; self._invalidate(preserve_axes=True); return
        if action=='jog':
            if self.status_wait or self.parser.buffer: raise ValueError('Drain status query before jogging')
            axis=kw['axis'].upper(); distance=float(kw['distance']); feed=float(kw.get('feed',60))
            if axis not in 'XYZ' or len(axis)!=1 or not all(map(math.isfinite,(distance,feed))) or not 0<feed<=300:
                raise ValueError('Invalid bounded jog')
            if not self.clearance: raise ValueError('Declare physical clearance first')
            i='XYZ'.index(axis); step=.25 if i==2 else 1.; limit=self.clearance[1 if i==2 else 0]
            if i==2 and feed>30: raise ValueError('Z jog feed maximum is 30 mm/min')
            target=tuple(v+(distance if k==i else 0) for k,v in enumerate(pos))
            if distance==0 or abs(distance)>step or any(not lo<=v<=hi for v,(lo,hi) in zip(target,self._clearance_bounds())):
                raise ValueError('Jog exceeds inspected displacement envelope or single step')
            self.travel=[v-c for v,c in zip(target,self.clearance_center)]; self.profile=None; self.authorization_id=None
            self.jog_target=target
            self.coordinate_time=-math.inf
            self.lines.append(f'$J=G21 G91 {axis}{distance:.4f} F{feed:.3f}'); return
        if action=='capture_point':
            p=(float(kw['page_x']),float(kw['page_y']))
            if not all(map(math.isfinite,p)): raise ValueError('Nonfinite page point')
            self.points.append((p,pos[:2])); return
        if action=='fit_calibration':
            mapping=fit_affine(self.points); geometry_id=kw['geometry_id']
            self.mapping=mapping; self.geometry_id=geometry_id; self.profile=None; self.authorization_id=None
            self.mapping_preset=self.anchor_preset_source=None; self.anchor_mapping=None
            return
        if action=='anchor':
            if not kw.get('axes_confirmed') or self.anchor_mapping is None: raise ValueError('Confirmed previous axes required')
            a,b,c,d,_,_=self.anchor_mapping; x,y=float(kw['page_x']),float(kw['page_y'])
            if not all(map(math.isfinite,(x,y))): raise ValueError('Nonfinite anchor')
            self.mapping=(a,b,c,d,pos[0]-a*x-c*y,pos[1]-b*x-d*y); self.anchor_mapping=None
            self.mapping_preset=self.anchor_preset_source; self.anchor_preset_source=None
            return
        if action=='anchor_preset':
            # Explicit device-specific orientation, verified by the user on this
            # machine. It never changes generic fitting or composes old mappings.
            presets=self.device_profile.presets
            preset=kw.get('preset')
            if not isinstance(preset,str) or preset not in presets or kw.get('axes_confirmed') is not True:
                raise ValueError('Known paper preset and explicit axes confirmation required')
            geometry_id=kw.get('geometry_id')
            if not isinstance(geometry_id,str) or not geometry_id.strip():
                raise ValueError('Current nonempty page geometry identity required')
            x,y=float(kw['page_x']),float(kw['page_y'])
            if not all(map(math.isfinite,(x,y))): raise ValueError('Nonfinite page anchor')
            a,b,c,d=presets[preset]
            # XY anchoring does not move the pen or the paper surface: pen
            # heights measured in this session stay valid. Mapping, points
            # and authorization are replaced.
            heights=(self.pen_up,self.pen_down,self.pen_touch)
            self._invalidate()
            self.pen_up,self.pen_down,self.pen_touch=heights
            self.mapping=(a,b,c,d,pos[0]-a*x-c*y,pos[1]-b*x-d*y)
            self.mapping_preset=preset
            self.geometry_id=geometry_id
            return
        if action=='capture_pen':
            if kw['kind']=='up': self.pen_up=pos[2]
            elif kw['kind']=='down': self.pen_down=pos[2]
            else: raise ValueError('Pen capture must be up/down')
            self.pen_touch=None; self.profile=None; self.authorization_id=None; return
        if action=='set_pen_stroke':
            distance=float(kw['distance']); direction=kw['direction']
            if kw.get('confirmed') is not True or direction not in (-1,1) or isinstance(direction,bool) or not math.isfinite(distance) or not 0<distance<=5:
                raise ValueError('Confirmed finite stroke and direction +/-1 required')
            if self.clearance is None: raise ValueError('Declare inspected clearance first')
            down=pos[2]+direction*distance; lo,hi=self._clearance_bounds()[2]
            if not lo<=down<=hi: raise ValueError('Pen stroke exceeds declared Z envelope')
            self.pen_up=pos[2]; self.pen_down=down; self.pen_touch=None; self.profile=None; self.authorization_id=None; return
        if action=='set_pen_touch':
            # Current Z is where the user saw the tip just touch the paper.
            # Writing presses a little further (spring holder); travel lifts clear.
            lift,press=float(kw['lift']),float(kw['press']); direction=kw['direction']
            if kw.get('confirmed') is not True or direction not in (-1,1) or isinstance(direction,bool) or not all(map(math.isfinite,(lift,press))) or not (0<lift<=5 and 0<=press<=5):
                raise ValueError('Confirmed touch with finite lift/press and direction +/-1 required')
            if self.clearance is None: raise ValueError('Declare inspected clearance first')
            touch=pos[2]; up=touch-direction*lift; down=touch+direction*press
            lo,hi=self._clearance_bounds()[2]
            if not (lo<=up<=hi and lo<=down<=hi): raise ValueError('Pen touch heights exceed declared Z envelope')
            self.pen_touch=touch; self.pen_up=up; self.pen_down=down; self.profile=None; self.authorization_id=None; return
        if action in {'lift_pen','lower_pen'}:
            if self.status_wait or self.parser.buffer: raise ValueError('Drain status query before jogging')
            if self.pen_up is None or self.pen_down is None or not self.clearance: raise ValueError('Pen heights required before lifting')
            height=self.pen_up if action=='lift_pen' else self.pen_down
            distance=height-pos[2]
            if abs(distance)<1e-4: return
            direction=self.pen_up-self.pen_down if action=='lift_pen' else self.pen_down-self.pen_up
            if distance*direction<=0: raise ValueError('Lift must move away from the paper' if action=='lift_pen' else 'Lower must move toward the captured paper height')
            target=(pos[0],pos[1],height)
            if any(not lo<=v<=hi for v,(lo,hi) in zip(target,self._clearance_bounds())):
                raise ValueError('Jog exceeds inspected displacement envelope or single step')
            self.travel=[v-c for v,c in zip(target,self.clearance_center)]; self.profile=None; self.authorization_id=None
            self.jog_target=target; self.coordinate_time=-math.inf
            self.lines.append(f'$J=G21 G91 Z{distance:.4f} F120.000'); return
        if action=='authorize':
            self.profile=None; self.authorization_id=None
            authorization_id=kw.get('authorization_id')
            if authorization_id is not None and (not isinstance(authorization_id,str) or not authorization_id.strip() or len(authorization_id)>128):
                raise ValueError('Invalid authorization identity')
            if self.mapping is None or self.pen_up is None or self.pen_down is None: raise ValueError('Runtime XY and Z captures required')
            params={key:value for key,value in kw.items() if key!='authorization_id'}; params.update(page_to_machine=self.mapping,z_up=self.pen_up,z_down=self.pen_down,
                xy_verified=True,z_verified=True)
            p=MachineProfile(**params); p.validate()
            for attribute in ('max_write_feed','max_travel_feed','max_z_feed'):
                model_limit=getattr(self.device_profile,attribute)
                runtime_limit=getattr(p,attribute)
                if model_limit is not None and runtime_limit is not None and runtime_limit>model_limit:
                    raise ValueError('Runtime feed exceeds configured device model maximum')
            self.profile=p; self.authorization_id=authorization_id
            self._emit('authorized',authorization_id=authorization_id); return
        if action=='start_job':
            validate_job_context(self,kw)
            if self.status_wait: raise ValueError('Wait for outstanding status before starting job')
            if self.profile is None or kw['geometry_id']!=self.geometry_id: raise ValueError('Current runtime authorization required')
            left,top,right,bottom=self.profile.work_bounds
            zlo,zhi=self.profile.z_bounds
            if not (left<=pos[0]<=right and top<=pos[1]<=bottom and zlo<=pos[2]<=zhi):
                raise ValueError('Current position is outside runtime authorized bounds')
            # Compile from geometry here, never trust arbitrary imported G-code.
            lines=compile_machine_job(kw['polylines'],self.profile,preflight_passed=kw.get('preflight_passed',False))
            validate_motion_lease(kw)  # Compilation may outlive a revoked/expired lease.
            self.lines=deque(JobSession._normalize(l) for l in lines)
            self.total=len(lines); self.sent=self.acked=0; self.job=True; self.barrier=False; self.final_query=False
            self.motion_finish=self.clock(); self.planned_position=tuple(pos); self.planned_feed=None
            self.pause_started=self.hold_deadline=None
            self.completion_deadline=None; self.next_final_poll=0.
            self.current_job_id=kw.get('job_id')
            self.state='running'; self._emit('started'); return
        raise ValueError('Unknown action '+action)

    def _handle(self,event):
        self._emit('rx',event.text)
        if event.kind in {'error','alarm','malformed','unexpected','reset'}:
            self._fault(event.text); return
        if event.kind=='info':
            if event.text=='$13=0': self.units_mm=True
            elif event.text.startswith('$13='): self.units_mm=False; self._fault('Status units are not millimetres')
        if event.kind=='ack':
            if self.pending is None: self._fault('Unexpected acknowledgement'); return
            command,_=self.pending; self.pending=None
            if command.startswith('$J='): self.coordinate_time=-math.inf
            if self.job:
                # This device can acknowledge dwell before motion has finished.
                # Preserve the outstanding motion estimate until a fresh Idle.
                if command=='G4 P0': self.barrier=True
                else: self.acked+=1
            return
        if event.kind!='status': return
        self.status_state=str(event.fields['state']).split(':')[0]
        if self.state=='connecting' and self.status_state!='Idle':
            self._fault('Connection found active device; inspect and explicitly recover'); return
        fresh_query=self.status_wait; self.status_wait=False; self.status_deadline=None
        newco=event.fields.get('WCO')
        if newco is not None:
            if self.wco is not None and newco!=self.wco:
                self._invalidate()
                self.clearance=self.clearance_center=None
                if self.state=='calibrating': self._fault('WCO changed during pen calibration'); return
                if self.job: self._fault('WCO changed during job'); return
            self.wco=newco
        if 'WPos' in event.fields:
            self.wpos=event.fields['WPos']
            if fresh_query: self.coordinate_time=self.clock()
        elif 'MPos' in event.fields and self.wco is not None:
            self.wpos=tuple(a-b for a,b in zip(event.fields['MPos'],self.wco))
            if fresh_query: self.coordinate_time=self.clock()
        if self.status_state in {'Alarm','Door','Sleep','Check','Home'}:
            self._fault('Unsafe status '+self.status_state); return
        if self.jog_target is not None and fresh_query and self.status_state=='Idle' and self.pending is None:
            if self.wpos is None or any(abs(a-b)>.05 for a,b in zip(self.wpos,self.jog_target)):
                self._fault('Jog endpoint does not match requested position'); return
            self.jog_target=None
            self.motion_finish=self.clock()
            if self.state=='calibrating' and self.pen_calibration:
                calibration=self.pen_calibration
                self.completion_deadline=None
                if calibration['phase']=='lowering':
                    calibration['phase']='lifting'
                    self.jog_target=calibration['origin']; self.coordinate_time=-math.inf
                    distance=calibration['up']-calibration['down']
                    self.lines.append(f'$J=G21 G91 Z{distance:.4f} F120.000')
                    self.travel=[v-c for v,c in zip(self.jog_target,self.clearance_center)]
                    self._emit('pen_calibration_progress',calibration_id=calibration['id'],phase='lifting')
                else:
                    calibration['phase']='inspect'; self.state='ready'
                    self.last_poll=self.clock()
                    self._emit('pen_calibration_ready',calibration_id=calibration['id'])
        if self.state=='pausing' and event.fields['state']=='Hold:0': self.state='paused'; self.hold_deadline=None
        if self.job and self.final_query and fresh_query and self.status_state=='Idle':
            self.job=False; self.state='complete'; self.final_query=False
            self.pause_started=self.hold_deadline=None
            self.completion_deadline=None; self.motion_finish=self.clock()
            self._emit('complete')

    # Actions that need a settled status. A routine status query is in flight
    # for a few ms every 0.5 s; such actions wait for its reply (in order)
    # instead of being rejected, then run with all their normal checks.
    WAIT_FOR_STATUS = {'jog','lift_pen','lower_pen','declare_clearance','capture_point','fit_calibration','anchor',
                       'anchor_preset','capture_pen','set_pen_stroke','set_pen_touch','authorize','start_job',
                       'calibrate_pen','confirm_pen_calibration'}
    STATUS_WAIT_TICKS = 200  # about 2 s at the worker's 10 ms loop

    def tick(self):
        """One nonblocking worker iteration; deterministic fake transport tests use this."""
        with self._lock:
            try:
                batch=list(self._deferred); self._deferred.clear()
                while True:
                    try: batch.append(self.commands.get_nowait())
                    except queue.Empty: break
                for action,kw in batch:
                    waited=kw.get('_waited',0)
                    if action in self.WAIT_FOR_STATUS and (self.status_wait or self._deferred) and waited<self.STATUS_WAIT_TICKS:
                        self._deferred.append((action,{**kw,'_waited':waited+1})); continue
                    kw={k:v for k,v in kw.items() if k!='_waited'}
                    try: self._action(action,kw)
                    except (ValueError,TypeError,KeyError) as exc: self._emit('rejected',str(exc),action=action,job_id=kw.get('job_id',self.current_job_id),authorization_id=kw.get('authorization_id'),calibration_id=kw.get('calibration_id'))
                if not self.serial: return
                n=self.transport.in_waiting
                if n:
                    for event in self.parser.feed(self.transport.read(min(n,65536))): self._handle(event)
                now=self.clock()
                if self.pending and self.pause_started is None and now>=self.pending[1]: self._fault('Command acknowledgement timeout')
                if self.completion_deadline is not None and self.pause_started is None and now>=self.completion_deadline:
                    self._fault('Motion completion timeout')
                if self.hold_deadline is not None and now>=self.hold_deadline: self._fault('Hold confirmation timeout')
                if self.status_wait and now>=self.status_deadline: self._fault('Status timeout'); self.status_wait=False
                if self.state in {'unknown','stopped','paused','pausing'}:
                    if self.state in {'pausing','paused'} and not self.status_wait and not self.parser.buffer and now-self.last_poll>.5:
                        self._query(); self.last_poll=now
                    return
                if self.state=='calibrating' and self.pending is not None:
                    return  # Only an after-ACK status query may advance the dot test.
                if self.pending is None and self.lines:
                    line=self.lines.popleft(); deadline=self._command_deadline(line,now)
                    self._write(line.encode('ascii')+b'\n'); self.pending=(line,deadline)
                    if self.state=='calibrating': self.completion_deadline=deadline
                    if self.job: self.sent+=1
                elif self.pending is None and self.state=='calibrating':
                    if not self.status_wait and not self.parser.buffer and now>=self.next_final_poll:
                        self._query(); self.next_final_poll=now+self.final_poll_interval
                elif self.pending is None and self.job:
                    if not self.barrier:
                        self._write(b'G4 P0\n'); self.pending=('G4 P0',self._command_deadline('G4 P0',now))
                    else:
                        if self.completion_deadline is None:
                            self.completion_deadline=now+self.timeout+2*max(0,self.motion_finish-now)
                        if not self.status_wait and not self.parser.buffer and now>=self.next_final_poll:
                            self.final_query=True
                            self._query(); self.next_final_poll=now+self.final_poll_interval
                elif self.state=='connecting' and not self.status_wait:
                    self.state='ready'
                elif not self.job and not self.status_wait and not self.parser.buffer and now-self.last_poll>.5:
                    self._query(); self.last_poll=now
            except Exception as exc:
                self._fault('Transport failure: '+str(exc))
                if self.serial:
                    try: self.transport.close()
                    except Exception: pass
                self.serial=None; self.transport=None

    def _run(self):
        while not self._quit.is_set():
            self.tick(); self._quit.wait(.01)
        with self._lock: self._disconnect()

    @staticmethod
    def device_list():
        from serial.tools import list_ports
        return [dict(port=p.device,description=p.description) for p in list_ports.comports()]
