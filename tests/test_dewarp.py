"""Curved-edge page flattening: geometry, rendering, persistence, picker and canvas."""
import math
import tempfile
from pathlib import Path
import unittest
from unittest.mock import patch
from PySide6.QtCore import QPointF, Qt
from PySide6.QtGui import QColor, QImage, QPainter, QPainterPath, QPen, QPolygonF, QTransform
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QDialog
from pagewriter import core, ui
from pagewriter.dewarp import PageWarp, ArcCurve, MAX_EDGE_POINTS
from pagewriter.rectify import RectifyDialog

app = QApplication.instance() or QApplication([])
CORNERS = [(80, 60), (550, 110), (510, 750), (35, 690)]
W, H = 120, 160


def bent_warp():
    """Book-like bend: top and bottom edges sag toward the spine side."""
    flat = PageWarp(CORNERS, None, W, H)
    top = [flat.to_image(40, -6), flat.to_image(85, -9)]
    bottom = [flat.to_image(40, H+5), flat.to_image(85, H+8)]
    return PageWarp(CORNERS, {'top': top, 'bottom': bottom}, W, H)


class Geometry(unittest.TestCase):
    def test_no_edge_points_is_exact_four_point_homography(self):
        warp = PageWarp(CORNERS, {}, W, H)
        homography = QTransform()
        QTransform.quadToQuad(QPolygonF([QPointF(0, 0), QPointF(W, 0), QPointF(W, H), QPointF(0, H)]),
                              QPolygonF([QPointF(*p) for p in CORNERS]), homography)
        for x, y in [(0, 0), (W, H), (31.5, 77.2), (119, 3)]:
            expected = homography.map(QPointF(x, y))
            actual = warp.to_image(x, y)
            self.assertAlmostEqual(actual[0], expected.x(), places=6)
            self.assertAlmostEqual(actual[1], expected.y(), places=6)
        self.assertTrue(warp.flat)

    def test_curved_edges_keep_corners_pass_through_nodes_and_leave_other_edges(self):
        warp = bent_warp()
        flat = PageWarp(CORNERS, None, W, H)
        for (x, y), corner in zip([(0, 0), (W, 0), (W, H), (0, H)], CORNERS):
            self.assertAlmostEqual(math.dist(warp.to_image(x, y), corner), 0, places=6)
        top = warp.edge_polyline('top', 400)
        for node in warp.edges['top']:
            self.assertLess(min(math.dist(node, p) for p in top), .6)
        for v in (0, .3, .8, 1):
            self.assertLess(math.dist(warp.to_image(0, v*H), flat.to_image(0, v*H)), 1e-6)
            self.assertLess(math.dist(warp.to_image(W, v*H), flat.to_image(W, v*H)), 1e-6)

    def test_arc_length_parameterisation(self):
        curve = ArcCurve([(0, 0), (50, 20), (100, 0)])
        self.assertAlmostEqual(curve.at(.5)[0], 50, delta=.5)
        self.assertGreater(curve.length, 100)
        straight = ArcCurve([(0, 0), (10, 0)])
        self.assertEqual(straight.at(.25), (2.5, 0))

    def test_rendering_straightens_lines_drawn_on_a_bent_page(self):
        warp = bent_warp()
        photo = QImage(600, 800, QImage.Format.Format_RGB32)
        photo.fill('white')
        painter = QPainter(photo)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(QPen(QColor('black'), 3))
        rows = [20, 60, 100, 140]
        for y in rows:
            path = QPainterPath(QPointF(*warp.to_image(0, y)))
            for i in range(1, 121):
                path.lineTo(QPointF(*warp.to_image(i, y)))
            painter.drawPath(path)
        painter.end()
        scale = 4
        flat = warp.render(photo, W*scale, H*scale)
        for y in rows:
            for x in (10, 45, 80, 110):
                column = [flat.pixelColor(x*scale, r).lightness() for r in range(y*scale-12, y*scale+13)]
                darkest = min(range(len(column)), key=column.__getitem__)
                self.assertLess(column[darkest], 120)
                self.assertLessEqual(abs(darkest-12), 3, f'line y={y} at x={x} is bent')

    def test_flat_render_matches_single_perspective_draw(self):
        photo = QImage(600, 800, QImage.Format.Format_RGB32)
        photo.fill('#d0e0f0')
        out = PageWarp(CORNERS, None, W, H).render(photo, 240, 320)
        self.assertEqual(out.pixelColor(120, 160), QColor('#d0e0f0'))

    def test_traced_text_line_becomes_straight_at_its_flat_end_height(self):
        flat = PageWarp(CORNERS, None, W, H)
        photo = QImage(600, 800, QImage.Format.Format_RGB32)
        photo.fill('white')
        painter = QPainter(photo)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(QPen(QColor('black'), 3))
        # A text line at page y=40 that rises by up to 8 mm toward the right (spine).
        line = [flat.to_image(x, 40-8*(x/W)**2) for x in range(0, 121, 4)]
        path = QPainterPath(QPointF(*line[0]))
        for point in line[1:]:
            path.lineTo(QPointF(*point))
        painter.drawPath(path)
        painter.end()
        traced = [flat.to_image(x, 40-8*(x/W)**2) for x in (5, 45, 80, 112)]
        warp = PageWarp(CORNERS, None, W, H, guides=[traced])
        self.assertEqual(warp.used_guides, {0})
        self.assertAlmostEqual(warp.levels[1][0], 40, delta=.3)
        scale = 4
        out = warp.render(photo, W*scale, H*scale)
        for x in (10, 50, 90, 115):
            column = [out.pixelColor(x*scale, r).lightness() for r in range(40*scale-30, 40*scale+31)]
            darkest = min(range(len(column)), key=column.__getitem__)
            self.assertLess(column[darkest], 120)
            self.assertLessEqual(abs(darkest-30), 4, f'traced line still bent at x={x}')
        # Without the guide the same line stays bent by ~8 mm at the right side.
        plain = flat.render(photo, W*scale, H*scale)
        column = [plain.pixelColor(115*scale, r).lightness() for r in range(0, H*scale)]
        self.assertLess(min(range(len(column)), key=column.__getitem__), 34*scale)

    def test_guides_too_close_outside_or_short_are_ignored(self):
        flat = PageWarp(CORNERS, None, W, H)
        guide = lambda y: [flat.to_image(10, y), flat.to_image(100, y)]
        warp = PageWarp(CORNERS, None, W, H, guides=[guide(50), guide(50.4), guide(-5), [flat.to_image(3, 3)], guide(H-.3)])
        self.assertEqual(warp.used_guides, {0})
        self.assertFalse(warp.flat)
        for x, y in [(0, 0), (60, 50), (W, H), (33, 120)]:
            self.assertLess(math.dist(warp.to_image(x, y), flat.to_image(x, y)), 1e-6)
        self.assertIsNone(warp.guide_polyline(1))
        self.assertEqual(len(warp.guide_polyline(0)), 65)

    def test_render_is_fast_enough_for_canvas(self):
        import time
        photo = QImage(1279, 1706, QImage.Format.Format_RGB32)
        photo.fill('white')
        start = time.perf_counter()
        bent_warp().render(photo, 1400, 1870)
        self.assertLess(time.perf_counter()-start, 3)


class Persistence(unittest.TestCase):
    def project(self, **changes):
        image = QImage(600, 800, QImage.Format.Format_RGB32)
        image.fill('white')
        values = dict(width=W, height=H, blocks=[], image_data=core.image_to_data(image), image_quad=[list(p) for p in CORNERS],
                      image_edges={'top': [[300, 70]], 'bottom': [[280, 740], [400, 745]]})
        values.update(changes)
        return core.Project(**values)

    def test_round_trip_and_old_projects(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/'bent.pagewriter'
            core.save_project(self.project(), path)
            self.assertEqual(core.load_project(path).image_edges, {'top': [[300, 70]], 'bottom': [[280, 740], [400, 745]]})
        self.assertIsNone(core.project_from_dict(core.project_to_dict(self.project(image_edges=None))).image_edges)

    def test_invalid_edges_rejected(self):
        for edges, quad in [({'middle': [[1, 2]]}, CORNERS), ({'top': [[1, 2]]*(MAX_EDGE_POINTS+1)}, CORNERS),
                            ({'top': [[float('nan'), 2]]}, CORNERS), ({'top': [[1, 2]]}, None), (['top'], CORNERS)]:
            with self.assertRaises(ValueError):
                core.project_to_dict(self.project(image_edges=edges, image_quad=quad))

    def test_guides_round_trip_and_validation(self):
        guides = [[[100, 300], [300, 290], [500, 270]]]
        loaded = core.project_from_dict(core.project_to_dict(self.project(image_guides=guides)))
        self.assertEqual(loaded.image_guides, guides)
        for bad, quad in [([[[1, 2]]], CORNERS), ([[[1, 2], [3, 4]]]*7, CORNERS), ([[[1, 2], [3, 4]]], None),
                          ([[[1, 2]]*7], CORNERS), ({'a': 1}, CORNERS)]:
            with self.assertRaises(ValueError):
                core.project_to_dict(self.project(image_guides=bad, image_quad=quad, image_edges=None))


class Picker(unittest.TestCase):
    def setUp(self):
        image = QImage(600, 800, QImage.Format.Format_RGB32)
        image.fill('white')
        self.d = RectifyDialog(None, image, CORNERS, (W, H))
        self.d.resize(1200, 800)

    def tearDown(self):
        self.d.close(); app.processEvents()

    def ghosts(self):
        return [key for key, _ in self.d.handle_positions() if isinstance(key, tuple) and key[0] == 'ghost']

    def test_ghosts_create_edge_points_and_straighten_clears(self):
        self.assertEqual(sorted(k[1] for k in self.ghosts()), ['bottom', 'left', 'right', 'top'])
        self.assertIsNone(self.d.edges)
        key = self.d.add_edge_point('top', 0, QPointF(300, 70))
        self.assertEqual(key, ('edge', 'top', 0))
        self.assertEqual(self.d.edges, {'top': [(300, 70)]})
        self.assertEqual(len([k for k in self.ghosts() if k[1] == 'top']), 2)
        self.assertTrue(self.d.straighten_button.isEnabled())
        self.d.move_point(key, QPointF(305, 66)); self.assertEqual(self.d.edges['top'], [(305, 66)])
        self.d.remove_edge_point('top', 0); self.assertIsNone(self.d.edges)
        self.d.add_edge_point('left', 0, QPointF(40, 380)); self.d.straighten(); self.assertIsNone(self.d.edges)

    def test_edge_point_limit_and_reset(self):
        for i in range(MAX_EDGE_POINTS):
            self.assertIsNotNone(self.d.add_edge_point('top', 0, QPointF(120+i*80, 75)))
        self.assertIsNone(self.d.add_edge_point('top', 0, QPointF(500, 90)))
        self.assertFalse([k for k in self.ghosts() if k[1] == 'top'])
        self.d.undo_point()
        self.assertIsNone(self.d.edges)

    def test_prefilled_edges_preview_and_selection_nudge(self):
        image = QImage(600, 800, QImage.Format.Format_RGB32)
        image.fill('white')
        d = RectifyDialog(None, image, CORNERS, (W, H), {'top': [[300, 70]]})
        self.assertEqual(d.edges, {'top': [(300, 70)]})
        self.assertFalse(d.preview.pixmap().isNull())
        self.assertIn('1 个弯边点', d.prompt.text())
        d.select(('edge', 'top', 0)); d.view.resetTransform(); d.nudge(2, -1)
        self.assertEqual(d.edges['top'], [(302, 69)])
        d.finish_edit(); self.assertEqual(d.points, CORNERS)
        d.close()

    def test_guide_tracing_finish_discard_and_remove(self):
        d = self.d
        d.toggle_guide()
        self.assertEqual(d.drawing_guide, 0)
        self.assertFalse(d.ok.isEnabled())
        self.assertFalse(self.ghosts())
        self.assertIn('参考线', d.prompt.text())
        for point in [(100, 300), (300, 292), (500, 275)]:
            d.add_guide_point(QPointF(*point))
        d.handle_key(__import__('PySide6.QtGui', fromlist=['QKeyEvent']).QKeyEvent(
            __import__('PySide6.QtGui', fromlist=['QKeyEvent']).QKeyEvent.Type.KeyPress, Qt.Key.Key_Escape, Qt.KeyboardModifier.NoModifier))
        self.assertIsNone(d.drawing_guide)
        self.assertEqual(d.guides, [[(100, 300), (300, 292), (500, 275)]])
        self.assertTrue(d.ok.isEnabled())
        self.assertIn('1 条参考线', d.prompt.text())
        d.toggle_guide(); d.add_guide_point(QPointF(100, 500)); d.finish_guide()
        self.assertEqual(len(d.guides), 1)
        d.move_point(('guide', 0, 1), QPointF(300, 290)); self.assertEqual(d.guides[0][1], (300, 290))
        d.remove_guide_point(0, 0); d.remove_guide_point(0, 0)
        self.assertIsNone(d.guides)

    def test_real_mouse_traces_guide_with_double_click(self):
        self.d.show(); app.processEvents(); self.d.view.fit(); app.processEvents()
        view = self.d.view
        self.d.guide_button.click()
        for point in [(100, 300), (300, 292)]:
            QTest.mouseClick(view.viewport(), Qt.MouseButton.LeftButton, pos=view.mapFromScene(QPointF(*point)))
        last = view.mapFromScene(QPointF(500, 275))
        QTest.mouseClick(view.viewport(), Qt.MouseButton.LeftButton, pos=last)
        QTest.mouseDClick(view.viewport(), Qt.MouseButton.LeftButton, pos=last)
        self.assertIsNone(self.d.drawing_guide)
        self.assertEqual(len(self.d.guides), 1)
        self.assertGreaterEqual(len(self.d.guides[0]), 3)
        self.assertEqual(self.d.points, CORNERS)

    def test_real_mouse_drag_of_ghost_bends_edge_right_click_removes(self):
        self.d.show(); app.processEvents(); self.d.view.fit(); app.processEvents()
        view = self.d.view
        ghost = next(p for k, p in self.d.handle_positions() if k == ('ghost', 'top', 0))
        start = view.mapFromScene(QPointF(*ghost))
        end = view.mapFromScene(QPointF(ghost[0], ghost[1]-30))
        QTest.mousePress(view.viewport(), Qt.MouseButton.LeftButton, pos=start)
        QTest.mouseMove(view.viewport(), end)
        QTest.mouseRelease(view.viewport(), Qt.MouseButton.LeftButton, pos=end)
        node = self.d.edges['top'][0]
        self.assertLess(abs(node[1]-(ghost[1]-30)), 4/view.scale_factor())
        self.assertEqual(self.d.points, CORNERS)
        QTest.mouseClick(view.viewport(), Qt.MouseButton.RightButton, pos=view.mapFromScene(QPointF(*node)))
        self.assertIsNone(self.d.edges)


class Canvas(unittest.TestCase):
    def test_window_stores_edges_and_draws_cached_flattened_page(self):
        w = ui.MainWindow()
        image = QImage(600, 800, QImage.Format.Format_RGB32)
        image.fill('white')
        w.project = core.Project(width=W, height=H, blocks=[], image_data=core.image_to_data(image))
        w.refresh()
        calls = []
        w.device_panel.send = lambda action, **kw: calls.append(action)
        edges = {'top': [(300, 70)]}
        with patch.object(ui, 'RectifyDialog') as dialog:
            dialog.return_value.exec.return_value = QDialog.DialogCode.Accepted
            dialog.return_value.points = CORNERS
            dialog.return_value.edges = edges
            dialog.return_value.guides = [[(100, 300), (500, 280)]]
            w.rectify()
        self.assertEqual(w.project.image_edges, {'top': [[300, 70]]})
        self.assertEqual(w.project.image_guides, [[[100, 300], [500, 280]]])
        self.assertIn('geometry_changed', calls)
        background = next(x for x in w.canvas.scene().items() if isinstance(x, ui.BackgroundItem))
        rect = background.mapRectToParent(background.boundingRect())
        self.assertAlmostEqual(rect.width(), W, delta=.5)
        self.assertAlmostEqual(rect.height(), H, delta=.5)
        cached = w._flattened_cache[1]
        w.refresh_scene()
        self.assertIs(w._flattened_cache[1], cached)
        w.reset_rectify()
        self.assertIsNone(w.project.image_edges)
        self.assertIsNone(w.project.image_guides)
        w.dirty = False; w.close(); app.processEvents()


if __name__ == '__main__':
    unittest.main()
