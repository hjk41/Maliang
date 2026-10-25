"""Native Windows hidden-widget integration tests; no device connection."""
import copy
import tempfile
import unittest
from pathlib import Path

from PySide6.QtCore import QPointF, Qt
from PySide6.QtGui import QFont, QPolygonF, QTextCursor, QTransform
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from pagewriter.core import build_block_path, export_svg, save_project, load_project, layout_info, ExportBlocked
from pagewriter.ui import MainWindow
from pagewriter import ui

app = QApplication.instance() or QApplication([])


class UIIntegration(unittest.TestCase):
    def setUp(self):
        self.window = MainWindow()
        self.window.select_block(self.window.project.blocks[0].id)

    def tearDown(self):
        self.window.dirty = False
        self.window.close()
        app.processEvents()

    def select_chars(self,start,end):
        cursor = self.window.editor.textCursor()
        cursor.setPosition(start)
        cursor.setPosition(end,QTextCursor.MoveMode.KeepAnchor)
        self.window.editor.setTextCursor(cursor)
        self.window.cursor_changed()

    def point_size(self,index):
        cursor = self.window.editor.textCursor()
        cursor.setPosition(index)
        cursor.movePosition(QTextCursor.MoveOperation.NextCharacter,QTextCursor.MoveMode.KeepAnchor)
        return cursor.charFormat().fontPointSize()

    def test_partial_style_survives_control_focus_and_undo(self):
        w = self.window
        w.editor.setPlainText("甲乙丙丁")
        before = copy.deepcopy(w.project)
        self.select_chars(1,3)
        w.font_size.setFocus()
        w.font_size.setValue(24)
        app.processEvents()
        self.assertEqual(self.point_size(1),24)
        self.assertEqual(self.point_size(2),24)
        self.assertNotEqual(self.point_size(0),24)
        self.assertNotEqual(self.point_size(3),24)
        after = w.project.blocks[0].html
        w.undo()
        self.assertEqual(w.project.blocks[0].html,before.blocks[0].html)
        w.redo()
        self.assertEqual(w.project.blocks[0].html,after)

    def test_font_change_only_selection_save_reopen(self):
        w = self.window
        w.editor.setPlainText("ABCDE")
        self.select_chars(1,4)
        w.font_box.setFocus()
        w.apply_font(QFont("Arial"))
        cursor = w.editor.textCursor()
        self.assertTrue(cursor.hasSelection())
        self.assertEqual(cursor.selectedText(),"BCD")
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/"book.pagewriter"
            before = build_block_path(w.project.blocks[0])
            save_project(w.project,path)
            after = load_project(path)
            self.assertEqual(after.image_data,w.project.image_data)
            self.assertEqual(after.blocks[0].html,w.project.blocks[0].html)
            self.assertEqual(build_block_path(after.blocks[0]),before)

    def test_zoom_does_not_change_geometry_and_collision_rechecks(self):
        w = self.window
        before = export_svg(w.project)
        w.set_zoom(30)
        w.set_zoom(220)
        self.assertEqual(before,export_svg(w.project))
        w.pos_x.setValue(-20)
        self.assertTrue(w.collisions)
        self.assertIn("输出已阻止",w.summary.text())
        w.undo()
        self.assertFalse(w.collisions)

    def test_four_point_transform_maps_actual_page(self):
        w = self.window
        source = QPolygonF([QPointF(12,20),QPointF(910,5),QPointF(950,1300),QPointF(5,1290)])
        target = QPolygonF([QPointF(0,0),QPointF(w.project.width,0),QPointF(w.project.width,w.project.height),QPointF(0,w.project.height)])
        transform = QTransform()
        self.assertTrue(QTransform.quadToQuad(source,target,transform))
        for original,expected in zip(source,target):
            actual = transform.map(original)
            self.assertAlmostEqual(actual.x(),expected.x(),8)
            self.assertAlmostEqual(actual.y(),expected.y(),8)

    def test_column_controls_reflow_without_copying_and_undo(self):
        w = self.window
        before_html = w.project.blocks[0].html
        w.columns.setCurrentIndex(1)
        block = w.project.blocks[0]
        self.assertEqual(block.columns,2)
        self.assertEqual(block.html,before_html)
        self.assertEqual(block.column_height,60)
        self.assertIn("50.00 mm",w.column_width_label.text())
        self.assertEqual(len([c for c in w.text_items[0].childItems() if not isinstance(c,ui.ResizeHandle)]),1)
        w.undo()
        self.assertEqual(w.project.blocks[0].columns,1)
        self.assertEqual(w.project.blocks[0].column_height,0)
        w.redo()
        self.assertEqual(w.project.blocks[0].columns,2)

    def test_columns_selection_style_overflow_and_portable_reopen(self):
        w = self.window
        w.editor.setPlainText("甲乙丙丁戊己庚辛壬癸")
        w.columns.setCurrentIndex(2)
        self.select_chars(1,3)
        w.font_size.setFocus()
        w.font_size.setValue(24)
        self.assertEqual(self.point_size(1),24)
        self.assertNotEqual(self.point_size(0),24)
        self.assertEqual(w.project.blocks[0].columns,3)
        w.column_height.setValue(.5)
        self.assertTrue(layout_info(w.project.blocks[0])["overflow"])
        self.assertIn("文字溢出",w.column_status.text())
        self.assertIn("输出已阻止",w.summary.text())
        with self.assertRaises(ExportBlocked):export_svg(w.project)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/"columns.pagewriter"
            save_project(w.project,path)
            loaded = load_project(path)
            self.assertEqual(loaded.blocks[0].columns,3)
            self.assertEqual(loaded.blocks[0].column_height,.5)
            self.assertTrue(layout_info(loaded.blocks[0])["overflow"])
        w.undo()
        self.assertEqual(w.project.blocks[0].column_height,60)
        self.assertNotIn("文字溢出",w.column_status.text())

    def test_column_guide_does_not_become_exported_ink(self):
        w = self.window
        w.editor.setPlainText("甲\n乙\n丙\n丁\n戊\n己\n庚\n辛\n壬\n癸")
        w.columns.setCurrentIndex(1)
        w.column_height.setValue(30)
        before = export_svg(w.project)
        w.column_gap.setValue(8)
        self.assertIn("49.00 mm",w.column_width_label.text())
        self.assertTrue(before != export_svg(w.project))
        self.assertEqual(export_svg(w.project).count("<path "),len(w.project.blocks))
        self.assertNotIn("<rect",export_svg(w.project))

    def test_invalid_column_control_geometry_is_rejected_cleanly(self):
        w = self.window
        errors = []
        w.error = errors.append
        w.columns.setCurrentIndex(2)
        w.column_gap.setValue(60)
        self.assertTrue(errors)
        self.assertEqual(w.project.blocks[0].column_gap,6)
        self.assertEqual(w.column_gap.value(),6)
        w.columns.setCurrentIndex(0)
        w.column_gap.setValue(60)
        before = len(errors)
        w.columns.setCurrentIndex(2)
        self.assertGreater(len(errors),before)
        self.assertEqual(w.project.blocks[0].columns,1)
        self.assertEqual(w.columns.currentIndex(),0)

    def test_underline_button_preserves_other_selected_styles(self):
        w = self.window
        w.editor.setHtml('<p><span style="font-family:Arial;font-size:18pt;font-weight:700;">ABCD</span></p>')
        self.select_chars(1,3)
        original = w.editor.textCursor().charFormat()
        w.underline_button.click()
        self.assertTrue(w.editor.textCursor().hasSelection())
        self.assertTrue(w.editor.textCursor().charFormat().fontUnderline())
        self.assertEqual(w.editor.textCursor().charFormat().fontPointSize(),original.fontPointSize())
        self.assertEqual(w.editor.textCursor().charFormat().fontWeight(),original.fontWeight())
        self.assertEqual(w.editor.textCursor().charFormat().fontFamilies(),original.fontFamilies())
        self.select_chars(0,1)
        self.assertFalse(w.editor.textCursor().charFormat().fontUnderline())
        self.select_chars(1,3)
        w.underline_button.click()
        self.assertFalse(w.editor.textCursor().charFormat().fontUnderline())

    def test_ctrl_u_without_selection_only_affects_future_typing(self):
        w = self.window
        w.editor.setPlainText("旧文字")
        cursor = w.editor.textCursor()
        cursor.movePosition(QTextCursor.MoveOperation.End)
        w.editor.setTextCursor(cursor)
        w.cursor_changed()
        QTest.keyClick(w.editor,Qt.Key.Key_U,Qt.KeyboardModifier.ControlModifier)
        w.editor.insertPlainText("新")
        self.select_chars(0,1)
        self.assertFalse(w.editor.textCursor().charFormat().fontUnderline())
        self.select_chars(3,4)
        self.assertTrue(w.editor.textCursor().charFormat().fontUnderline())
        html = w.project.blocks[0].html
        w.undo()
        w.redo()
        self.assertEqual(w.project.blocks[0].html,html)

    def test_separator_toggle_preserves_frozen_text_geometry(self):
        w = self.window
        w.columns.setCurrentIndex(1)
        block = w.project.blocks[0]
        before = copy.deepcopy(block.frozen_path)
        key = block.frozen_key
        original = build_block_path(block)
        w.column_separators.setChecked(True)
        self.assertTrue(block.column_separators)
        self.assertEqual(block.frozen_path,before)
        self.assertEqual(block.frozen_key,key)
        self.assertTrue(build_block_path(block).elementCount() > original.elementCount())
        w.undo()
        self.assertFalse(w.project.blocks[0].column_separators)
        self.assertEqual(build_block_path(w.project.blocks[0]),original)

    def test_open_project_resets_old_insertion_underline_and_font(self):
        w = self.window
        w.editor.setHtml('<p style="font-family:Arial;font-size:24pt;"><u>旧格式</u></p>')
        cursor = w.editor.textCursor()
        cursor.setPosition(1)
        w.editor.setTextCursor(cursor)
        w.cursor_changed()
        self.assertTrue(w.underline_button.isChecked())
        with tempfile.TemporaryDirectory() as directory:
            from pagewriter.core import Project, TextBlock
            path = Path(directory)/"new.pagewriter"
            project = Project(blocks=[TextBlock(html='<p style="font-family:Arial;font-size:12pt;">新项目</p>')])
            save_project(project,path)
            w.open_path(path)
            self.assertFalse(w.underline_button.isChecked())
            self.assertFalse(w.editor.currentCharFormat().fontUnderline())
            self.assertEqual(w.editor.currentCharFormat().fontPointSize(),12)
            self.assertFalse(w.editor.textCursor().hasSelection())
            w.editor.insertPlainText("添")
            self.select_chars(0,1)
            self.assertFalse(w.editor.textCursor().charFormat().fontUnderline())
            self.assertEqual(w.editor.textCursor().charFormat().fontPointSize(),12)


if __name__ == "__main__":
    unittest.main()
