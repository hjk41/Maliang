"""Parent-owned independent acceptance; native hidden Qt, no device access."""
import collections
import copy
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from PySide6.QtWidgets import QApplication
from PySide6.QtGui import QTextCursor
from pagewriter import core
from pagewriter.ui import MainWindow

APP = QApplication.instance() or QApplication([])
QA = Path(__file__).parent

def letters(columns=3, height=11, gap=4, html=None):
    return core.TextBlock(id='flow', name='分栏验收', x=10, y=10, width=60,
        html=html or '<p style="font-family:Arial;font-size:12pt;margin:0">A<br/>B<br/>C<br/>D<br/>E<br/>F</p>',
        columns=columns, column_height=height, column_gap=gap)

def outlines(path):
    groups=[]
    for kind,x,y in core.path_to_data(path):
        if kind==0: groups.append([])
        groups[-1].append((kind,x,y))
    return groups

def signatures(path):
    # Translation-invariant true curve signatures, in original path order.
    return [tuple((k,x-p[0][1],y-p[0][2]) for k,x,y in p)
            for p in outlines(path) if len(p)>1]

def assert_same_glyph_shapes(test,first,second):
    a,b=signatures(first),signatures(second)
    test.assertEqual(len(a),len(b))
    for shape_a,shape_b in zip(a,b):
        test.assertEqual(len(shape_a),len(shape_b))
        for (ka,xa,ya),(kb,xb,yb) in zip(shape_a,shape_b):
            test.assertEqual(ka,kb)
            test.assertAlmostEqual(xa,xb,delta=1e-7)
            test.assertAlmostEqual(ya,yb,delta=1e-7)

def populated_columns(block):
    width=(block.width-(block.columns-1)*block.column_gap)/block.columns
    step=width+block.column_gap
    return {int(((min(p[1] for p in outline)+max(p[1] for p in outline))/2)//step)
            for outline in outlines(core.local_block_path(block)) if len(outline)>1}

class ColumnAcceptance(unittest.TestCase):
    def test_two_three_columns_preserve_every_glyph_and_reading_order(self):
        baseline=core.local_block_path(letters(1,0))
        for count,height in [(2,15.5),(3,11)]:
            with self.subTest(columns=count):
                block=letters(count,height)
                actual=core.local_block_path(block)
                assert_same_glyph_shapes(self,baseline,actual)
                self.assertEqual(set(range(count)),populated_columns(block))
                self.assertEqual([],core.preflight(core.Project(blocks=[block])))

    def test_mixed_font_sizes_survive_column_flow_without_loss(self):
        text='<p style="font-family:Arial;font-size:12pt;margin:0">A<br/><span style="font-family:KaiTi;font-size:24pt">乙</span><br/>B<br/><span style="font-family:KaiTi;font-size:24pt">丙</span><br/>C<br/><span style="font-family:KaiTi;font-size:24pt">丁</span></p>'
        flat=letters(1,0,html=text)
        columns=letters(3,19,html=text)
        assert_same_glyph_shapes(self,core.local_block_path(flat),core.local_block_path(columns))
        self.assertEqual({0,1,2},populated_columns(columns))
        self.assertEqual([],core.preflight(core.Project(blocks=[columns])))

    def test_last_column_overflow_preserves_all_glyphs_and_blocks_export(self):
        block=letters(2,5)
        p=core.Project(blocks=[block])
        assert_same_glyph_shapes(self,core.local_block_path(letters(1,0)),core.local_block_path(block))
        self.assertTrue(core.preflight(p))
        with self.assertRaises(core.ExportBlocked):core.export_svg(p)
        with self.assertRaises(core.ExportBlocked):core.offline_gcode(p)

    def test_blank_lines_consume_space_before_later_text(self):
        block=letters(2,10,html='<p style="font-family:Arial;font-size:12pt;margin:0">A<br/><br/><br/><br/><br/><br/>Z</p>')
        self.assertTrue(core.preflight(core.Project(blocks=[block])))

    def test_single_line_taller_than_column_cannot_be_exported(self):
        block=letters(3,3,html='<p style="font-family:Arial;font-size:36pt;margin:0">A</p>')
        self.assertFalse(core.local_block_path(block).isEmpty())
        with self.assertRaises(core.ExportBlocked):core.export_svg(core.Project(blocks=[block]))

    def test_narrow_columns_cannot_silently_bleed_into_gutter(self):
        block=letters(3,40,html='<p style="font-family:Arial;font-size:24pt;margin:0">W</p>')
        block.width=12
        with self.assertRaises(core.ExportBlocked):core.export_svg(core.Project(blocks=[block]))

    def test_third_column_ink_hits_zone_but_column_gap_is_empty(self):
        block=letters();block.avoid_zones=False  # safety check without flow-around
        width=(block.width-2*block.column_gap)/3
        x=block.x+2*(width+block.column_gap)
        zone=core.ExclusionZone(id='third',points=[(x,10),(x+width,10),(x+width,21),(x,21)])
        self.assertIn('third',{c.zone_id for c in core.preflight(core.Project(blocks=[block],zones=[zone]))})
        x=block.x+width+.8
        gap_zone=core.ExclusionZone(id='gutter',points=[(x,10),(x+2,10),(x+2,21),(x,21)])
        self.assertEqual([],core.preflight(core.Project(blocks=[block],zones=[gap_zone])))

    def test_editing_height_reflows_cached_text_and_clears_overflow(self):
        block=letters(2,5)
        p=core.Project(blocks=[block])
        bad_path=core.local_block_path(block)
        self.assertTrue(core.preflight(p))
        block.column_height=15.5
        self.assertNotEqual(bad_path,core.local_block_path(block))
        self.assertEqual([],core.preflight(p))
        block.column_height=5
        self.assertTrue(core.preflight(p))

    def test_gap_change_rebuilds_geometry_without_losing_content(self):
        block=letters()
        before=core.local_block_path(block)
        block.column_gap=8
        after=core.local_block_path(block)
        self.assertNotEqual(before,after)
        assert_same_glyph_shapes(self,before,after)

    def test_column_count_change_reflows_cached_text(self):
        block=letters(2,11)
        p=core.Project(blocks=[block])
        self.assertTrue(core.preflight(p))
        block.columns=3
        self.assertEqual([],core.preflight(p))
        self.assertEqual({0,1,2},populated_columns(block))

    def test_save_reopen_preserves_curves_and_overflow_without_fonts(self):
        for height in (5,11):
            with self.subTest(height=height),tempfile.TemporaryDirectory() as folder:
                block=letters(3,height)
                p=core.Project(blocks=[block])
                path=core.local_block_path(block)
                before=[c.message for c in core.preflight(p)]
                file=Path(folder)/'columns.pagewriter'
                core.save_project(p,file)
                loaded=core.load_project(file)
                self.assertEqual(3,loaded.blocks[0].columns)
                self.assertEqual(height,loaded.blocks[0].column_height)
                self.assertEqual(4,loaded.blocks[0].column_gap)
                with patch.object(core,'make_document',side_effect=AssertionError('Frozen paths must survive absent fonts')):
                    self.assertEqual(path,core.local_block_path(loaded.blocks[0]))
                    self.assertEqual(before,[c.message for c in core.preflight(loaded)])

    def test_old_v1_project_preserves_original_frozen_geometry(self):
        raw=json.loads((QA/'fixtures'/'v1-input.pagewriter').read_text(encoding='utf-8'))
        loaded=core.project_from_dict(copy.deepcopy(raw))
        for original,block in zip(raw['project']['blocks'],loaded.blocks):
            self.assertEqual(1,block.columns)
            self.assertEqual(core.path_from_data(original['frozen_path']),core.local_block_path(block))

    def test_bad_column_parameters_rejected_before_layout(self):
        base=core.project_to_dict(core.Project(blocks=[letters()]))
        cases=[('columns',0),('columns',4),('columns',True),('columns',2.5),
               ('column_gap',-1),('column_gap',float('nan')),('column_gap',31),
               ('column_height',-1),('column_height',float('nan'))]
        for key,value in cases:
            with self.subTest(key=key,value=value):
                bad=copy.deepcopy(base);bad['project']['blocks'][0][key]=value
                with self.assertRaises(ValueError):core.project_from_dict(bad)

class ColumnUIAcceptance(unittest.TestCase):
    def setUp(self):
        self.w=MainWindow()
        self.w.project=core.Project(blocks=[letters(1,0)])
        self.w.selected_id='flow'
        self.w.refresh()

    def tearDown(self):
        self.w.dirty=False
        self.w.close()
        APP.processEvents()

    def test_column_controls_reflow_and_undo_redo_preserve_html(self):
        w=self.w
        html=w.project.blocks[0].html
        w.columns.setCurrentIndex(2)
        w.column_height.setValue(11)
        w.column_gap.setValue(4)
        APP.processEvents()
        block=w.project.blocks[0]
        self.assertEqual((3,11,4),(block.columns,block.column_height,block.column_gap))
        self.assertEqual(html,block.html)
        self.assertEqual({0,1,2},populated_columns(block))
        before=core.local_block_path(block)
        w.column_gap.setValue(8)
        after=core.local_block_path(w.project.blocks[0])
        self.assertNotEqual(before,after)
        w.undo()
        self.assertEqual(before,core.local_block_path(w.project.blocks[0]))
        self.assertEqual(4,w.column_gap.value())
        w.redo()
        self.assertEqual(after,core.local_block_path(w.project.blocks[0]))

    def test_column_height_overflow_rechecks_then_clears(self):
        w=self.w
        w.columns.setCurrentIndex(2)
        w.column_height.setValue(5)
        self.assertTrue(w.collisions)
        self.assertIn('溢出',w.column_status.text())
        w.column_height.setValue(11)
        self.assertFalse(w.collisions)

    def test_style_selection_survives_column_control_focus(self):
        w=self.w
        w.editor.setPlainText('甲乙丙丁')
        cursor=w.editor.textCursor();cursor.setPosition(1)
        cursor.setPosition(3,QTextCursor.MoveMode.KeepAnchor)
        w.editor.setTextCursor(cursor)
        w.cursor_changed()
        w.font_size.setValue(18)
        before=w.project.blocks[0].html
        w.columns.setCurrentIndex(1)
        self.assertEqual(before,w.project.blocks[0].html)
        self.assertIn('18pt',w.project.blocks[0].html)

    def test_bad_geometry_controls_do_not_escape_event_handlers(self):
        w=self.w
        w.columns.setCurrentIndex(2)
        w.column_height.setValue(11)
        errors=[]
        original=sys.excepthook
        sys.excepthook=lambda *args:errors.append(args)
        try:
            with patch.object(w,'error') as notice:
                w.block_w.setValue(5)
                w.column_gap.setValue(20)
                APP.processEvents()
                self.assertTrue(notice.called)
        finally:sys.excepthook=original
        self.assertEqual([],errors,'Qt callbacks must handle temporarily invalid frame dimensions')
        w.block_w.setValue(60)
        w.column_gap.setValue(4)
        self.assertFalse(w.collisions)

if __name__=='__main__':unittest.main(verbosity=2)
