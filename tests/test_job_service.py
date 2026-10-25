"""Durable job tests use isolated databases, virtual devices and memory fakes."""
import copy
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch
from PySide6.QtWidgets import QApplication
from pagewriter import core, svgimport
from pagewriter.devices import create_controller
from pagewriter.document_service import DocumentService, geometry_identity
from pagewriter.job_service import JobManager
from pagewriter.service_errors import ServiceError
from pagewriter.controller import MachineController

app=QApplication.instance() or QApplication([])


def project():
    vector=svgimport.parse('<svg xmlns="http://www.w3.org/2000/svg" width="10" height="10"><path d="M1 1 L9 9" fill="none" stroke="black"/></svg>')
    return core.Project(width=100,height=100,drawings=[core.Drawing(id='d',x=10,y=10,width=10,vector=vector)])


class FakeReal:
    def __init__(self,geometry):
        self.profile=dict(verified=True,feed=60,work_bounds=(-10,-10,100,100))
        self.state=dict(connection=True,state='ready',session_id='s',geometry_id=geometry,
            simulation=False,device_profile='songboshi_2024',capabilities={'axes':['X','Y','Z']},
            calibrated=True,authorized=True,page_to_machine=(1,0,0,1,0,0),
            wpos=(0,0,0),wco=(0,0,0),coordinate_age=0,idle=True,pending=False,status_pending=False,
            pen_up=0,pen_down=1,pen_touch=.5,clearance=(10,5),progress=(0,0),job_id=None)
        self.calls=[]; self.listeners=[]
    def snapshot(self): return copy.deepcopy(self.state)
    def subscribe_events(self,listener):
        self.listeners.append(listener)
        return lambda: self.listeners.remove(listener)
    def submit(self,action,**kwargs): self.calls.append((action,kwargs))
    def emit(self,kind,**kwargs):
        event=dict(kind=kind,job_id=kwargs.pop('job_id',self.state['job_id']),**kwargs)
        for listener in tuple(self.listeners): listener(event)


class JobServiceTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.docs=DocumentService(project(),workspace=self.tmp.name)
        self.c=create_controller('simulator_servo')
        self.geometry=geometry_identity(self.docs.project)
        self.c.submit('connect'); self.c.submit('geometry_changed',geometry_id=self.geometry)
        self.c.submit('prepare_virtual',geometry_id=self.geometry,work_bounds=(0,0,100,100)); self.c.tick()
        self.jobs=JobManager(self.docs,self.c,Path(self.tmp.name)/'state')
    def tearDown(self):
        self.jobs.close(); self.c.close(); self.tmp.cleanup()
    def finish(self,job_id):
        for _ in range(1000):
            self.c.tick(); self.jobs.poll()
            state=self.jobs.status(job_id)
            if state['state'] in {'complete','failed','rejected','unknown','cancelled'}: return state
        self.fail('Job stalled')
    def error(self,code,call):
        with self.assertRaises(ServiceError) as caught: call()
        self.assertEqual(caught.exception.code,code)
        return caught.exception
    def test_simulation_plan_start_completion_and_idempotent_retry(self):
        plan=self.jobs.prepare(expected_revision=1)
        self.assertTrue(plan['runnable']); self.assertEqual(plan['stroke_count'],1)
        self.assertNotIn('paths',plan); json.dumps(plan,allow_nan=False)
        job=self.jobs.start(plan['plan_id'],'once')
        self.assertEqual(job['state'],'queued'); self.assertEqual(self.c.commands.qsize(),1)
        self.assertEqual(self.jobs.start(plan['plan_id'],'once')['job_id'],job['job_id'])
        self.assertEqual(self.c.commands.qsize(),1)
        done=self.finish(job['job_id'])
        self.assertEqual(done['state'],'complete'); self.assertEqual(done['completion_evidence'],'virtual_observation')
        self.assertEqual(done['progress'][0],done['progress'][1]); self.assertTrue(done['started'])
        self.docs.project.drawings[0].x+=1
        self.assertEqual(self.jobs.start(plan['plan_id'],'once')['job_id'],job['job_id'])
        other=self.jobs.prepare()
        self.error('IDEMPOTENCY_CONFLICT',lambda:self.jobs.start(other['plan_id'],'once'))
    def test_out_of_band_edit_and_reanchor_invalidate_plans(self):
        plan=self.jobs.prepare(); self.docs.project.drawings[0].x+=1
        self.error('REVISION_CONFLICT',lambda:self.jobs.start(plan['plan_id'],'changed'))
        plan=self.jobs.prepare(); self.c.mapping=(1,0,0,1,1,0)
        self.error('STALE_PLAN',lambda:self.jobs.start(plan['plan_id'],'reanchor'))
        self.assertEqual(self.c.commands.qsize(),0)
    def test_offline_plan_reports_requirements_without_opening_transport(self):
        self.c.submit('disconnect'); self.c.tick()
        plan=self.jobs.prepare()
        self.assertFalse(plan['runnable']); self.assertIn('connection',plan['missing_requirements'])
        self.assertEqual(plan['stroke_count'],1)
        self.error('DEVICE_NOT_READY',lambda:self.jobs.start(plan['plan_id'],'offline'))
    def test_restart_observes_unknown_without_replay_or_startup_database_change(self):
        plan=self.jobs.prepare(); job=self.jobs.start(plan['plan_id'],'restart')
        before=self.jobs._row('job_id',job['job_id'])
        self.jobs.close(); self.jobs=JobManager(self.docs,self.c,Path(self.tmp.name)/'state')
        unknown=self.jobs.start(plan['plan_id'],'restart')
        self.assertEqual(unknown['state'],'unknown'); self.assertEqual(unknown['job_id'],job['job_id'])
        self.assertEqual(self.jobs._row('job_id',job['job_id']),before)
        self.assertEqual(self.c.commands.qsize(),1)
        self.error('JOB_OUTCOME_UNKNOWN',lambda:self.jobs.control('resume',job['job_id']))
        new_plan=self.jobs.prepare()
        self.error('JOB_OUTCOME_UNKNOWN',lambda:self.jobs.start(new_plan['plan_id'],'distinct'))
        self.error('JOB_BUSY',lambda:self.jobs.resolve_unknown(job['job_id'],'virtual queue still pending'))
        self.c.submit('stop'); self.c.tick()
        self.jobs.resolve_unknown(job['job_id'],'Inspected virtual queue; explicitly stopped.')
        resolved=self.jobs.status(job['job_id'])
        self.assertEqual(resolved['state'],'interrupted_resolved')
        self.assertIsNone(resolved['completion_evidence'])
        self.assertEqual(self.c.commands.qsize(),0)
    def test_storage_failure_before_enqueue_locks_motion(self):
        plan=self.jobs.prepare()
        self.jobs._db.set_authorizer(lambda action,a,b,c,d: sqlite3.SQLITE_DENY if action==sqlite3.SQLITE_INSERT and a=='jobs' else sqlite3.SQLITE_OK)
        self.error('JOB_STORAGE_UNAVAILABLE',lambda:self.jobs.start(plan['plan_id'],'diskfail'))
        self.assertEqual(self.c.commands.qsize(),0)
        self.error('JOB_STORAGE_UNAVAILABLE',lambda:self.jobs.prepare())
    def test_storage_failure_after_start_never_replays(self):
        plan=self.jobs.prepare(); job=self.jobs.start(plan['plan_id'],'updatefail')
        self.jobs._db.set_authorizer(lambda action,a,b,c,d: sqlite3.SQLITE_DENY if action==sqlite3.SQLITE_UPDATE and a=='jobs' else sqlite3.SQLITE_OK)
        self.c.tick()
        self.error('JOB_STORAGE_UNAVAILABLE',self.jobs.poll)
        count=len(self.c.transport.sent)
        retry=self.jobs.start(plan['plan_id'],'updatefail')
        self.assertEqual(retry['state'],'unknown'); self.assertEqual(len(self.c.transport.sent),count)
        self.error('JOB_STORAGE_UNAVAILABLE',lambda:self.jobs.start(plan['plan_id'],'new'))
        self.jobs.control('cancel',job['job_id']); self.c.tick()
        self.assertEqual(self.c.snapshot()['state'],'stopped')
    def test_pause_resume_cancel_virtual_job(self):
        plan=self.jobs.prepare(); job=self.jobs.start(plan['plan_id'],'pause')
        self.c.tick(); self.jobs.poll()
        self.jobs.control('pause',job['job_id']); self.c.tick(); self.jobs.poll()
        self.assertEqual(self.jobs.status(job['job_id'])['state'],'paused')
        self.jobs.control('resume',job['job_id']); self.c.tick(); self.jobs.poll()
        self.jobs.control('cancel',job['job_id']); self.c.tick(); self.jobs.poll()
        self.assertEqual(self.jobs.status(job['job_id'])['state'],'cancelled')
    def test_initial_travel_obstacle_rechecked_at_start(self):
        self.docs.project.zones=[core.ExclusionZone(id='z',name='障碍',kind='obstacle',points=[[4,4],[6,4],[6,6],[4,6]])]
        plan=self.jobs.prepare()
        self.assertFalse(plan['runnable']); self.assertIn('travel_preflight',plan['missing_requirements'])
        self.error('PREFLIGHT_FAILED',lambda:self.jobs.start(plan['plan_id'],'blocked'))
        self.assertEqual(self.c.commands.qsize(),0)
    def test_gui_operator_stale_complete_and_pending_authorization(self):
        self.c.state='complete'; self.c.current_job_id='previous'; self.c.profile=None
        self.c.submit('authorize',geometry_id=self.geometry,work_bounds=(0,0,100,100))
        paths=core.all_subpaths(self.docs.project)
        job=self.jobs.submit_operator_paths(paths,self.geometry)
        self.assertEqual(self.jobs.status(job['job_id'])['state'],'queued')
        self.jobs._receive(dict(kind='complete',job_id='previous'))
        self.jobs.poll(); self.assertEqual(self.jobs.status(job['job_id'])['state'],'queued')
        self.assertEqual(self.finish(job['job_id'])['state'],'complete')
    def test_gui_rejection_before_start_is_terminal_and_correlated(self):
        self.c.profile=None; self.c.current_job_id='previous'; self.c.state='complete'
        job=self.jobs.submit_operator_paths(core.all_subpaths(self.docs.project),self.geometry)
        self.c.tick(); self.jobs.poll()
        self.assertEqual(self.jobs.status(job['job_id'])['state'],'rejected')
        self.assertFalse(self.jobs.status(job['job_id'])['started'])
    def test_gui_test_paths_independently_check_ink_zones(self):
        self.docs.project.zones=[core.ExclusionZone(id='ink',points=[[20,20],[30,20],[30,30],[20,30]])]
        self.error('PREFLIGHT_FAILED',lambda:self.jobs.submit_operator_paths([[(21,21),(29,29)]],self.geometry,'test'))
        self.assertEqual(self.c.commands.qsize(),0)
    def test_gui_late_page_edit_cannot_write_obsolete_paths(self):
        paths=core.all_subpaths(self.docs.project); self.docs.project.drawings[0].x+=5
        self.error('STALE_PLAN',lambda:self.jobs.submit_operator_paths(paths,self.geometry))

    def test_worker_rejects_context_change_after_job_queued(self):
        plan=self.jobs.prepare(); job=self.jobs.start(plan['plan_id'],'delayed')
        self.c.mapping=(1,0,0,1,1,0)
        self.c.tick(); self.jobs.poll()
        self.assertEqual(self.jobs.status(job['job_id'])['state'],'rejected')
        self.assertEqual(self.c.transport.sent,[])

    def test_disconnect_before_start_becomes_unknown_and_blocks_replay(self):
        plan=self.jobs.prepare(); job=self.jobs.start(plan['plan_id'],'lost')
        self.c._disconnect(); self.jobs.poll()
        self.assertEqual(self.jobs.status(job['job_id'])['state'],'unknown')
        self.assertEqual(self.jobs.start(plan['plan_id'],'lost')['job_id'],job['job_id'])


class RealGrantTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(); self.docs=DocumentService(project(),workspace=self.tmp.name)
        self.c=FakeReal(geometry_identity(self.docs.project))
        self.jobs=JobManager(self.docs,self.c,self.tmp.name)
    def tearDown(self): self.jobs.close(); self.tmp.cleanup()
    def error(self,code,call):
        with self.assertRaises(ServiceError) as caught: call()
        self.assertEqual(caught.exception.code,code)
    def test_real_api_requires_current_operator_grant(self):
        plan=self.jobs.prepare(); self.assertIn('operator_motion_grant',plan['missing_requirements'])
        self.error('DEVICE_NOT_READY',lambda:self.jobs.start(plan['plan_id'],'no-grant'))
        self.jobs.grant_motion(); job=self.jobs.start(plan['plan_id'],'granted')
        self.assertEqual(self.c.calls[-1][0],'start_job'); self.assertEqual(self.c.calls[-1][1]['job_id'],job['job_id'])
    def test_grant_bound_to_mapping_pen_profile_session_and_expiry(self):
        for key,value in [('page_to_machine',(1,0,0,1,1,0)),('pen_down',2),('session_id','new')]:
            previous=self.c.state[key]; self.jobs.grant_motion(); self.c.state[key]=value
            self.error('OPERATOR_GRANT_REQUIRED',self.jobs.require_motion); self.c.state[key]=previous
        self.jobs.grant_motion(); self.c.profile['feed']=100
        self.error('OPERATOR_GRANT_REQUIRED',self.jobs.require_motion)
        self.jobs.grant_motion(ttl=1)
        with patch('pagewriter.job_service.time.monotonic',return_value=self.jobs._grant['expires']+1):
            self.error('OPERATOR_GRANT_REQUIRED',self.jobs.require_motion)
    def test_document_change_revokes_grant_and_no_grants_restore(self):
        self.jobs.grant_motion(); self.jobs.document_changed(True)
        self.error('OPERATOR_GRANT_REQUIRED',self.jobs.require_motion)
        self.jobs.grant_motion(); self.jobs.close(); self.jobs=JobManager(self.docs,self.c,self.tmp.name)
        self.error('OPERATOR_GRANT_REQUIRED',self.jobs.require_motion)

    def test_raw_document_edit_revokes_grant_without_callback(self):
        self.jobs.grant_motion(); self.docs.project.width+=1
        self.error('OPERATOR_GRANT_REQUIRED',self.jobs.require_motion)

    def test_content_edit_retains_grant_but_old_plan_stale_and_bounds_cannot_expand(self):
        self.jobs.grant_motion(); old=self.jobs.prepare()
        self.docs.project.drawings[0].x+=1; self.jobs.document_changed(False)
        self.assertTrue(self.jobs.require_motion()['granted'])
        self.error('REVISION_CONFLICT',lambda:self.jobs.start(old['plan_id'],'old-content'))
        current=self.jobs.prepare(); job=self.jobs.start(current['plan_id'],'inside-range')
        self.c.emit('rejected',job_id=job['job_id'],action='start_job',text='test ends job before motion'); self.jobs.poll()
        self.c.profile['work_bounds']=(-10,-10,20,20); self.jobs.grant_motion()
        self.docs.project.drawings[0].x=30; self.jobs.document_changed(False)
        outside=self.jobs.prepare(); count=len(self.c.calls)
        self.error('PREFLIGHT_FAILED',lambda:self.jobs.start(outside['plan_id'],'outside-range'))
        self.assertEqual(len(self.c.calls),count)
    def test_completion_requires_correlated_started_not_previous_complete_snapshot(self):
        self.jobs.grant_motion(); plan=self.jobs.prepare(); job=self.jobs.start(plan['plan_id'],'race')
        self.c.state.update(state='complete',job_id='old')
        self.c.emit('complete',job_id='old'); self.jobs.poll()
        self.assertEqual(self.jobs.status(job['job_id'])['state'],'queued')
        self.c.state.update(state='running',job_id=job['job_id'])
        self.c.emit('started'); self.jobs.poll()
        self.c.state['state']='complete'; self.jobs.poll()
        self.assertEqual(self.jobs.status(job['job_id'])['state'],'running')
        self.c.emit('complete'); self.jobs.poll()
        self.assertEqual(self.jobs.status(job['job_id'])['completion_evidence'],'fresh_idle_after_final_ack')

    def test_cancel_deferred_before_start_does_not_wait_for_nonexistent_started_event(self):
        self.jobs.grant_motion(); plan=self.jobs.prepare(); job=self.jobs.start(plan['plan_id'],'deferred-cancel')
        self.jobs.control('cancel',job['job_id'])
        self.c.state.update(state='stopped',job_id='previous')
        self.jobs.poll()
        result=self.jobs.status(job['job_id'])
        self.assertEqual(result['state'],'cancelled'); self.assertFalse(result['started'])
        self.assertIsNone(result['completion_evidence'])

    def test_physical_unknown_cannot_be_resolved_using_simulator(self):
        self.jobs.grant_motion(); plan=self.jobs.prepare(); job=self.jobs.start(plan['plan_id'],'physical-origin')
        self.jobs.close()
        virtual=create_controller('simulator_servo')
        try:
            self.jobs=JobManager(self.docs,virtual,self.tmp.name)
            self.error('DEVICE_NOT_READY',lambda:self.jobs.resolve_unknown(job['job_id'],'Simulator is disconnected; cannot inspect real machine'))
            self.assertEqual(self.jobs.status(job['job_id'])['state'],'unknown')
            self.assertIsNone(self.jobs.status(job['job_id'])['completion_evidence'])
        finally: virtual.close()


class MemoryGrbl:
    def __init__(self,**kwargs): self.rx=bytearray(); self.sent=[]
    def open(self): pass
    def close(self): pass
    @property
    def in_waiting(self): return len(self.rx)
    def read(self,count): result=bytes(self.rx[:count]); del self.rx[:count]; return result
    def write(self,payload): self.sent.append(payload); return len(payload)


class GrblAuthorityAudit(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(); self.docs=DocumentService(project(),workspace=self.tmp.name)
        self.now=[0.]; self.c=MachineController(MemoryGrbl,clock=lambda:self.now[0])
        self.c._connect(port='COM_AUDIT_FIXTURE')
        self.c.lines.clear(); self.c.pending=None; self.c.status_wait=False; self.c.last_poll=0.
        self.c.state='ready'; self.c.status_state='Idle'; self.c.units_mm=True
        self.c.wpos=(0.,0.,0.); self.c.wco=(0.,0.,0.); self.c.coordinate_time=0.
        self.c.mapping=(1,0,0,1,0,0); self.c.geometry_id=geometry_identity(self.docs.project)
        self.c.pen_up=0.; self.c.pen_down=1.; self.c.clearance=(10.,5.); self.c.clearance_center=(0.,0.,0.)
        self.params=dict(work_bounds=(-10,-10,100,100),z_bounds=(-2,2),write_feed=100,
            travel_feed=200,z_feed=60,max_write_feed=200,max_travel_feed=300,max_z_feed=100,
            clearance_verified=True,validation_note='Synthetic memory fixture')
        self.c.submit('authorize',authorization_id='scope-wide',**self.params); self.c.tick()
        self.jobs=JobManager(self.docs,self.c,self.tmp.name)
    def tearDown(self): self.jobs.close(); self.c.close(); self.tmp.cleanup()
    def test_rejected_new_candidate_cannot_reuse_old_authority_identity(self):
        self.assertEqual(self.c.snapshot()['authorization_id'],'scope-wide')
        self.c.submit('authorize',authorization_id='scope-narrow',**{**self.params,'write_feed':999999})
        self.assertFalse(self.c.snapshot()['authorized'])
        self.assertIsNone(self.c.snapshot()['authorization_id'])
        self.c.tick()
        self.assertFalse(self.c.snapshot()['authorized'])
        self.assertIsNone(self.c.snapshot()['authorization_id'])
        events=self.c.drain_events()
        self.assertTrue(any(e['kind']=='authorized' and e['authorization_id']=='scope-wide' for e in events))
        self.assertFalse(any(e['kind']=='authorized' and e['authorization_id']=='scope-narrow' for e in events))
        self.assertTrue(any(e['kind']=='rejected' and e.get('authorization_id')=='scope-narrow' for e in events))
    def test_successful_candidate_correlates_and_profile_invalidators_clear_identity(self):
        self.c.submit('authorize',authorization_id='scope-new',**self.params); self.c.tick()
        self.assertEqual(self.c.snapshot()['authorization_id'],'scope-new')
        self.c.submit('capture_pen',kind='up'); self.c.tick()
        self.assertIsNone(self.c.snapshot()['authorization_id']); self.assertFalse(self.c.snapshot()['authorized'])
    def test_running_stale_status_does_not_erase_grant_and_fresh_hold_can_resume(self):
        self.jobs.grant_motion(); plan=self.jobs.prepare(); job=self.jobs.start(plan['plan_id'],'long-running')
        self.c.tick(); self.now[0]=3.; self.c.tick(); self.jobs.poll()
        before=self.jobs._grant
        status=self.jobs.grant_status(readonly=True)
        self.assertTrue(status['granted']); self.assertFalse(status['actionable'])
        with self.assertRaises(ServiceError) as caught: self.jobs.require_motion()
        self.assertEqual(caught.exception.code,'DEVICE_NOT_READY'); self.assertIs(self.jobs._grant,before)
        self.jobs.control('pause',job['job_id']); self.c.tick()
        self.c.serial.rx.extend(b'<Hold:0|WPos:0,0,0|WCO:0,0,0>\n'); self.c.tick(); self.jobs.poll()
        self.assertEqual(self.c.state,'paused'); self.assertTrue(self.jobs.grant_status()['can_resume'])
        self.jobs.control('resume',job['job_id']); self.c.tick()
        self.assertEqual(self.c.state,'running'); self.assertIn(b'~',self.c.serial.sent)
        self.assertIs(self.jobs._grant,before)
    def test_deferred_start_revoked_or_expired_before_dispatch_never_sends_job(self):
        for expired in (False,True):
            with self.subTest(expired=expired):
                if expired:
                    self.jobs.close(); self.jobs=JobManager(self.docs,self.c,self.tmp.name)
                self.jobs.grant_motion(); plan=self.jobs.prepare(); job=self.jobs.start(plan['plan_id'],'expired' if expired else 'revoked')
                self.c.status_wait=True; self.c.status_deadline=10.
                self.c.tick(); self.assertTrue(self.c._deferred)
                lease=self.jobs._grant['lease']
                if not expired: self.jobs.revoke_motion()
                self.c.serial.rx.extend(b'<Idle|WPos:0,0,0|WCO:0,0,0>\n'); self.c.tick()
                if expired:
                    with patch('pagewriter.devices.base.time.monotonic',return_value=lease.deadline+1): self.c.tick()
                else: self.c.tick()
                self.jobs.poll()
                self.assertEqual(self.jobs.status(job['job_id'])['state'],'rejected')
                self.assertFalse(any(payload.startswith(b'G') for payload in self.c.serial.sent))

    def test_queued_resume_lease_revocation_keeps_confirmed_hold(self):
        self.jobs.grant_motion(); plan=self.jobs.prepare(); job=self.jobs.start(plan['plan_id'],'resume-fence')
        self.c.tick(); self.jobs.poll()
        self.jobs.control('pause',job['job_id']); self.c.tick()
        self.c.serial.rx.extend(b'<Hold:0|WPos:0,0,0|WCO:0,0,0>\n'); self.c.tick(); self.jobs.poll()
        self.jobs.control('resume',job['job_id']); self.jobs.revoke_motion(); self.c.tick(); self.jobs.poll()
        self.assertEqual(self.c.state,'paused'); self.assertNotIn(b'~',self.c.serial.sent)
        self.assertEqual(self.jobs.status(job['job_id'])['state'],'paused')
