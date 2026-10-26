"""Drawing edits through the shared document, real CLI and MCP, with native Qt."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from PySide6.QtCore import QEventLoop, QRectF, Qt
from PySide6.QtGui import QColor, QImage, QPainter, QPen
from PySide6.QtWidgets import QApplication

from pagewriter import core, lineart
from pagewriter.application import ApplicationService
from pagewriter.document_service import DocumentService, fingerprint
from pagewriter.service_errors import ServiceError

app = QApplication.instance() or QApplication([])
ROOT = Path(__file__).resolve().parents[1]
SVG = '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 20 20"><path d="M2 10 H18" fill="none" stroke="black"/></svg>'


def image_file(root):
    image = QImage(400, 200, QImage.Format.Format_RGB32)
    image.fill(Qt.GlobalColor.white)
    painter = QPainter(image)
    painter.setPen(QPen(Qt.GlobalColor.black, 4))
    painter.drawEllipse(QRectF(20, 40, 100, 100))
    painter.fillRect(160, 50, 90, 90, Qt.GlobalColor.black)
    painter.fillRect(290, 50, 90, 90, QColor(110, 110, 110))
    painter.end()
    target = root / 'drawing.png'
    assert image.save(str(target))
    return target


def ink(drawing):
    return sum(lineart.length(p) for p in core.drawing_strokes(drawing))


class DrawingDocumentTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name).resolve()
        self.svg = self.root / 'line.svg'
        self.svg.write_text(SVG, encoding='utf-8')
        self.doc = DocumentService(core.blank_project(100, 100), workspace=self.root)

    def tearDown(self):
        self.temp.cleanup()

    def apply(self, *ops):
        return self.doc.apply(list(ops), self.doc.revision)

    def import_svg(self, **kw):
        return self.apply({'op': 'import_drawing', 'path': str(self.svg), 'id': 'line',
                           'x_mm': 10, 'y_mm': 10, 'width_mm': 20, **kw})

    def test_raster_tracing_and_grayscale_hatching_match_gui_engine(self):
        self.apply({'op': 'import_drawing', 'path': str(image_file(self.root)), 'id': 'raster',
                    'width_mm': 40, 'fill': True, 'fill_cutoff': .4,
                    'fill_spacing_mm': .5, 'fill_angle_degrees': 0, 'fill_levels': 3,
                    'fill_min_width_mm': .6, 'detail': .5, 'threshold': 128,
                    'min_stroke_mm': .2})
        drawing = self.doc.project.drawings[0]
        expected = lineart.convert(drawing.image_data, 40, **{
            key: getattr(drawing, key) for key in core.DRAWING_PARAMS})
        self.assertEqual(core.drawing_local_paths(drawing),
                         [[list(point) for point in path] for path in expected['paths']])
        summary = self.doc.get()['drawings'][0]
        self.assertEqual(summary['source_type'], 'image')
        self.assertEqual(summary['height_mm'], 20)
        self.assertTrue(summary['stats']['hatched'])
        black = sum(1 for p in drawing.paths if all(15.5 <= x <= 25.5 for x, y in p))
        gray = sum(1 for p in drawing.paths if all(28.5 <= x <= 38.5 for x, y in p))
        self.assertGreater(black, gray)
        self.assertGreater(gray, 0)
        revision = self.doc.revision
        self.doc.get()
        self.assertEqual(revision, self.doc.revision, 'derived drawing caches are not edits')

    def test_svg_edit_erase_scale_restore_and_history(self):
        result = self.import_svg(rotation_degrees=30, name='线条')
        self.assertEqual(result['added_ids'], ['line'])
        drawing = self.doc.project.drawings[0]
        self.assertTrue(drawing.vector)
        self.assertFalse(drawing.image_data)
        before = ink(drawing)
        self.apply({'op': 'erase_drawing', 'id': 'line', 'circles_mm': [[10, 10, 2]]})
        self.assertLess(ink(self.doc.project.drawings[0]), before-3)
        self.assertAlmostEqual(self.doc.get()['drawings'][0]['stats']['ink_mm'], ink(self.doc.project.drawings[0]))
        self.apply({'op': 'update_drawing', 'id': 'line', 'width_mm': 40,
                    'x_mm': 15, 'y_mm': 16, 'rotation_degrees': -15})
        drawing = self.doc.get()['drawings'][0]
        self.assertEqual(drawing['erase_circles_mm'], [[20, 20, 4]])
        self.assertEqual(drawing['rotation_degrees'], -15)
        self.apply({'op': 'restore_drawing', 'id': 'line'})
        self.assertEqual(self.doc.get()['drawings'][0]['erase_circles_mm'], [])
        self.assertAlmostEqual(ink(self.doc.project.drawings[0]), before*2)
        self.doc.history(expected_revision=self.doc.revision)
        self.assertEqual(self.doc.get()['drawings'][0]['erase_circles_mm'], [[20, 20, 4]])
        self.doc.history(redo=True, expected_revision=self.doc.revision)
        self.assertEqual(self.doc.get()['drawings'][0]['erase_circles_mm'], [])

    def test_svg_fill_and_drawing_save_reopen_export(self):
        self.svg.write_text('<svg viewBox="0 0 20 20"><rect x="2" y="2" width="16" height="16" fill="black"/></svg>', encoding='utf-8')
        self.import_svg(fill=True, fill_spacing_mm=1, fill_levels=2)
        self.assertTrue(self.doc.get()['drawings'][0]['stats']['hatched'])
        self.apply({'op': 'erase_drawing', 'id': 'line', 'circles_mm': [[5, 5, 1]]})
        paths = core.all_subpaths(self.doc.project)
        self.assertTrue(paths)
        self.assertEqual(core.preflight(self.doc.project), [])
        self.assertIn('<path', core.export_svg(self.doc.project))
        self.doc.save('drawing.pagewriter')
        self.doc.new(100, 100)
        loaded = self.doc.open('drawing.pagewriter')['drawings'][0]
        self.assertEqual(loaded['erase_circles_mm'], [[5, 5, 1]])
        self.assertEqual(core.all_subpaths(self.doc.project), paths)

    def test_bad_source_settings_and_missing_id_roll_back_entire_batch(self):
        self.import_svg()
        bad_svg = self.root / 'bad.svg'
        bad_svg.write_text('<svg><path>', encoding='utf-8')
        for operation in [
            {'op': 'import_drawing', 'path': str(bad_svg)},
            {'op': 'update_drawing', 'id': 'line', 'fill_spacing_mm': 0},
            {'op': 'update_drawing', 'id': 'missing', 'width_mm': 25},
            {'op': 'erase_drawing', 'id': 'line', 'circles_mm': [[10, 10, 0]]},
            {'op': 'erase_drawing', 'id': 'line', 'circles_mm': [[10, 10, 30]]},
            {'op': 'erase_drawing', 'id': 'line', 'circles_mm': [[10, 10, 1], [0, 0, -1]]},
            {'op': 'import_drawing', 'path': str(self.svg), 'id': 'line'},
        ]:
            with self.subTest(operation=operation):
                signature, revision, undo = fingerprint(self.doc.project), self.doc.revision, len(self.doc.undo_stack)
                with self.assertRaises((ServiceError, ValueError)):
                    self.apply({'op': 'update_drawing', 'id': 'line', 'x_mm': 55}, operation)
                self.assertEqual(fingerprint(self.doc.project), signature)
                self.assertEqual((self.doc.revision, len(self.doc.undo_stack)), (revision, undo))

    def test_workspace_and_source_protection(self):
        with tempfile.TemporaryDirectory() as outside:
            path = Path(outside) / 'outside.svg'
            path.write_text(SVG, encoding='utf-8')
            with self.assertRaises(ServiceError) as caught:
                self.apply({'op': 'import_drawing', 'path': str(path)})
            self.assertEqual(caught.exception.code, 'PATH_OUTSIDE_WORKSPACE')
        self.import_svg()
        with self.assertRaises(ServiceError) as caught:
            self.doc.output_path(str(self.svg), 'svg')
        self.assertEqual(caught.exception.code, 'PROTECTED_PATH')
        huge = self.root / 'huge.svg'
        huge.write_bytes(b' ' * 4_000_001)
        with self.assertRaises(ServiceError):
            self.apply({'op': 'import_drawing', 'path': str(huge)})

    def test_drawing_edit_invalidates_plan_and_new_simulated_job_completes(self):
        self.import_svg()
        with patch('serial.Serial', side_effect=AssertionError('No physical port')):
            service = ApplicationService(project=self.doc.project, workspace=self.root)
            try:
                service.dispatch('device.connect', {'profile_id': 'simulator_servo'})
                deadline = time.monotonic()+10
                while True:
                    state = service.dispatch('device.status')
                    if state.get('authorized') and state.get('state') == 'ready' and not state.get('queued_commands'):
                        break
                    self.assertLess(time.monotonic(), deadline, state)
                    time.sleep(.01)
                original = service.dispatch('job.prepare')
                self.assertTrue(original['runnable'], original)
                service.dispatch('document.apply', {'expected_revision': service.documents.revision,
                    'operations': [{'op': 'erase_drawing', 'id': 'line', 'circles_mm': [[10, 10, 1]]}]})
                with self.assertRaises(ServiceError) as caught:
                    service.dispatch('job.start', {'plan_id': original['plan_id'], 'request_id': 'stale'})
                self.assertEqual(caught.exception.code, 'REVISION_CONFLICT')
                plan = service.dispatch('job.prepare')
                self.assertTrue(plan['runnable'], plan)
                job = service.dispatch('job.start', {'plan_id': plan['plan_id'], 'request_id': 'drawing-once'})
                duplicate = service.dispatch('job.start', {'plan_id': plan['plan_id'], 'request_id': 'drawing-once'})
                self.assertEqual(job['job_id'], duplicate['job_id'])
                deadline = time.monotonic()+15
                while True:
                    status = service.dispatch('job.status', {'job_id': job['job_id']})
                    if status['state'] in {'complete', 'failed', 'cancelled', 'unknown', 'rejected'}:
                        break
                    self.assertLess(time.monotonic(), deadline, status)
                    time.sleep(.01)
                self.assertEqual(status['state'], 'complete', status)
                self.assertTrue(status['simulation'])
            finally:
                service.close()


class DrawingDesktopTransportTests(unittest.TestCase):
    def setUp(self):
        from pagewriter.ui import MainWindow
        from pagewriter.agent_ui import start_api
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name).resolve()
        self.svg = self.root / 'line.svg'
        self.svg.write_text(SVG, encoding='utf-8')
        self.window = MainWindow(blank=True)
        self.window._agent_workspace = self.root
        self.api = start_api(self.window)
        self.service = self.window.application_service
        self.pool = ThreadPoolExecutor(max_workers=1)

    def tearDown(self):
        self.pool.shutdown(wait=True)
        self.window.dirty = False
        self.window.close()
        app.processEvents()
        self.temp.cleanup()

    def run_client(self, fn):
        future = self.pool.submit(fn)
        deadline = time.monotonic()+30
        while not future.done():
            self.assertLess(time.monotonic(), deadline, 'CLI/MCP did not finish')
            app.processEvents(QEventLoop.ProcessEventsFlag.AllEvents, 20)
            time.sleep(.005)
        return future.result()

    def apply(self, operations):
        return self.service.dispatch('document.apply', {
            'expected_revision': self.service.documents.revision, 'operations': operations})

    @unittest.skipUnless(importlib.util.find_spec('mcp'), 'optional official MCP SDK not installed')
    def test_real_cli_and_mcp_update_same_canvas_panels_and_undo(self):
        params = self.root / 'edits.json'
        params.write_text(json.dumps({'expected_revision': self.service.documents.revision,
            'operations': [{'op': 'import_drawing', 'path': str(self.svg), 'id': 'shared',
                            'x_mm': 10, 'y_mm': 15, 'width_mm': 20}]}), encoding='utf-8')

        def cli():
            return subprocess.run([sys.executable, '-B', str(ROOT/'agent_main.py'),
                '--connection', str(self.api.connection_file), 'call', 'document.apply',
                '--file', str(params)], cwd=ROOT, capture_output=True, text=True, encoding='utf-8', timeout=20)

        response = self.run_client(cli)
        self.assertEqual(response.returncode, 0, response.stdout+response.stderr)
        envelope = json.loads(response.stdout)
        self.assertTrue(envelope['ok'], envelope)
        self.assertEqual(self.window.selected_id, 'shared')
        self.assertEqual(self.window.objects.count(), 1)
        self.assertEqual(len(self.window.drawing_items), 1)
        self.assertEqual(self.window.drawing_controls['width'].value(), 20)
        self.assertTrue(self.window.undo_stack)

        async def mcp_edit():
            from mcp import Client, StdioServerParameters
            params = StdioServerParameters(command=sys.executable,
                args=['-B', str(ROOT/'agent_main.py'), 'mcp', '--connection', str(self.api.connection_file)], cwd=ROOT)
            async with Client(params, read_timeout_seconds=10) as client:
                tools = await client.list_tools()
                schema = next(tool for tool in tools.tools if tool.name == 'document.apply').input_schema
                self.assertIn('import_drawing', json.dumps(schema))
                response = await client.call_tool('document.apply', {
                    'expected_revision': envelope['result']['revision'], 'operations': [
                        {'op': 'update_drawing', 'id': 'shared', 'x_mm': 25, 'width_mm': 30,
                         'rotation_degrees': 15, 'fill': True, 'fill_spacing_mm': 1},
                        {'op': 'erase_drawing', 'id': 'shared', 'circles_mm': [[15, 15, 2]]}]})
                self.assertFalse(response.is_error, response)
                preview = await client.call_tool('preview.render', {'scale': 1})
                self.assertFalse(preview.is_error, preview)
                self.assertIn('image', [part.type for part in preview.content])
                return response.structured_content

        updated = self.run_client(lambda: asyncio.run(mcp_edit()))
        self.assertTrue(updated['ok'])
        drawing = self.window.project.drawings[0]
        item = self.window.drawing_items[0]
        self.assertIs(item.drawing, drawing)
        self.assertEqual(item.pos().x(), 25)
        self.assertEqual(item.rotation(), 15)
        self.assertEqual(self.window.drawing_controls['width'].value(), 30)
        self.assertTrue(self.window.drawing_fill.isChecked())
        self.assertIn('已擦 1 处', self.window.drawing_stats.text())
        self.assertTrue(self.window.dirty)
        self.window.undo()
        self.assertEqual(self.window.project.drawings[0].width, 20)
        self.assertEqual(self.window.project.drawings[0].erase, [])
        # Native undo is observed by the service and invalidates the old revision.
        with self.assertRaises(ServiceError) as caught:
            self.service.dispatch('document.apply', {'expected_revision': updated['result']['revision'],
                'operations': [{'op': 'remove', 'id': 'shared'}]})
        self.assertEqual(caught.exception.code, 'REVISION_CONFLICT')
        self.window.redo()
        self.service.dispatch('document.get')
        self.apply([{'op': 'remove', 'id': 'shared'}])
        self.assertEqual(self.window.drawing_items, [])
        self.assertEqual(self.window.selected_id, '')

    def test_pending_slider_and_eraser_do_not_overwrite_agent_or_native_edits(self):
        self.apply([{'op': 'import_drawing', 'path': str(self.svg), 'id': 'shared'}])
        revision = self.service.documents.revision
        control = self.window.drawing_controls['detail']
        control.setValue(control.value()+1)
        self.assertIsNotNone(self.window.pending_drawing_change)
        with self.assertRaises(ServiceError) as caught:
            self.apply([{'op': 'update_drawing', 'id': 'shared', 'x_mm': 5}])
        self.assertEqual(caught.exception.code, 'GUI_BUSY')
        self.window.drawing_apply_timer.stop()
        self.window.apply_pending_drawing_change()
        with self.assertRaises(ServiceError) as caught:
            self.service.dispatch('document.apply', {'expected_revision': revision,
                'operations': [{'op': 'remove', 'id': 'shared'}]})
        self.assertEqual(caught.exception.code, 'REVISION_CONFLICT')
        self.window.erase_pending = [[.5, .5, .1]]
        with self.assertRaises(ServiceError) as caught:
            self.apply([{'op': 'remove', 'id': 'shared'}])
        self.assertEqual(caught.exception.code, 'GUI_BUSY')
        self.window.erase_finish()
        self.service.dispatch('document.get')
        self.apply([{'op': 'restore_drawing', 'id': 'shared'}])
        self.assertEqual(self.window.project.drawings[0].erase, [])


if __name__ == '__main__':
    unittest.main()
