"""Parent-owned independent acceptance cases. No serial or device access."""
import hashlib
import json
import math
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from PySide6.QtCore import QPointF
from PySide6.QtGui import QFont, QImage, QPainterPath, QTextCursor
from PySide6.QtWidgets import QApplication
from pagewriter import core
from pagewriter.protocol import JobSession, JobState, MachineProfile, compile_machine_job
from pagewriter.ui import MainWindow

app=QApplication.instance() or QApplication([])

def shape_block(points, x=0, y=0, rotation=0):
    b=core.TextBlock(id='test-shape',html='<p>test</p>',x=x,y=y,rotation=rotation)
    p=QPainterPath(QPointF(*points[0]))
    for point in points[1:]:p.lineTo(QPointF(*point))
    b.frozen_key=hashlib.sha256((b.html+'\0'+str(float(b.width))).encode()).hexdigest()
    b.frozen_path=core.path_to_data(p)
    return b

class GeometryAcceptance(unittest.TestCase):
    def test_segment_crossing_zone_without_vertex_inside(self):
        p=core.Project(width=100,height=100,blocks=[shape_block([(10,50),(90,50)])],
            zones=[core.ExclusionZone(points=[(45,45),(55,45),(55,55),(45,55)])])
        self.assertTrue(core.preflight(p))
        with self.assertRaises(core.ExportBlocked):core.export_svg(p)

    def test_bounding_box_overlap_without_ink_is_allowed(self):
        p=core.Project(width=100,height=100,blocks=[shape_block([(10,10),(90,10),(90,90)])],
            zones=[core.ExclusionZone(points=[(30,30),(40,30),(40,40),(30,40)])])
        self.assertEqual([],core.preflight(p))

    def test_exclusion_inside_hollow_outline_is_allowed(self):
        p=core.Project(width=100,height=100,blocks=[shape_block([(10,10),(90,10),(90,90),(10,90),(10,10)])],
            zones=[core.ExclusionZone(points=[(40,40),(60,40),(60,60),(40,60)])])
        self.assertEqual([],core.preflight(p))

    def test_rotated_pen_width_outside_page_blocks(self):
        p=core.Project(width=100,height=100,blocks=[shape_block([(0,0),(15,0)],x=1,y=20,rotation=135)])
        self.assertTrue(any(x.zone_id=='page' for x in core.preflight(p)))

    def test_safe_margin_recomputed_after_edit(self):
        b=shape_block([(10,10),(90,10)])
        p=core.Project(width=100,height=100,margin=0,pen_width=.4,blocks=[b],
            zones=[core.ExclusionZone(points=[(30,10.4),(50,10.4),(50,12),(30,12)])])
        self.assertEqual([],core.preflight(p))
        p.margin=.3
        self.assertTrue(core.preflight(p))

    def test_portable_source_image_and_exact_curves(self):
        p=core.Project(width=150,height=200,blocks=[core.TextBlock(html='<p style="font-family:Arial;font-size:14pt">Hello <span style="font-size:22pt">大</span></p>')])
        p.image_data=core.image_to_data(QImage(str(Path(__file__).parent / 'fixtures' / 'printed-page.png')))
        p.image_quad=[[40,40],[1160,40],[1160,1560],[40,1560]]
        before=core.export_svg(p)
        with tempfile.TemporaryDirectory() as d:
            file=Path(d)/'mixed.pagewriter';core.save_project(p,file);loaded=core.load_project(file)
        self.assertEqual(p.image_data,loaded.image_data)
        self.assertEqual(p.image_quad,loaded.image_quad)
        self.assertEqual(before,core.export_svg(loaded))

    def test_nan_zone_is_rejected_before_qt_geometry(self):
        p=core.Project(zones=[core.ExclusionZone(points=[(0,0),(1,0),(1,1)])])
        data=core.project_to_dict(p)
        data['project']['zones'][0]['points'][0]=[float('nan'),0]
        with self.assertRaises(ValueError):core.project_from_dict(data)

    def test_non_executable_preview_has_no_active_gcode(self):
        text=core.offline_gcode(core.starter_project())
        self.assertTrue(all(line.startswith(';') for line in text.splitlines()))

class ProtocolAcceptance(unittest.TestCase):
    def test_old_query_reply_cannot_complete_current_job(self):
        s=JobSession(['G1 X1 F60']);s.send_next()
        old_request=s.poll_status()
        s.feed(b'ok\n')
        s.poll_status()
        if old_request is not None:
            s.feed(b'<Idle|MPos:0,0,15>\n')
            self.assertNotEqual(JobState.COMPLETE,s.state)
        s.poll_status();s.feed(b'<Idle|MPos:1,0,15>\n')
        self.assertEqual(JobState.COMPLETE,s.state)

    def test_uncalibrated_profile_rejects_machine_job(self):
        with self.assertRaises(ValueError):compile_machine_job([[(0,0),(1,1)]],MachineProfile(),preflight_passed=True)

    def test_alarm_never_succeeds_or_replays(self):
        s=JobSession(['G1 X1 F60','G1 X2 F60']);s.send_next()
        s.feed(b'ALARM:1\r\nok\r\n<Idle|MPos:0,0,15>\r\n')
        self.assertEqual(JobState.FAILED,s.state)
        self.assertIsNone(s.send_next())

class EditorAcceptance(unittest.TestCase):
    def setUp(self):
        self.w=MainWindow()
        self.w.project=core.Project(blocks=[core.TextBlock(id='mixed',html='<p style="font-family:Arial;font-size:12pt">ABCD</p>')])
        self.w.selected_id='mixed';self.w.refresh()

    def tearDown(self):
        self.w.dirty=False;self.w.replay_timer.stop();self.w.close();self.w.deleteLater();app.processEvents()

    def character_sizes(self):
        result=[]
        for i in range(4):
            c=QTextCursor(self.w.editor.document());c.setPosition(i);c.setPosition(i+1,QTextCursor.MoveMode.KeepAnchor)
            result.append(c.charFormat().fontPointSize())
        return result

    def test_selected_character_sizes_and_undo_redo(self):
        c=self.w.editor.textCursor();c.setPosition(1);c.setPosition(3,QTextCursor.MoveMode.KeepAnchor)
        self.w.editor.setTextCursor(c);self.w.font_size.setValue(24)
        self.assertEqual([12,24,24,12],self.character_sizes())
        self.w.undo();self.assertEqual([12,12,12,12],self.character_sizes())
        self.w.redo();self.assertEqual([12,24,24,12],self.character_sizes())

    def test_zoom_does_not_change_output(self):
        before=core.export_svg(self.w.project)
        for value in (30,100,220):self.w.zoom.setValue(value)
        self.assertEqual(before,core.export_svg(self.w.project))

    def test_geometry_control_refreshes_collisions(self):
        self.assertEqual([],self.w.collisions)
        self.w.pos_x.setValue(-30)
        self.assertTrue(self.w.collisions)
        self.w.undo();self.assertEqual([],self.w.collisions)

if __name__=='__main__':unittest.main(verbosity=2)
