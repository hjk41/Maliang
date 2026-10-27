"""Five-millimetre dot calibration: fake serial and native Qt, no hardware."""
import unittest
import tempfile
from unittest.mock import patch

from PySide6.QtWidgets import QApplication
from pagewriter.controller import MachineController
from pagewriter.device_ui import DevicePanel
from test_controller import FakeSerial
from test_device_ui import FakeController

app = QApplication.instance() or QApplication([])


class DotCalibrationControllerTests(unittest.TestCase):
    def setUp(self):
        self.now = [0.]
        self.controller = MachineController(FakeSerial, lambda: self.now[0], 2)
        self.addCleanup(self.controller.close)
        self.controller.submit('connect')
        self.controller.tick()
        self.serial = self.controller.serial
        self.rx(b'<Idle|MPos:0,0,15|WCO:0,0,15>\n')
        for response in (b'ok\n', b'ok\n', b'$13=0\nok\n', b'ok\n'):
            self.rx(response)
        if self.controller.status_wait:
            self.rx(b'<Idle|MPos:0,0,15>\n')
        self.controller.submit('declare_clearance', xy_mm=10, z_mm=5)
        self.controller.tick()

    def rx(self, response):
        self.serial.rx.extend(response)
        self.controller.tick()

    def moves(self):
        return [line for line in self.serial.sent if line.startswith(b'$J=')]

    def start(self, direction=1, calibration_id='dot-1'):
        self.controller.submit('calibrate_pen', direction=direction,
                               calibration_id=calibration_id, confirmed=True)
        self.controller.tick()

    def settle(self, z):
        self.rx(b'ok\n')
        if not self.controller.status_wait:
            self.now[0] += .3
            self.controller.tick()
        self.assertTrue(self.controller.status_wait)
        self.rx(f'<Idle|MPos:0,0,{15+z}>\n'.encode())

    def finish_dot(self, direction=1):
        self.start(direction)
        self.settle(direction*5)
        self.settle(0)

    def test_down_five_then_return_only_after_ack_and_fresh_idle(self):
        self.start()
        self.assertEqual(self.moves(), [b'$J=G21 G91 Z5.0000 F30.000\n'])
        self.assertEqual(self.controller.state, 'calibrating')
        # Neither an unsolicited Idle before ACK nor ACK alone completes motion.
        self.rx(b'<Idle|MPos:0,0,20>\n')
        self.assertEqual(len(self.moves()), 1)
        self.rx(b'ok\n')
        self.assertEqual(len(self.moves()), 1)
        self.rx(b'<Jog|MPos:0,0,20>\n')
        self.assertEqual(len(self.moves()), 1)
        self.now[0] += .3
        self.controller.tick()
        self.rx(b'<Idle|MPos:0,0,20>\n')
        self.assertEqual(self.moves()[-1], b'$J=G21 G91 Z-5.0000 F120.000\n')
        self.assertEqual(self.controller.pen_calibration['phase'], 'lifting')
        self.settle(0)
        self.assertEqual(self.controller.state, 'ready')
        self.assertEqual(self.controller.pen_calibration['phase'], 'inspect')
        self.assertIsNone(self.controller.pen_up)
        self.assertIsNone(self.controller.pen_down)
        self.assertIsNone(self.controller.pen_touch)
        self.controller.submit('confirm_pen_calibration', calibration_id='dot-1', confirmed=True)
        self.controller.tick()
        self.assertEqual((self.controller.pen_up, self.controller.pen_down), (0, 5))
        self.assertIsNone(self.controller.pen_touch, 'The approximate 3 mm gap is not a sensor measurement')
        self.assertEqual(self.controller.pen_calibration['phase'], 'confirmed')
        self.assertEqual(len(self.moves()), 2)

    def test_reverse_direction_and_confirmation_cannot_be_guessed(self):
        self.finish_dot(-1)
        self.assertEqual(self.moves(), [b'$J=G21 G91 Z-5.0000 F30.000\n',
                                       b'$J=G21 G91 Z5.0000 F120.000\n'])
        for args in ({'calibration_id': 'old', 'confirmed': True},
                     {'calibration_id': 'dot-1', 'confirmed': False}):
            self.controller.submit('confirm_pen_calibration', **args)
            self.controller.tick()
            self.assertIsNone(self.controller.pen_up)
        self.controller.submit('confirm_pen_calibration', calibration_id='dot-1', confirmed=True)
        self.controller.tick()
        self.assertEqual((self.controller.pen_up, self.controller.pen_down), (0, -5))

    def test_invalid_request_and_insufficient_clearance_never_move(self):
        for changes in ({'confirmed': False}, {'direction': True}, {'direction': 0}, {'calibration_id': ''}):
            self.controller.submit('calibrate_pen', **{
                'direction': 1, 'calibration_id': 'dot-1', 'confirmed': True, **changes})
            self.controller.tick()
            self.assertEqual(self.moves(), [])
        self.controller.clearance = (10, 4)
        self.start()
        self.assertEqual(self.moves(), [])
        self.assertIsNone(self.controller.pen_calibration)
        self.assertTrue(any(e.get('text') == 'Pen calibration exceeds inspected Z envelope'
                            for e in self.controller.drain_events()))

    def test_error_and_stop_do_not_send_a_blind_return_move(self):
        self.start()
        self.rx(b'error:2\n')
        self.assertEqual(self.controller.state, 'unknown')
        self.assertIsNone(self.controller.pen_calibration)
        self.assertEqual(len(self.moves()), 1)
        self.assertEqual(self.serial.sent[-1], b'!')

    def test_stop_clears_the_whole_calibration(self):
        self.start()
        self.controller.submit('stop')
        self.controller.tick()
        self.assertEqual(self.controller.state, 'stopped')
        self.assertIsNone(self.controller.pen_calibration)
        self.assertEqual(len(self.moves()), 1)
        self.assertIsNone(self.controller.pen_down)

    def test_wrong_endpoint_never_arms_return_or_confirmation(self):
        self.start()
        self.settle(4)
        self.assertEqual(self.controller.state, 'unknown')
        self.assertEqual(len(self.moves()), 1)
        self.assertIsNone(self.controller.pen_calibration)

    def test_motion_timeout_does_not_restart_or_lift(self):
        self.start()
        deadline = self.controller.completion_deadline
        self.rx(b'ok\n')
        self.rx(b'<Jog|MPos:0,0,18>\n')
        self.now[0] = deadline+.01
        self.rx(b'<Jog|MPos:0,0,18>\n')
        self.assertEqual(self.controller.failure, 'Motion completion timeout')
        self.assertEqual(len(self.moves()), 1)
        self.assertIsNone(self.controller.pen_calibration)

    def test_coordinate_or_page_changes_cancel_motion(self):
        self.start()
        self.rx(b'ok\n')
        self.rx(b'<Jog|MPos:0,0,18|WCO:0,0,14>\n')
        self.assertEqual(self.controller.state, 'unknown')
        self.assertEqual(len(self.moves()), 1)
        self.assertIsNone(self.controller.pen_calibration)

    def test_page_change_cancels_calibration_without_return_motion(self):
        self.start()
        self.controller.submit('geometry_changed', geometry_id='another-page')
        self.controller.tick()
        self.assertEqual(self.controller.state, 'unknown')
        self.assertEqual(len(self.moves()), 1)
        self.assertIsNone(self.controller.pen_calibration)

    def test_visual_confirmation_rejects_position_drift(self):
        self.finish_dot()
        self.controller.wpos = (0, 0, .1)
        self.controller.submit('confirm_pen_calibration', calibration_id='dot-1', confirmed=True)
        self.controller.tick()
        self.assertIsNone(self.controller.pen_up)
        self.assertIsNone(self.controller.pen_down)

    def test_inspection_expires_after_reposition_and_retries_start_at_current_height(self):
        self.finish_dot()
        self.controller.submit('manual_reposition')
        self.controller.tick()
        self.controller.submit('confirm_pen_calibration', calibration_id='dot-1', confirmed=True)
        self.controller.tick()
        self.assertIsNone(self.controller.pen_up)
        self.assertIsNone(self.controller.pen_calibration)
        self.controller.submit('declare_clearance', xy_mm=10, z_mm=5)
        self.controller.tick()
        self.start(calibration_id='dot-2')
        self.assertEqual(self.controller.pen_calibration['id'], 'dot-2')


class DotCalibrationUiTests(unittest.TestCase):
    def setUp(self):
        self.controller = FakeController()
        self.panel = DevicePanel(None, self.controller)
        self.panel.timer.stop()
        self.addCleanup(self.panel.close)
        self.addCleanup(self.panel.shutdown)

    def test_simple_step_is_one_calibration_button_and_precise_controls_are_advanced(self):
        p = self.panel
        p.update_state()
        self.assertFalse(p.calibrate_button.isEnabled())
        self.assertIs(p.precise_pen_box.parentWidget(), p.advanced_box)
        p.clearance4.setChecked(True)
        p.update_state()
        self.controller.calls.clear()
        p.calibrate_button.click()
        action, args = self.controller.calls[0]
        self.assertEqual(action, 'calibrate_pen')
        self.assertEqual((args['direction'], args['confirmed']), (1, True))
        self.assertTrue(args['calibration_id'])
        self.assertFalse(p.calibrate_button.isEnabled())
        self.assertFalse(p.calibration_stop_button.isHidden())
        p.calibrate_pen()
        self.assertEqual(len(self.controller.calls), 1, 'Repeated click must not queue a second dot')

    def test_dot_completion_waits_for_user_to_check_paper(self):
        p = self.panel
        p.clearance4.setChecked(True)
        self.controller.state.update(calibrated=True, pen_calibration={
            'id': 'dot', 'phase': 'inspect', 'up': -15, 'down': -10})
        p.update_state()
        self.assertTrue(p.calibration_confirm_button.isEnabled())
        self.assertTrue(p.calibration_confirm_button.property('primary'))
        self.assertFalse(p.calibrate_button.property('primary'))
        self.assertNotEqual(p.step4.state[0], 'done')
        self.assertFalse(p.send_button.isEnabled())
        self.assertIn('搓破', p.touch_result.text())
        self.controller.calls.clear()
        p.calibration_confirm_button.click()
        self.assertEqual(self.controller.calls, [('confirm_pen_calibration',
                         {'calibration_id': 'dot', 'confirmed': True})])
        self.controller.state.update(pen_up=-15, pen_down=-10,
            pen_calibration={'id': 'dot', 'phase': 'confirmed', 'up': -15, 'down': -10})
        p.update_state()
        self.assertEqual(p.step4.state[0], 'done')
        self.assertTrue(p.send_button.isEnabled())

    def test_motion_blocks_other_controls_and_stop_is_available(self):
        p = self.panel
        p.clearance4.setChecked(True)
        self.controller.state.update(state='calibrating', pen_calibration={
            'id': 'dot', 'phase': 'lifting', 'up': -15, 'down': -10})
        p.update_state()
        self.assertFalse(p.calibrate_button.isEnabled())
        self.assertFalse(p.manual_button.isEnabled())
        self.assertFalse(p.clearance4.isEnabled())
        self.assertFalse(p.send_button.isEnabled())
        self.assertIn('抬回', p.touch_result.text())
        p.calibration_stop_button.click()
        self.assertEqual(self.controller.calls[-1], ('stop', {}))

    def test_rejection_unlocks_retry_without_marking_calibrated(self):
        p = self.panel
        p.clearance4.setChecked(True)
        p.update_state()
        p.calibrate_button.click()
        request_id = p._calibration_request_id
        self.controller.events.append({'kind': 'rejected', 'action': 'calibrate_pen',
            'calibration_id': request_id, 'text': 'Pen calibration exceeds inspected Z envelope'})
        p.update_state()
        self.assertIsNone(p._calibration_request_id)
        self.assertTrue(p.calibrate_button.isEnabled())
        self.assertIn('5 毫米', p.log.text())

    def test_agent_cannot_confirm_paper_condition_or_edit_during_motion(self):
        from pagewriter.application import ApplicationService
        from pagewriter.service_errors import ServiceError
        with tempfile.TemporaryDirectory() as directory:
            service = ApplicationService(workspace=directory)
            try:
                for command in ('calibrate_pen', 'confirm_pen_calibration'):
                    with self.assertRaises(ServiceError) as caught:
                        service.dispatch('device.command', {'command': command})
                    self.assertEqual(caught.exception.code, 'UNSUPPORTED_COMMAND')
                state = {**service.controller.snapshot(), 'state': 'calibrating'}
                with patch.object(service.controller, 'snapshot', return_value=state):
                    with self.assertRaises(ServiceError) as caught:
                        service.dispatch('document.new', {})
                    self.assertEqual(caught.exception.code, 'DEVICE_BUSY')
            finally:
                service.close()


if __name__ == '__main__':
    unittest.main()
