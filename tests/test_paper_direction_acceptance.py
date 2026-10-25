"""Independent acceptance against the user's observed physical paper axes."""
from pathlib import Path
import re
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from pagewriter.controller import MachineController
from pagewriter.protocol import compile_machine_job
from test_direct_acceptance import Port


class PaperDirectionAcceptance(unittest.TestCase):
    def setUp(self):
        self.now = [0.]
        self.c = MachineController(Port, lambda: self.now[0], 2)
        self.c.submit('connect'); self.c.tick(); self.port = self.c.serial
        self.rx(b'<Idle|MPos:0,0,15|WCO:0,0,15>\n')
        for payload in (b'ok\n', b'[GC:G0 G54 G17 G21 G90 G94]\nok\n',
                        b'$13=0\nok\n', b'ok\n'):
            self.rx(payload)
        if self.c.status_wait:
            self.rx(b'<Idle|MPos:0,0,15>\n')

    def rx(self, data):
        self.port.rx.extend(data); self.c.tick()

    def action(self, name, **kw):
        self.c.submit(name, **kw); self.c.tick()

    def preset(self, x=10, y=10, preset='songboshi_2024_paper'):
        self.action('anchor_preset', preset=preset,
                    page_x=x, page_y=y, geometry_id='paper', axes_confirmed=True)

    def test_compiled_asymmetric_strokes_match_physical_right_and_down(self):
        self.action('declare_clearance', xy_mm=10, z_mm=5)
        before = list(self.port.sent)
        self.preset()
        self.assertEqual(before, self.port.sent)
        self.assertFalse(self.c.snapshot()['authorized'])
        self.action('set_pen_stroke', distance=5, direction=1, confirmed=True)
        self.action('authorize', work_bounds=(-10,-10,10,10), z_bounds=(0,5),
                    write_feed=3000, travel_feed=4000, z_feed=3000,
                    max_write_feed=4000, max_travel_feed=4000, max_z_feed=5000,
                    clearance_verified=True, validation_note='independent physical-axis fixture')
        paths = [[(10,10),(13,10),(13,12)], [(11,13),(12,11)]]
        lines = compile_machine_job(paths, self.c.profile, preflight_passed=True)
        machine = []
        for line in lines:
            match = re.match(r'G1 X([-\d.]+) Y([-\d.]+) F', line)
            if match:
                machine.append(tuple(map(float, match.groups())))
        # Observed machine X+ points paper-up; machine Y+ points paper-right.
        paper = [(my, -mx) for mx, my in machine]
        expected = [(x-10, y-10) for path in paths for x,y in path]
        self.assertEqual(expected, paper)
        self.assertEqual([0,5,0,5,0], [float(line.split()[1][1:])
                         for line in lines if line.startswith('G1 Z')])

    def test_compiled_180_strokes_remain_upright_on_inverted_book(self):
        self.action('declare_clearance', xy_mm=10, z_mm=5)
        before = list(self.port.sent)
        self.preset(preset='songboshi_2024_paper_180')
        self.action('set_pen_stroke', distance=4, direction=1, confirmed=True)
        self.action('authorize', work_bounds=(-10,-10,10,10), z_bounds=(0,5),
                    write_feed=3000, travel_feed=4000, z_feed=3000,
                    max_write_feed=4000, max_travel_feed=4000, max_z_feed=5000,
                    clearance_verified=True, validation_note='offline inverted-book fixture')
        paths = [[(10,10),(13,10),(13,12)], [(11,13),(12,11)]]
        lines = compile_machine_job(paths, self.c.profile, preflight_passed=True)
        machine = [tuple(map(float,m.groups())) for line in lines
                   if (m := re.match(r'G1 X([-\d.]+) Y([-\d.]+) F', line))]
        # A physically inverted book reverses both of the previously observed
        # paper axes. Convert compiled endpoints back to upright reading space.
        paper = [(-my,mx) for mx,my in machine]
        self.assertEqual([(x-10,y-10) for path in paths for x,y in path], paper)
        self.assertEqual(before, self.port.sent)  # compilation never transmits

    def test_manual_push_preserves_only_axes_and_reanchors_new_point(self):
        self.preset(7,8)
        self.action('manual_reposition')
        self.assertIsNone(self.c.mapping)
        self.assertIsNone(self.c.clearance)
        self.action('declare_clearance', xy_mm=10, z_mm=5)
        self.action('anchor', page_x=2, page_y=3, axes_confirmed=True)
        self.assertEqual((0,1,-1,0,3,-2), self.c.mapping)
        self.assertIsNone(self.c.profile)
        self.assertIsNone(self.c.pen_down)

    def test_new_three_point_page_replaces_preset_without_extra_rotation(self):
        self.preset()
        for page, machine in [((0,0),(2,3)), ((5,0),(7,3)), ((0,5),(2,8))]:
            self.rx(f'<Idle|MPos:{machine[0]},{machine[1]},15>\n'.encode())
            self.action('capture_point', page_x=page[0], page_y=page[1])
        self.action('fit_calibration', geometry_id='turned-book')
        self.assertEqual((1,0,0,1,2,3), self.c.mapping)
        self.assertEqual('turned-book', self.c.geometry_id)
        self.assertIsNone(self.c.profile)


if __name__ == '__main__':
    unittest.main(verbosity=2)
