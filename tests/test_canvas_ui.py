"""Native hidden Qt tests for canvas editing and reference dispatch."""
import unittest
from PySide6.QtCore import QPointF,Qt
from PySide6.QtGui import QTextCursor
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication
from pagewriter.ui import MainWindow
from pagewriter.inline_edit import lines_for,hit_position
app=QApplication.instance() or QApplication([])

class CanvasEditingTests(unittest.TestCase):
    def setUp(self):self.w=MainWindow()
    def tearDown(self):self.w.dirty=False;self.w.close();app.processEvents()
    def test_inline_model_hidden_and_native_typing(self):
        w=self.w;self.assertTrue(w.editor.isHidden())
        w.canvas.editing_id=w.selected_id
        before=w.editor.toPlainText()
        QTest.keyClicks(w.canvas,'abc')
        self.assertIn('abc',w.editor.toPlainText())
        self.assertNotEqual(before,w.editor.toPlainText())
        self.assertIn('abc',w.project.blocks[0].html)
    def test_every_column_hit_position_uses_actual_flow(self):
        w=self.w;w.editor.setPlainText('\n'.join('abcdefghij' for _ in range(18)))
        w.columns.setCurrentIndex(2);w.column_height.setValue(30)
        block=w.block_by_id();doc,lines=lines_for(block)
        seen=set()
        for pos,line,x,y,ox in lines:
            if x<block.width:
                self.assertTrue(w.text_items[0].shape().contains(QPointF(x+.1,y+line.height()/2*25.4/96)))
                hit=hit_position(block,QPointF(x+.1,y+line.height()/2*25.4/96))
                self.assertGreaterEqual(hit,pos+line.textStart())
                self.assertLessEqual(hit,pos+line.textStart()+line.textLength())
                seen.add(round(x,1))
        self.assertGreaterEqual(len(seen),3)
    def test_scene_inverse_rotation_and_zoom(self):
        w=self.w;w.angle.setValue(37);w.set_zoom(170)
        item=w.text_items[0];local=QPointF(3,2)
        actual=item.mapFromScene(item.mapToScene(local))
        self.assertAlmostEqual(actual.x(),local.x());self.assertAlmostEqual(actual.y(),local.y())
        w.canvas.editing_id=w.selected_id
        w.canvas.place_cursor(item,actual)
        self.assertEqual(w.editor.textCursor().position(),hit_position(w.block_by_id(),local))
    def test_photo_reference_dispatch_is_page_mm(self):
        calls=[];w=self.w;w.device_panel.send=lambda action,**kw:calls.append((action,kw))
        w.device_panel.pick('anchor');w.device_panel.reference_clicked(12.3,45.6)
        self.assertEqual(calls,[('anchor',{'page_x':12.3,'page_y':45.6,'axes_confirmed':True})])
    def test_opacity_zoom_preserve_calibration_geometry(self):
        calls=[];w=self.w;w.device_panel.send=lambda action,**kw:calls.append(action)
        w.opacity.setValue(35);w.set_zoom(80);w.refresh_scene()
        self.assertNotIn('geometry_changed',calls)
        w.page_w.setValue(149)
        self.assertIn('geometry_changed',calls)

    def test_ime_preedit_visible_but_not_exported_or_undoable(self):
        from PySide6.QtGui import QInputMethodEvent,QImage,QPainter,QTextCharFormat
        from pagewriter.inline_edit import preedit_layout
        from pagewriter.core import export_svg
        w=self.w;w.canvas.editing_id=w.selected_id
        before=w.block_by_id().html;undo=len(w.undo_stack);svg=export_svg(w.project)
        fmt=QTextCharFormat();fmt.setFontUnderline(True)
        event=QInputMethodEvent('zhong文',[QInputMethodEvent.Attribute(QInputMethodEvent.AttributeType.TextFormat,0,5,fmt)])
        app.sendEvent(w.canvas,event)
        self.assertEqual(w.canvas.preedit_text,'zhong文')
        self.assertEqual(w.block_by_id().html,before)
        self.assertEqual(len(w.undo_stack),undo)
        self.assertEqual(export_svg(w.project),svg)
        layout=preedit_layout(w.canvas,w.editor)
        self.assertGreater(layout.boundingRect().width(),0)
        image=QImage(800,800,QImage.Format.Format_ARGB32);image.fill(Qt.GlobalColor.white)
        painter=QPainter(image);w.canvas.scene().render(painter);painter.end()
        self.assertFalse(w.canvas.inputMethodQuery(Qt.InputMethodQuery.ImCursorRectangle).isEmpty())
        commit=QInputMethodEvent();commit.setCommitString('中文')
        app.sendEvent(w.canvas,commit)
        self.assertEqual(w.canvas.preedit_text,'')
        self.assertIn('中文',w.editor.toPlainText())
        self.assertGreater(len(w.undo_stack),undo)
        app.sendEvent(w.canvas,QInputMethodEvent('pin',[]))
        app.sendEvent(w.canvas,QInputMethodEvent('',[]))
        self.assertEqual(w.canvas.preedit_text,'')

    def test_soft_wrap_has_single_next_line_caret_geometry(self):
        from pagewriter.inline_edit import cursor_geometry
        w=self.w;w.editor.setPlainText('abcdefghijklmnopqrstuvwxyz '*20)
        w.block_w.setValue(30)
        doc,lines=lines_for(w.block_by_id())
        first,nextline=lines[0],lines[1]
        boundary=nextline[0]+nextline[1].textStart()
        rect=cursor_geometry(w.block_by_id(),boundary)
        self.assertAlmostEqual(rect.y(),nextline[3])
        w.canvas.editing_id=w.selected_id
        cursor=w.editor.textCursor();cursor.setPosition(boundary);w.editor.setTextCursor(cursor)
        item=w.text_items[0]
        expected=w.canvas.mapFromScene(item.mapRectToScene(rect)).boundingRect()
        self.assertEqual(w.canvas.inputMethodQuery(Qt.InputMethodQuery.ImCursorRectangle),expected)

    def test_loaded_single_line_font_display_is_read_only_sync(self):
        import tempfile
        from test_gfont import GFontTests
        from pagewriter.core import Project
        w=self.w
        with tempfile.TemporaryDirectory() as directory:
            helper=GFontTests();font=helper.font(directory)
            block=helper.block(font)
            w.project=Project(blocks=[block]);w.selected_id=block.id
            w.refresh()
            undo=len(w.undo_stack);html=block.html
            self.assertEqual(w.stroke_fonts.currentText(),font['name'])
            self.assertFalse(w.system_font_action.isVisible())
            self.assertFalse(w.font_box.isEnabled())
            self.assertIn('单线',w.mode_label.text())
            self.assertIn('单线',w.summary.text())
            w.cursor_changed()
            self.assertEqual(block.html,html)
            self.assertEqual(len(w.undo_stack),undo)
            w.apply_font(__import__('PySide6.QtGui',fromlist=['QFont']).QFont('Arial'))
            self.assertEqual(block.html,html)
            bars=w.findChildren(__import__('PySide6.QtWidgets',fromlist=['QToolBar']).QToolBar)
            styles=next(b for b in bars if b.windowTitle()=='文字样式')
            self.assertTrue(w.toolBarBreak(styles))

    def test_real_viewport_double_click_enters_editing_and_types(self):
        w=self.w
        w.resize(1500,980);w.canvas.resize(800,800)
        w.canvas.setSceneRect(-12,-12,w.project.width+24,w.project.height+24)
        w.canvas.fitInView(w.canvas.sceneRect(),Qt.AspectRatioMode.KeepAspectRatio)
        item=w.text_items[0]
        local=item.path().boundingRect().center()
        point=w.canvas.mapFromScene(item.mapToScene(local))
        self.assertIsNone(w.canvas.editing_id)
        QTest.mouseDClick(w.canvas.viewport(),Qt.MouseButton.LeftButton,pos=point)
        self.assertEqual(w.canvas.editing_id,item.block_id)
        self.assertIs(w.focusWidget(),w.canvas)
        before=w.editor.toPlainText()
        QTest.keyClicks(w.canvas,'input')
        self.assertNotEqual(w.editor.toPlainText(),before)
        self.assertIn('input',w.block_by_id().html)
        self.assertTrue(w.dirty)

    def test_native_click_sequence_enters_without_double_click_event(self):
        w=self.w;w.canvas.resize(800,800);w.canvas.fitInView(w.canvas.sceneRect(),Qt.AspectRatioMode.KeepAspectRatio)
        item=w.text_items[0];point=w.canvas.mapFromScene(item.mapToScene(item.path().boundingRect().center()))
        QTest.mouseClick(w.canvas.viewport(),Qt.MouseButton.LeftButton,pos=point)
        self.assertEqual(w.canvas.editing_id,item.block_id)
        self.assertIs(w.focusWidget(),w.canvas)
        QTest.mouseDClick(w.canvas.viewport(),Qt.MouseButton.LeftButton,pos=point)
        QTest.mouseRelease(w.canvas.viewport(),Qt.MouseButton.LeftButton,pos=point)
        QTest.keyClicks(w.canvas,'entry')
        self.assertIn('entry',w.block_by_id().html)
        w.canvas.set_mode('move')
        self.assertIsNone(w.canvas.editing_id)
        self.assertEqual(w.canvas.mode,'move')

    def test_actual_saved_sample_column_blank_space_native_click(self):
        from pathlib import Path
        from pagewriter.core import load_project
        w=self.w
        w.project=load_project(Path(__file__).parent/'fixtures/inline-columns.pagewriter')
        w.selected_id=w.project.blocks[0].id;w.refresh()
        w.canvas.resize(800,800);w.canvas.fitInView(w.canvas.sceneRect(),Qt.AspectRatioMode.KeepAspectRatio)
        item=next(item for item in w.text_items if abs(item.pos().x()-20)<.01 and abs(item.pos().y()-110)<.01)
        local=QPointF(3.5,3.6)
        self.assertTrue(item.shape().contains(local))
        point=w.canvas.mapFromScene(item.mapToScene(local))
        QTest.mouseClick(w.canvas.viewport(),Qt.MouseButton.LeftButton,pos=point)
        self.assertEqual(w.canvas.editing_id,item.block_id)
        self.assertIs(w.focusWidget(),w.canvas)
        self.assertIn('原位编辑中',w.statusBar().currentMessage())
        before=w.editor.toPlainText()
        QTest.keyClicks(w.canvas,'real')
        self.assertNotEqual(w.editor.toPlainText(),before)
        self.assertIn('real',w.block_by_id().html)

if __name__=='__main__':unittest.main()
