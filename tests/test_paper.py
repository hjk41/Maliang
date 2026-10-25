"""Paper presets, blank start page and SVG vector import."""
import math
import unittest
from PySide6.QtWidgets import QApplication
from pagewriter import core, svgimport, ui

app = QApplication.instance() or QApplication([])


class Paper(unittest.TestCase):
    def setUp(self):
        self.w = ui.MainWindow(blank=True)

    def tearDown(self):
        self.w.dirty = False; self.w.close(); app.processEvents()

    def test_app_starts_on_empty_a4(self):
        p = self.w.project
        self.assertEqual((210, 297), (p.width, p.height))
        self.assertEqual(([], [], [], ""), (p.blocks, p.zones, p.drawings, p.image_data))
        self.assertEqual("A4", self.w.paper.currentText())
        self.assertEqual([], self.w.collisions)

    def test_choose_preset_and_landscape_then_custom(self):
        w = self.w
        w.paper.setCurrentIndex(next(i for i in range(w.paper.count()) if w.paper.itemText(i).startswith("B5（国际")))
        w.paper_chosen()
        self.assertEqual((176, 250), (w.project.width, w.project.height))
        w.paper_landscape.setChecked(True)
        self.assertEqual((250, 176), (w.project.width, w.project.height))
        w.undo()
        self.assertEqual((176, 250), (w.project.width, w.project.height))
        w.page_w.setValue(150)
        self.assertEqual("自定义", w.paper.currentText())
        w.page_w.setValue(148); w.page_h.setValue(210)
        self.assertEqual("A5", w.paper.currentText())

    def test_new_project_is_blank_and_keeps_paper_size(self):
        w = self.w
        w.paper.setCurrentIndex(1); w.paper_chosen()          # A5
        w.add_text()
        w.dirty = False
        w.new_project()
        self.assertEqual((148, 210, []), (w.project.width, w.project.height, w.project.blocks))


class ChineseInput(unittest.TestCase):
    def test_add_text_is_ready_for_ime_and_commit_replaces_placeholder(self):
        from PySide6.QtCore import Qt
        from PySide6.QtGui import QInputMethodEvent
        w = ui.MainWindow(blank=True)
        w.show(); app.processEvents()
        w.set_stage(1)
        w.add_text()
        c = w.canvas
        self.assertEqual(w.project.blocks[0].id, c.editing_id)
        self.assertTrue(c.inputMethodQuery(Qt.InputMethodQuery.ImEnabled))
        app.sendEvent(c, QInputMethodEvent("pi", []))            # pinyin being composed
        self.assertEqual("pi", c.preedit_text)
        event = QInputMethodEvent(); event.setCommitString("批注")
        app.sendEvent(c, event)
        self.assertIn("批注", w.editor.toPlainText())
        self.assertNotIn("在这里写下", w.editor.toPlainText())
        self.assertIn("批注", w.project.blocks[0].html)
        # 编辑文字 re-enters editing of the selected box with the cursor at the end.
        c.editing_id = None
        w.edit_or_move("select")
        self.assertEqual(w.project.blocks[0].id, c.editing_id)
        w.dirty = False; w.close(); app.processEvents()


class SvgVectors(unittest.TestCase):
    SVG = ('<svg xmlns="http://www.w3.org/2000/svg" width="200" height="100" viewBox="0 0 200 100">'
           '<g transform="translate(10,0)"><circle cx="40" cy="50" r="40" fill="none" stroke="#000"/></g>'
           '<path d="M120 20 h60 v60 h-60 z" fill="#808080"/>'
           '<path d="M10 95 Q100 60 190 95" fill="none" stroke="black"/>'
           '<defs><rect width="999" height="999"/></defs></svg>')

    def test_parse_keeps_exact_geometry_not_pixels(self):
        v = svgimport.parse(self.SVG)
        self.assertAlmostEqual(.5, v["aspect"])
        circle = max(v["lines"], key=lambda l: max(abs(x-.25) for x, _ in l) < .21 and len(l))
        radius = [math.dist((x, y), (.25, .25)) for x, y in circle]
        self.assertAlmostEqual(.2, min(radius), places=3); self.assertAlmostEqual(.2, max(radius), places=3)
        self.assertEqual(1, len(v["fills"]))                       # <defs> content skipped
        self.assertAlmostEqual(v["fills"][0][1], 1-128/255, places=2)

    def test_vector_drawing_hatches_fill_by_gray(self):
        d = core.Drawing(id='v', vector=svgimport.parse(self.SVG), width=100, fill=True, fill_cutoff=.3, fill_spacing=1, fill_levels=1)
        p = core.Project(width=148, height=210, drawings=[d])
        self.assertEqual([], core.preflight(p))
        self.assertAlmostEqual(50, d.height)
        hatch = [s for s in d.paths if len(s) == 2 and all(60 <= x <= 90 for x, _ in s)]
        self.assertGreater(len(hatch), 30)
        d.fill_cutoff = .7                                         # square is lighter than this: no hatch
        core.drawing_local_paths(d)
        self.assertFalse(d.stats["hatched"])
        loaded = core.project_from_dict(core.project_to_dict(p)).drawings[0]
        self.assertEqual(d.vector, loaded.vector)


if __name__ == '__main__':
    unittest.main()
