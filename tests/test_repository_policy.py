"""Keep private material dependencies and skipped verification out of releases."""
import io
from pathlib import Path
import runpy
import tempfile
import unittest
from unittest.mock import patch

from font_fixtures import make_font

ROOT = Path(__file__).resolve().parents[1]
repository_policy = runpy.run_path(str(ROOT / 'tools/check_repository.py'))
check = repository_policy['check']
run_suite = runpy.run_path(str(ROOT / 'tools/run_tests.py'))['run_suite']


class RepositoryPolicyTests(unittest.TestCase):
    def test_audit_includes_new_files_and_handles_working_tree_deletions(self):
        entries = repository_policy['entries']
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'new.py').write_bytes(b'pass\n')
            with patch.object(repository_policy['subprocess'], 'check_output',
                              return_value=b'deleted.py\0new.py\0new.py\0'):
                with patch.dict(entries.__globals__, {'Path': lambda name: root/name}):
                    self.assertEqual(list(entries(None)), [('new.py', b'pass\n')])

    def test_external_paths_and_private_assets_fail_publication_audit(self):
        for source in [
            "from pathlib import Path\nf = Path('D:/private/font.gfont')\n",
            "from pathlib import Path\nf = Path('/home/example/font.gfont')\n",
            "image = ROOT / 'pics' / 'page.jpg'\n",
            "source = PROJECT / 'evidence' / 'original.ttf'\n",
            "source = PROJECT / 'evidence/research/original.ttf'\n",
            "source = ROOT.joinpath('pics', 'page.jpg')\n",
        ]:
            with self.subTest(source=source):
                self.assertTrue(check('tests/test_example.py', source.encode('utf-8')))
        source = b"from pathlib import Path\nf = Path(__file__).parent / 'fixtures' / 'printed-page.png'\n"
        self.assertEqual(check('tests/test_example.py', source), [])

    def test_complete_runner_rejects_skips_and_failures(self):
        for outcome, expected in [('pass', 0), ('skip', 1), ('fail', 1)]:
            with self.subTest(outcome=outcome):
                class Example(unittest.TestCase):
                    def runTest(self):
                        if outcome == 'skip':
                            self.skipTest('deliberate runner-policy probe')
                        self.assertNotEqual(outcome, 'fail')
                self.assertEqual(run_suite(unittest.TestSuite([Example()]), stream=io.StringIO()), expected)

    def test_gui_only_discovers_builtins_and_imports_extra_font_explicitly(self):
        from PySide6.QtWidgets import QApplication
        from pagewriter import ui
        app = QApplication.instance() or QApplication([])
        window = ui.MainWindow(blank=True)
        try:
            entries = [window.stroke_fonts.itemData(i) for i in range(1, window.stroke_fonts.count())]
            self.assertEqual(len(entries), 9)
            self.assertTrue(all(entry.get('builtin_id') for entry in entries))
            with tempfile.TemporaryDirectory() as directory:
                font = make_font(directory)
                with patch.object(ui.QFileDialog, 'getOpenFileName', return_value=(font['path'], '')):
                    window.import_stroke_font()
                self.assertEqual(window.stroke_fonts.count(), 11)  # Outline option, nine builtins, explicit import.
                self.assertEqual(window.stroke_fonts.currentData()['sha256'], font['sha256'])
        finally:
            window.dirty = False
            window.close()
            app.processEvents()
