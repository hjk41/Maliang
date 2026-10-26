"""Portable stroke-font acceptance using only original repository geometry."""
import copy
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from PySide6.QtWidgets import QApplication
from pagewriter import core
from font_fixtures import make_font, TEXT

app = QApplication.instance() or QApplication([])


class StrokeFontAcceptance(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.joined = make_font(self.temp.name, joined=True)
        self.separated = make_font(self.temp.name)

    def block(self, font, text=TEXT, **args):
        descriptor = copy.deepcopy(font)
        return core.TextBlock(html=f'<p style="font-size:14pt">{text}</p>',
            writing_mode='single_line', stroke_font=descriptor['family'],
            stroke_fonts={descriptor['family']: descriptor}, **args)

    def test_connected_paths_reduce_lifts_without_joining_characters(self):
        counts = []
        for descriptor in [self.separated, self.joined]:
            project = core.Project(width=148, height=210, blocks=[self.block(descriptor, width=105)])
            self.assertEqual([], core.preflight(project))
            paths = core.all_subpaths(project)
            counts.append(len(paths))
            # No path may connect separate characters or separate text lines.
            self.assertTrue(all(max(x for x, y in path)-min(x for x, y in path) < 5 for path in paths))
        self.assertEqual(counts[0], len(TEXT)*4)
        self.assertEqual(counts[1], len(TEXT))

    def test_three_columns_no_false_overflow_and_portable_paths(self):
        block = self.block(self.joined, width=108, columns=3, column_height=40,
                           column_gap=8, column_separators=True)
        project = core.Project(width=148, height=210, blocks=[block])
        self.assertEqual([], core.preflight(project))
        before = core.export_svg(project)
        path = Path(self.temp.name) / 'portable.pagewriter'
        core.save_project(project, path)
        loaded = core.load_project(path)
        Path(self.joined['path']).unlink()
        self.assertEqual(before, core.export_svg(loaded))
        self.assertLessEqual(len(loaded.blocks[0].stroke_fonts[block.stroke_font]['glyphs']), len(set(TEXT)))

    def test_partial_mixed_font_and_size_frozen_after_missing_source(self):
        block = self.block(self.joined, text='读到这里', width=108)
        other = copy.deepcopy(self.separated)
        block.stroke_fonts[other['family']] = other
        block.html = (f'<p style="font-size:14pt">读<span style="font-family:\'{other["family"]}\';'
                      'font-size:20pt">到</span>这里</p>')
        project = core.Project(width=148, height=210, blocks=[block])
        self.assertEqual([], core.preflight(project))
        self.assertEqual({self.joined['family'], self.separated['family']}, {f['family'] for f in block.fonts})
        before = core.export_svg(project)
        Path(self.joined['path']).unlink()
        Path(self.separated['path']).unlink()
        block.frozen_key = ''
        self.assertEqual(before, core.export_svg(project))


if __name__ == '__main__':
    unittest.main(verbosity=2)
