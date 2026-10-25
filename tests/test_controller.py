import unittest
from pagewriter.controller import MachineController,fit_affine

class FakeSerial:
    def __init__(self,**kw):
        assert kw['port'] is None and kw['timeout']==0
        self.rx=bytearray(); self.sent=[]; self.dtr=True; self.rts=True; self.opened=False
    def open(self):
        assert self.dtr is False and self.rts is False
        self.opened=True
    def close(self): self.opened=False
    @property
    def in_waiting(self): return len(self.rx)
    def read(self,n):
        out=bytes(self.rx[:n]); del self.rx[:n]; return out
    def write(self,b): self.sent.append(b); return len(b)

class ControllerTests(unittest.TestCase):
    def use_preset(self):
        self.ready(); self.c.submit('anchor_preset',preset='songboshi_2024_paper_180',
            page_x=0,page_y=0,geometry_id='page',axes_confirmed=True); self.c.tick()

    def test_preset_provenance_survives_manual_anchor_and_range_declaration(self):
        self.use_preset(); name='songboshi_2024_paper_180'
        self.assertEqual(self.c.snapshot()['mapping_preset'],name)
        self.c.submit('manual_reposition'); self.c.tick()
        self.c.submit('manual_reposition'); self.c.tick()
        self.assertIsNone(self.c.mapping_preset)
        self.assertEqual(self.c.snapshot()['anchor_preset_source'],name)
        self.c.submit('declare_clearance',xy_mm=2,z_mm=3); self.c.tick()
        self.assertEqual(self.c.anchor_preset_source,name)
        self.c.submit('anchor',page_x=1,page_y=2,axes_confirmed=True); self.c.tick()
        self.assertEqual(self.c.mapping_preset,name); self.assertIsNone(self.c.anchor_preset_source)

    def test_failed_fit_retains_preset_source_successful_fit_clears_it(self):
        self.use_preset(); original=self.c.mapping
        self.c.points=[((0,0),(0,0))]
        self.c.submit('fit_calibration',geometry_id='page'); self.c.tick()
        self.assertEqual(self.c.mapping,original)
        self.assertEqual(self.c.mapping_preset,'songboshi_2024_paper_180')
        self.c.points=[((0,0),(0,0)),((10,0),(10,0)),((0,10),(0,10))]
        self.c.submit('fit_calibration',geometry_id='page'); self.c.tick()
        self.assertEqual(self.c.mapping,(1,0,0,1,0,0))
        self.assertIsNone(self.c.mapping_preset); self.assertIsNone(self.c.anchor_preset_source)
        self.c.submit('geometry_changed',geometry_id='changed'); self.c.tick()
        self.assertIsNone(self.c.mapping_preset); self.assertIsNone(self.c.anchor_preset_source)
    def test_physical_180_preset_axes_and_no_double_rotation(self):
        self.ready(); self.c.wpos=(9,-16.25,0)
        self.c.clearance=(5,3); self.c.clearance_center=self.c.wpos
        self.c.mapping=(0,1,-1,0,999,888); self.c.profile=object()
        self.c.pen_up=0; self.c.pen_down=2
        before=list(self.s.sent)
        args=dict(preset='songboshi_2024_paper_180',page_x=2,page_y=3,
                  geometry_id='physical-right-page',axes_confirmed=True)
        self.c.submit('anchor_preset',**args); self.c.tick()
        self.assertEqual(self.c.mapping,(0,-1,1,0,6,-14.25))
        a,b,c,d,tx,ty=self.c.mapping
        def mapped(x,y): return (a*x+c*y+tx,b*x+d*y+ty)
        self.assertEqual(mapped(2,3),(9,-16.25))
        self.assertEqual(mapped(3,3),(9,-17.25))  # paper right = -machine Y
        self.assertEqual(mapped(2,4),(10,-16.25)) # paper down = +machine X
        self.c.submit('anchor_preset',**args); self.c.tick()
        self.assertEqual(self.c.mapping,(0,-1,1,0,6,-14.25))
        # XY anchoring keeps this session's pen heights; authorization is cleared.
        self.assertIsNone(self.c.profile); self.assertEqual((self.c.pen_up,self.c.pen_down),(0,2))
        self.assertEqual(self.c.clearance,(5,3)); self.assertEqual(self.s.sent,before)

    def test_physical_180_preset_has_same_strict_validation(self):
        self.ready(); original=(1,0,0,1,0,0); self.c.mapping=original
        base=dict(preset='songboshi_2024_paper_180',page_x=0,page_y=0,
                  geometry_id='page',axes_confirmed=True)
        for change in [dict(axes_confirmed=1),dict(page_x=float('nan')),
                       dict(page_y=float('inf')),dict(geometry_id='')]:
            self.c.submit('anchor_preset',**{**base,**change}); self.c.tick()
            self.assertEqual(self.c.mapping,original)
        self.c.status_wait=True; self.c.status_deadline=2
        self.c.submit('anchor_preset',**base); self.c.tick()
        self.assertEqual(self.c.mapping,original)
    def test_explicit_paper_preset_replaces_mapping_without_motion(self):
        self.ready(); self.c.wpos=(9,-16.25,0)
        self.c.clearance=(5,3); self.c.clearance_center=self.c.wpos
        self.c.mapping=(2,0,0,2,99,99); self.c.anchor_mapping=(1,0,0,1,9,9)
        self.c.points=[((0,0),(0,0))]; self.c.profile=object(); self.c.pen_up=0; self.c.pen_down=2
        before=list(self.s.sent)
        self.c.submit('anchor_preset',preset='songboshi_2024_paper',page_x=2,page_y=3,
                      geometry_id='paper-current',axes_confirmed=True); self.c.tick()
        self.assertEqual(self.c.mapping,(0,1,-1,0,12,-18.25))
        self.assertEqual(self.c.geometry_id,'paper-current')
        self.assertIsNone(self.c.profile); self.assertEqual((self.c.pen_up,self.c.pen_down),(0,2))
        self.assertEqual(self.c.points,[]); self.assertIsNone(self.c.anchor_mapping)
        self.assertEqual(self.c.clearance,(5,3)); self.assertEqual(self.s.sent,before)

    def test_paper_preset_rejects_unconfirmed_unknown_nonfinite_or_geometry(self):
        self.ready(); original=(1,0,0,1,0,0); self.c.mapping=original
        base=dict(preset='songboshi_2024_paper',page_x=0,page_y=0,geometry_id='page',axes_confirmed=True)
        for change in [dict(axes_confirmed=1),dict(axes_confirmed=False),dict(preset='unknown'),
                       dict(page_x=float('nan')),dict(page_y=float('inf')),dict(geometry_id=' '),
                       dict(geometry_id=None)]:
            args={**base,**change}; self.c.submit('anchor_preset',**args); self.c.tick()
            self.assertEqual(self.c.mapping,original)

    def test_paper_preset_requires_fresh_idle_and_does_not_survive_reconnect(self):
        self.ready(); args=dict(preset='songboshi_2024_paper',page_x=0,page_y=0,
                               geometry_id='page',axes_confirmed=True)
        self.c.status_wait=True; self.c.status_deadline=2
        self.c.submit('anchor_preset',**args); self.c.tick(); self.assertIsNone(self.c.mapping)
        self.rx(b'<Idle|WPos:0,0,0>\n'); self.c.submit('anchor_preset',**args); self.c.tick()
        self.assertEqual(self.c.mapping,(0,1,-1,0,0,0))
        self.c.submit('disconnect'); self.c.tick(); self.c.submit('connect'); self.c.tick()
        self.assertIsNone(self.c.mapping); self.assertIsNone(self.c.profile)

    def test_generic_fit_and_anchor_do_not_apply_preset_rotation(self):
        self.ready()
        self.c.points=[((0,0),(2,3)),((10,0),(12,3)),((0,10),(2,13))]
        self.c.submit('fit_calibration',geometry_id='page'); self.c.tick()
        self.assertEqual(self.c.mapping,(1,0,0,1,2,3))
        self.c.submit('manual_reposition'); self.c.tick()
        self.c.submit('anchor',page_x=2,page_y=3,axes_confirmed=True); self.c.tick()
        self.assertEqual(self.c.mapping,(1,0,0,1,-2,-3))
    def setUp(self):
        self.now=[0.]; self.c=MachineController(FakeSerial,lambda:self.now[0],2)
        self.c.submit('connect'); self.c.tick(); self.s=self.c.serial
    def rx(self,b): self.s.rx.extend(b); self.c.tick()
    def ready(self):
        self.rx(b'<Idle|MPos:0,0,15|WCO:0,0,15>\n')
        self.rx(b'ok\n'); self.rx(b'ok\n'); self.rx(b'$13=0\nok\n'); self.rx(b'ok\n')
        self.assertEqual(self.c.state,'ready')
        if self.c.status_wait: self.rx(b'<Idle|MPos:0,0,15>\n')
    def test_connect_read_only_before_open_levels(self):
        self.ready()
        self.assertEqual(self.s.sent[:5],[b'?',b'$I\n',b'$G\n',b'$$\n',b'$#\n'])
    def test_split_and_cached_wco(self):
        self.ready(); self.rx(b'<Idle|MPos:1,2,'); self.rx(b'16>\n')
        self.assertEqual(self.c.wpos,(1,2,1))
    def test_error_never_sends_next(self):
        self.rx(b'error:2\n')
        self.assertEqual(self.c.state,'unknown'); self.assertFalse(self.c.lines)
        self.assertEqual(self.s.sent[-1],b'!')
    def test_disconnect_no_reset(self):
        self.c.submit('disconnect'); self.c.tick()
        self.assertNotIn(b'\x18',self.s.sent); self.assertFalse(self.s.opened)
    def test_timeout_locks_unknown(self):
        self.now[0]=3; self.c.tick(); self.assertEqual(self.c.state,'unknown')
    def test_manual_move_invalidates_and_anchor_translation(self):
        self.ready(); self.c.mapping=(1,0,0,1,0,0); self.c.pen_up=1; self.c.pen_down=0
        self.c.submit('manual_reposition'); self.c.tick()
        self.assertIsNone(self.c.mapping); self.assertIsNone(self.c.pen_up)
        self.rx(b'<Idle|MPos:0,0,15>\n')
        self.c.submit('anchor',page_x=2,page_y=3,axes_confirmed=True); self.c.tick()
        self.assertEqual(self.c.mapping,(1,0,0,1,-2,-3))
    def test_job_barrier_stale_idle_and_hold(self):
        self.ready(); self.c.job=True; self.c.state='running'; self.c.total=1
        self.c.lines.append('G1 X1 F60'); self.c.tick()
        self.rx(b'ok\n<Idle|MPos:1,0,15>\n')
        self.assertEqual(self.s.sent[-1],b'G4 P0\n'); self.assertNotEqual(self.c.state,'complete')
        self.rx(b'ok\n<Idle|MPos:1,0,15>\n')
        self.assertEqual(self.s.sent[-1],b'?'); self.assertNotEqual(self.c.state,'complete')
        self.rx(b'<Idle|MPos:1,0,15>\n'); self.assertEqual(self.c.state,'complete')
    def test_pause_resume_and_stop_pending(self):
        self.ready(); self.c.job=True; self.c.state='running'; self.c.lines.append('G1 X1 F60'); self.c.tick()
        self.c.submit('pause'); self.c.tick(); self.assertEqual(self.c.state,'pausing')
        self.rx(b'<Hold:0|MPos:0,0,15>\n'); self.assertEqual(self.c.state,'paused')
        self.c.submit('resume'); self.c.tick(); self.assertIn(b'~',self.s.sent)
        self.c.submit('stop'); self.c.tick(); self.rx(b'ok\n')
        self.assertEqual(self.c.state,'stopped'); self.assertFalse(self.c.lines)
    def test_degenerate_and_finite_fit(self):
        self.assertEqual(fit_affine([((0,0),(1,2)),((10,0),(11,2)),((0,10),(1,12))]),(1,0,0,1,1,2))
        for points in [[((0,0),(0,0)),((10,0),(0,0)),((0,10),(0,0))],
                       [((0,0),(0,0)),((10,0),(10,0)),((20,0),(20,0))]]:
            with self.assertRaises(ValueError): fit_affine(points)

    def test_decelerating_hold_cannot_resume(self):
        self.ready(); self.c.job=True; self.c.state='pausing'
        self.rx(b'<Hold:1|MPos:0,0,15>\n')
        self.assertEqual(self.c.state,'pausing')
        self.c.submit('resume'); self.c.tick(); self.assertNotIn(b'~',self.s.sent)

    def test_disconnect_running_requests_hold(self):
        self.ready(); self.c.job=True; self.c.state='running'
        self.c.submit('disconnect'); self.c.tick()
        self.assertEqual(self.s.sent[-1],b'!'); self.assertNotIn(b'\x18',self.s.sent)

    def test_clearance_cannot_reset_accumulated_motion(self):
        self.ready(); self.c.submit('declare_clearance',xy_mm=10,z_mm=1); self.c.tick()
        self.c.travel[0]=9
        self.c.submit('declare_clearance',xy_mm=10,z_mm=1); self.c.tick()
        self.assertEqual(self.c.travel[0],9)

    def jog(self,axis,distance,position):
        if self.c.status_wait: self.rx(('<Idle|WPos:'+','.join(map(str,self.c.wpos))+' >\n').replace(' >','>').encode())
        self.c.submit('jog',axis=axis,distance=distance,feed=30); self.c.tick()
        self.rx(b'ok\n')
        self.now[0]+=.6; self.c.tick()
        self.rx(('<Idle|WPos:'+','.join(map(str,position))+'>\n').encode())

    def test_clearance_is_displacement_not_path_length(self):
        self.ready(); self.c.submit('declare_clearance',xy_mm=1,z_mm=3); self.c.tick()
        self.rx(b'<Idle|WPos:0,0,0>\n')
        for _ in range(6):
            self.jog('Z',.25,(0,0,.254))
            self.jog('Z',-.25,(0,0,0))
        self.assertEqual(self.c.state,'ready'); self.assertIsNone(self.c.jog_target)
        self.assertAlmostEqual(self.c.snapshot()['clearance_remaining'][2][0],3)

    def test_clearance_endpoint_and_feed_rejections(self):
        self.ready(); self.c.submit('declare_clearance',xy_mm=.5,z_mm=.2); self.c.tick()
        self.rx(b'<Idle|WPos:0,0,0>\n'); count=len(self.s.sent)
        self.c.submit('jog',axis='Z',distance=.25,feed=30); self.c.tick()
        self.c.submit('jog',axis='Z',distance=.1,feed=31); self.c.tick()
        self.assertEqual(len(self.s.sent),count)
        self.c.mapping=(1,0,0,1,0,0); self.c.pen_up=1; self.c.pen_down=0
        self.c.submit('declare_clearance',xy_mm=2,z_mm=3,confirmed_new_range=True); self.c.tick()
        self.assertIsNone(self.c.mapping); self.assertIsNone(self.c.pen_up)
        self.assertEqual(self.c.clearance,(2,3))

    def run_lines(self,lines):
        self.ready(); self.c.job=True; self.c.state='running'; self.c.total=len(lines)
        self.c.planned_position=self.c.wpos; self.c.lines.extend(lines); self.c.tick()

    def test_long_move_ack_has_finite_motion_budget(self):
        self.run_lines(['G1 X100 F60'])
        deadline=self.c.pending[1]
        self.assertGreater(deadline,200); self.assertLess(deadline,210)
        self.now[0]=50; self.c.tick(); self.assertEqual(self.c.state,'running')
        self.now[0]=deadline+.01; self.c.tick(); self.assertEqual(self.c.state,'unknown')

    def test_square_barrier_budgets_accepted_motion_and_drains(self):
        self.run_lines(['G1 Z2 F30','G1 X4 F60','G1 Y4 F60',
                        'G1 X0 F60','G1 Y0 F60','G1 Z0 F30'])
        for _ in range(6): self.rx(b'ok\n')
        self.assertEqual(self.c.pending[0],'G4 P0')
        self.assertGreater(self.c.pending[1],48)
        self.now[0]=24; self.c.tick(); self.assertEqual(self.c.state,'running')
        self.rx(b'ok\n'); self.assertTrue(self.c.status_wait)
        self.now[0]=27; self.c.tick()
        self.assertEqual(self.c.state,'unknown'); self.assertEqual(self.c.failure,'Status timeout')

    def test_short_move_really_times_out(self):
        self.run_lines(['G1 X.01 F60'])
        self.assertLess(self.c.pending[1],3)
        self.now[0]=3; self.c.tick(); self.assertEqual(self.c.state,'unknown')

    def test_elapsed_motion_is_removed_from_later_barrier_budget(self):
        self.run_lines(['G1 X4 F60'])
        self.now[0]=5; self.rx(b'ok\n')
        self.assertEqual(self.c.pending[0],'G4 P0')
        self.assertAlmostEqual(self.c.pending[1],7)

    def test_hold_confirmation_and_status_remain_finite(self):
        self.run_lines(['G1 X4 F60']); self.c.submit('pause'); self.c.tick()
        self.rx(b'<Hold:1|WPos:0,0,0>\n')
        self.now[0]=2.01; self.c.tick()
        self.assertEqual(self.c.state,'unknown')

    def test_pause_during_final_query_then_idle_clears_pause_deadlines(self):
        self.run_lines(['G1 X.01 F60'])
        self.rx(b'ok\n'); self.rx(b'ok\n')
        self.assertTrue(self.c.final_query); self.assertTrue(self.c.status_wait)
        self.c.submit('pause'); self.c.tick()
        old_hold_deadline=self.c.hold_deadline
        self.rx(b'<Idle|WPos:.01,0,0>\n')
        self.assertEqual(self.c.state,'complete')
        self.assertIsNone(self.c.pause_started); self.assertIsNone(self.c.hold_deadline)
        # Keep normal background status replies healthy past the old Hold limit.
        self.now[0]=old_hold_deadline+1; self.c.tick()
        self.rx(b'<Idle|WPos:.01,0,0>\n')
        self.assertEqual(self.c.state,'complete')
        # A following job must retain a finite, active acknowledgement deadline.
        self.c.job=True; self.c.state='running'; self.c.barrier=False
        self.c.planned_position=self.c.wpos; self.c.motion_finish=self.now[0]
        self.c.lines.append('G1 X.02 F60'); self.c.tick()
        deadline=self.c.pending[1]
        self.assertTrue(deadline>self.now[0]); self.assertLess(deadline-self.now[0],3)
        self.now[0]=deadline+.01; self.c.tick()
        self.assertEqual(self.c.state,'unknown')
        self.assertEqual(self.c.failure,'Command acknowledgement timeout')

    def early_barrier_square(self):
        self.run_lines(['G1 Z2 F30','G1 X4 F60','G1 Y4 F60',
                        'G1 X0 F60','G1 Y0 F60','G1 Z0 F30'])
        for _ in range(7): self.rx(b'ok\n')
        self.assertTrue(self.c.final_query)

    def status_until(self,end,state='Run'):
        while self.now[0]<end:
            self.now[0]=min(end,self.now[0]+.1); self.c.tick()
            if self.c.status_wait:
                self.rx(('<'+state+'|WPos:0,0,0>\n').encode())

    def test_early_barrier_ack_preserves_24_second_motion_budget(self):
        self.early_barrier_square(); deadline=self.c.completion_deadline
        self.assertGreater(deadline,48)
        self.status_until(24)
        self.assertEqual(self.c.state,'running'); self.assertEqual(self.c.completion_deadline,deadline)
        self.now[0]+=.3; self.c.tick(); self.rx(b'<Idle|WPos:0,0,0>\n')
        self.assertEqual(self.c.state,'complete'); self.assertIsNone(self.c.completion_deadline)

    def test_permanent_run_hits_fixed_completion_timeout(self):
        self.early_barrier_square(); deadline=self.c.completion_deadline
        self.status_until(deadline+.1)
        self.assertEqual(self.c.state,'unknown')
        self.assertEqual(self.c.failure,'Motion completion timeout')

    def test_final_status_queries_have_bounded_frequency(self):
        self.early_barrier_square(); self.rx(b'<Run|WPos:0,0,0>\n')
        before=self.s.sent.count(b'?')
        for _ in range(100):
            self.now[0]+=.01; self.c.tick()
            if self.c.status_wait: self.rx(b'<Run|WPos:0,0,0>\n')
        self.assertLessEqual(self.s.sent.count(b'?')-before,4)
        self.assertGreaterEqual(self.s.sent.count(b'?')-before,3)

    def test_final_completion_budget_freezes_during_hold(self):
        self.early_barrier_square(); deadline=self.c.completion_deadline
        self.c.submit('pause'); self.c.tick(); self.rx(b'<Hold:0|WPos:0,0,0>\n')
        self.status_until(100,'Hold:0')
        self.assertEqual(self.c.state,'paused'); self.assertEqual(self.c.completion_deadline,deadline)
        self.c.submit('resume'); self.c.tick()
        self.assertAlmostEqual(self.c.completion_deadline,deadline+100)
        self.status_until(124)
        self.assertEqual(self.c.state,'running')
        self.now[0]+=.3; self.c.tick(); self.rx(b'<Idle|WPos:0,0,0>\n')
        self.assertEqual(self.c.state,'complete')

    def test_long_confirmed_pause_freezes_ack_budget_but_not_status(self):
        self.run_lines(['G1 X4 F60'])
        before=self.c.pending[1]
        self.c.submit('pause'); self.c.tick(); self.rx(b'<Hold:0|WPos:0,0,0>\n')
        for _ in range(10):
            self.now[0]+=1; self.c.tick(); self.rx(b'<Hold:0|WPos:0,0,0>\n')
        self.assertEqual(self.c.state,'paused')
        self.c.submit('resume'); self.c.tick()
        self.assertAlmostEqual(self.c.pending[1],before+10)
        self.now[0]=before+10+.01; self.c.tick(); self.assertEqual(self.c.state,'unknown')

    def test_set_pen_stroke_sets_only_runtime_values_and_bounds(self):
        self.ready(); self.c.submit('declare_clearance',xy_mm=2,z_mm=3); self.c.tick()
        before=list(self.s.sent)
        self.c.submit('set_pen_stroke',distance=2,direction=1,confirmed=True); self.c.tick()
        self.assertEqual((self.c.pen_up,self.c.pen_down),(0,2))
        self.assertEqual(before,self.s.sent)
        for kwargs in [dict(distance=4,direction=1,confirmed=True),
                       dict(distance=2,direction=1,confirmed=False),
                       dict(distance=float('nan'),direction=1,confirmed=True),
                       dict(distance=2,direction=0,confirmed=True)]:
            self.c.submit('set_pen_stroke',**kwargs); self.c.tick()
            self.assertEqual((self.c.pen_up,self.c.pen_down),(0,2))

    def touch_at(self,z):
        self.ready(); self.c.submit('declare_clearance',xy_mm=2,z_mm=5); self.c.tick()
        self.rx(f'<Idle|MPos:0,0,{15+z}>\n'.encode())

    def test_pen_touch_records_contact_and_derives_heights_without_motion(self):
        self.touch_at(2); before=list(self.s.sent)
        self.c.submit('set_pen_touch',lift=2,press=3,direction=1,confirmed=True); self.c.tick()
        self.assertEqual((self.c.pen_touch,self.c.pen_up,self.c.pen_down),(2,0,5))
        self.assertEqual(self.c.snapshot()['pen_touch'],2)
        self.assertEqual(before,self.s.sent)
        self.c.submit('set_pen_stroke',distance=2,direction=1,confirmed=True); self.c.tick()
        self.assertIsNone(self.c.pen_touch)

    def test_pen_touch_rejects_unconfirmed_invalid_or_out_of_envelope(self):
        self.touch_at(3)
        for kwargs in [dict(lift=2,press=3,direction=1,confirmed=True),  # down 6 > +5 envelope
                       dict(lift=2,press=1,direction=1,confirmed=False),
                       dict(lift=0,press=1,direction=1,confirmed=True),
                       dict(lift=2,press=float('nan'),direction=1,confirmed=True),
                       dict(lift=2,press=1,direction=True,confirmed=True)]:
            self.c.submit('set_pen_touch',**kwargs); self.c.tick()
            self.assertIsNone(self.c.pen_touch); self.assertIsNone(self.c.pen_up)
        self.c.submit('set_pen_touch',lift=2,press=1,direction=1,confirmed=True); self.c.tick()
        self.assertEqual((self.c.pen_up,self.c.pen_down),(1,4))

    def test_pen_touch_needs_clearance_survives_anchor_but_not_page_change(self):
        self.ready()
        self.c.submit('set_pen_touch',lift=2,press=1,direction=1,confirmed=True); self.c.tick()
        self.assertIsNone(self.c.pen_touch)
        self.c.submit('declare_clearance',xy_mm=2,z_mm=5); self.c.tick()
        self.c.submit('set_pen_touch',lift=2,press=1,direction=1,confirmed=True); self.c.tick()
        self.assertEqual(self.c.pen_touch,0)
        self.c.submit('anchor_preset',preset='songboshi_2024_paper',page_x=0,page_y=0,geometry_id='page',axes_confirmed=True); self.c.tick()
        self.assertIsNotNone(self.c.mapping)
        self.assertEqual((self.c.pen_touch,self.c.pen_up,self.c.pen_down),(0,-2,1))
        self.c.submit('geometry_changed',geometry_id='other-page'); self.c.tick()
        self.assertIsNone(self.c.pen_touch); self.assertIsNone(self.c.pen_up)
        self.c.submit('declare_clearance',xy_mm=2,z_mm=5,confirmed_new_range=True); self.c.tick()
        self.c.submit('set_pen_touch',lift=2,press=1,direction=1,confirmed=True); self.c.tick()
        self.c.submit('manual_reposition'); self.c.tick()
        self.assertIsNone(self.c.pen_touch); self.assertIsNone(self.c.pen_up)

    def test_lift_pen_only_moves_away_from_paper_to_pen_up(self):
        self.touch_at(2)
        self.c.submit('set_pen_touch',lift=2,press=1,direction=1,confirmed=True); self.c.tick()
        self.c.submit('lift_pen'); self.c.tick()
        self.assertEqual(self.s.sent[-1],b'$J=G21 G91 Z-2.0000 F120.000\n')
        self.assertEqual(self.c.jog_target,(0,0,0))
        self.rx(b'ok\n'); self.now[0]=1; self.c.tick(); self.assertTrue(self.c.status_wait)
        self.rx(b'<Idle|MPos:0,0,15>\n')
        self.assertIsNone(self.c.jog_target); self.assertNotEqual(self.c.state,'unknown')
        sent=len(self.s.sent); self.c.submit('lift_pen'); self.c.tick()
        self.assertFalse(any(b.startswith(b'$J') for b in self.s.sent[sent:]))
        # Pen above the lift height: "lifting" would move toward the paper.
        self.c.pen_up,self.c.pen_down=1,4
        self.c.submit('lift_pen'); self.c.tick()
        self.assertFalse(any(b.startswith(b'$J') for b in self.s.sent[sent:]))

    def test_actions_wait_for_status_reply_in_order_instead_of_failing(self):
        self.touch_at(0)
        self.c._query(); self.assertTrue(self.c.status_wait)
        rejected=[]
        self.c.submit('set_pen_touch',lift=2,press=1,direction=1,confirmed=True); self.c.submit('lift_pen')
        self.c.tick()
        self.assertIsNone(self.c.pen_touch)                 # waiting, not rejected
        self.assertFalse([e for e in self.c.drain_events() if e['kind']=='rejected'])
        self.rx(b'<Idle|MPos:0,0,15>\n'); self.c.tick()
        self.assertEqual(self.c.pen_touch,0)
        self.assertTrue(self.s.sent[-1].startswith(b'$J=G21 G91 Z-2'))   # lift ran after the touch, in order

    def test_stop_drops_waiting_motion(self):
        self.touch_at(0)
        self.c._query(); self.c.submit('jog',axis='X',distance=.2,feed=60); self.c.tick()
        self.assertTrue(self.c._deferred)
        self.c.submit('stop'); self.c.tick()
        self.assertFalse(self.c._deferred)
        self.rx(b'<Idle|MPos:0,0,15>\n'); self.c.tick()
        self.assertFalse(any(b.startswith(b'$J') for b in self.s.sent))

if __name__=='__main__': unittest.main()


