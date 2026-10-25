"""Offline hidden Qt photo operations; no device connection or visible window."""
import base64
import copy
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from PySide6.QtGui import QImage,QColor
from PySide6.QtWidgets import QApplication
from pagewriter.ui import MainWindow
from pagewriter.core import image_to_data
app=QApplication.instance() or QApplication([])

class PhotoRotationTests(unittest.TestCase):
    def setUp(self):
        self.w=MainWindow();self.calls=[]
        self.w.device_panel.send=lambda action,**kw:self.calls.append((action,kw))
        image=QImage(8,5,QImage.Format.Format_RGB32);image.fill(QColor('white'))
        image.setPixelColor(0,0,QColor('red'));image.setPixelColor(7,0,QColor('green'))
        image.setPixelColor(7,4,QColor('blue'));image.setPixelColor(0,4,QColor('yellow'))
        self.image=image;self.w.project.image_data=image_to_data(image)
        self.w.project.image_quad=[[1,1],[7,0],[7,4],[0,4]];self.w.project.image_offset=[3,4]
        self.w.refresh();self.calls=[]
    def tearDown(self):self.w.dirty=False;self.w.close();app.processEvents()
    def image_now(self):return QImage.fromData(base64.b64decode(self.w.project.image_data))
    def test_clockwise_button_rotates_pixels_resets_reference_and_invalidates(self):
        w=self.w;before=copy.deepcopy(w.project)
        w.rotate_photo_cw.click();image=self.image_now()
        self.assertEqual((image.width(),image.height()),(5,8))
        self.assertEqual(image.pixelColor(4,0),QColor('red'))
        self.assertEqual(image.pixelColor(4,7),QColor('green'))
        self.assertIsNone(w.project.image_quad);self.assertEqual(w.project.image_offset,[0,0])
        self.assertEqual(w.project.blocks,before.blocks);self.assertEqual(w.project.zones,before.zones)
        self.assertEqual((w.project.width,w.project.height),(before.width,before.height))
        self.assertEqual([a for a,_ in self.calls],['geometry_changed'])
        self.assertIn('重新四点校正',w.statusBar().currentMessage())
        after=w.project.image_data
        w.undo();self.assertEqual(w.project.image_data,before.image_data)
        self.assertEqual(w.project.image_quad,before.image_quad);self.assertEqual(w.project.image_offset,[3,4])
        w.redo();self.assertEqual(w.project.image_data,after);self.assertIsNone(w.project.image_quad)
        self.assertEqual([a for a,_ in self.calls],['geometry_changed']*3)
    def test_counterclockwise_button_and_roundtrip(self):
        self.w.rotate_photo_ccw.click();image=self.image_now()
        self.assertEqual((image.width(),image.height()),(5,8))
        self.assertEqual(image.pixelColor(0,7),QColor('red'))
        self.assertEqual(image.pixelColor(0,0),QColor('green'))
        self.w.rotate_photo_cw.click()
        self.assertEqual(self.image_now(),self.image)
    def test_import_then_rotate_does_not_modify_source_file(self):
        with tempfile.TemporaryDirectory() as directory:
            source=Path(directory)/'source.png';self.image.save(str(source));original=source.read_bytes()
            with patch('pagewriter.ui.QFileDialog.getOpenFileName',return_value=(str(source),'PNG')):
                self.w.import_image()
            self.w.rotate_photo_cw.click()
            self.assertEqual(source.read_bytes(),original)

if __name__=='__main__':unittest.main()
