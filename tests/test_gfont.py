import copy
import os
import struct
import tempfile
import unittest
import zipfile
from pathlib import Path
os.environ['QT_QPA_PLATFORM']='windows' if os.name=='nt' else 'offscreen'
from PySide6.QtWidgets import QApplication
from pagewriter import core, gfont
from font_fixtures import make_font

app=QApplication.instance() or QApplication([])


def record(cp, points):
    return struct.pack('>Hi',cp,len(points)*2)+b''.join(struct.pack('>ff',x,y) for _,x,y in points)+struct.pack('>i',len(points))+bytes(p[0] for p in points)


class GFontTests(unittest.TestCase):
    def font(self, directory):
        path=Path(directory)/'synthetic.gfont'
        path.write_bytes(struct.pack('>ii',5,0))
        with zipfile.ZipFile(path,'a') as z:
            for char in gfont.REFERENCE+'甲乙，。':
                points=[[0,-30,-100],[1,30,0]] if char not in '，。' else [[0,-3,-5],[1,3,0]]
                z.writestr(str(ord(char)),record(ord(char),points))
        return gfont.register_font(path)

    def block(self,font,text='甲乙，。'):
        return core.TextBlock(html='<p style="font-size:12pt">'+text+'</p>',writing_mode='single_line',stroke_font=font['family'],stroke_fonts={font['family']:font})

    def test_parser_rejects_bad_records(self):
        good=record(65,[[0,0,0],[1,4,5]])
        self.assertEqual(gfont.parse_glyph(good,65),[[0,0,0],[1,4,5]])
        for data in (good[:-1],good+b'x',record(65,[[1,0,0]]),record(65,[[0,float('nan'),0]])):
            with self.assertRaises(ValueError):gfont.parse_glyph(data,65)
        with self.assertRaises(ValueError):gfont.parse_glyph(good,66)

    def test_same_scale_punctuation_and_portable_edit(self):
        with tempfile.TemporaryDirectory() as d:
            f=self.font(d);b=self.block(f)
            path=core.local_block_path(b)
            sub=core.path_subpaths(path)
            self.assertAlmostEqual(abs(sub[0][-1][1]-sub[0][0][1])/abs(sub[2][-1][1]-sub[2][0][1]),20)
            self.assertEqual(set(f['glyphs']),{str(ord(c)) for c in '甲乙，。'})
            data=core.project_to_dict(core.Project(blocks=[b]))
            Path(f['path']).unlink()
            loaded=core.project_from_dict(data)
            self.assertEqual(core.path_to_data(path),core.path_to_data(core.local_block_path(loaded.blocks[0])))
            loaded.blocks[0].html='<p style="font-size:18pt">乙甲。</p>'
            self.assertTrue(core.local_block_path(loaded.blocks[0]).elementCount())
            self.assertFalse(loaded.blocks[0].fonts[0]['missing_glyphs'])

    def test_missing_glyph_blocks_without_outline_fallback(self):
        with tempfile.TemporaryDirectory() as d:
            b=self.block(self.font(d),'甲A😀')
            path=core.local_block_path(b)
            self.assertEqual(path.elementCount(),2)
            self.assertEqual(b.fonts[0]['missing_characters'],'A😀')
            with self.assertRaises(core.ExportBlocked):core.export_svg(core.Project(blocks=[b]))

    def test_selected_font_size_and_utf16_boxes(self):
        with tempfile.TemporaryDirectory() as d:
            f=self.font(d);b=self.block(f)
            b.html='<p style="font-size:12pt">甲<span style="font-size:24pt;font-family:'+f['family']+'">乙</span></p>'
            subs=core.path_subpaths(core.local_block_path(b))
            self.assertAlmostEqual(abs(subs[1][-1][1]-subs[1][0][1])/abs(subs[0][-1][1]-subs[0][0][1]),2)
            self.assertEqual([v['start'] for v in b.frozen_layout['char_boxes']],[0,1])

    def test_malformed_portable_cache_rejected(self):
        with tempfile.TemporaryDirectory() as d:
            f=self.font(d);b=self.block(f)
            data=core.project_to_dict(core.Project(blocks=[b]))
            data['project']['blocks'][0]['stroke_fonts'][f['family']]['glyphs']['65']=[[]]
            with self.assertRaises(ValueError):core.project_from_dict(data)

    def test_font_wide_reference_origin_and_column_edges(self):
        with tempfile.TemporaryDirectory() as d:
            f=self.font(d)
            self.assertEqual(f['origin_x'],30)
            self.assertEqual(f['em_size'],100)
            self.assertEqual(f['reference']['method'],'whole-font-common-envelope-v2')
            for count in (2,3):
                b=self.block(copy.deepcopy(f),'甲乙甲乙甲乙甲乙甲乙甲乙甲乙甲乙甲乙')
                b.columns=count;b.column_height=40;b.width=108;b.column_gap=8
                core.local_block_path(b)
                self.assertFalse(b.frozen_overflow)
                self.assertFalse(core.preflight(core.Project(blocks=[b])))

    def test_generated_joined_font_columns(self):
        with tempfile.TemporaryDirectory() as directory:
            f=make_font(directory, joined=True)
            for columns in (2,3):
                b=self.block(copy.deepcopy(f))
                b.width=108;b.columns=columns;b.column_height=40;b.column_gap=8
                b.html='<p style="font-size:14pt">读到这里，慢一点。<br>在书页上直接编辑。<br><u>重点可以加下划线。</u><br>两栏文字连续流动。</p>'
                core.local_block_path(b)
                self.assertFalse(b.frozen_overflow)
                self.assertFalse(core.preflight(core.Project(blocks=[b])))
                self.assertLess(len(b.stroke_fonts[f['family']]['glyphs']),50)


if __name__=='__main__':unittest.main()
