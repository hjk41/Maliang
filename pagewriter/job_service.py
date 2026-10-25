"""Durable job intent and runtime-only motion authority shared by GUI/API.

SQLite records intent before a controller command is queued. Recovery observes
old jobs as unknown and never repeats their side effect. This is local
idempotency, not a promise of exactly-once execution on a physical machine.
"""
from __future__ import annotations
import copy
import hashlib
import json
import math
from pathlib import Path
import queue
import sqlite3
import threading
import time
import uuid

from PySide6.QtCore import QPointF
from PySide6.QtGui import QPainterPath, QTransform
from . import core
from .document_service import fingerprint, geometry_identity
from .service_errors import ServiceError
from .devices.base import runtime_binding, RuntimeMotionLease
from .devices.ir import compile_paths


TERMINAL = {'complete','failed','cancelled','unknown','rejected','interrupted_resolved'}
ACTIVE = {'accepted','queued','running','pausing','paused','waiting_idle','cancelling'}


def _json(value):
    return json.dumps(value,ensure_ascii=False,sort_keys=True,separators=(',',':'),allow_nan=False)


def _digest(value): return hashlib.sha256(_json(value).encode()).hexdigest()


class JobManager:
    def __init__(self, documents, controller, state_dir, runtime_id=None):
        self.documents=documents; self.controller=controller
        self.runtime_id=runtime_id or uuid.uuid4().hex
        self._lock=threading.RLock(); self._events=queue.Queue(maxsize=2048)
        self._unsubscribe=None; self._grant=None; self._storage_error=None; self._closed=False
        self._cache={}; self._latest=None
        try:
            directory=Path(state_dir); directory.mkdir(parents=True,exist_ok=True)
            self.db_path=directory/'jobs.sqlite3'
            self._db=sqlite3.connect(self.db_path,timeout=5,check_same_thread=False)
            self._db.execute('PRAGMA journal_mode=WAL')
            self._db.execute('PRAGMA synchronous=FULL')
            self._db.execute('CREATE TABLE IF NOT EXISTS plans (plan_id TEXT PRIMARY KEY, runtime TEXT NOT NULL, payload TEXT NOT NULL)')
            self._db.execute('CREATE TABLE IF NOT EXISTS jobs (job_id TEXT PRIMARY KEY, request_id TEXT NOT NULL UNIQUE, plan_id TEXT NOT NULL, runtime TEXT NOT NULL, payload TEXT NOT NULL)')
            self._db.commit()
        except (sqlite3.Error,OSError) as exc:
            raise ServiceError('JOB_STORAGE_UNAVAILABLE','无法打开作业状态库，未发送设备命令',{'reason':str(exc)}) from exc
        self._subscribe(controller)

    def _subscribe(self,controller):
        subscribe=getattr(controller,'subscribe_events',None)
        if subscribe: self._unsubscribe=subscribe(self._receive)

    def _receive(self,event):
        # Never drain the UI queue, touch Qt, or write SQLite on the worker.
        if event.get('kind') in {'tx','rx','ack','prepared','connected','warning'}: return
        try: self._events.put_nowait(dict(event))
        except queue.Full:
            self._storage_error='Controller event queue overflow; job evidence incomplete'

    def _check_write(self):
        if self._closed: raise ServiceError('SERVICE_CLOSED','作业服务已关闭')
        if self._storage_error:
            raise ServiceError('JOB_STORAGE_UNAVAILABLE','作业状态不可可靠记录，已锁定新的设备动作',{'reason':self._storage_error})

    def _storage_failure(self,exc):
        self._storage_error=str(exc)
        self.revoke_motion()
        raise ServiceError('JOB_STORAGE_UNAVAILABLE','作业状态写入失败，未重试设备动作',{'reason':str(exc)}) from exc

    def _execute_write(self,sql,args):
        self._check_write()
        try:
            with self._db: self._db.execute(sql,args)
        except (sqlite3.Error,OSError) as exc: self._storage_failure(exc)

    def _row(self,column,value):
        try:
            row=self._db.execute(f'SELECT payload FROM jobs WHERE {column}=?',(value,)).fetchone()
        except sqlite3.Error as exc: self._storage_failure(exc)
        return json.loads(row[0]) if row else None

    def _binding(self,state=None):
        return runtime_binding(self.controller,state)

    def _requirements(self,state,geometry,*,operator=False):
        missing=[]
        for condition,name in [
            (state.get('connection'),'connection'),(bool(state.get('session_id')),'session'),
            (state.get('geometry_id')==geometry,'current_geometry'),
            (state.get('calibrated') and state.get('page_to_machine') is not None,'runtime_mapping'),
            (state.get('authorized'),'runtime_authorization'),
            (state.get('coordinate_age',math.inf)<=2,'fresh_coordinates'),
            (state.get('state') in {'ready','idle','complete'} and state.get('idle',True),'idle'),
            (not state.get('pending') and not state.get('status_pending') and not state.get('queued_commands'),'empty_queues')]:
            if not condition: missing.append(name)
        if not state.get('simulation') and not operator:
            try: self.require_motion()
            except ServiceError: missing.append('operator_motion_grant')
        model=getattr(self.controller,'device_profile',None)
        page_size=getattr(model,'max_page_size',None)
        if page_size and (self.documents.project.width>page_size[0] or self.documents.project.height>page_size[1]):
            missing.append('device_page_size')
        return missing

    @staticmethod
    def _paths(paths):
        result=[]; count=0
        try:
            for path in paths:
                points=[]
                for point in path:
                    if len(point)!=2: raise ValueError('Expected XY point')
                    x,y=point
                    if type(x) not in (int,float) or type(y) not in (int,float) or not math.isfinite(x) or not math.isfinite(y):
                        raise ValueError('Expected finite XY coordinates')
                    points.append((float(x),float(y))); count+=1
                    if count>2_000_000: raise ValueError('Too many job points')
                if len(points)>=2: result.append(tuple(points))
        except (TypeError,ValueError) as exc:
            raise ServiceError('INVALID_ARGUMENT','作业路径无效',{'reason':str(exc)}) from exc
        return tuple(result)

    @staticmethod
    def _validate_paths(project,paths,state,*,initial=True):
        """Independent checks also used for operator-generated test squares."""
        if not paths: raise ServiceError('EMPTY_JOB','没有可书写路径')
        page=QPainterPath(); page.addRect(0,0,project.width,project.height)
        regions=[(z,core.zone_path(z)) for z in project.zones]
        width=project.pen_width+2*project.margin
        for points in paths:
            path=QPainterPath(QPointF(*points[0]))
            for point in points[1:]: path.lineTo(QPointF(*point))
            envelope=core.stroke_envelope(path,width)
            if not page.contains(envelope):
                raise ServiceError('PREFLIGHT_FAILED','笔迹含安全余量超出纸页')
            for zone,region in regions:
                if region.intersects(envelope) or region.contains(envelope):
                    raise ServiceError('PREFLIGHT_FAILED','笔迹与保护区冲突',{'zone_id':zone.id})
        connectors=[(a[-1],b[0]) for a,b in zip(paths,paths[1:])]
        if initial:
            mapping=state.get('page_to_machine'); position=state.get('wpos')
            if mapping is None or position is None:
                raise ServiceError('DEVICE_NOT_READY','需要当前页面映射和笔位置才能检查首笔路线')
            try:
                inverse,ok=QTransform(*mapping).inverted()
                if not ok: raise ValueError('Singular mapping')
                origin=inverse.map(QPointF(position[0],position[1]))
                if not all(math.isfinite(v) for v in (origin.x(),origin.y())): raise ValueError('Nonfinite position')
            except (ValueError,TypeError,IndexError) as exc:
                raise ServiceError('DEVICE_NOT_READY','当前映射或位置无效') from exc
            connectors.insert(0,((origin.x(),origin.y()),paths[0][0]))
        for first,last in connectors:
            travel=QPainterPath(QPointF(*first)); travel.lineTo(QPointF(*last))
            envelope=core.stroke_envelope(travel,width)
            for zone,region in regions:
                if zone.kind=='obstacle' and (region.intersects(envelope) or region.contains(envelope)):
                    raise ServiceError('PREFLIGHT_FAILED','抬笔路线经过实体障碍',{'zone_id':zone.id})

    @staticmethod
    def _summary(paths):
        points=[p for path in paths for p in path]
        bbox=None if not points else [min(p[0] for p in points),min(p[1] for p in points),max(p[0] for p in points),max(p[1] for p in points)]
        return dict(bbox_mm=bbox,stroke_count=len(paths),lift_count=len(paths),
                    point_count=len(points),estimate_seconds=None)

    def _validate_machine_bounds(self,paths,state):
        profile=getattr(self.controller,'profile',None)
        authorization=self._binding(state).get('authorization') or {}
        if hasattr(profile,'validate'): profile.validate()
        bounds=authorization.get('work_bounds')
        if bounds is None: raise ServiceError('DEVICE_NOT_READY','当前设备没有运行期授权范围')
        write=authorization.get('write_feed',getattr(self.controller,'write_feed',None)) or authorization.get('feed')
        travel=authorization.get('travel_feed',getattr(self.controller,'travel_feed',None)) or write
        try:
            compile_paths(paths,mapping=state['page_to_machine'],work_bounds=bounds,
                write_feed=write,travel_feed=travel,preflight_passed=True)
        except (TypeError,ValueError,KeyError) as exc:
            raise ServiceError('PREFLIGHT_FAILED','路径超出当前操作员授权范围；不会自动扩展范围',{'reason':str(exc)}) from exc

    def prepare(self,expected_revision=None):
        with self._lock:
            self._check_write(); self.documents.check_revision(expected_revision)
            project=copy.deepcopy(self.documents.project)
            geometry=geometry_identity(project); state=self.controller.snapshot()
            failures=core.preflight(project)
            paths=self._paths(core.all_subpaths(project)) if not failures else ()
            missing=self._requirements(state,geometry)
            if failures: missing.append('geometry_preflight')
            if any(getattr(b,'writing_mode','outline')!='single_line' for b in project.blocks): missing.append('single_line_text')
            if not paths: missing.append('nonempty_paths')
            if paths:
                try: self._validate_paths(project,paths,state,initial=state.get('page_to_machine') is not None and state.get('wpos') is not None)
                except ServiceError as exc: missing.append('travel_preflight' if exc.code=='PREFLIGHT_FAILED' else 'initial_travel')
                if state.get('authorized'):
                    try: self._validate_machine_bounds(paths,state)
                    except (ServiceError,ValueError): missing.append('machine_authorized_bounds')
            plan_id=uuid.uuid4().hex
            payload=dict(plan_id=plan_id,runtime_id=self.runtime_id,revision=self.documents.revision,
                geometry_id=geometry,fingerprint=fingerprint(self.documents.project),binding=self._binding(state),
                session_id=state.get('session_id'),device_profile=state.get('device_profile'),
                capabilities=state.get('capabilities',{}),simulation=bool(state.get('simulation')),
                missing_requirements=list(dict.fromkeys(missing)),runnable=not missing,
                preflight=[c.message for c in failures],paths=paths,**self._summary(paths))
            self._execute_write('INSERT INTO plans VALUES (?,?,?)',(plan_id,self.runtime_id,_json(payload)))
            return self._public_plan(payload)

    @staticmethod
    def _public_plan(plan): return copy.deepcopy({k:v for k,v in plan.items() if k not in {'paths','binding','runtime_id'}})

    def _plan(self,plan_id):
        row=self._db.execute('SELECT payload FROM plans WHERE plan_id=?',(plan_id,)).fetchone()
        if not row: raise ServiceError('PLAN_NOT_FOUND','作业预检不存在')
        return json.loads(row[0])

    def _active(self):
        # Foreign runtime records are observations only; never take ownership.
        for payload, in self._db.execute('SELECT payload FROM jobs WHERE runtime=?',(self.runtime_id,)):
            job=json.loads(payload)
            if job['state'] in ACTIVE: return job
        return None

    def _unresolved(self):
        result=[]
        for payload, in self._db.execute('SELECT payload FROM jobs'):
            job=json.loads(payload)
            if job['state']=='unknown' or (job['runtime_id']!=self.runtime_id and job['state'] not in TERMINAL):
                result.append(job['job_id'])
        return result

    def _check_unresolved(self):
        affected=self._unresolved()
        if affected:
            raise ServiceError('JOB_OUTCOME_UNKNOWN','先由操作员现场核查并处理旧作业的不确定结果',
                {'job_ids':affected,'action':'operator inspection and internal resolve_unknown required; no replay'})

    def _new_job(self,plan_id,request_id,plan,*,operator=False,kind='page'):
        self._check_write()
        lease=None
        if not operator and not plan.get('simulation'):
            self.require_motion(); lease=self._grant['lease']; lease.check()
        job=dict(job_id=uuid.uuid4().hex,request_id=request_id,plan_id=plan_id,runtime_id=self.runtime_id,
            state='queued',accepted=True,started=False,created_at=time.time(),updated_at=time.time(),
            revision=plan['revision'],geometry_id=plan['geometry_id'],session_id=plan.get('session_id'),
            device_profile=plan.get('device_profile'),simulation=plan.get('simulation',False),
            operator=bool(operator),kind=kind,progress=[0,0],completion_evidence=None,trace=[])
        try:
            with self._db:
                self._db.execute('INSERT INTO jobs VALUES (?,?,?,?,?)',
                    (job['job_id'],request_id,plan_id,self.runtime_id,_json(job)))
        except sqlite3.IntegrityError as exc:
            previous=self._row('request_id',request_id)
            if previous is None: self._storage_failure(exc)
            if previous['plan_id']!=plan_id:
                raise ServiceError('IDEMPOTENCY_CONFLICT','同一 request_id 已绑定另一作业预检') from exc
            return self._public_job(previous)
        except (sqlite3.Error,OSError) as exc: self._storage_failure(exc)
        self._cache[job['job_id']]=job; self._latest=job['job_id']
        try:
            self.controller.submit('start_job',polylines=copy.deepcopy(plan['paths']),geometry_id=plan['geometry_id'],
                preflight_passed=True,job_id=job['job_id'],expected_session_id=plan.get('session_id'),
                expected_mapping=plan.get('mapping'),expected_position=plan.get('position'),
                **({'expected_binding':plan['binding']} if not operator else {}),
                **({'motion_lease':lease} if lease is not None else {}))
        except Exception as exc:
            # submit may have queued before raising. Keep the durable key and never retry.
            self._update(job,state='unknown',failure='Controller submission outcome unknown: '+str(exc))
        return self._public_job(job)

    def start(self,plan_id,request_id,operator=False):
        with self._lock:
            if not isinstance(plan_id,str) or not plan_id or len(plan_id)>128:
                raise ServiceError('INVALID_ARGUMENT','plan_id 必须为有效字符串')
            if not isinstance(request_id,str) or not request_id.strip() or len(request_id)>256:
                raise ServiceError('INVALID_ARGUMENT','request_id 必须是非空且不超过 256 字符的字符串')
            previous=self._row('request_id',request_id)
            if previous:
                if previous['plan_id']!=plan_id: raise ServiceError('IDEMPOTENCY_CONFLICT','同一 request_id 已绑定另一作业预检')
                return self._public_job(previous)
            self._check_write(); self.poll(); self._check_unresolved()
            if self._active(): raise ServiceError('JOB_BUSY','当前已有排队或执行中的作业')
            plan=self._plan(plan_id)
            self.documents.check_revision(plan['revision'])
            if plan['runtime_id']!=self.runtime_id or plan['fingerprint']!=fingerprint(self.documents.project):
                raise ServiceError('STALE_PLAN','文档或服务会话已改变，请重新预检')
            state=self.controller.snapshot()
            if _digest(plan['binding'])!=_digest(self._binding(state)):
                raise ServiceError('STALE_PLAN','设备会话、映射或授权已改变，请重新预检')
            missing=self._requirements(state,plan['geometry_id'],operator=operator)
            if plan['preflight'] or not plan['paths'] or any(getattr(b,'writing_mode','outline')!='single_line' for b in self.documents.project.blocks):
                missing.append('geometry_preflight')
            if missing: raise ServiceError('DEVICE_NOT_READY','作业准备条件未满足',{'missing_requirements':missing})
            self._validate_paths(self.documents.project,plan['paths'],state)
            self._validate_machine_bounds(plan['paths'],state)
            plan['mapping']=state.get('page_to_machine'); plan['position']=state.get('wpos')
            return self._new_job(plan_id,request_id,plan,operator=operator)

    def submit_operator_paths(self,paths,geometry_id,kind='page'):
        with self._lock:
            self._check_write(); self.poll(); self._check_unresolved(); self.documents.observe()
            if self._active(): raise ServiceError('JOB_BUSY','当前已有排队或执行中的作业')
            if kind not in {'page','test'}: raise ServiceError('INVALID_ARGUMENT','未知操作员作业种类')
            project=copy.deepcopy(self.documents.project)
            if geometry_id!=geometry_identity(project): raise ServiceError('STALE_PLAN','页面定位已改变')
            if kind=='page':
                failures=core.preflight(project)
                if failures or any(getattr(b,'writing_mode','outline')!='single_line' for b in project.blocks):
                    raise ServiceError('PREFLIGHT_FAILED','当前页面未通过单线书写预检',{'failures':[c.message for c in failures]})
            paths=self._paths(paths); state=self.controller.snapshot()
            if kind=='page' and _digest(paths)!=_digest(self._paths(core.all_subpaths(project))):
                raise ServiceError('STALE_PLAN','页面笔迹在确认期间已改变，请重新开始书写')
            if not state.get('connection') or not state.get('session_id'):
                raise ServiceError('DEVICE_NOT_READY','设备未连接')
            self._validate_paths(project,paths,state)
            plan=dict(plan_id='operator-'+uuid.uuid4().hex,revision=self.documents.revision,geometry_id=geometry_id,
                session_id=state.get('session_id'),device_profile=state.get('device_profile'),
                simulation=state.get('simulation',False),paths=paths)
            plan['mapping']=state.get('page_to_machine'); plan['position']=state.get('wpos')
            # The prior GUI authorization command may still be queued. The worker
            # independently checks it before this job starts; no grant is restored.
            return self._new_job(plan['plan_id'],'operator-'+uuid.uuid4().hex,plan,operator=True,kind=kind)

    def _update(self,job,**changes):
        updated=copy.deepcopy(job); updated.update(changes); updated['updated_at']=time.time()
        self._execute_write('UPDATE jobs SET payload=? WHERE job_id=? AND runtime=?',
                            (_json(updated),job['job_id'],self.runtime_id))
        job.clear(); job.update(updated); self._cache[job['job_id']]=job

    def _public_job(self,job):
        result=copy.deepcopy(job)
        if result['runtime_id']!=self.runtime_id and result['state'] not in TERMINAL:
            result['stored_state']=result['state']; result['state']='unknown'
            result['failure']='Previous runtime; execution outcome requires operator inspection. No replay.'
        result.pop('runtime_id',None)
        if self._storage_error:
            result['storage_error']=self._storage_error
            if result['state'] not in TERMINAL: result['state']='unknown'
        return result

    def poll(self):
        with self._lock:
            if self._closed: return None
            while True:
                try: event=self._events.get_nowait()
                except queue.Empty: break
                job_id=event.get('job_id')
                job=self._cache.get(job_id) or (self._row('job_id',job_id) if job_id else None)
                if not job or job['runtime_id']!=self.runtime_id or job['state'] in TERMINAL: continue
                kind=event.get('kind'); changes={}
                if kind=='started': changes=dict(state='running',started=True)
                elif kind=='complete' and job.get('started'):
                    changes=dict(state='complete',completion_evidence='virtual_observation' if job['simulation'] else 'fresh_idle_after_final_ack')
                    snapshot=self.controller.snapshot()
                    if snapshot.get('job_id')==job['job_id']:
                        changes['progress']=list(snapshot.get('progress',job['progress']))
                elif kind=='rejected' and event.get('action')=='start_job' and not job.get('started'):
                    changes=dict(state='rejected',failure=str(event.get('text','Start rejected'))[:1000])
                elif kind=='fault': changes=dict(state='failed',failure=str(event.get('text','Device fault'))[:1000])
                elif kind=='cancelled': changes=dict(state='cancelled')
                elif kind=='paused' and job.get('started'): changes=dict(state='paused')
                elif kind=='resumed' and job.get('started'): changes=dict(state='running')
                if changes:
                    changes['trace']=(job.get('trace',[])+[dict(kind=kind,at=time.time(),text=str(event.get('text',''))[:300])])[-32:]
                    self._update(job,**changes)
            state=self.controller.snapshot()
            for payload, in self._db.execute('SELECT payload FROM jobs WHERE runtime=?',(self.runtime_id,)).fetchall():
                job=json.loads(payload)
                if job['state'] not in ACTIVE: continue
                changes={}
                if not state.get('connection') or state.get('session_id')!=job['session_id']:
                    changes=dict(state='unknown',failure='Device disconnected or session changed; no replay')
                elif job['state']=='cancelling' and state.get('state')=='stopped' and not state.get('queued_commands'):
                    changes=dict(state='cancelled',cancellation_evidence='sender_stopped; physical stop not asserted')
                elif state.get('job_id')==job['job_id'] and job.get('started'):
                    current=state.get('state')
                    if current in {'running','pausing','paused','waiting_idle'}: changes['state']=current
                    elif current in {'unknown','failed'}: changes=dict(state='failed',failure=str(state.get('failure','Device fault'))[:1000])
                    elif current=='stopped': changes=dict(state='cancelled')
                    # A complete snapshot alone is insufficient completion evidence.
                    progress=list(state.get('progress',(0,0)))
                    if progress!=job.get('progress'): changes['progress']=progress
                if changes and any(job.get(k)!=v for k,v in changes.items()): self._update(job,**changes)
            return self.status(self._latest,_poll=False) if self._latest else None

    def status(self,job_id=None,*,_poll=True):
        with self._lock:
            if _poll and not self._storage_error: self.poll()
            if job_id is None:
                jobs=[json.loads(row[0]) for row in self._db.execute('SELECT payload FROM jobs ORDER BY rowid DESC LIMIT 50')]
                return dict(jobs=[self._public_job(j) for j in jobs],storage_locked=bool(self._storage_error))
            job=self._row('job_id',job_id)
            if not job: raise ServiceError('JOB_NOT_FOUND','作业不存在')
            return self._public_job(job)

    def control(self,action,job_id=None):
        with self._lock:
            if action not in {'pause','resume','cancel','stop'}: raise ServiceError('INVALID_ARGUMENT','作业控制仅支持 pause/resume/cancel')
            if self._storage_error:
                if action=='resume': self._check_write()
                cached=self._cache.get(job_id) if job_id else next((j for j in self._cache.values() if j['runtime_id']==self.runtime_id and j['state'] in ACTIVE),None)
                if cached is None or cached['runtime_id']!=self.runtime_id or cached['state'] not in ACTIVE:
                    raise ServiceError('JOB_NOT_FOUND','没有可控制的当前作业')
                # Safety controls remain available even if SQLite cannot be read.
                self.controller.submit('stop' if action in {'cancel','stop'} else 'pause')
                return self._public_job(cached)
            if not self._storage_error: self.poll()
            job=self._row('job_id',job_id) if job_id else self._active()
            if not job: raise ServiceError('JOB_NOT_FOUND','没有可控制的当前作业')
            if job['runtime_id']!=self.runtime_id: raise ServiceError('JOB_OUTCOME_UNKNOWN','旧服务作业只能现场核查，不会重新派发')
            if job['state'] not in ACTIVE: raise ServiceError('JOB_NOT_ACTIVE','作业已不在执行或排队')
            if action=='resume':
                self._check_write()
                if not job['simulation']: self.require_motion()
            if self._storage_error:
                if action not in {'pause','cancel','stop'}: self._check_write()
                self.controller.submit('stop' if action in {'cancel','stop'} else 'pause')
                return self._public_job(job)
            next_state='cancelling' if action in {'cancel','stop'} else 'pausing' if action=='pause' else 'running'
            self._update(job,state=next_state)
            lease_kwargs={'motion_lease':self._grant['lease']} if action=='resume' and not job['simulation'] else {}
            try: self.controller.submit('stop' if action in {'cancel','stop'} else action,**lease_kwargs)
            except Exception as exc: self._update(job,state='unknown',failure='Control submission outcome unknown: '+str(exc))
            # A queued start is followed by the stop intention; never resend start.
            return self._public_job(job)

    def grant_motion(self,ttl=900):
        with self._lock:
            self._check_write()
            if type(ttl) not in (float,int) or not math.isfinite(ttl) or not 0<ttl<=3600:
                raise ServiceError('INVALID_ARGUMENT','授权期限须在 0–3600 秒内')
            self.documents.observe(); state=self.controller.snapshot()
            missing=self._requirements(state,geometry_identity(self.documents.project),operator=True)
            if missing: raise ServiceError('DEVICE_NOT_READY','先完成当前会话的定位、笔和现场授权',{'missing_requirements':missing})
            self.revoke_motion()
            deadline=time.monotonic()+ttl
            self._grant=dict(binding=_digest(self._binding(state)),expires=deadline,lease=RuntimeMotionLease(deadline),
                             fingerprint=fingerprint(self.documents.project),geometry_id=geometry_identity(self.documents.project))
            return dict(granted=True,expires_in_seconds=ttl,session_id=state['session_id'],geometry_id=state['geometry_id'])

    def revoke_motion(self):
        with self._lock:
            if self._grant: self._grant['lease'].revoke()
            self._grant=None
            return dict(granted=False)

    def require_motion(self):
        self._check_write(); self.documents.observe(); state=self.controller.snapshot()
        if state.get('simulation'): return dict(granted=True,simulation=True)
        scope=self.grant_status(readonly=False)
        if not scope['granted']:
            raise ServiceError('OPERATOR_GRANT_REQUIRED','真实设备动作需要当前 GUI 操作员授权；重新定位或断线后授权失效')
        if state.get('coordinate_age',math.inf)>2:
            raise ServiceError('DEVICE_NOT_READY','先等待设备提供新的坐标；操作员授权范围仍有效',
                               {'missing_requirements':['fresh_coordinates'],'grant_retained':True})
        return scope

    def grant_status(self,readonly=True):
        """Observe persistent authority separately from transient motion readiness.

        Routine Grbl status coordinates become old while a job runs. That
        affects new motion readiness, never the lifetime of an unchanged scope.
        """
        with self._lock:
            state=self.controller.snapshot(); grant=self._grant
            simulation=bool(state.get('simulation'))
            valid=simulation or bool(grant and time.monotonic()<grant['expires'] and state.get('connection')
                and state.get('authorized') and state.get('calibrated')
                and grant['binding']==_digest(self._binding(state))
                and grant['geometry_id']==geometry_identity(self.documents.project))
            if not readonly and not valid: self.revoke_motion()
            fresh=state.get('coordinate_age',math.inf)<=2
            settled=not state.get('pending') and not state.get('status_pending') and not state.get('queued_commands')
            can_start=bool(valid and fresh and settled and state.get('state') in {'ready','idle','complete'} and not self._storage_error)
            can_resume=bool(valid and fresh and state.get('state')=='paused' and not self._storage_error)
            return dict(granted=valid,simulation=simulation,actionable=can_start or can_resume,
                can_start=can_start,can_resume=can_resume,fresh_coordinates=fresh,
                expires_in_seconds=None if simulation or not grant else max(0,grant['expires']-time.monotonic()))

    def document_changed(self,geometry_changed):
        with self._lock:
            if geometry_changed: self.revoke_motion()
            # Plans retain immutable intent for audit/idempotent lookup; revision,
            # fingerprint and geometry checks make them unusable for a new start.
            return dict(plans_invalidated=True,geometry_changed=bool(geometry_changed))

    def resolve_unknown(self,job_id,note):
        """Internal GUI/operator reconciliation; records inspection, never motion."""
        with self._lock:
            self._check_write(); self.poll()
            if not isinstance(note,str) or not note.strip() or len(note)>2000:
                raise ServiceError('INVALID_ARGUMENT','需要非空且不超过 2000 字符的现场核查记录')
            job=self._row('job_id',job_id)
            if not job: raise ServiceError('JOB_NOT_FOUND','作业不存在')
            if job_id not in self._unresolved(): raise ServiceError('JOB_NOT_UNKNOWN','该作业没有待处理的不确定结果')
            state=self.controller.snapshot()
            if self._active() or state.get('pending') or state.get('status_pending') or state.get('queued_commands') or state.get('state') in {'running','pausing','paused','waiting_idle'}:
                raise ServiceError('JOB_BUSY','先停止并排空当前设备作业')
            if not job.get('simulation'):
                if state.get('simulation') or state.get('device_profile')!=job.get('device_profile'):
                    raise ServiceError('DEVICE_NOT_READY','真实历史作业须使用相同设备配置重新核查；模拟器不能代替现场核查',
                        {'required_device_profile':job.get('device_profile')})
                missing=self._requirements(state,geometry_identity(self.documents.project),operator=True)
                if missing: raise ServiceError('DEVICE_NOT_READY','先连接并重新标定当前设备',{'missing_requirements':missing})
                self.require_motion()
            updated=copy.deepcopy(job)
            updated.update(state='interrupted_resolved',resolution_note=note.strip(),resolved_at=time.time(),
                resolved_by_runtime=self.runtime_id,updated_at=time.time(),completion_evidence=None)
            # Explicit operator resolution is the only path allowed to update a
            # foreign record. Startup/poll/retry never changes its ownership.
            self._execute_write('UPDATE jobs SET payload=? WHERE job_id=?',(_json(updated),job_id))
            self._cache[job_id]=updated
            return self._public_job(updated)

    def set_controller(self,new):
        with self._lock:
            self.poll()
            if self._active(): raise ServiceError('JOB_BUSY','作业结束前不能切换设备')
            if self._unsubscribe: self._unsubscribe()
            self.revoke_motion()
            self.controller=new; self._unsubscribe=None; self._subscribe(new)

    def close(self):
        with self._lock:
            if self._closed: return
            if self._unsubscribe: self._unsubscribe(); self._unsubscribe=None
            self.revoke_motion(); self._closed=True
            self._db.close()
