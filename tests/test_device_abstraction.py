"""Driver contracts; these tests use only memory and injected fake serial."""
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from pagewriter.devices import create_controller, get_profile, list_profiles
from pagewriter.devices import register_profile, load_profile
from pagewriter.devices import profiles as profile_registry
from pagewriter.devices.ir import Move, Pen, compile_paths
from pagewriter.devices.simulator import ServoEncoder
from pagewriter.protocol import MachineProfile, compile_machine_job
from pagewriter.controller import MachineController


class FakeSerial:
    opened_count=0
    def __init__(self,**kwargs):
        self.sent=[]; self.rx=bytearray(); self.opened=False
    def open(self): self.opened=True; type(self).opened_count+=1
    def close(self): self.opened=False
    @property
    def in_waiting(self): return len(self.rx)
    def read(self,n): result=bytes(self.rx[:n]); del self.rx[:n]; return result
    def write(self,data): self.sent.append(data); return len(data)


class ProfilesAndIR(unittest.TestCase):
    def test_factory_selects_registered_driver_instead_of_simulation_flag(self):
        marker=object()
        with patch('pagewriter.devices.get_profile',return_value=SimpleNamespace(
                driver='grbl',capabilities=SimpleNamespace(simulation=True))), \
             patch.dict('pagewriter.devices._DRIVER_FACTORIES',{'grbl':lambda **kwargs:marker}):
            self.assertIs(create_controller('synthetic_profile'),marker)

    def test_profiles_are_serializable_and_do_not_overclaim_support(self):
        profiles={p['id']:p for p in json.loads(json.dumps(list_profiles()))}
        self.assertEqual(set(profiles),{'songboshi_2024','grbl_generic','simulator_servo'})
        self.assertEqual(profiles['grbl_generic']['support_status'],'unverified_configuration')
        self.assertEqual(profiles['simulator_servo']['capabilities']['axes'],['X','Y'])
        self.assertEqual(profiles['simulator_servo']['capabilities']['pen_actuation'],'servo')
        self.assertFalse(profiles['songboshi_2024']['capabilities']['manual_reposition_feedback'])
        self.assertFalse(profiles['simulator_servo']['capabilities']['manual_reposition_feedback'])
        self.assertEqual(profiles['songboshi_2024']['capabilities']['cancellation_semantics'],'hold_and_lock_device_queue_not_cleared')
        self.assertEqual(profiles['simulator_servo']['capabilities']['cancellation_semantics'],'clear_virtual_queue')
        self.assertEqual(profiles['grbl_generic']['capabilities']['completion_detection'],'fresh_controller_idle')
        self.assertEqual(profiles['simulator_servo']['capabilities']['completion_detection'],'virtual_observation')
        self.assertEqual(get_profile('grbl_generic').presets,{})
        with self.assertRaises(ValueError): create_controller('guess')

    def test_one_ir_supports_z_adapter_and_servo_without_z(self):
        ir=compile_paths([[(1,2),(3,4)]],mapping=(1,0,0,1,0,0),work_bounds=(0,0,10,10),
                         write_feed=100,travel_feed=200,preflight_passed=True)
        self.assertEqual(ir,(Pen(False),Move(1,2,200),Pen(True),Move(3,4,100),Pen(False)))
        encoded=[json.loads(ServoEncoder.encode(op)) for op in ir]
        self.assertEqual(encoded[0],{'servo':'up'})
        self.assertEqual(encoded[2],{'servo':'down'})
        self.assertNotIn('Z',json.dumps(encoded))
        profile=MachineProfile(page_to_machine=(1,0,0,1,0,0),xy_verified=True,z_verified=True,
            clearance_verified=True,validation_note='synthetic',work_bounds=(0,0,10,10),z_bounds=(-2,2),
            z_up=-1,z_down=1,write_feed=100,travel_feed=200,z_feed=60,max_write_feed=200,
            max_travel_feed=300,max_z_feed=100)
        self.assertEqual(compile_machine_job([[(1,2),(3,4)]],profile,preflight_passed=True),[
            'G21','G90','G94','G1 Z-1.0000 F60.000','G1 X1.0000 Y2.0000 F200.000',
            'G1 Z1.0000 F60.000','G1 X3.0000 Y4.0000 F100.000','G1 Z-1.0000 F60.000'])

    def test_ir_rejects_whole_invalid_job_before_encoding(self):
        for paths in ([[(0,0),(100,0)]],[[(0,0),(float('nan'),0)]],[]):
            with self.assertRaises(ValueError):
                compile_paths(paths,mapping=(1,0,0,1,0,0),work_bounds=(0,0,10,10),write_feed=100,preflight_passed=True)

    def test_custom_model_data_is_validated_and_limits_stay_unknown_when_omitted(self):
        model=dict(id='custom_grbl_fixture',name='Fixture only',driver='grbl',description='Unverified test fixture',
            capabilities=dict(axes=['X','Y','Z'],pen_actuation='z_axis',transport='serial'),serial_baud=57600)
        with patch.dict(profile_registry._PROFILES):
            registered=register_profile(model)
            self.assertIsNone(registered.max_write_feed); self.assertIsNone(registered.max_page_size)
            c=create_controller(registered.id,serial_factory=FakeSerial)
            self.assertEqual(c.device_profile.serial_baud,57600)
            self.assertEqual(c.device_profile.support_status,'unverified_configuration')
            for change in [dict(id='bad',serial_baud=-1),dict(id='bad',max_z_feed=float('nan')),
                           dict(id='bad',support_status='observed_device'),dict(id='bad',max_page_size=[-1,1])]:
                with self.assertRaises(ValueError): register_profile({**model,**change})
            with tempfile.TemporaryDirectory() as tmp:
                path=Path(tmp)/'model.json'; path.write_text(json.dumps({**model,'id':'loaded_fixture','max_write_feed':100}),encoding='utf-8')
                loaded=load_profile(path)
                self.assertEqual(loaded.max_write_feed,100)
            with self.assertRaises(ValueError): register_profile(model)
            for capability in [dict(manual_reposition_feedback=True),dict(cancellation_semantics='guaranteed_physical_stop'),dict(completion_detection='physical_encoder')]:
                with self.assertRaises(ValueError):
                    register_profile({**model,'id':'false_claim_fixture','capabilities':{**model['capabilities'],**capability}})


class EndpointOwnership(unittest.TestCase):
    def test_two_controllers_cannot_open_same_endpoint_and_release_allows_new_owner(self):
        a=MachineController(FakeSerial); b=MachineController(FakeSerial)
        try:
            a.submit('connect',port='COM_TEST_OWNER'); a.tick()
            before=FakeSerial.opened_count
            b.submit('connect',port='com_test_owner'); b.tick()
            self.assertIsNone(b.serial)
            self.assertEqual(FakeSerial.opened_count,before)
            self.assertTrue(any('already owned' in e['text'] for e in b.drain_events()))
            a.submit('disconnect'); a.tick()
            b.submit('connect',port='COM_TEST_OWNER'); b.tick()
            self.assertIsNotNone(b.serial)
        finally: a.close(); b.close()

    def test_generic_profile_has_no_songboshi_preset(self):
        c=MachineController(FakeSerial,profile_id='grbl_generic')
        self.assertEqual(c.device_profile.presets,{})
        self.assertEqual(c.snapshot()['device_profile'],'grbl_generic')
        self.assertFalse(c.snapshot()['simulation'])

    def test_lower_pen_uses_captured_height_and_inspected_envelope(self):
        c=MachineController(FakeSerial,clock=lambda:0.)
        try:
            c.submit('connect',port='COM_TEST_LOWER'); c.tick()
            c.lines.clear(); c.pending=None; c.status_wait=False
            c.state='ready'; c.status_state='Idle'; c.units_mm=True
            c.wpos=(0,0,0); c.wco=(0,0,0); c.coordinate_time=0.
            c.pen_up=0.; c.pen_down=2.; c.clearance=(5.,3.); c.clearance_center=(0.,0.,0.)
            c.submit('lower_pen'); c.tick()
            self.assertEqual(c.serial.sent[-1],b'$J=G21 G91 Z2.0000 F120.000\n')
            self.assertEqual(c.jog_target,(0,0,2))
        finally: c.close()


class ServoSimulator(unittest.TestCase):
    def setUp(self):
        self.c=create_controller('simulator_servo')
        self.c.submit('connect'); self.c.tick()
        self.c.submit('geometry_changed',geometry_id='page'); self.c.tick()
        self.c.submit('prepare_virtual',geometry_id='page',work_bounds=(0,0,100,100)); self.c.tick()
    def tearDown(self): self.c.close()
    def begin(self):
        self.c.submit('start_job',polylines=[[(1,2),(3,4)]],geometry_id='page',preflight_passed=True)
        self.c.tick()
    def finish(self):
        for _ in range(100):
            self.c.tick()
            if self.c.snapshot()['state']=='complete': return
        self.fail('Virtual job did not complete')
    def test_virtual_preparation_has_identity_xy_and_no_z_requirements(self):
        state=self.c.snapshot()
        self.assertTrue(state['simulation']); self.assertTrue(state['authorized'])
        self.assertEqual(state['wpos'],(0,0)); self.assertEqual(state['page_to_machine'],(1,0,0,1,0,0))
        self.assertIsNone(state['pen_up']); self.assertEqual(state['position_source'],'virtual_model')
        self.assertIsNone(self.c.serial)
    def test_ack_is_not_completion_and_final_pen_is_up(self):
        self.begin()
        while self.c.snapshot()['progress'][0]<self.c.snapshot()['progress'][1]: self.c.tick()
        state=self.c.snapshot()
        self.assertNotEqual(state['state'],'complete')
        self.assertTrue(state['pending'])
        self.assertEqual(state['pen_state'],'down')
        self.c.tick(); self.assertEqual(self.c.snapshot()['state'],'waiting_idle')
        self.c.tick(); self.assertEqual(self.c.snapshot()['state'],'complete')
        self.assertEqual(self.c.snapshot()['wpos'],(3,4)); self.assertEqual(self.c.snapshot()['pen_state'],'up')
        self.assertFalse(any(b'G1' in b or b'Z' in b for b in self.c.transport.sent))
    def test_pause_resume_and_cancel_do_not_replay(self):
        self.begin(); self.c.submit('pause'); self.c.tick()
        sent=list(self.c.transport.sent)
        for _ in range(10): self.c.tick()
        self.assertEqual(self.c.transport.sent,sent)
        self.c.submit('resume'); self.c.tick(); self.finish()
        self.begin(); self.c.submit('cancel'); self.c.tick()
        count=len(self.c.transport.sent)
        for _ in range(10): self.c.tick()
        self.assertEqual(len(self.c.transport.sent),count)
        self.assertFalse(self.c.snapshot()['authorized'])
    def test_manual_reposition_and_geometry_change_require_explicit_preparation(self):
        for action,kw in [('manual_reposition',{}),('geometry_changed',{'geometry_id':'new'})]:
            self.c.submit(action,**kw); self.c.tick()
            self.assertFalse(self.c.snapshot()['authorized']); self.assertFalse(self.c.snapshot()['calibrated'])
            self.c.submit('start_job',polylines=[[(1,2),(3,4)]],geometry_id='new',preflight_passed=True); self.c.tick()
            self.assertFalse(self.c.job)
            self.c.submit('prepare_virtual',geometry_id='new'); self.c.tick()
            self.assertTrue(self.c.snapshot()['authorized'])
    def test_bad_job_and_unsupported_z_never_encode(self):
        self.c.submit('start_job',polylines=[[(0,0),(101,0)]],geometry_id='page',preflight_passed=True); self.c.tick()
        self.assertEqual(self.c.transport.sent,[])
        self.c.submit('jog',axis='Z',distance=.1); self.c.tick()
        self.assertEqual(self.c.transport.sent,[])
        self.c.submit('lower_pen'); self.c.tick(); self.c.tick(); self.c.tick()
        self.assertEqual(self.c.snapshot()['pen_state'],'down')
    def test_subscription_is_independent_from_event_queue(self):
        seen=[]; unsubscribe=self.c.subscribe_events(seen.append)
        self.begin(); self.finish()
        queue_events=self.c.drain_events()
        self.assertTrue(any(e['kind']=='complete' for e in seen))
        self.assertTrue(any(e['kind']=='complete' for e in queue_events))
        unsubscribe(); count=len(seen)
        self.c.submit('manual_reposition'); self.c.tick()
        self.assertEqual(len(seen),count)


class SimulatorPanel(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from PySide6.QtWidgets import QApplication
        cls.app=QApplication.instance() or QApplication([])

    def test_xy_servo_panel_never_indexes_or_displays_z(self):
        from types import SimpleNamespace
        from pagewriter.core import Project
        from pagewriter.device_ui import DevicePanel
        c=create_controller('simulator_servo')
        c.submit('connect'); c.tick()
        panel=DevicePanel(SimpleNamespace(project=Project()),c)
        panel.timer.stop()
        try:
            panel.prepare_virtual(); c.tick(); panel.update_state()
            self.assertEqual(panel.profile_selector.currentData(),'simulator_servo')
            self.assertTrue(panel.step4.isHidden())
            self.assertFalse(panel.virtual_controls.isHidden())
            self.assertIn('舵机笔',panel.status.text())
            self.assertNotIn(' Z ',panel.status.text())
            self.assertTrue(panel.send_button.isEnabled())
        finally: panel.shutdown(); panel.close()
