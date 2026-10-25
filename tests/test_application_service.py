"""Application-boundary and desktop attachment acceptance; no physical ports."""
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from PySide6.QtCore import QEventLoop
from PySide6.QtWidgets import QApplication

from pagewriter.application import ApplicationService
from pagewriter.service_errors import ServiceError
from pagewriter import core

app = QApplication.instance() or QApplication([])


class ApplicationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='maliang-app-test-')
        self.root = Path(self.temp.name)
        self.service = ApplicationService(workspace=self.root)

    def tearDown(self):
        self.service.close()
        self.temp.cleanup()

    def assert_error(self, code, method, params):
        with self.assertRaises(ServiceError) as caught:
            self.service.dispatch(method, params)
        self.assertEqual(caught.exception.code, code)

    def add_text(self):
        rev = self.service.dispatch('document.get')['revision']
        return self.service.dispatch('document.apply', {'expected_revision': rev, 'operations': [
            {'op': 'add_text', 'id': 'annotation', 'x_mm': 20, 'y_mm': 30, 'width_mm': 100,
             'runs': [{'text': '中文 Read', 'font_id': 'maoken', 'size_pt': 14, 'underline': True}]}]})

    def test_no_hardware_permission_or_public_authorization_bypass(self):
        with patch('serial.Serial', side_effect=AssertionError('Physical serial must not open')):
            self.assert_error('HARDWARE_DISABLED', 'device.connect', {'profile_id': 'songboshi_2024', 'port': 'COM3'})
        self.assert_error('UNSUPPORTED_COMMAND', 'device.command', {'command': 'authorize', 'params': {'confirmed': True}})
        self.assert_error('INVALID_ARGUMENT', 'job.start', {'plan_id': 'x', 'request_id': 'y', 'operator': True})
        self.assert_error('INVALID_ARGUMENT', 'job.prepare', {'preflight_passed': True})

    def test_summary_font_discovery_and_preview_dont_change_revision(self):
        self.add_text()
        before = self.service.dispatch('document.get')['revision']
        fonts = self.service.dispatch('fonts.list')
        self.assertEqual(len(fonts['fonts']), 9)
        self.assertLess(len(json.dumps(fonts)), 10000)
        result = self.service.dispatch('preview.render')
        self.assertTrue(Path(result['path']).is_file())
        self.assertTrue(result['preflight']['passed'])
        self.assertEqual(self.service.dispatch('document.get')['revision'], before)

    def test_context_new_invalidates_even_equal_empty_document(self):
        before = self.service.dispatch('document.get')['revision']
        result = self.service.dispatch('document.new', {'width_mm': 210, 'height_mm': 297})
        self.assertGreater(result['revision'], before)
        self.assert_error('REVISION_CONFLICT', 'document.apply', {'expected_revision': before,
            'operations': [{'op': 'page', 'width_mm': 200}]})

    def test_reserved_state_and_source_files_not_overwritten(self):
        self.add_text()
        source = self.root / 'source.py'; source.write_text('original', encoding='utf-8')
        self.assert_error('INVALID_ARGUMENT', 'document.save', {'path': 'source.py'})
        self.assert_error('RESERVED_PATH', 'document.export', {'path': '.maliang/jobs.sqlite3', 'format': 'pagewriter'})
        self.assertEqual(source.read_text(), 'original')
        self.service.reserve_path(self.root / 'private.json')
        self.assert_error('RESERVED_PATH', 'document.save', {'path': 'private.json'})

    def test_device_event_broadcast_survives_ui_queue_drain(self):
        self.service.dispatch('device.connect', {'profile_id': 'simulator_servo'})
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            app.processEvents(QEventLoop.ProcessEventsFlag.AllEvents, 5)
            state = self.service.dispatch('device.status')
            if state.get('authorized'):
                break
            time.sleep(.01)
        self.assertTrue(state['simulation']); self.assertEqual(state['capabilities']['axes'], ['X', 'Y'])
        self.service.controller.drain_events()
        events = self.service.dispatch('device.events')
        self.assertTrue(any(e['kind'] == 'connected' for e in events['events']))
        self.assertEqual(self.service.dispatch('device.events', {'after': events['cursor']})['events'], [])
        self.assert_error('UNSUPPORTED_CAPABILITY', 'device.command', {'command': 'jog', 'params': {'axis': 'Z', 'distance': .1}})

    def test_public_calls_enforce_owner_thread(self):
        errors = []
        def other():
            try: self.service.dispatch('document.get')
            except ServiceError as exc: errors.append(exc.code)
        thread = threading.Thread(target=other); thread.start(); thread.join(2)
        self.assertEqual(errors, ['WRONG_THREAD'])

    def test_failed_atomic_edit_doesnt_leave_aliases(self):
        original = self.service.dispatch('document.get')
        self.assert_error('INVALID_ARGUMENT', 'document.apply', {'expected_revision': original['revision'], 'operations': [
            {'op': 'add_text', 'runs': [{'text': 'abc', 'font_id': 'ems-tech', 'size_pt': 14}]},
            {'op': 'page', 'width_mm': float('inf')}]})
        self.assertEqual(self.service.dispatch('document.get'), original)
        points = [[100,100],[110,100],[110,110],[100,110]]
        self.service.dispatch('document.apply', {'expected_revision': original['revision'],
            'operations': [{'op': 'add_zone', 'kind': 'ink', 'points_mm': points}]})
        points[0][0] = -100
        self.assertEqual(self.service.documents.project.zones[0].points[0][0], 100)


class DesktopServiceTests(unittest.TestCase):
    def test_api_edits_are_visible_undoable_and_detect_native_edits(self):
        from pagewriter.ui import MainWindow
        with tempfile.TemporaryDirectory(prefix='maliang-desktop-') as folder:
            window = MainWindow(blank=True)
            window._agent_workspace = Path(folder)
            service = window.ensure_application_service()
            try:
                result = service.dispatch('document.apply', {'expected_revision': 1, 'operations': [
                    {'op': 'add_text', 'id': 'shared', 'x_mm': 20, 'y_mm': 30, 'width_mm': 80,
                     'runs': [{'text': '共同编辑 Hello', 'font_id': 'maoken', 'size_pt': 12}]}]})
                self.assertIs(window.project, service.documents.project)
                self.assertEqual(window.project.blocks[0].id, 'shared')
                self.assertTrue(window.undo_stack)
                self.assertIs(window.device_panel.controller, service.controller)
                window.record(); window.project.blocks[0].x = 31; window.refresh_scene()
                with self.assertRaises(ServiceError) as caught:
                    service.dispatch('document.apply', {'expected_revision': result['revision'],
                        'operations': [{'op': 'remove', 'id': 'shared'}]})
                self.assertEqual(caught.exception.code, 'REVISION_CONFLICT')
                current = service.dispatch('document.get')
                self.assertEqual(current['blocks'][0]['x_mm'], 31)
                service.dispatch('document.undo', {'expected_revision': current['revision']})
                self.assertEqual(window.project.blocks[0].x, 20)
                service.select_controller('grbl_generic')
                self.assertEqual(window.device_panel.preset_placement.count(), 0)
                self.assertEqual(window.device_panel.profile_selector.currentData(), 'grbl_generic')
            finally:
                window.dirty = False; window.close(); app.processEvents()

    def test_api_enable_disable_keeps_shared_controller(self):
        from pagewriter.ui import MainWindow
        from pagewriter.agent_ui import start_api, stop_api
        with tempfile.TemporaryDirectory(prefix='maliang-desktop-api-') as folder:
            window = MainWindow(blank=True); window._agent_workspace = Path(folder)
            try:
                original = window.device_panel.controller
                handle = start_api(window)
                self.assertIs(original, window.application_service.controller)
                self.assertTrue(handle.connection_file.is_file())
                self.assertIs(start_api(window), handle)
                stop_api(window)
                self.assertIsNone(window.agent_api)
                self.assertFalse(window.application_service.closed)
                self.assertIs(original, window.device_panel.controller)
            finally:
                window.dirty = False; window.close(); app.processEvents()
