"""Resize handles for text boxes and protected zones; repaint bounds during drags."""
import unittest
import unittest.mock
from PySide6.QtCore import QPointF, Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication
from pagewriter import core, ui

app = QApplication.instance() or QApplication([])
HTML = '<p style="font-family:Microsoft YaHei;font-size:10pt;">resize 测试文字 resize 测试文字 resize 测试文字</p>'


class ResizeTests(unittest.TestCase):
    def setUp(self):
        self.w = ui.MainWindow()
        self.w.resize(1500, 980)
        self.w.project = core.Project(width=148, height=210, blocks=[
            core.TextBlock(id='single', name='single', html=HTML, x=20, y=30, width=60),
            core.TextBlock(id='cols', name='cols', html=HTML*3, x=20, y=90, width=100, columns=2, column_height=30, column_gap=6)],
            zones=[core.ExclusionZone(id='rect', name='rect', points=[(10, 150), (50, 150), (50, 170), (10, 170)]),
                   core.ExclusionZone(id='poly', name='poly', points=[(80, 150), (120, 150), (100, 190)])])
        self.w.selected_id = 'single'
        self.w.refresh()

    def tearDown(self):
        self.w.dirty = False
        self.w.close(); self.w.deleteLater(); app.processEvents()

    def item(self, item_id):
        for item in self.w.canvas.scene().items():
            if getattr(item, 'block_id', None) == item_id or getattr(item, 'zone_id', None) == item_id:
                return item

    def select(self, item_id):
        self.w.selected_id = item_id
        self.w.refresh()
        return self.item(item_id)

    def drag(self, item, role, scene_point):
        item.begin_resize()
        item.drag_resize(role, QPointF(*scene_point))
        item.end_resize()
        app.processEvents()

    def test_selection_frame_is_inside_repaint_bounds(self):
        for item_id in ('single', 'cols'):
            item = self.select(item_id)
            self.assertTrue(item.isSelected())
            frame = item.content_rect().adjusted(-1, -1, 1, 1)
            self.assertTrue(item.boundingRect().contains(frame), item_id)

    def test_single_column_width_handles_and_undo(self):
        item = self.select('single')
        self.assertEqual(set(item.handles), {'left', 'right', 'rotate'})
        self.drag(item, 'right', (100, 35))
        block = self.w.block_by_id('single')
        self.assertAlmostEqual(block.width, 80, places=1)
        self.assertEqual((block.x, block.y), (20, 30))
        self.w.undo()
        self.assertEqual(self.w.block_by_id('single').width, 60)

    def test_left_handle_keeps_right_edge(self):
        item = self.select('single')
        self.drag(item, 'left', (30, 35))
        block = self.w.block_by_id('single')
        self.assertAlmostEqual(block.width, 50, places=1)
        self.assertAlmostEqual(block.x+block.width, 80, places=1)

    def test_rotated_box_resizes_along_its_own_axis(self):
        self.w.block_by_id('single').rotation = 90
        item = self.select('single')
        self.drag(item, 'right', (20-5, 30+70))
        self.assertAlmostEqual(self.w.block_by_id('single').width, 70, places=1)

    def test_columns_resize_height_and_corner(self):
        item = self.select('cols')
        self.assertEqual(set(item.handles), {'left', 'right', 'bottom', 'corner', 'rotate'})
        self.drag(item, 'bottom', (70, 90+45))
        self.assertAlmostEqual(self.w.block_by_id('cols').column_height, 45, places=1)
        item = self.item('cols')
        self.drag(item, 'corner', (20+110, 90+40))
        block = self.w.block_by_id('cols')
        self.assertAlmostEqual((block.width, block.column_height)[0], 110, places=1)
        self.assertAlmostEqual(block.column_height, 40, places=1)

    def test_width_cannot_collapse_below_gaps(self):
        item = self.select('cols')
        self.drag(item, 'right', (0, 100))
        block = self.w.block_by_id('cols')
        self.assertGreater(block.width, block.column_gap)
        self.assertFalse(any('无效' in c.message for c in core.preflight(self.w.project)))

    def test_rectangle_zone_has_eight_handles_and_min_size(self):
        item = self.select('rect')
        self.assertEqual(len(item.handles), 8)
        self.drag(item, 'br', (70, 180))
        zone = next(z for z in self.w.project.zones if z.id == 'rect')
        self.assertEqual(zone.points, [(10, 150), (70, 150), (70, 180), (10, 180)])
        self.drag(self.item('rect'), 'tl', (200, 200))
        zone = next(z for z in self.w.project.zones if z.id == 'rect')
        xs, ys = [p[0] for p in zone.points], [p[1] for p in zone.points]
        self.assertAlmostEqual(max(xs)-min(xs), .5); self.assertAlmostEqual(max(ys)-min(ys), .5)

    def test_moved_zone_resizes_from_its_moved_position(self):
        item = self.select('rect')
        item.setPos(5, 5)
        self.drag(item, 'r', (60, 160))
        zone = next(z for z in self.w.project.zones if z.id == 'rect')
        self.assertEqual(zone.points, [(15, 155), (60, 155), (60, 175), (15, 175)])

    def test_polygon_zone_moves_one_vertex(self):
        item = self.select('poly')
        self.assertEqual(set(item.handles), {'v0', 'v1', 'v2'})
        self.drag(item, 'v2', (105, 200))
        zone = next(z for z in self.w.project.zones if z.id == 'poly')
        self.assertEqual(zone.points, [(80, 150), (120, 150), (105, 200)])

    def test_handles_hidden_in_writing_stage_and_when_deselected(self):
        item = self.select('rect')
        self.assertTrue(item.handles)
        self.w.set_stage(2)
        self.assertFalse(self.item('rect').handles)
        self.w.set_stage(1)
        self.assertTrue(self.item('rect').handles)
        self.item('rect').setSelected(False)
        self.assertFalse(self.item('rect').handles)

    def test_single_frame_on_box_and_handles_on_frame(self):
        item = self.select('cols')
        frame = item.frame()
        self.assertEqual(frame, item.column_rect)
        for role, point in [('left', QPointF(0, frame.height()/2)), ('right', QPointF(frame.width(), frame.height()/2)),
                            ('bottom', QPointF(frame.width()/2, frame.height())), ('corner', frame.bottomRight())]:
            self.assertEqual(item.handles[role].pos(), point)
        self.assertTrue(item.boundingRect().contains(frame))
        self.assertTrue(item.boundingRect().contains(item.rotate_anchor()))
        from PySide6.QtGui import QImage, QPainter
        from PySide6.QtWidgets import QStyleOptionGraphicsItem, QStyle
        seen = []
        original = ui.QGraphicsPathItem.paint
        def spy(self_, painter, option, widget=None):
            seen.append(bool(option.state & QStyle.StateFlag.State_Selected))
            return original(self_, painter, option, widget)
        option = QStyleOptionGraphicsItem(); option.state |= QStyle.StateFlag.State_Selected
        image = QImage(50, 50, QImage.Format.Format_ARGB32); painter = QPainter(image)
        with unittest.mock.patch.object(ui.QGraphicsPathItem, 'paint', spy):
            item.paint(painter, option)
        painter.end()
        self.assertEqual(seen, [False])

    def test_direction_handle_rotates_about_center_with_snapping(self):
        item = self.select('single')
        self.assertIn('rotate', item.handles)
        center = item.mapToScene(item.frame().center())
        item.begin_resize()
        # Pull the handle straight down: text turned upside down (180°).
        item.drag_resize('rotate', center+QPointF(2, 30))
        self.assertEqual(item.rotation(), 180)
        moved = item.mapToScene(item.frame().center())
        self.assertAlmostEqual(moved.x(), center.x(), places=6); self.assertAlmostEqual(moved.y(), center.y(), places=6)
        item.end_resize(); app.processEvents()
        block = self.w.block_by_id('single')
        self.assertEqual(block.rotation, 180)
        self.w.undo(); self.assertEqual(self.w.block_by_id('single').rotation, 0)

    def test_direction_snaps_to_15_degrees_shift_is_free(self):
        item = self.select('single')
        center = item.mapToScene(item.frame().center())
        item.begin_resize()
        item.drag_resize('rotate', center+QPointF(30, -28))   # ~47°
        self.assertEqual(item.rotation(), 45)
        item.drag_resize('rotate', center+QPointF(30, -28), Qt.KeyboardModifier.ShiftModifier)
        self.assertEqual(item.rotation(), 47)
        item.drag_resize('rotate', center+QPointF(-30, 0.5))  # pointing left
        self.assertEqual(item.rotation(), 270)

    def test_real_mouse_drag_of_direction_handle(self):
        self.w.show(); app.processEvents(); self.w.fit_page(); app.processEvents()
        item = self.select('single')
        canvas = self.w.canvas
        center = item.mapToScene(item.frame().center())
        start = canvas.mapFromScene(item.handles['rotate'].scenePos())
        end = canvas.mapFromScene(center+QPointF(40, 0))
        QTest.mousePress(canvas.viewport(), Qt.MouseButton.LeftButton, pos=start)
        QTest.mouseMove(canvas.viewport(), end)
        QTest.mouseRelease(canvas.viewport(), Qt.MouseButton.LeftButton, pos=end)
        app.processEvents()
        self.assertEqual(self.w.block_by_id('single').rotation, 90)

    def test_magnifier_only_while_picking_a_point_on_the_photo(self):
        self.w.show(); app.processEvents(); self.w.fit_page(); app.processEvents()
        from PySide6.QtCore import QEvent
        from PySide6.QtGui import QMouseEvent
        canvas = self.w.canvas
        here = QPointF(canvas.mapFromScene(QPointF(40, 40)))
        def move(point):
            # Deliver directly: QTest.mouseMove drives the real cursor, which
            # another window in front of the test window would intercept.
            event = QMouseEvent(QEvent.Type.MouseMove, point, canvas.viewport().mapToGlobal(point),
                                Qt.MouseButton.NoButton, Qt.MouseButton.NoButton, Qt.KeyboardModifier.NoModifier)
            app.sendEvent(canvas.viewport(), event); app.processEvents()
        move(here)
        self.assertFalse(canvas.loupe.isVisible())
        self.w.set_stage(2)
        self.w.device_panel.pick('test_square')
        self.assertEqual(canvas.mode, 'reference')
        move(here+QPointF(3, 3))
        self.assertTrue(canvas.loupe.isVisible())
        self.assertEqual(canvas.loupe.pixmap().width(), canvas.LOUPE)
        canvas.set_mode('select')
        self.assertFalse(canvas.loupe.isVisible())

    def test_real_mouse_drag_on_handle(self):
        self.w.show(); app.processEvents(); self.w.fit_page(); app.processEvents()
        item = self.select('single')
        handle = item.handles['right']
        canvas = self.w.canvas
        start = canvas.mapFromScene(handle.scenePos())
        end = canvas.mapFromScene(handle.scenePos()+QPointF(15, 0))
        QTest.mousePress(canvas.viewport(), Qt.MouseButton.LeftButton, pos=start)
        QTest.mouseMove(canvas.viewport(), end)
        QTest.mouseRelease(canvas.viewport(), Qt.MouseButton.LeftButton, pos=end)
        app.processEvents()
        self.assertAlmostEqual(self.w.block_by_id('single').width, 75, delta=1)
        self.assertIsNone(canvas.editing_id)


if __name__ == '__main__':
    unittest.main()
