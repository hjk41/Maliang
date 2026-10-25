"""Bundled-font portability, real layout metrics, and rich-text integration."""
import copy
import hashlib
import json
import os
import sqlite3
import tempfile
from types import SimpleNamespace
import unittest
import zlib
from collections import Counter
from pathlib import Path
from unittest.mock import patch
from contextlib import closing

os.environ['QT_QPA_PLATFORM'] = 'windows' if os.name == 'nt' else 'offscreen'
from PySide6.QtCore import QPointF
from PySide6.QtGui import QFont, QTextCursor, QTextLayout
from PySide6.QtWidgets import QApplication

from pagewriter import core, gfont, stroke_fonts
from pagewriter.inline_edit import hit_position, cursor_geometry
from pagewriter.resources import resource_path

app = QApplication.instance() or QApplication([])


def block_for(font, text='中文 Abc，。'):
    return core.TextBlock(html=f'<p style="font-family:\'{font["family"]}\';font-size:12pt">{text}</p>',
        writing_mode='single_line', stroke_font=font['family'], stroke_fonts={font['family']:font})


class PortableStrokeTests(unittest.TestCase):
    def test_lazy_fallback_supplementary_cache_and_missing_glyph(self):
        # Deliberately distinct pen paths identify the fallback source without
        # consulting its implementation. No TTF converter is needed at runtime.
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            entries = []
            for name, text, fallback in [('primary', 'A中', ['first','fallback']),
                    ('first', '', ['transitive','primary']), ('transitive', '中𠀀', []), ('fallback', '中𠀀', [])]:
                path = root / (name + '.mlfont')
                with closing(sqlite3.connect(path)) as db:
                    db.execute('CREATE TABLE glyphs(cp INTEGER PRIMARY KEY, advance REAL, data BLOB)')
                    for char in text:
                        points = [[0, 0, -700], [1, 400 if name == 'primary' else 900, 0]]
                        if name == 'primary' and char == '中':
                            points = []  # A source blank must not suppress a real fallback.
                        db.execute('INSERT INTO glyphs VALUES (?, ?, ?)',
                            (ord(char), 500 if name == 'primary' else 1000, zlib.compress(json.dumps(points).encode())))
                    db.commit()
                entries.append({'id': name, 'family': 'Maliang Stroke '+name, 'name': name,
                    'file': path.name, 'sha256': hashlib.sha256(path.read_bytes()).hexdigest(), 'fallback_ids': fallback})
            with patch.object(stroke_fonts, 'catalog', return_value=entries), \
                 patch.object(stroke_fonts, 'resource_path', side_effect=lambda *parts:root.joinpath(*parts[1:])), \
                 patch.object(stroke_fonts, 'layout_families', side_effect=lambda font:['Arial']):
                font = stroke_fonts.descriptor('primary')
                self.assertEqual(font['glyphs'], {})
                block = block_for(font, 'A中𠀀')
                self.assertEqual(len(core.path_subpaths(core.local_block_path(block))), 3)
                self.assertEqual([box['length'] for box in block.frozen_layout['char_boxes']], [1, 1, 2])
                self.assertEqual(font['glyph_sources'][str(ord('𠀀'))], 'fallback')
                self.assertEqual(font['glyph_sources'][str(ord('中'))], 'fallback')
                payload = core.project_to_dict(core.Project(blocks=[block]))
                loaded = core.project_from_dict(payload).blocks[0]
                self.assertEqual(set(loaded.stroke_fonts[font['family']]['glyphs']), {str(ord(c)) for c in 'A中𠀀'})
                block.html = '<p style="font-size:12pt">A𠀀</p>'
                core.local_block_path(block)
                kept = {str(ord(c)) for c in 'A𠀀'}
                self.assertEqual(set(font['glyphs']), kept)
                self.assertEqual(set(font['glyph_sources']), kept)
                font['glyph_sources'][str(ord('中'))] = 'fallback'
                core.project_to_dict(core.Project(blocks=[block]))  # Already frozen: repair older source-only residue too.
                self.assertEqual(set(font['glyph_sources']), kept)
                # A portable cache remains editable after its source is gone.
                for path in root.glob('*.mlfont'):
                    path.unlink()
                loaded.html = '<p style="font-size:18pt">𠀀中A</p>'
                self.assertEqual(len(core.path_subpaths(core.local_block_path(loaded))), 3)
                self.assertFalse(loaded.fonts[0]['missing_glyphs'])

    def test_controlled_rfn_alias_and_unknown_portable_family(self):
        for font_id, family in [('yozai','Maliang Stroke relaxed'), ('future-face','Maliang Stroke future-face')]:
            font = {'family':family, 'builtin_id':font_id, 'name':'portable',
                    'sha256':'a'*64, 'em_size':1000, 'origin_x':0,
                    'glyphs':{'65':[[0,0,-700],[1,500,0]]}}
            with patch.object(stroke_fonts, 'layout_families', return_value=['Arial']):
                payload = core.project_to_dict(core.Project(blocks=[block_for(font,'A')]))
            loaded = core.project_from_dict(payload)
            self.assertEqual(loaded.blocks[0].stroke_font, family)
            payload['project']['blocks'][0]['stroke_fonts'][family]['family'] = 'Maliang Stroke unrelated'
            changed = payload['project']['blocks'][0]['stroke_fonts'].pop(family)
            payload['project']['blocks'][0]['stroke_fonts'][changed['family']] = changed
            with self.assertRaisesRegex(ValueError, '族名'):
                core.project_from_dict(payload)

    def test_unicode_surrogates_are_rejected_in_project_glyph_cache(self):
        font = {'family':'Maliang Stroke test', 'builtin_id':'test', 'name':'test',
                'sha256':'a'*64, 'em_size':1000, 'origin_x':0,
                'glyphs':{'55296':[[0,0,0],[1,1,1]]}}
        block = block_for(font, '')
        with self.assertRaisesRegex(ValueError, '字符编号'):
            core.project_to_dict(core.Project(blocks=[block]))

    def test_changed_layout_resource_keeps_frozen_paths_and_blocks_edited_export(self):
        font = {'family':'Maliang Stroke old', 'builtin_id':'old', 'name':'old',
                'sha256':'a'*64, 'em_size':1000, 'origin_x':0,
                'resource_versions':{'old':{'sha256':'a'*64,'layout_sha256':'b'*64}},
                'glyphs':{'65':[[0,0,-700],[1,500,0]]}}
        block = block_for(font, 'A')
        with patch.object(stroke_fonts, 'layout_families', return_value=['Arial']):
            expected = core.path_to_data(core.local_block_path(block))
            project = core.project_from_dict(core.project_to_dict(core.Project(blocks=[block])))
        loaded = project.blocks[0]
        with patch.object(stroke_fonts, 'layout_families', side_effect=ValueError('changed layout version')):
            self.assertEqual(core.path_to_data(core.local_block_path(loaded)), expected)
            self.assertTrue(core.font_warnings(project))
            loaded.html = '<p style="font-size:18pt">A</p>'
            self.assertTrue(core.local_block_path(loaded).isEmpty())
            self.assertTrue(loaded.fonts[0]['missing_glyphs'])
            self.assertIn('65', loaded.stroke_fonts[font['family']]['glyphs'])
            with self.assertRaises(core.ExportBlocked):
                core.export_svg(project)


@unittest.skipUnless(resource_path('fonts', 'catalog.json').is_file(), 'Bundled font build is not present')
class BundledFontsAcceptance(unittest.TestCase):
    def test_invisible_format_characters_preserve_positions_without_pen_paths(self):
        font = stroke_fonts.descriptor('wenkai-gb')
        text = '中文 co\u00adoperate X\u200bY a\u200db'
        block = block_for(font, text)
        path = core.local_block_path(block)
        self.assertFalse(any(item['missing_glyphs'] for item in block.fonts))
        self.assertEqual(len(block.frozen_layout['char_boxes']), len(text))
        self.assertEqual([box['start'] for box in block.frozen_layout['char_boxes']],list(range(len(text))))
        for char in ('\u00ad','\u200b','\u200d'):
            self.assertNotIn(str(ord(char)),font['glyphs'])
            self.assertNotIn(str(ord(char)),font.get('glyph_sources',{}))
        without = block_for(stroke_fonts.descriptor('wenkai-gb'),text.replace('\u00ad','').replace('\u200b','').replace('\u200d',''))
        actual=core.path_to_data(path)
        expected=core.path_to_data(core.local_block_path(without))
        self.assertEqual(len(actual),len(expected))
        # Qt may change the line ascent for a layout control's fallback font;
        # keep that baseline, but never add ink or change horizontal advances.
        self.assertTrue(all(kind==other_kind and abs(x-other_x)<1e-8
            for (kind,x,_),(other_kind,other_x,_) in zip(actual,expected)))
        self.assertFalse(core.preflight(core.Project(blocks=[block])))
        # Variation selectors are Mn, not layout controls: unsupported ones
        # must remain an explicit missing-glyph boundary.
        block.html = '<p style="font-size:12pt">中\ufe0f</p>'
        core.local_block_path(block)
        self.assertTrue(any('\ufe0f' in item['missing_characters'] for item in block.fonts))

    def test_visible_soft_hyphen_wrap_blocks_export_with_actionable_error(self):
        block = block_for(stroke_fonts.descriptor('wenkai-gb'),'co\u00adoperate')
        block.width = 15
        project = core.Project(blocks=[block])
        core.local_block_path(block)
        self.assertGreater(block.frozen_layout['line_count'],1)
        failures = core.preflight(project)
        self.assertTrue(any('普通连字符或加宽文字框' in failure.message for failure in failures))
        with self.assertRaises(core.ExportBlocked):
            core.export_svg(project)

    def test_license_menu_shows_names_sources_and_complete_local_text(self):
        from pagewriter.ui import MainWindow
        window = MainWindow(blank=True)
        try:
            with patch.object(window, 'show_text') as show:
                window.font_licenses_action.trigger()
            show.assert_called_once()
            title, body = show.call_args.args
            self.assertEqual(title, '内置字体与许可')
            for entry in stroke_fonts.catalog():
                self.assertIn(entry['name'], body)
                self.assertIn(entry['source_name'], body)
                self.assertIn(entry['license'], body)
                original = resource_path('fonts',entry['license_file']).read_text(encoding='utf-8')
                self.assertIn(original, body)
            self.assertFalse(window.dirty)
        finally:
            window.close()
            app.processEvents()

    def test_nine_builtins_draw_without_local_vendor_fonts(self):
        fonts = stroke_fonts.descriptors()
        self.assertEqual({f['builtin_id'] for f in fonts}, {'maoken','wenkai-gb','xiaolai','yozai','hanzi-writer',
            'ems-readability','ems-tech','ems-neato','ems-allure'})
        for font in fonts:
            with self.subTest(font=font['builtin_id']):
                self.assertEqual(font['glyphs'], {})
                block = block_for(font)
                self.assertGreater(core.local_block_path(block).elementCount(), 10)
                self.assertFalse(any(item['missing_glyphs'] for item in block.fonts))
                self.assertFalse(core.font_warnings(core.Project(blocks=[block])))
                self.assertLessEqual(len(font['glyphs']), len(set('中文 Abc，。')))

    def test_ems_advances_are_real_and_utf16_hit_geometry_agrees(self):
        font = stroke_fonts.descriptor('ems-readability')
        entry = next(f for f in stroke_fonts.catalog() if f['id'] == 'ems-readability')
        with closing(sqlite3.connect(resource_path('fonts', entry['file']))) as database:
            advances = {chr(cp): advance for cp, advance in database.execute('SELECT cp,advance FROM glyphs WHERE cp IN (105,87)')}
        self.assertNotEqual(advances['i'], advances['W'])
        block = block_for(font, 'iW中')
        core.local_block_path(block)
        boxes = block.frozen_layout['char_boxes']
        for index, char in enumerate('iW'):
            self.assertAlmostEqual(boxes[index]['width'], advances[char]/1000*12*25.4/72, delta=.015)
        for index, box in enumerate(boxes):
            caret = cursor_geometry(block, box['start'])
            self.assertAlmostEqual(caret.x(), box['x'], places=5)
            self.assertEqual(hit_position(block, QPointF(box['x']+box['width']*.1,box['y']+box['height']*.5)), index)

    def test_missing_source_character_blocks_export_without_system_pen_paths(self):
        font = stroke_fonts.descriptor('maoken')
        block = block_for(font, '中文\U0010ffff')
        project = core.Project(blocks=[block])
        core.local_block_path(block)
        self.assertIn('\U0010ffff', block.fonts[0]['missing_characters'])
        with self.assertRaises(core.ExportBlocked):
            core.export_svg(project)

    def test_pinyin_and_missing_supplementary_fallback_use_the_same_raw_fonts(self):
        font = stroke_fonts.descriptor('ems-readability')
        text = 'iW中 nǐ hǎo üāé𠀀'
        block = block_for(font, text)
        core.local_block_path(block)
        entries = {entry['id']:entry for entry in stroke_fonts.catalog()}
        document = core.make_document(block)
        paragraph = document.begin()
        line = paragraph.layout().lineAt(0)
        runs = paragraph.layout().glyphRuns(line.textStart(), line.textLength(), QTextLayout.GlyphRunRetrievalFlag.RetrieveAll)
        raw_fonts = {run.rawFont().familyName():run.rawFont() for run in runs}
        actual = Counter((run.rawFont().familyName(), glyph) for run in runs
            if run.rawFont().familyName().startswith('Maliang Stroke ') for glyph in run.glyphIndexes())
        expected = Counter()
        for char, box in zip(text, block.frozen_layout['char_boxes']):
            if char.isspace():
                source = 'ems-readability'
            else:
                source = font.get('glyph_sources', {}).get(str(ord(char)))
            if source is None:
                self.assertEqual(char, '𠀀')
                self.assertEqual(box['length'], 2)
                self.assertTrue(any(char in f['missing_characters'] for f in block.fonts))
                continue
            entry = entries[source]
            family = entry['family']
            self.assertIn(family, raw_fonts)
            expected.update((family, glyph) for glyph in raw_fonts[family].glyphIndexesForString(char))
            with closing(sqlite3.connect(resource_path('fonts',entry['file']))) as database:
                advance = database.execute('SELECT advance FROM glyphs WHERE cp=?',(ord(char),)).fetchone()[0]
            self.assertAlmostEqual(box['width'], advance/1000*12*25.4/72, delta=.015)
        self.assertEqual(actual, expected)

    def test_metric_proxy_chosen_in_generic_picker_uses_real_pen_paths(self):
        from pagewriter.ui import MainWindow
        window = MainWindow()
        try:
            window.select_block(window.project.blocks[0].id)
            window.editor.setPlainText('中文 ABC')
            window.editor.selectAll()
            window.cursor_changed()
            window.apply_font(QFont('Maliang Stroke maoken'))
            block = window.project.blocks[0]
            self.assertEqual(block.writing_mode, 'single_line')
            self.assertGreater(core.local_block_path(block).elementCount(), 10)
            self.assertFalse(any(f['missing_glyphs'] for f in block.fonts))
            block.stroke_fonts[block.stroke_font]['resource_versions']['maoken']['layout_sha256'] = '0'*64
            self.assertTrue(core.local_block_path(block).isEmpty())
            window.sync_stroke_fonts(block)
            index = next(i for i in range(1,window.stroke_fonts.count())
                if (window.stroke_fonts.itemData(i) or {}).get('builtin_id') == 'maoken')
            window.choose_stroke_font(index)
            self.assertGreater(core.local_block_path(block).elementCount(), 10)
            self.assertFalse(any(f['missing_glyphs'] for f in block.fonts))
        finally:
            window.dirty = False
            window.close()
            app.processEvents()

    def test_mix_underline_columns_and_reopen_have_exact_paths(self):
        first = stroke_fonts.descriptor('wenkai-gb')
        second = stroke_fonts.descriptor('ems-tech')
        block = block_for(first, '')
        block.stroke_fonts[second['family']] = second
        block.columns = 2
        block.column_height = 35
        block.width = 110
        block.html = (f'<p style="font-family:\'{first["family"]}\';font-size:12pt">清风明月，'
            f'<span style="font-family:\'{second["family"]}\';font-size:18pt"><u>Ab12</u></span>'
            '<br/>两栏连续流动。<br/>书页留白。<br/>新的想法。</p>')
        path = core.path_to_data(core.local_block_path(block))
        self.assertFalse(block.frozen_overflow)
        self.assertTrue(block.frozen_layout['underlines'])
        self.assertEqual(len(block.fonts), 2)
        with tempfile.TemporaryDirectory() as directory:
            filename = Path(directory)/'builtins.pagewriter'
            core.save_project(core.Project(blocks=[block]), filename)
            self.assertLess(filename.stat().st_size, 2_000_000)
            loaded = core.load_project(filename)
            self.assertEqual(core.path_to_data(core.local_block_path(loaded.blocks[0])), path)
            self.assertEqual(core.export_svg(loaded), core.export_svg(core.Project(blocks=[block])))

    def test_clean_window_selects_builtins_and_partial_font_changes(self):
        from pagewriter.ui import MainWindow
        window = MainWindow(blank=True)
        try:
            self.assertEqual(len(window.project.blocks), 0)
            builtins = [window.stroke_fonts.itemData(i) for i in range(1, window.stroke_fonts.count())
                if (window.stroke_fonts.itemData(i) or {}).get('builtin_id')]
            self.assertEqual(len(builtins), 9)
            # Choose an English face before creating the first text object.
            index_for = lambda font_id: next(i for i in range(1, window.stroke_fonts.count())
                if (window.stroke_fonts.itemData(i) or {}).get('builtin_id') == font_id)
            window.stroke_fonts.setCurrentIndex(index_for('ems-readability'))
            window.add_text()
            block = window.project.blocks[0]
            self.assertEqual(block.stroke_font, 'Maliang Stroke ems-readability')
            window.editor.setPlainText('中文 ABCD')
            cursor = window.editor.textCursor()
            cursor.setPosition(3)
            cursor.setPosition(5, QTextCursor.MoveMode.KeepAnchor)
            window.editor.setTextCursor(cursor)
            window.cursor_changed()
            window.stroke_fonts.setCurrentIndex(index_for('wenkai-gb'))
            window.apply_underline(True)
            window.apply_size(18)
            self.assertEqual(window.editor.textCursor().selectedText(), 'AB')
            self.assertEqual(window.editor.textCursor().charFormat().fontFamilies()[0], 'Maliang Stroke wenkai-gb')
            core.local_block_path(block)
            self.assertEqual(len(block.fonts), 2)
            self.assertTrue(block.frozen_layout['underlines'])
            self.assertFalse(any(f['missing_glyphs'] for f in block.fonts))
            from pagewriter.inline_edit import preedit_layout
            html_before = block.html
            transient = preedit_layout(SimpleNamespace(preedit_text='中文',preedit_attributes=[]), window.editor)
            runs = transient.glyphRuns()
            self.assertTrue(runs)
            self.assertTrue(any(not run.rawFont().pathForGlyph(glyph).isEmpty()
                for run in runs for glyph in run.glyphIndexes()))
            self.assertEqual(block.html, html_before)
        finally:
            window.dirty = False
            window.close()
            app.processEvents()


if __name__ == '__main__':
    unittest.main()
