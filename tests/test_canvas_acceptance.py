"""Parent acceptance of page-native text input and initial machine travel."""
from pathlib import Path
import sys
import unittest
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from PySide6.QtCore import QPointF, Qt
from PySide6.QtGui import QInputMethodEvent, QTextCursor
from PySide6.QtWidgets import QApplication
from pagewriter import core
from pagewriter.ui import MainWindow
from pagewriter.inline_edit import lines_for, hit_position
app=QApplication.instance() or QApplication([])

class CanvasAcceptance(unittest.TestCase):
    def setUp(self):
        self.w=MainWindow()
        self.w.project=core.Project(width=140,height=180,blocks=[core.TextBlock(
            id='inline-parent',html='<p style="font-family:Arial;font-size:12pt">'+
            '<br>'.join(['abcde']*9)+'</p>',width=108,x=10,y=20,
            columns=3,column_height=16,column_gap=5)])
        self.w.selected_id='inline-parent';self.w.refresh()
    def tearDown(self):
        self.w.dirty=False;self.w.close();self.w.deleteLater();app.processEvents()

    def test_third_column_click_then_unicode_commit_keeps_full_text(self):
        w=self.w;block=w.block_by_id();doc,lines=lines_for(block)
        target=next(row for row in lines if row[2]>70)
        pos,line,x,y,ox=target
        hit=hit_position(block,QPointF(x+.05,y+.5))
        before=w.editor.toPlainText()
        w.canvas.editing_id=block.id
        w.canvas.place_cursor(w.text_items[0],QPointF(x+.05,y+.5))
        event=QInputMethodEvent();event.setCommitString('页')
        QApplication.sendEvent(w.canvas,event)
        self.assertEqual(before[:hit]+'页'+before[hit:],w.editor.toPlainText())
        self.assertEqual(3,w.block_by_id().columns)
        self.assertTrue(w.editor.isHidden())

    def test_selected_size_changes_only_selection_from_canvas(self):
        w=self.w;w.canvas.editing_id=w.selected_id
        cursor=w.editor.textCursor();cursor.setPosition(1);cursor.setPosition(3,QTextCursor.MoveMode.KeepAnchor)
        w.editor.setTextCursor(cursor);w.apply_size(22)
        values=[]
        for i in range(5):
            c=QTextCursor(w.editor.document());c.setPosition(i);c.setPosition(i+1,QTextCursor.MoveMode.KeepAnchor)
            values.append(c.charFormat().fontPointSize())
        self.assertEqual([12,22,22,12,12],values)
        self.assertEqual(3,w.block_by_id().columns)

    def test_first_travel_obstacle_checks_current_machine_position(self):
        w=self.w;w.project.zones=[core.ExclusionZone(kind='obstacle',points=[(4,-1),(6,-1),(6,1),(4,1)])]
        state={'wpos':(0,0,0),'page_to_machine':(1,0,0,1,0,0)}
        with patch.object(w.device_panel.controller,'snapshot',return_value=state):
            self.assertFalse(w.device_panel.initial_travel_clear([[(10,0),(11,0)]]))
            self.assertTrue(w.device_panel.initial_travel_clear([[(0,10),(0,11)]]))

    def test_first_travel_inverse_swapped_axes(self):
        w=self.w;w.project.zones=[core.ExclusionZone(kind='obstacle',points=[(4,-1),(6,-1),(6,1),(4,1)])]
        state={'wpos':(20,30,0),'page_to_machine':(0,-1,1,0,20,30)}
        with patch.object(w.device_panel.controller,'snapshot',return_value=state):
            self.assertFalse(w.device_panel.initial_travel_clear([[(10,0),(11,0)]]))

    def test_unknown_live_position_cannot_start_even_with_page_paths(self):
        with patch.object(self.w.device_panel.controller,'snapshot',return_value={'wpos':None}):
            self.assertFalse(self.w.device_panel.initial_travel_clear([[(10,10),(11,10)]]))

    def test_ink_and_column_hit_rectangles_do_not_cancel_each_other(self):
        item=self.w.text_items[0]
        # Both the ink rectangle and column rectangle overlap here. OddEvenFill
        # used to subtract this region, so real mouse clicks missed all text.
        point=QPointF(2,2)
        self.assertTrue(item.boundingRect().contains(point))
        self.assertTrue(item.shape().contains(point))
        self.assertTrue(item.contains(point))

if __name__=='__main__':unittest.main(verbosity=2)
