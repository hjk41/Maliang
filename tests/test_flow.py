"""Text boxes flow around protected zones and grow along their own "down"."""
import unittest
from PySide6.QtCore import QPointF, QRectF
from PySide6.QtGui import QPainterPath
from PySide6.QtWidgets import QApplication
from pagewriter import core, ui
from pagewriter.inline_edit import lines_for, hit_position

app = QApplication.instance() or QApplication([])
TEXT = '<p style="font-family:Microsoft YaHei;font-size:10pt;">' + '批注文字自动避开保护区，' * 10 + '</p>'


def rect_zone(zone_id, left, top, right, bottom, kind='ink'):
    return core.ExclusionZone(id=zone_id, name=zone_id, points=[(left, top), (right, top), (right, bottom), (left, bottom)], kind=kind)


def ink_bounds(block):
    return core.build_block_path(block).boundingRect()


class Flow(unittest.TestCase):
    def project(self, blocks, zones):
        return core.Project(width=148, height=210, blocks=blocks, zones=zones, pen_width=.3, margin=.4)

    def test_no_nearby_zone_keeps_exact_qt_layout_and_identity(self):
        plain = core.TextBlock(id='a', html=TEXT, x=20, y=30, width=60)
        before_key = core._layout_key(plain)
        before = core.path_to_data(core.local_block_path(plain))
        far = rect_zone('far', 100, 150, 140, 200)
        p = self.project([plain], [far])
        self.assertEqual([], core.preflight(p))
        self.assertEqual([], plain.flow_exclusions)
        self.assertEqual(before_key, core._layout_key(plain))
        self.assertEqual(before, core.path_to_data(core.local_block_path(plain)))

    def test_single_column_flows_around_zone_and_continues_below(self):
        zone = rect_zone('z', 30, 38, 60, 55)
        flowed = core.TextBlock(id='a', html=TEXT, x=20, y=30, width=60)
        reference = core.TextBlock(id='r', html=TEXT, x=20, y=30, width=60)
        p = self.project([flowed], [zone])
        self.assertEqual([], core.preflight(p))
        self.assertEqual(1, len(flowed.flow_exclusions))
        envelope = core.stroke_envelope(core.build_block_path(flowed), p.pen_width+2*p.margin)
        self.assertFalse(core.zone_path(zone).intersects(envelope))
        self.assertGreater(ink_bounds(flowed).bottom(), ink_bounds(reference).bottom()+1)
        # Lines beside the zone use the free strip to its right or left, not the zone.
        info = core.layout_info(flowed)
        self.assertTrue(any(line['y'] > 8 and line['y'] < 25 for line in info['lines']))

    def test_unavoidable_without_switch_is_still_blocked(self):
        zone = rect_zone('z', 30, 38, 60, 55)
        block = core.TextBlock(id='a', html=TEXT, x=20, y=30, width=60, avoid_zones=False)
        p = self.project([block], [zone])
        self.assertIn('z', {c.zone_id for c in core.preflight(p)})
        self.assertEqual([], block.flow_exclusions)

    def test_columns_grow_downward_when_flowing_no_longer_fits(self):
        zone = rect_zone('z', 25, 104, 70, 124)
        block = core.TextBlock(id='b', html=TEXT*2, x=20, y=100, width=100, columns=2, column_height=30, column_gap=6,
                               column_separators=True)
        p = self.project([block], [zone])
        self.assertEqual([], core.preflight(p))
        info = core.layout_info(block)
        self.assertFalse(info['overflow'])
        self.assertGreater(info['flow_height'], 30)
        self.assertEqual(info['height'], info['flow_height'])
        self.assertEqual(core.effective_column_height(block), info['flow_height'])
        separator = core.separator_path(block).boundingRect()
        self.assertAlmostEqual(separator.height(), info['flow_height'])
        self.assertEqual(30, block.column_height)   # the user's own setting is kept
        p.zones = []
        core.preflight(p)
        self.assertNotIn('flow_height', core.layout_info(block))
        self.assertEqual(30, core.effective_column_height(block))

    def test_separator_line_skips_zone_when_avoiding(self):
        block = core.TextBlock(id='s', html='', x=20, y=20, width=100, columns=2, column_height=50, column_gap=6, column_separators=True)
        zone = rect_zone('gap', 69.9, 35, 70.1, 36)
        p = self.project([block], [zone])
        self.assertEqual([], core.preflight(p))
        separator = core.separator_path(block)
        self.assertEqual(2, len(core.path_subpaths(separator)))   # split above and below the zone

    def test_rotated_180_box_grows_along_its_own_down(self):
        # Rotated 180° about its origin: local "down" is page "up".
        def box():
            return core.TextBlock(id='c', html=TEXT, x=120, y=200, width=60, rotation=180)
        reference = box()
        bounds = ink_bounds(reference)
        zone = rect_zone('z', bounds.left()+10, bounds.bottom()-12, bounds.left()+40, bounds.bottom()-2)
        flowed = box()
        p = self.project([flowed], [zone])
        self.assertEqual([], core.preflight(p))
        self.assertEqual(1, len(flowed.flow_exclusions))
        self.assertLess(ink_bounds(flowed).top(), bounds.top()-1)          # extended upward on the page
        self.assertAlmostEqual(ink_bounds(flowed).bottom(), bounds.bottom(), delta=1)

    def test_chain_of_zones_reached_only_after_pushing_down(self):
        first = rect_zone('first', 20, 30, 80, 50)
        reference = core.TextBlock(id='r', html=TEXT, x=20, y=30, width=60)
        below = ink_bounds(reference).bottom()+2
        second = rect_zone('second', 20, below+18, 80, below+22)
        block = core.TextBlock(id='a', html=TEXT, x=20, y=30, width=60)
        p = self.project([block], [first, second])
        self.assertEqual([], core.preflight(p))
        self.assertEqual(2, len(block.flow_exclusions))

    def test_flow_fields_round_trip_and_validation(self):
        zone = rect_zone('z', 30, 38, 60, 55)
        block = core.TextBlock(id='a', html=TEXT, x=20, y=30, width=60)
        p = self.project([block], [zone])
        core.preflight(p)
        loaded = core.project_from_dict(core.project_to_dict(p))
        self.assertEqual(block.flow_exclusions, loaded.blocks[0].flow_exclusions)
        self.assertEqual(core._layout_key(block), core._layout_key(loaded.blocks[0]))
        self.assertEqual([], core.preflight(loaded))
        for change in (dict(avoid_zones=1), dict(flow_clearance=-1), dict(flow_exclusions=[[[0, 0], [1, 1]]])):
            bad = core.TextBlock(id='x', html=TEXT, **change)
            with self.assertRaises(ValueError):
                core.project_to_dict(core.Project(blocks=[bad]))

    def test_editing_hits_the_flowed_line_position(self):
        zone = rect_zone('z', 20, 38, 45, 55)   # blocks the left part: lines start right of it
        block = core.TextBlock(id='a', html=TEXT, x=20, y=30, width=60)
        core.preflight(self.project([block], [zone]))
        doc, lines = lines_for(block)
        shifted = [entry for entry in lines if entry[1].x() > 1]
        self.assertTrue(shifted)
        pos, line, x, y, ox = shifted[0]
        left = (ox+line.x())*core.MM_PER_PX
        hit = hit_position(block, QPointF(left+.3, y+line.height()*core.MM_PER_PX/2))
        self.assertGreaterEqual(hit, pos+line.textStart())
        self.assertLessEqual(hit, pos+line.textStart()+1)


class FlowUi(unittest.TestCase):
    def test_checkbox_toggles_avoidance_and_status_reports_growth(self):
        w = ui.MainWindow()
        block = core.TextBlock(id='b', name='b', html=TEXT*2, x=20, y=100, width=100, columns=2, column_height=30, column_gap=6)
        w.project = core.Project(width=148, height=210, blocks=[block], zones=[rect_zone('z', 25, 104, 70, 124)])
        w.selected_id = 'b'
        w.refresh()
        self.assertTrue(w.avoid_zones.isChecked())
        self.assertIn('自动加到', w.column_status.text())
        item = w.text_items[0]
        self.assertGreater(item.frame().height(), 30)
        self.assertFalse(w.collisions)
        w.avoid_zones.setChecked(False)
        self.assertFalse(w.block_by_id('b').avoid_zones)
        self.assertTrue(w.collisions)
        w.undo()
        self.assertTrue(w.block_by_id('b').avoid_zones)
        w.dirty = False; w.close(); app.processEvents()


if __name__ == '__main__':
    unittest.main()
