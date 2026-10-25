"""Operator confirmations cannot migrate to changed sessions or old scopes."""
import copy
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import patch
from unittest.mock import Mock

from PySide6.QtWidgets import QApplication
from pagewriter import core
from pagewriter.agent_ui import AgentDialog, grant_context, check_grant_context, check_authorization
from pagewriter.document_service import DocumentService
from pagewriter.application import ApplicationService
from pagewriter.service_errors import ServiceError

app = QApplication.instance() or QApplication([])


class FakePhysical:
    def __init__(self):
        self.state = {'session_id': 'session-one', 'device_profile': 'songboshi_2024', 'geometry_id': 'page',
            'page_to_machine': (0,1,-1,0,0,0), 'wpos': (10,20,0), 'wco': (0,0,0),
            'pen_up': 0, 'pen_down': 4, 'pen_touch': 1, 'clearance': (10,5),
            'capabilities': {'simulation': False}, 'authorized': True, 'authorization_id': 'old'}
        self.profile = {'work_bounds': [-100,-100,100,100], 'z_bounds': [-5,5]}
    def snapshot(self): return copy.deepcopy(self.state)


class ConfirmationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='maliang-confirm-')
        self.documents = DocumentService(workspace=self.temp.name)
        self.controller = FakePhysical()
        self.service = SimpleNamespace(documents=self.documents, controller=self.controller)

    def tearDown(self): self.temp.cleanup()

    def test_same_labels_cannot_replace_inspected_controller_object(self):
        expected = grant_context(self.service)
        self.service.controller = FakePhysical()
        with self.assertRaises(ServiceError) as caught:
            check_grant_context(self.service, self.controller, expected)
        self.assertEqual(caught.exception.code, 'STALE_CONFIRMATION')

    def test_each_physical_or_page_change_invalidates_confirmation(self):
        for key, value in [('session_id', 'new'), ('wpos', (11,20,0)),
                           ('page_to_machine', (1,0,0,1,0,0)), ('pen_down', 5)]:
            with self.subTest(key=key):
                expected = grant_context(self.service)
                old = self.controller.state[key]; self.controller.state[key] = value
                with self.assertRaises(ServiceError):
                    check_grant_context(self.service, self.controller, expected)
                self.controller.state[key] = old
        expected = grant_context(self.service)
        self.documents.apply([{'op': 'page', 'width_mm': 200}], self.documents.revision)
        with self.assertRaises(ServiceError):
            check_grant_context(self.service, self.controller, expected)

    def test_old_or_wider_authorization_is_not_accepted_as_new_scope(self):
        intended = {'work_bounds': [-20,-20,20,20], 'z_bounds': [-5,5]}
        with self.assertRaises(ServiceError) as caught:
            check_authorization(self.controller, 'new', intended)
        self.assertEqual(caught.exception.code, 'AUTHORIZATION_PENDING')
        self.controller.state['authorization_id'] = 'new'
        with self.assertRaises(ServiceError) as caught:
            check_authorization(self.controller, 'new', intended)
        self.assertEqual(caught.exception.code, 'AUTHORIZATION_MISMATCH')
        self.controller.profile = copy.deepcopy(intended)
        check_authorization(self.controller, 'new', intended)

    def test_actual_dialog_rejects_device_change_during_nested_confirmation(self):
        from PySide6.QtWidgets import QMessageBox
        self.controller.state.update(connection=True, idle=True, state='ready', coordinate_age=0,
                                     pending=False, status_pending=False, queued_commands=0)
        self.controller.submit = Mock()
        self.service.dispatch = Mock(return_value={'issues': []})
        self.service.jobs = SimpleNamespace(grant_motion=Mock())
        window = SimpleNamespace(project=self.documents.project, agent_api=object(), show_job_area=Mock(),
            device_panel=SimpleNamespace(job_bounds=Mock(return_value=((-20,-20,20,20),(-5,5),(0,0,40,40))), authorization=Mock()))
        dialog = SimpleNamespace(window=window, status=SimpleNamespace(setText=Mock()))
        def changed(*args):
            self.controller.state['session_id'] = 'reconnected'
            return QMessageBox.StandardButton.Yes
        with patch('pagewriter.agent_ui.ensure_service', return_value=self.service), \
             patch('pagewriter.core.all_subpaths', return_value=[[(1,1),(2,2)]]), \
             patch('pagewriter.agent_ui.QMessageBox.question', side_effect=changed):
            AgentDialog.grant_page(dialog)
        self.controller.submit.assert_not_called()
        self.service.jobs.grant_motion.assert_not_called()
        self.assertIn('改变', dialog.status.setText.call_args.args[0])

    def test_actual_dialog_requires_its_own_successful_scope_ack(self):
        from PySide6.QtWidgets import QMessageBox
        self.controller.state.update(connection=True, idle=True, state='ready', coordinate_age=0,
                                     pending=False, status_pending=False, queued_commands=0)
        self.controller.submit = Mock()
        self.service.dispatch = Mock(return_value={'issues': []})
        self.service.closed = False
        self.service.jobs = SimpleNamespace(grant_motion=Mock())
        intended = {'work_bounds': [-20,-20,20,20], 'z_bounds': [-5,5]}
        window = SimpleNamespace(project=self.documents.project, agent_api=object(), show_job_area=Mock(),
            device_panel=SimpleNamespace(job_bounds=Mock(return_value=((-20,-20,20,20),(-5,5),(0,0,40,40))),
                                         authorization=Mock(return_value=intended)))
        dialog = SimpleNamespace(window=window, status=SimpleNamespace(setText=Mock()), isVisible=lambda: True, refresh=Mock())
        callbacks = []
        with patch('pagewriter.agent_ui.ensure_service', return_value=self.service), \
             patch('pagewriter.core.all_subpaths', return_value=[[(1,1),(2,2)]]), \
             patch('pagewriter.agent_ui.QMessageBox.question', return_value=QMessageBox.StandardButton.Yes), \
             patch('pagewriter.agent_ui.QTimer.singleShot', side_effect=lambda delay, fn: callbacks.append(fn)), \
             patch('pagewriter.agent_ui.time.monotonic', side_effect=[0, 10]):
            AgentDialog.grant_page(dialog)
            self.assertEqual(len(callbacks), 1)
            callbacks.pop()()
        self.service.jobs.grant_motion.assert_not_called()
        self.assertIn('授权未完成', dialog.status.setText.call_args.args[0])

    def test_application_package_resources_are_protected_without_trying_to_write(self):
        from pagewriter import document_service
        source_root = Path(document_service.__file__).resolve().parents[1]
        service = ApplicationService(workspace=source_root, state_dir=Path(self.temp.name)/'state')
        try:
            with patch('pagewriter.core.save_project', side_effect=AssertionError('No resource writes')):
                with self.assertRaises(ServiceError) as caught:
                    service.dispatch('document.save', {'path': 'pagewriter/resources/fonts/catalog.json'})
                self.assertEqual(caught.exception.code, 'PROTECTED_PATH')
            with patch('pagewriter.application.render_page', side_effect=AssertionError('No icon writes')):
                with self.assertRaises(ServiceError) as caught:
                    service.dispatch('preview.render', {'path': 'pagewriter/resources/maliang.png'})
                self.assertEqual(caught.exception.code, 'PROTECTED_PATH')
        finally:
            service.close()

    def test_unknown_existing_outputs_and_reference_photos_are_preserved(self):
        from PySide6.QtGui import QImage, QColor
        service = ApplicationService(workspace=self.temp.name)
        try:
            path = Path(self.temp.name)/'original.png'
            image = QImage(20,20,QImage.Format.Format_ARGB32); image.fill(QColor('white')); image.save(str(path))
            original = path.read_bytes()
            with self.assertRaises(ServiceError) as caught:
                service.dispatch('preview.render', {'path': 'original.png'})
            self.assertEqual(caught.exception.code, 'FILE_EXISTS')
            service.dispatch('document.apply', {'expected_revision': 1,
                'operations': [{'op':'import_photo','path':'original.png'}]})
            with self.assertRaises(ServiceError) as caught:
                service.dispatch('preview.render', {'path': 'original.png'})
            self.assertEqual(caught.exception.code, 'PROTECTED_PATH')
            self.assertEqual(path.read_bytes(), original)
        finally:
            service.close()
