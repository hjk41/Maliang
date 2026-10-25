"""Parent-owned photo/reference acceptance. Hidden native Qt; no hardware."""
import base64
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from PIL import Image
from PySide6.QtCore import QPointF
from PySide6.QtGui import QImage, QPolygonF, QTransform
from PySide6.QtWidgets import QApplication, QDialog
from pagewriter import core, ui

app = QApplication.instance() or QApplication([])

class PhotoAcceptance(unittest.TestCase):
    def setUp(self):
        self.w = ui.MainWindow()
        self.w.project = core.Project(width=120, height=160, blocks=[])
        image = QImage(600, 800, QImage.Format.Format_RGB32)
        image.fill('white')
        self.w.project.image_data = core.image_to_data(image)
        self.w.selected_id = None
        self.w.refresh()

    def tearDown(self):
        self.w.dirty = False
        self.w.replay_timer.stop()
        self.w.close()
        self.w.deleteLater()
        app.processEvents()

    def background(self):
        return next(x for x in self.w.canvas.scene().items() if isinstance(x, ui.BackgroundItem))

    def rectify(self, points):
        dialog = SimpleNamespace(points=points, exec=lambda: QDialog.DialogCode.Accepted)
        with patch.object(ui, 'RectifyDialog', return_value=dialog):
            self.w.rectify()

    def test_smartphone_exif_orientation_applied_before_embedding(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'phone.jpg'
            raw = Image.new('RGB', (400, 200), 'white')
            raw.paste('red', (0, 0, 100, 100))
            exif = raw.getexif()
            exif[274] = 6
            raw.save(path, exif=exif)
            with patch.object(ui.QFileDialog, 'getOpenFileName', return_value=(str(path), '')):
                self.w.import_image()
        normalized = QImage.fromData(base64.b64decode(self.w.project.image_data))
        self.assertEqual((200, 400), (normalized.width(), normalized.height()))
        self.assertGreater(normalized.pixelColor(150, 50).red(), 220)
        self.assertLess(normalized.pixelColor(150, 50).green(), 40)

    def test_four_perspective_corners_map_to_exact_page_mm(self):
        points = [(80, 60), (550, 110), (510, 750), (35, 690)]
        self.rectify(points)
        image = self.background()
        for source, expected in zip(points, [(0, 0), (120, 0), (120, 160), (0, 160)]):
            actual = image.mapToScene(QPointF(*source))
            self.assertAlmostEqual(actual.x(), expected[0], places=7)
            self.assertAlmostEqual(actual.y(), expected[1], places=7)

    def test_reference_inverse_mapping_survives_zoom_and_image_offset(self):
        points = [(80, 60), (550, 110), (510, 750), (35, 690)]
        self.rectify(points)
        self.w.project.image_offset = [4.5, -2]
        self.w.refresh_scene()
        background = self.background()
        for zoom in [50, 125, 200]:
            self.w.zoom.setValue(zoom)
            page = background.mapToScene(QPointF(310, 400))
            source = background.mapFromScene(page)
            self.assertAlmostEqual(source.x(), 310, places=7)
            self.assertAlmostEqual(source.y(), 400, places=7)
            # Pixel rounding is the only allowed view-space error.
            viewport = self.w.canvas.mapFromScene(page)
            recovered = self.w.canvas.mapToScene(viewport)
            px_scale = self.w.canvas.transform().m11()
            self.assertLessEqual(abs(recovered.x() - page.x()), .51 / px_scale)
            self.assertLessEqual(abs(recovered.y() - page.y()), .51 / px_scale)

    def test_invalid_quad_rejected_without_changing_existing_photo(self):
        valid = [(80, 60), (550, 110), (510, 750), (35, 690)]
        self.rectify(valid)
        for invalid in [[(0,0), (500,700), (500,0), (0,700)],
                        [(0,0), (500,0), (100,100), (0,700)],
                        [(0,0), (0,0), (500,700), (0,700)]]:
            with patch.object(self.w, 'error') as error:
                self.rectify(invalid)
                self.assertTrue(error.called)
            self.assertEqual(valid, self.w.project.image_quad)

    def test_photo_survives_save_without_source_and_is_never_in_svg(self):
        self.w.project.blocks = [core.TextBlock(html='<p>ABC</p>')]
        self.w.project.image_quad = [(80, 60), (550, 110), (510, 750), (35, 690)]
        self.w.project.image_offset = [2, 3]
        before = core.export_svg(self.w.project)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'portable.pagewriter'
            core.save_project(self.w.project, path)
            recovered = core.load_project(path)
        self.assertEqual(self.w.project.image_data, recovered.image_data)
        self.assertEqual([list(p) for p in self.w.project.image_quad], recovered.image_quad)
        self.assertEqual([2, 3], recovered.image_offset)
        self.assertEqual(before, core.export_svg(recovered))
        self.assertNotIn('<image', before)

if __name__ == '__main__':
    unittest.main(verbosity=2)
