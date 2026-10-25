"""Parent integration of the real page click route with an inert controller."""
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from PySide6.QtCore import QPointF, Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication
from pagewriter.ui import MainWindow
from test_device_ui import FakeController

app = QApplication.instance() or QApplication([])


class PaperAnchorUiAcceptance(unittest.TestCase):
    def setUp(self):
        self.c = FakeController()
        self.c.state['session_id'] = 'parent-paper-session'
        with patch('pagewriter.controller.MachineController', return_value=self.c):
            self.w = MainWindow()
        self.panel = self.w.device_panel
        self.panel.timer.stop()
        self.panel.update_state()
        self.c.calls.clear()

    def tearDown(self):
        self.w.dirty = False
        self.w.close(); self.w.deleteLater(); app.processEvents()

    def arm_and_click(self, change_page=False):
        self.panel.preset_confirmed.setChecked(True)
        self.panel.preset_button.click()
        self.assertEqual('reference', self.w.canvas.mode)
        location = self.w.canvas.mapFromScene(QPointF(20,30))
        actual = self.w.canvas.mapToScene(location)
        if change_page:
            # Simulate a different page before the next 100ms state refresh.
            self.w.project.width += 1
        QTest.mouseClick(self.w.canvas.viewport(), Qt.MouseButton.LeftButton, pos=location)
        return actual

    def test_actual_reference_button_and_canvas_event_emit_one_anchor_only(self):
        expected = self.arm_and_click()
        self.assertEqual(1, len(self.c.calls))
        action, kw = self.c.calls[0]
        self.assertEqual('anchor_preset', action)
        self.assertEqual('songboshi_2024_paper', kw['preset'])
        self.assertEqual(self.panel.geometry_id(), kw['geometry_id'])
        self.assertAlmostEqual(expected.x(), kw['page_x'])
        self.assertAlmostEqual(expected.y(), kw['page_y'])
        self.assertIs(True, kw['axes_confirmed'])
        self.assertEqual('select', self.w.canvas.mode)
        self.assertFalse(self.panel.preset_confirmed.isChecked())
        self.assertIsNone(self.panel.reference_action)

    def test_page_change_between_button_and_point_invalidates_confirmation(self):
        self.arm_and_click(change_page=True)
        self.assertEqual([], self.c.calls)
        self.assertFalse(self.panel.preset_confirmed.isChecked())
        self.assertIsNone(self.panel.reference_action)

    def test_inverted_book_uses_same_canvas_point_and_page_geometry(self):
        self.w.project.width = 140
        self.w.project.height = 227
        self.w.refresh()
        self.panel.update_state()
        self.c.calls.clear()
        self.panel.preset_placement.setCurrentIndex(1)
        geometry = self.panel.geometry_id()
        expected = self.arm_and_click()
        self.assertEqual(1, len(self.c.calls))
        action, kw = self.c.calls[0]
        self.assertEqual('anchor_preset', action)
        self.assertEqual('songboshi_2024_paper_180', kw['preset'])
        self.assertAlmostEqual(expected.x(), kw['page_x'])
        self.assertAlmostEqual(expected.y(), kw['page_y'])
        self.assertEqual(geometry, kw['geometry_id'])
        self.assertEqual(geometry, self.panel.geometry_id())
        self.assertFalse(self.panel.preset_confirmed.isChecked())


if __name__ == '__main__':
    unittest.main(verbosity=2)
