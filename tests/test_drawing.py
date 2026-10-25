"""Pictures converted to single-stroke line art, with gray-dependent hatching."""
import base64
import math
import tempfile
import unittest
from pathlib import Path
from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QImage, QPainter, QPen
from PySide6.QtWidgets import QApplication
from pagewriter import core, lineart, ui

app = QApplication.instance() or QApplication([])


def picture(gray_square=128):
    """400x200: thin circle outline (left), solid black square (middle), gray square (right)."""
    image = QImage(400, 200, QImage.Format.Format_RGB32)
    image.fill(Qt.GlobalColor.white)
    p = QPainter(image)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    p.setPen(QPen(Qt.GlobalColor.black, 4))
    p.drawEllipse(QRectF(20, 40, 100, 100))
    p.fillRect(QRectF(160, 50, 90, 90), Qt.GlobalColor.black)
    p.fillRect(QRectF(290, 50, 90, 90), QColor(gray_square, gray_square, gray_square))
    p.end()
    return image


def data(image):
    return core.image_to_data(image)


def inside(path, left, right):
    """Every point's x within [left, right] mm (pictures here are 40 mm wide: 1 px = 0.1 mm)."""
    return all(left <= x <= right for x, _ in path)


class LineArt(unittest.TestCase):
    def test_outline_becomes_one_closed_stroke_without_fill(self):
        result = lineart.convert(data(picture(255)), 40)
        self.assertFalse(result['hatched'])
        circle = [p for p in result['paths'] if inside(p, 0, 14)]
        self.assertEqual(1, len(circle))
        self.assertAlmostEqual(lineart.length(circle[0]), math.pi*10, delta=1.5)  # 100 px = 10 mm diameter
        self.assertAlmostEqual(result['height'], 20, places=6)

    def test_fill_hatches_only_solid_areas_not_outlines(self):
        result = lineart.convert(data(picture(255)), 40, fill=True, fill_spacing=.5, fill_levels=1)
        self.assertTrue(result['hatched'])
        hatch = [p for p in result['paths'] if len(p) == 2 and inside(p, 15.5, 25.5)]
        self.assertGreater(len(hatch), 15)                       # ~9 mm square, 0.5 mm spacing, 45°
        self.assertFalse([p for p in result['paths'] if len(p) == 2 and inside(p, 1, 13) and lineart.length(p) > 3])

    def test_density_follows_gray_with_levels(self):
        def count(levels, cutoff):
            result = lineart.convert(data(picture(110)), 40, fill=True, fill_spacing=.4, fill_levels=levels, fill_cutoff=cutoff)
            black = sum(1 for p in result['paths'] if len(p) == 2 and inside(p, 15.5, 25.5))
            gray = sum(1 for p in result['paths'] if len(p) == 2 and inside(p, 28.5, 38.5))
            return black, gray
        black, gray = count(3, .4)                   # gray square darkness ~0.57
        self.assertGreater(gray, 0)
        self.assertGreater(black, gray*1.5)          # darker -> denser
        _, gray_excluded = count(3, .7)              # cutoff darker than the gray square
        self.assertEqual(0, gray_excluded)
        flat_black, flat_gray = count(1, .4)         # one level: same density once dark enough
        self.assertAlmostEqual(flat_black, flat_gray, delta=max(3, flat_black*.2))


class DrawingProject(unittest.TestCase):
    def make(self, **kw):
        return core.Drawing(id='d', name='图', image_data=data(picture()), x=20, y=30, width=40, **kw)

    def test_cached_strokes_round_trip_and_checks(self):
        d = self.make(fill=True)
        p = core.Project(width=148, height=210, drawings=[d])
        self.assertEqual([], core.preflight(p))
        loaded = core.project_from_dict(core.project_to_dict(p)).drawings[0]
        self.assertEqual(d.frozen_key, loaded.frozen_key)
        self.assertEqual(d.paths, loaded.paths)
        p.zones = [core.ExclusionZone(id='z', name='z', points=[(35, 35), (45, 35), (45, 45), (35, 45)])]
        self.assertIn('z', {c.zone_id for c in core.preflight(p)})
        p.zones = []
        self.assertIn('id="d"', core.export_svg(p))
        self.assertEqual(len(d.paths), len(core.all_subpaths(p)))

    def test_settings_or_size_change_regenerates(self):
        d = self.make()
        first = core.drawing_local_paths(d)
        key = d.frozen_key
        d.width = 80
        core.drawing_local_paths(d)
        self.assertNotEqual(key, d.frozen_key)
        self.assertAlmostEqual(d.height, 40, places=6)
        d.fill = True
        core.drawing_local_paths(d)
        self.assertTrue(d.stats['hatched'])
        self.assertNotEqual(len(first), len(d.paths))

    def test_rotation_turns_strokes_about_origin(self):
        d = self.make(rotation=180)
        r = core.build_drawing_path(d).boundingRect()
        self.assertLessEqual(r.right(), 20+1e-6)
        self.assertLessEqual(r.bottom(), 30+1e-6)

    def test_validation_rejects_bad_settings(self):
        for change in (dict(threshold=0), dict(fill_levels=5), dict(fill_spacing=0), dict(width=0), dict(fill=1)):
            d = self.make(); [setattr(d, k, v) for k, v in change.items()]
            with self.assertRaises(ValueError):
                core.project_to_dict(core.Project(drawings=[d]))


class DrawingUi(unittest.TestCase):
    def setUp(self):
        self.w = ui.MainWindow()
        self.w.project = core.Project(width=148, height=210)
        self.w.refresh()
        self.tmp = tempfile.TemporaryDirectory()
        self.file = Path(self.tmp.name)/'pic.png'
        picture().save(str(self.file))

    def tearDown(self):
        self.w.dirty = False
        self.w.close(); app.processEvents(); self.tmp.cleanup()

    def test_add_adjust_resize_delete_with_undo(self):
        w = self.w
        d = w.add_drawing_from_file(self.file)
        self.assertEqual([d], w.project.drawings)
        self.assertTrue(w.drawing_group.isVisibleTo(w))
        self.assertIn('笔', w.drawing_stats.text())
        self.assertEqual(1, len(w.drawing_items))
        w.drawing_fill.setChecked(True)
        self.assertTrue(w.drawing_by_id().fill and w.drawing_by_id().stats['hatched'])
        self.assertTrue(w.drawing_controls['fill_spacing'].isEnabled())
        slider = w.drawing_controls['fill_cutoff']                # a slider, shown as gray level
        self.assertIsInstance(slider, ui.QSlider)
        slider.setValue(25)                                        # 0 + 25*8 = gray 200
        self.assertEqual('200', w.drawing_value_labels['fill_cutoff'].text())
        self.assertNotAlmostEqual(w.drawing_by_id().fill_cutoff, round(1-200/255, 4))   # waits for the pause
        w.drawing_apply_timer.timeout.emit()
        self.assertAlmostEqual(w.drawing_by_id().fill_cutoff, round(1-200/255, 4))
        item = w.drawing_items[0]
        self.assertEqual({'corner', 'right', 'rotate'}, set(item.handles))
        item.begin_resize()
        item.drag_resize('corner', item.mapToScene(QPointF(60, 1)))
        item.end_resize(); app.processEvents()
        self.assertAlmostEqual(w.drawing_by_id().width, 60, places=1)
        self.assertAlmostEqual(w.drawing_by_id().height, 30, places=1)   # proportions kept
        w.delete_selected()
        self.assertEqual([], w.project.drawings)
        w.undo()
        self.assertEqual(60, round(w.project.drawings[0].width))


class SvgImport(unittest.TestCase):
    def test_svg_file_becomes_a_drawing(self):
        w = ui.MainWindow()
        w.project = core.Project(width=148, height=210)
        w.refresh()
        with tempfile.TemporaryDirectory() as folder:
            file = Path(folder)/'ring.svg'
            file.write_text('<svg xmlns="http://www.w3.org/2000/svg" width="200" height="100" viewBox="0 0 200 100">'
                            '<circle cx="50" cy="50" r="40" fill="none" stroke="black" stroke-width="3"/>'
                            '<rect x="120" y="20" width="60" height="60" fill="black"/></svg>', encoding='utf-8')
            d = w.add_drawing_from_file(file)
        self.assertIsNotNone(d)
        self.assertAlmostEqual(d.height, d.width/2, places=1)        # proportions from the SVG
        ring = [p for p in core.drawing_strokes(d) if max(x for x, _ in p) < d.width*.5]
        self.assertEqual(1, len(ring))                                # stroked circle -> one stroke
        self.assertAlmostEqual(lineart.length(ring[0]), math.pi*d.width*.4, delta=d.width*.06)
        w.dirty = False; w.close(); app.processEvents()


class Erasing(unittest.TestCase):
    def test_erase_splits_strokes_and_scales_with_width(self):
        line = [[0, 0], [10, 0]]
        out = core.apply_erase([line], [[.5, 0, .1]], 10)          # 1 mm radius circle at x=5
        self.assertEqual(2, len(out))
        self.assertLessEqual(max(x for x, _ in out[0]), 4.01)
        self.assertGreaterEqual(min(x for x, _ in out[1]), 5.99)
        self.assertEqual([[[0, 5], [10, 5]]], core.apply_erase([[[0, 5], [10, 5]]], [[.5, 0, .1]], 10))  # far away: untouched
        self.assertEqual(2, len(core.apply_erase([[[0, 0], [20, 0]]], [[.5, 0, .1]], 20)))   # same spot at double size

    def test_ui_eraser_drag_undo_restore_and_settings_keep_erasure(self):
        w = ui.MainWindow()
        w.project = core.Project(width=148, height=210)
        w.refresh()
        with tempfile.TemporaryDirectory() as folder:
            file = Path(folder)/'pic.png'; picture(255).save(str(file))
            d = w.add_drawing_from_file(file)
        before = len(core.drawing_strokes(d))
        w.erase_button.setChecked(True)
        self.assertEqual('erase', w.canvas.mode)
        item = w.drawing_items[0]
        # Brush over the leftmost point of the circle stroke and just below it.
        x, y = min(core.drawing_local_paths(d)[0], key=lambda p: p[0])
        w.erase_at(item.mapToScene(QPointF(x, y)), start=True)
        w.erase_at(item.mapToScene(QPointF(x, y+1)))
        w.erase_finish()
        d = w.drawing_by_id()
        self.assertEqual(2, len(d.erase))
        self.assertGreater(len(core.drawing_strokes(d)), 0)
        self.assertLess(sum(lineart.length(s) for s in core.drawing_strokes(d)),
                        sum(lineart.length(s) for s in core.drawing_local_paths(d))-1)
        self.assertIn('已擦 2 处', w.drawing_stats.text())
        w.drawing_controls['detail'].setValue(0); w.drawing_apply_timer.timeout.emit()   # settings change keeps erasures
        self.assertEqual(2, len(w.drawing_by_id().erase))
        w.restore_erased()
        self.assertEqual([], w.drawing_by_id().erase)
        w.undo()
        self.assertEqual(2, len(w.drawing_by_id().erase))
        w.canvas.set_mode('select')
        self.assertFalse(w.erase_button.isChecked())
        loaded = core.project_from_dict(core.project_to_dict(w.project)).drawings[0]
        self.assertEqual(w.drawing_by_id().erase, loaded.erase)
        w.dirty = False; w.close(); app.processEvents()


if __name__ == '__main__':
    unittest.main()
