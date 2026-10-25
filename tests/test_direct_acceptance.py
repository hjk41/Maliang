"""Independent direct-controller acceptance; fake port only."""
from pathlib import Path
import sys
import unittest
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from pagewriter.controller import MachineController, fit_affine
from pagewriter.protocol import MachineProfile, compile_machine_job

class Port:
    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.rx = bytearray()
        self.sent = []
        self.opened = False
    def open(self):
        assert not self.dtr and not self.rts
        self.opened = True
    def close(self): self.opened = False
    @property
    def in_waiting(self): return len(self.rx)
    def read(self, count):
        result = bytes(self.rx[:count]); del self.rx[:count]; return result
    def write(self, data): self.sent.append(data); return len(data)

def profile():
    return MachineProfile(page_to_machine=(1,0,0,1,0,0), xy_verified=True,
        z_verified=True, clearance_verified=True, work_bounds=(-10,-10,10,10),
        z_bounds=(-2,2), z_up=1, z_down=0, write_feed=30, z_feed=20,
        max_write_feed=60, max_z_feed=30, validation_note='independent fake fixture')

class DirectAcceptance(unittest.TestCase):
    def setUp(self):
        self.now = [0.]
        self.c = MachineController(Port, lambda:self.now[0], 2)
        self.c.submit('connect'); self.c.tick(); self.port = self.c.serial
        self.rx(b'<Idle|MPos:0,0,15|WCO:0,0,15>\n')
        self.rx(b'ok\n'); self.rx(b'[GC:G0 G54 G17 G21 G90 G94]\nok\n')
        self.rx(b'$13=0\nok\n'); self.rx(b'ok\n')
        self.assertEqual('ready', self.c.state)
        if self.c.status_wait: self.rx(b'<Idle|MPos:0,0,15>\n')
    def rx(self, data): self.port.rx.extend(data); self.c.tick()
    def action(self, action, **args): self.c.submit(action, **args); self.c.tick()
    def ready_job(self):
        self.c.job = True; self.c.state = 'running'; self.c.total = 1
        self.c.lines.append('G1 X1 F30'); self.c.tick()

    def test_hold_decelerating_cannot_resume(self):
        self.ready_job(); self.action('pause')
        self.rx(b'<Hold:1|MPos:0,0,15>\n')
        self.action('resume')
        self.assertNotIn(b'~', self.port.sent)
        self.rx(b'<Hold:0|MPos:0,0,15>\n')
        self.action('resume')
        self.assertEqual(b'~', self.port.sent[-1])

    def test_disconnect_active_job_requests_hold_before_closing(self):
        self.ready_job(); self.action('disconnect')
        self.assertIn(b'!', self.port.sent)
        self.assertNotIn(b'\x18', self.port.sent)
        self.assertFalse(self.port.opened)

    def test_compiler_does_not_use_unbounded_firmware_rapid(self):
        lines = compile_machine_job([[(0,0),(1,1)]], profile(), preflight_passed=True)
        self.assertFalse(any(line.split()[0] in {'G0','G00'} for line in lines))
        self.assertTrue(any(line.startswith('G1 X0.0000 Y0.0000 F') for line in lines))

    def test_repeated_manual_movement_retains_axes_only(self):
        self.c.mapping = (0,1,-1,0,4,5)
        self.c.profile = profile(); self.c.pen_up = 1; self.c.pen_down = 0
        self.action('manual_reposition'); self.action('manual_reposition')
        self.assertIsNone(self.c.profile)
        self.assertIsNone(self.c.pen_down)
        self.action('anchor', page_x=2, page_y=3, axes_confirmed=True)
        self.assertEqual((0,1,-1,0,3,-2), self.c.mapping)

    def test_wco_change_removes_runtime_authority_and_anchor(self):
        self.c.mapping = (1,0,0,1,0,0); self.c.profile = profile()
        self.rx(b'<Idle|MPos:0,0,15|WCO:1,0,15>\n')
        self.assertIsNone(self.c.profile); self.assertIsNone(self.c.mapping)
        self.action('anchor', page_x=0, page_y=0, axes_confirmed=True)
        self.assertIsNone(self.c.mapping)

    def test_reconnect_never_restores_previous_authority(self):
        self.c.mapping = (1,0,0,1,0,0); self.c.profile = profile()
        old_session = self.c.session_id
        self.action('disconnect'); self.action('connect')
        self.assertNotEqual(old_session, self.c.session_id)
        self.assertIsNone(self.c.profile); self.assertIsNone(self.c.mapping)

    def test_start_outside_live_bounds_rejected(self):
        self.c.mapping=(1,0,0,1,0,0); self.c.profile=profile(); self.c.geometry_id='page'
        self.rx(b'<Idle|MPos:100,100,15>\n')
        self.action('start_job', polylines=[[(0,0),(1,1)]], geometry_id='page', preflight_passed=True)
        self.assertFalse(self.c.job)

    def test_unreported_manual_three_points_cannot_make_zero_matrix(self):
        for point in [(0,0),(10,0),(0,10)]:
            self.action('capture_point', page_x=point[0], page_y=point[1])
        self.action('fit_calibration', geometry_id='page')
        self.assertIsNone(self.c.mapping)

    def test_affine_handles_swapped_reversed_axes(self):
        result=fit_affine([((10,20),(5,7)),((20,20),(5,-3)),((10,30),(15,7))])
        self.assertEqual((0,-1,1,0,-15,17), result)

    def test_fixed_stroke_compiles_realistic_square_without_early_barrier_timeout(self):
        self.action('declare_clearance',xy_mm=10,z_mm=2)
        before=list(self.port.sent)
        self.action('set_pen_stroke',distance=2,direction=1,confirmed=True)
        self.assertEqual(before,self.port.sent)
        self.c.mapping=(1,0,0,1,0,0);self.c.geometry_id='coupon'
        self.action('authorize',work_bounds=(-1,-1,5,5),z_bounds=(-.1,2.1),
                    write_feed=60,z_feed=30,max_write_feed=60,max_z_feed=30,
                    clearance_verified=True,validation_note='independent finite square fixture')
        self.action('start_job',polylines=[[(0,0),(4,0),(4,4),(0,4),(0,0)]],
                    geometry_id='coupon',preflight_passed=True)
        for _ in range(11):self.rx(b'ok\n')
        self.assertEqual(self.c.pending[0],'G4 P0')
        self.assertGreater(self.c.pending[1],48)
        self.now[0]=24;self.c.tick()
        self.assertEqual(self.c.state,'running')
        self.rx(b'ok\n')
        self.assertTrue(self.c.status_wait)
        self.rx(b'<Idle|MPos:0,0,15>\n')
        self.assertEqual(self.c.state,'complete')
        self.assertEqual(self.c.snapshot()['progress'],(11,11))

    def test_reverse_fixed_stroke_is_runtime_only_and_stale_redefinition_rejected(self):
        self.action('declare_clearance',xy_mm=10,z_mm=2)
        self.c.profile=profile()
        before=list(self.port.sent)
        self.action('set_pen_stroke',distance=2,direction=-1,confirmed=True)
        self.assertEqual((self.c.pen_up,self.c.pen_down),(0,-2))
        self.assertIsNone(self.c.profile)
        self.assertEqual(before,self.port.sent)
        self.now[0]=3
        self.action('set_pen_stroke',distance=1,direction=1,confirmed=True)
        self.assertEqual(self.c.pen_down,-2)
        self.action('disconnect')
        self.assertIsNone(self.c.pen_up)
        self.assertIsNone(self.c.pen_down)

    def test_early_barrier_ack_has_throttled_queries_and_nonrenewable_motion_deadline(self):
        self.action('declare_clearance',xy_mm=10,z_mm=4)
        self.action('set_pen_stroke',distance=4,direction=1,confirmed=True)
        self.c.mapping=(1,0,0,1,0,0);self.c.geometry_id='z4'
        self.action('authorize',work_bounds=(-1,-1,5,5),z_bounds=(-.1,4.1),
                    write_feed=60,z_feed=30,max_write_feed=60,max_z_feed=30,
                    clearance_verified=True,validation_note='independent early-ACK firmware fixture')
        self.action('start_job',polylines=[[(0,0),(4,0),(4,4),(0,4),(0,0)]],
                    geometry_id='z4',preflight_passed=True)
        for _ in range(12):self.rx(b'ok\n')
        deadline=self.c.completion_deadline
        self.assertGreater(deadline,64)
        self.assertLess(deadline,90)
        begin=self.port.sent.count(b'?')
        for i in range(1,101):
            self.now[0]=i*.01
            self.c.tick()
            if self.c.status_wait:self.rx(b'<Run|WPos:0,0,.5>\n')
            self.assertEqual(deadline,self.c.completion_deadline)
        self.assertLessEqual(self.port.sent.count(b'?')-begin,4)
        self.now[0]=32;self.c.tick()
        if self.c.status_wait:self.rx(b'<Run|WPos:0,0,.1>\n')
        self.assertEqual(self.c.state,'running')
        self.assertEqual(deadline,self.c.completion_deadline)
        self.now[0]=deadline+.01;self.c.tick()
        self.assertEqual(self.c.state,'unknown')
        self.assertEqual(self.c.failure,'Motion completion timeout')

if __name__=='__main__': unittest.main(verbosity=2)
