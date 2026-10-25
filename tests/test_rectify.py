"""Four-corner picker: hidden native Qt, synthetic and real photos, no hardware."""
from pathlib import Path
import unittest
from unittest.mock import patch
from PySide6.QtCore import QPointF, Qt
from PySide6.QtGui import QImage, QKeyEvent
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QDialog
from pagewriter import core, ui
from pagewriter.rectify import RectifyDialog, order_corners, is_convex, CORNERS, LOUPE

app = QApplication.instance() or QApplication([])
TL, TR, BR, BL = (80, 60), (550, 110), (510, 750), (35, 690)


def image(w=600, h=800):
    result = QImage(w, h, QImage.Format.Format_RGB32)
    result.fill('white')
    return result


class Geometry(unittest.TestCase):
    def test_any_click_order_becomes_tl_tr_br_bl(self):
        for order in [(TL, TR, BR, BL), (BR, TL, BL, TR), (BL, BR, TR, TL), (TR, BL, TL, BR)]:
            self.assertEqual(order_corners(list(order)), [TL, TR, BR, BL])
        self.assertTrue(is_convex([TL, TR, BR, BL]))
        self.assertFalse(is_convex([TL, TR, BL, BR]))
        self.assertFalse(is_convex([TL, TR, BR]))


class Picker(unittest.TestCase):
    def setUp(self):
        self.d = RectifyDialog(None, image(), page_size=(120, 160))
        self.d.resize(1200, 800)

    def tearDown(self):
        self.d.close(); self.d.deleteLater(); app.processEvents()

    def add(self, *points):
        for point in points:
            self.d.add_point(QPointF(*point))
            self.d.finish_edit()

    def test_shuffled_clicks_are_reordered_and_labelled(self):
        self.add(BR, TL, BL)
        self.assertFalse(self.d.ok.isEnabled())
        self.assertIn('第 4 / 4 点', self.d.prompt.text())
        self.add(TR)
        self.assertEqual(self.d.points, [TL, TR, BR, BL])
        self.assertEqual([self.d.label_for(i) for i in range(4)], list(CORNERS))
        self.assertTrue(self.d.ok.isEnabled())
        self.assertIsNone(self.d.add_point(QPointF(1, 1)))

    def test_dragging_past_another_corner_is_rejected_until_fixed(self):
        self.add(TL, TR, BR, BL)
        self.d.move_point(0, QPointF(560, 760))
        self.assertFalse(self.d.ok.isEnabled())
        self.d.move_point(0, QPointF(*TL)); self.d.finish_edit()
        self.assertTrue(self.d.ok.isEnabled())

    def test_corners_may_be_estimated_outside_photo_but_are_bounded(self):
        self.add(TL, TR, BR)
        self.d.add_point(QPointF(-100, 900)); self.d.finish_edit()
        self.assertTrue(self.d.ok.isEnabled())
        self.assertIn('照片外面', self.d.message.text())
        self.d.move_point(3, QPointF(-10000, 10000))
        self.assertEqual(self.d.points[3], (self.d.bounds.left(), self.d.bounds.bottom()))

    def test_existing_quad_prefills_for_adjustment_and_previews(self):
        d = RectifyDialog(None, image(), [TL, TR, BR, BL], (120, 160))
        self.assertTrue(d.ok.isEnabled())
        pixmap = d.preview.pixmap()
        self.assertFalse(pixmap.isNull())
        self.assertAlmostEqual(pixmap.width()/pixmap.height(), 120/160, delta=.02)
        d.close()

    def test_undo_reset_and_keyboard_nudge(self):
        self.add(TL, TR)
        self.d.undo_point(); self.assertEqual(self.d.points, [TL])
        self.d.select(0)
        self.d.view.resetTransform()
        self.d.handle_key(QKeyEvent(QKeyEvent.Type.KeyPress, Qt.Key.Key_Right, Qt.KeyboardModifier.NoModifier))
        self.d.handle_key(QKeyEvent(QKeyEvent.Type.KeyPress, Qt.Key.Key_Down, Qt.KeyboardModifier.ShiftModifier))
        self.assertEqual(self.d.points[0], (81, 70))
        self.d.handle_key(QKeyEvent(QKeyEvent.Type.KeyPress, Qt.Key.Key_Backspace, Qt.KeyboardModifier.NoModifier))
        self.assertEqual(self.d.points, [])
        self.add(TL); self.d.reset(); self.assertEqual(self.d.points, [])

    def test_zoom_limits_and_fit(self):
        self.d.show(); app.processEvents(); self.d.view.fit()
        fitted = self.d.view.scale_factor()
        self.d.view.zoom(4)
        self.assertAlmostEqual(self.d.view.scale_factor(), fitted*4, places=3)
        self.assertFalse(self.d.view.auto_fit)
        self.d.view.zoom(1000); self.assertAlmostEqual(self.d.view.scale_factor(), 40)
        self.d.view.fit(); self.assertAlmostEqual(self.d.view.scale_factor(), fitted, places=3)
        self.assertTrue(self.d.view.auto_fit)

    def test_loupe_magnifies_relative_to_view(self):
        pixmap = self.d.loupe_pixmap(QPointF(100, 100), 1)
        self.assertEqual((pixmap.width(), pixmap.height()), (LOUPE, LOUPE))
        self.assertFalse(self.d.loupe_pixmap(QPointF(-500, -500), 10).isNull())

    def test_real_mouse_click_adds_and_drag_moves_point(self):
        self.d.show(); app.processEvents(); self.d.view.fit(); app.processEvents()
        view = self.d.view
        for point in (TL, TR, BR, BL):
            QTest.mouseClick(view.viewport(), Qt.MouseButton.LeftButton, pos=view.mapFromScene(QPointF(*point)))
        self.assertTrue(self.d.ok.isEnabled())
        for actual, expected in zip(self.d.points, (TL, TR, BR, BL)):
            self.assertLess(abs(actual[0]-expected[0])+abs(actual[1]-expected[1]), 4/view.scale_factor())
        start = view.mapFromScene(QPointF(*self.d.points[1]))
        end = view.mapFromScene(QPointF(530, 130))
        QTest.mousePress(view.viewport(), Qt.MouseButton.LeftButton, pos=start)
        QTest.mouseMove(view.viewport(), end)
        QTest.mouseRelease(view.viewport(), Qt.MouseButton.LeftButton, pos=end)
        self.assertLess(abs(self.d.points[1][0]-530)+abs(self.d.points[1][1]-130), 4/view.scale_factor())
        self.assertEqual(len(self.d.points), 4)
        self.assertEqual(self.d.selected, 1)


class RealPhoto(unittest.TestCase):
    def test_partial_page_photo_with_hidden_corner(self):
        path = Path(__file__).resolve().parents[1]/'pics'/'e7ea4e6b80775209e3f4d6a7113a31b6.jpg'
        if not path.is_file():
            self.skipTest('user photo absent')
        photo = QImage(str(path))
        d = RectifyDialog(None, photo, page_size=(185, 260))
        # Bottom-left corner is under a hand: estimated a little outside the frame.
        for point in [(130, 120), (1170, 150), (1120, 1700), (90, 1760)]:
            d.add_point(QPointF(*point)); d.finish_edit()
        self.assertTrue(d.ok.isEnabled())
        self.assertIn('照片外面', d.message.text())
        self.assertFalse(d.preview.pixmap().isNull())
        d.close()


class WindowIntegration(unittest.TestCase):
    def test_rectify_passes_existing_quad_and_page_size(self):
        w = ui.MainWindow()
        w.project = core.Project(width=120, height=160, blocks=[], image_data=core.image_to_data(image()), image_quad=[TL, TR, BR, BL])
        w.refresh()
        with patch.object(ui, 'RectifyDialog') as dialog:
            dialog.return_value.exec.return_value = QDialog.DialogCode.Rejected
            w.rectify()
        args = dialog.call_args.args
        self.assertEqual(args[2], [TL, TR, BR, BL])
        self.assertEqual(args[3], (120, 160))
        w.dirty = False; w.close(); app.processEvents()


if __name__ == '__main__':
    unittest.main()
