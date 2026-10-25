"""Independent local-only integration against user-installed fonts; no redistribution."""
import copy
from pathlib import Path
import sys
import tempfile
import unittest
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from PySide6.QtWidgets import QApplication
from pagewriter import core,gfont
app=QApplication.instance() or QApplication([])
FONT_DIR=Path(r'C:\Program Files (x86)\写字机软件\gcodeFonts')
FAST=FONT_DIR/'超连笔快写-3500字_kvenjoy_gantry.gfont'
KAI=FONT_DIR/'田英章楷书_kvenjoy_瑞瑞.gfont'
TEXT='读到这里慢一点把新的想法留在纸上今天学习新的知识在已有文字和图片的书页空白处添加批注记录重要内容'

@unittest.skipUnless(FAST.is_file() and KAI.is_file(),'User-installed local fonts absent')
class RealFontsAcceptance(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.fast=gfont.register_font(FAST);cls.kai=gfont.register_font(KAI)
    def block(self,font,text=TEXT,**args):
        descriptor=copy.deepcopy(font)
        return core.TextBlock(html=f'<p style="font-size:14pt">{text}</p>',
            writing_mode='single_line',stroke_font=descriptor['family'],
            stroke_fonts={descriptor['family']:descriptor},**args)
    def test_actual_fast_font_reduces_pen_lifts_without_joining(self):
        counts=[]
        for descriptor in [self.kai,self.fast]:
            project=core.Project(width=148,height=210,blocks=[self.block(descriptor,width=105)])
            self.assertEqual([],core.preflight(project))
            counts.append(len(core.all_subpaths(project)))
        self.assertLess(counts[1],counts[0]*.4)
        print('Independent local corpus paths: kai=%s fast=%s'%tuple(counts))
    def test_real_three_columns_no_false_overflow_and_portable_paths(self):
        block=self.block(self.fast,width=108,columns=3,column_height=40,column_gap=8,column_separators=True)
        project=core.Project(width=148,height=210,blocks=[block])
        self.assertEqual([],core.preflight(project))
        before=core.export_svg(project)
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'portable.pagewriter';core.save_project(project,path);loaded=core.load_project(path)
        for descriptor in loaded.blocks[0].stroke_fonts.values():descriptor['path']='missing-local-font.gfont'
        self.assertEqual(before,core.export_svg(loaded))
        self.assertLessEqual(len(loaded.blocks[0].stroke_fonts[block.stroke_font]['glyphs']),len(set(TEXT)))
    def test_partial_mixed_font_and_size_frozen_after_missing_source(self):
        block=self.block(self.fast,text='读到这里',width=108)
        kai=copy.deepcopy(self.kai);block.stroke_fonts[kai['family']]=kai
        block.html=f'<p style="font-size:14pt">读<span style="font-family:\'{kai["family"]}\';font-size:20pt">到</span>这里</p>'
        project=core.Project(width=148,height=210,blocks=[block])
        self.assertEqual([],core.preflight(project))
        self.assertEqual({self.fast['family'],self.kai['family']},{f['family'] for f in block.fonts})
        before=core.export_svg(project)
        for font in block.stroke_fonts.values():font['path']='absent.gfont'
        block.frozen_key=''  # Regenerate from only used glyph subset.
        self.assertEqual(before,core.export_svg(project))

if __name__=='__main__':unittest.main(verbosity=2)
