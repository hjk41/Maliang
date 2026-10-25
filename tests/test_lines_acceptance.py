"""Independent parent acceptance for underline and separator output; no device I/O."""
import copy
import hashlib
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from PySide6.QtGui import QTextCursor
from PySide6.QtWidgets import QApplication, QPushButton, QCheckBox
from pagewriter import core
from pagewriter.ui import MainWindow

APP = QApplication.instance() or QApplication([])


def block(text='ABCD', **kwargs):
    return core.TextBlock(id='lines', name='线条验收', x=20, y=20, width=100,
        html=f'<p style="font-family:Arial;font-size:16pt;margin:0">{text}</p>', **kwargs)


def pieces(b):
    out=[]
    for kind,x,y in core.path_to_data(core.build_block_path(b)):
        if kind==0: out.append([])
        out[-1].append((kind,x,y))
    return [p for p in out if len(p)>1]


def straight(b):
    return [p for p in pieces(b) if len(p)==2 and p[1][0]==1]


def horizontal(b):
    return [p for p in straight(b) if abs(p[0][2]-p[1][2])<1e-8]


def vertical(b):
    return [p for p in straight(b) if abs(p[0][1]-p[1][1])<1e-8]


def range_x(b,start,end):
    doc=core.make_document(b)
    p=doc.begin(); line=p.layout().lineAt(0)
    offset=doc.documentLayout().blockBoundingRect(p).x()
    return sorted([b.x+(offset+line.cursorToX(pos)[0])*core.MM_PER_PX for pos in (start,end)])


def fmt_at(editor,index):
    c=QTextCursor(editor.document());c.setPosition(index)
    c.setPosition(index+1,QTextCursor.MoveMode.KeepAnchor)
    return c.charFormat()


class LinePathAcceptance(unittest.TestCase):
    def test_partial_underline_is_single_actual_path_and_preserves_all_glyphs(self):
        plain=block('ABCDEF');under=block('AB<u>CD</u>EF')
        self.assertEqual(pieces(plain),[p for p in pieces(under) if p not in straight(under)])
        lines=horizontal(under);self.assertEqual(1,len(lines))
        self.assertEqual([],vertical(under))
        expected=range_x(under,2,4)
        for actual,want in zip(sorted(p[1] for p in lines[0]),expected):
            self.assertAlmostEqual(actual,want,delta=.02)
        self.assertEqual([],core.preflight(core.Project(blocks=[under])))

    def test_underline_utf16_range_after_non_bmp_character(self):
        b=block('😀<u>中</u>甲')
        lines=horizontal(b);self.assertEqual(1,len(lines))
        for actual,want in zip(sorted(p[1] for p in lines[0]),range_x(b,2,3)):
            self.assertAlmostEqual(actual,want,delta=.02)

    def test_underline_splits_at_hard_breaks_and_column_boundaries(self):
        b=block('<u>A<br/>B<br/>C<br/>D<br/>E<br/>F</u>',columns=3,column_height=15,column_gap=8)
        b.width=80
        lines=horizontal(b)
        self.assertEqual(6,len(lines))
        cw=(80-16)/3;step=cw+8
        self.assertEqual({0,1,2},{round((min(p[1] for p in line)-b.x)/step) for line in lines})
        for line in lines:
            self.assertLess(abs(line[1][1]-line[0][1]),cw)
            self.assertGreaterEqual(line[0][2],b.y)
            self.assertLessEqual(line[0][2],b.y+b.column_height)

    def test_underline_soft_wrap_never_crosses_gutter(self):
        b=block('<u>'+'甲乙丙丁'*12+'</u>',columns=3,column_height=36,column_gap=8)
        b.width=80
        lines=horizontal(b);info=core.layout_info(b)
        self.assertEqual(info['line_count'],len(lines))
        self.assertGreater(len(lines),3)
        cw=(80-16)/3;step=cw+8
        for line in lines:
            left,right=sorted(p[1] for p in line)
            col=round((left-b.x)/step)
            self.assertGreaterEqual(left,b.x+col*step-.02)
            self.assertLessEqual(right,b.x+col*step+cw+.02)

    def test_underline_only_collision_blocks_both_exports(self):
        # Empty spaces have no glyph outline; underline itself must trigger collision.
        b=block('<span style="white-space:pre;text-decoration:underline">&#160;&#160;&#160;&#160;</span>',avoid_zones=False)
        line=horizontal(b)[0];x=(line[0][1]+line[1][1])/2;y=line[0][2]
        z=core.ExclusionZone(id='line-only',points=[(x-.1,y-.1),(x+.1,y-.1),(x+.1,y+.1),(x-.1,y+.1)])
        p=core.Project(blocks=[b],zones=[z],pen_width=.2,margin=0)
        self.assertIn('line-only',{c.zone_id for c in core.preflight(p)})
        with self.assertRaises(core.ExportBlocked):core.export_svg(p)
        with self.assertRaises(core.ExportBlocked):core.offline_gcode(p)

    def test_empty_columns_have_exact_centerline_separators(self):
        for count in (1,2,3):
            with self.subTest(columns=count):
                b=block('',columns=count,column_height=40,column_gap=6,column_separators=True)
                lines=vertical(b);self.assertEqual(count-1,len(lines))
                cw=(100-(count-1)*6)/count
                for i,line in enumerate(lines):
                    self.assertAlmostEqual(line[0][1],20+(i+1)*cw+i*6+3)
                    self.assertEqual([20,60],sorted(p[2] for p in line))
                self.assertEqual(count-1,len(core.all_subpaths(core.Project(blocks=[b]))))

    def test_separator_uses_frozen_letters_without_font_regeneration(self):
        b=block(columns=3,column_height=40,column_gap=6)
        original=pieces(b)
        with patch.object(core,'make_document',side_effect=AssertionError('Opening separator must not regenerate fonts')):
            b.column_separators=True
            self.assertEqual(2,len(vertical(b)))
            self.assertEqual(original,[p for p in pieces(b) if p not in vertical(b)])
            b.column_separators=False
            self.assertEqual(original,pieces(b))

    def test_separator_collision_and_pen_radius_are_not_just_guides(self):
        b=block('',columns=2,column_height=40,column_gap=6,column_separators=True,avoid_zones=False)
        z=core.ExclusionZone(id='divider',points=[(69.9,38),(70.1,38),(70.1,42),(69.9,42)])
        p=core.Project(blocks=[b],zones=[z],pen_width=.4,margin=0)
        with self.assertRaises(core.ExportBlocked):core.export_svg(p)
        b.column_separators=False
        self.assertEqual([],core.preflight(p))
        b.column_separators=True;b.y=0
        self.assertIn('page',{c.zone_id for c in core.preflight(p)})

    def test_rotated_separator_joins_same_transform_and_output(self):
        b=block('',columns=3,column_height=30,column_gap=6,column_separators=True)
        original=core.build_block_path(b)
        b.rotation=90;b.x=70;b.y=40
        transformed=core.build_block_path(b)
        self.assertEqual(2,len(horizontal(b)))
        # Map each point from old page coords to the new frame; not bbox equivalence.
        for old,new in zip(core.path_to_data(original),core.path_to_data(transformed)):
            self.assertEqual(old[0],new[0])
            self.assertAlmostEqual(new[1],70-(old[2]-20))
            self.assertAlmostEqual(new[2],40+(old[1]-20))
        p=core.Project(blocks=[b])
        self.assertIn(core.svg_path_data(transformed),core.export_svg(p))

    def test_both_features_save_reopen_and_remain_without_fonts(self):
        b=block('<u>甲乙</u><br/>AB',columns=3,column_height=30,column_gap=6,column_separators=True)
        p=core.Project(blocks=[b]);expected=core.build_block_path(b);svg=core.export_svg(p)
        with tempfile.TemporaryDirectory() as d:
            f=Path(d)/'lines.pagewriter';core.save_project(p,f)
            with patch.object(core,'make_document',side_effect=AssertionError('Frozen project needs no fonts')):
                reopened=core.load_project(f)
                self.assertTrue(reopened.blocks[0].column_separators)
                self.assertEqual(expected,core.build_block_path(reopened.blocks[0]))
                self.assertEqual(svg,core.export_svg(reopened))

    def test_old_underlined_html_cache_does_not_silently_omit_line(self):
        b=block('ABC');core.local_block_path(b)
        b.html=b.html.replace('ABC','<u>ABC</u>')
        # Simulate pre-underline-engine v0.2 cache: HTML key matched, glyphs only.
        b.frozen_key=hashlib.sha256((b.html+'\0'+str(float(b.width))).encode()).hexdigest()
        self.assertEqual(1,len(horizontal(b)))

    def test_separator_flag_rejects_non_boolean_import(self):
        payload=core.project_to_dict(core.Project(blocks=[block()]))
        for value in ('false',1,None,[],{}):
            with self.subTest(value=value):
                bad=copy.deepcopy(payload);bad['project']['blocks'][0]['column_separators']=value
                with self.assertRaises(ValueError):core.project_from_dict(bad)


class LineUIAcceptance(unittest.TestCase):
    def setUp(self):
        self.w=MainWindow();self.w.project=core.Project(blocks=[block('AB<span style="font-size:24pt;font-weight:700">CD</span>EF')])
        self.w.selected_id='lines';self.w.refresh()

    def tearDown(self):
        self.w.dirty=False;self.w.close();APP.processEvents()

    def underline_button(self):
        return next(b for b in self.w.findChildren(QPushButton) if '下划线' in b.text())

    def separator_checkbox(self):
        return next(b for b in self.w.findChildren(QCheckBox) if '分割线' in b.text())

    def select(self,start,end):
        c=self.w.editor.textCursor();c.setPosition(start);c.setPosition(end,QTextCursor.MoveMode.KeepAnchor)
        self.w.editor.setTextCursor(c);self.w.cursor_changed()

    def test_partial_underline_button_retains_mixed_sizes_weight_and_undo(self):
        w=self.w;before=w.project.blocks[0].html
        self.select(1,5);self.underline_button().click()
        for i in range(6):self.assertEqual(1<=i<5,fmt_at(w.editor,i).fontUnderline())
        for i in (2,3):
            self.assertEqual(24,fmt_at(w.editor,i).fontPointSize())
            self.assertGreaterEqual(fmt_at(w.editor,i).fontWeight(),700)
        self.assertTrue(horizontal(w.project.blocks[0]))
        under=core.build_block_path(w.project.blocks[0]);w.undo()
        self.assertEqual(before,w.project.blocks[0].html)
        w.redo();self.assertEqual(under,core.build_block_path(w.project.blocks[0]))
        self.select(1,5);self.underline_button().click()
        self.assertFalse(horizontal(w.project.blocks[0]))

    def test_no_selection_changes_future_input_only(self):
        w=self.w;self.select(6,6)
        before=core.build_block_path(w.project.blocks[0])
        self.underline_button().click()
        self.assertEqual(before,core.build_block_path(w.project.blocks[0]))
        w.editor.insertPlainText('新')
        self.assertTrue(fmt_at(w.editor,6).fontUnderline())
        self.assertFalse(fmt_at(w.editor,5).fontUnderline())
        self.assertEqual(1,len(horizontal(w.project.blocks[0])))

    def test_separator_toggle_and_undo_no_text_change(self):
        w=self.w;w.columns.setCurrentIndex(2);w.column_height.setValue(40)
        before=w.project.blocks[0].html
        self.separator_checkbox().click()
        self.assertEqual(2,len(vertical(w.project.blocks[0])))
        self.assertEqual(before,w.project.blocks[0].html)
        w.undo();self.assertEqual([],vertical(w.project.blocks[0]))
        w.redo();self.assertEqual(2,len(vertical(w.project.blocks[0])))
        w.columns.setCurrentIndex(0);self.assertEqual([],vertical(w.project.blocks[0]))

    def test_open_another_project_drops_previous_underline_insertion_state(self):
        w=self.w;self.select(0,2);self.underline_button().click();self.select(1,1)
        self.assertTrue(w.editor.textCursor().charFormat().fontUnderline())
        other=core.Project(blocks=[block('<span style="font-size:12pt">没有下划线</span>')])
        with tempfile.TemporaryDirectory() as d:
            path=Path(d)/'another.pagewriter';core.save_project(other,path)
            w.open_path(path)
        self.assertFalse(self.underline_button().isChecked())
        self.assertFalse(w.editor.textCursor().charFormat().fontUnderline())
        w.editor.insertPlainText('新')
        self.assertFalse(fmt_at(w.editor,0).fontUnderline())
        self.assertEqual(12,fmt_at(w.editor,0).fontPointSize())


if __name__=='__main__':unittest.main(verbosity=2)
