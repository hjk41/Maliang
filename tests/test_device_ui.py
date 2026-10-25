"""Hidden controls with a fake controller; never opens a device."""
import unittest
from types import SimpleNamespace
from PySide6.QtWidgets import QApplication
from pagewriter.device_ui import DevicePanel
from pagewriter.core import Project, ExclusionZone
app=QApplication.instance() or QApplication([])

class FakeController:
    def __init__(self):
        self.calls=[];self.events=[]
        self.state=dict(connection=True,state='ready',coordinate_age=0,pending=False,status_pending=False,wpos=(0,0,-15),progress=(0,0))
    def start(self):pass
    def close(self):pass
    def submit(self,action,**kwargs):self.calls.append((action,kwargs))
    def snapshot(self):return self.state.copy()
    def drain_events(self):
        events,self.events=self.events,[];return events

class PanelTest(unittest.TestCase):
    window=None
    def setUp(self):
        self.controller=FakeController();self.panel=DevicePanel(self.window,self.controller);self.panel.timer.stop()
    def tearDown(self):self.panel.shutdown();self.panel.close()

class PenStrokeControls(PanelTest):
    def test_default_four_mm_requires_explicit_confirmation_and_only_sets(self):
        p=self.panel
        self.assertEqual(p.pen_stroke.value(),4)
        self.assertEqual(p.z_clearance.value(),5)
        self.assertFalse(p.clearance.isChecked())
        self.assertEqual(p.pen_direction.currentData(),1)
        self.assertFalse(p.stroke_confirmed.isChecked())
        p.update_state();self.assertFalse(p.stroke_button.isEnabled())
        p.stroke_confirmed.setChecked(True)
        self.assertTrue(p.stroke_button.isEnabled())
        p.stroke_button.click()
        self.assertEqual(self.controller.calls,[('set_pen_stroke',dict(distance=4,direction=1,confirmed=True))])
    def test_direction_relabels_jog_and_confirmation_resets(self):
        p=self.panel
        self.assertIn('降笔',p.z_jog_buttons[1].text())
        self.assertIn('Z+',p.z_jog_buttons[1].text())
        p.stroke_confirmed.setChecked(True)
        p.pen_direction.setCurrentIndex(1)
        self.assertFalse(p.stroke_confirmed.isChecked())
        self.assertIn('抬笔',p.z_jog_buttons[1].text())
        self.assertIn('降笔',p.z_jog_buttons[-1].text())
        p.clearance.setChecked(True);p.z_step.setCurrentIndex(2);p.feed.setValue(60);p.update_state()
        self.assertEqual(self.controller.calls[-1],('declare_clearance',dict(xy_mm=10,z_mm=5,confirmed_new_range=False)))
        self.assertTrue(p.z_jog_buttons[-1].isEnabled())
        self.controller.calls=[]
        p.z_jog_buttons[-1].click()
        self.assertEqual(self.controller.calls,[('jog',dict(axis='Z',distance=-.25,feed=30))])
        self.controller.state['connection']=False;p.update_state()
        self.assertFalse(p.z_jog_buttons[-1].isEnabled())
        p.stroke_confirmed.setChecked(True);self.assertFalse(p.stroke_button.isEnabled())
    def test_pending_and_changed_distance_cannot_reuse_confirmation(self):
        p=self.panel;p.stroke_confirmed.setChecked(True);p.pen_stroke.setValue(3)
        self.assertFalse(p.stroke_confirmed.isChecked())
        p.stroke_confirmed.setChecked(True)
        self.controller.state['pending']=True;p.update_state()
        self.assertFalse(p.stroke_button.isEnabled())
        self.controller.state['pending']=False;p.update_state();p.stroke_button.click()
        self.assertEqual(self.controller.calls,[('set_pen_stroke',dict(distance=3,direction=1,confirmed=True))])

    def test_rejections_have_actionable_chinese_messages(self):
        p=self.panel
        for reason,expected in [('Confirmed finite stroke and direction +/-1 required','勾选'),('Declare inspected clearance first','第 3 步'),
                                ('Pen stroke exceeds declared Z envelope','范围'),('Pen touch heights exceed declared Z envelope','1 毫米'),
                                ('Jog exceeds inspected displacement envelope or single step','手推')]:
            message=p.translate_error(reason)
            self.assertIn(expected,message)
            self.assertNotIn(reason,message)

    def test_independent_authorized_speeds_and_machine_caps(self):
        p=self.panel
        self.assertEqual(p.feed.value(),300)
        self.assertEqual(p.feed.maximum(),300)
        self.assertEqual((p.write_feed.value(),p.travel_feed.value(),p.z_feed.value()),(3000,4000,3000))
        self.assertEqual((p.write_feed.maximum(),p.travel_feed.maximum(),p.z_feed.maximum()),(4000,4000,5000))
        p.feed.setValue(42);p.verified.setChecked(True);p.validation.setText('现场验证')
        p.authorize()
        action,values=self.controller.calls[-1]
        self.assertEqual(action,'authorize')
        self.assertEqual((values['write_feed'],values['travel_feed'],values['z_feed']),(3000,4000,3000))
        self.assertEqual((values['max_write_feed'],values['max_travel_feed'],values['max_z_feed']),(4000,4000,5000))
        p.write_feed.setValue(9999);p.travel_feed.setValue(9999);p.z_feed.setValue(9999)
        p.authorize();values=self.controller.calls[-1][1]
        self.assertEqual((values['write_feed'],values['travel_feed'],values['z_feed']),(4000,4000,5000))

class TouchCalibration(PanelTest):
    def test_touch_requires_clearance_then_records_and_lifts(self):
        p=self.panel;p.update_state()
        self.assertFalse(p.touch_button.isEnabled())
        p.clearance.setChecked(True);p.update_state()
        self.assertTrue(p.touch_button.isEnabled())
        self.controller.calls=[]
        p.touch_button.click()
        self.assertEqual(self.controller.calls,[('set_pen_touch',dict(lift=2,press=3,direction=1,confirmed=True)),('lift_pen',{})])
    def test_touch_uses_advanced_lift_press_and_direction(self):
        p=self.panel;p.lift.setValue(1.5);p.press.setValue(2.5);p.pen_direction.setCurrentIndex(1)
        p.record_touch()
        self.assertEqual(self.controller.calls[0],('set_pen_touch',dict(lift=1.5,press=2.5,direction=-1,confirmed=True)))
    def test_recorded_touch_summary_and_room_below(self):
        p=self.panel
        self.controller.state.update(wpos=(0,0,-13),calibrated=True,pen_touch=-13,pen_up=-15,pen_down=-10,clearance_remaining=((1,1),(1,1),(2,8)))
        p.update_state()
        self.assertIn('还能往下 8.00',p.height_label.text())
        self.assertIn('碰纸高度 Z -13.00',p.touch_result.text())
        self.assertIn('多压 3 mm',p.touch_result.text())
        self.assertIn('离纸 2 mm',p.touch_result.text())
        self.assertEqual(p.step4.state[0],'done')
        self.assertTrue(p.lift_button.isEnabled())
    def test_first_rejection_is_shown_over_followup(self):
        p=self.panel
        self.controller.events=[dict(kind='rejected',text='Pen touch heights exceed declared Z envelope',action='set_pen_touch'),
                                dict(kind='rejected',text='Pen heights required before lifting',action='lift_pen'),dict(kind='rx',text='<Idle>')]
        p.update_state()
        self.assertIn('笔离纸太远',p.log.text())

class StepOrderAndReasons(PanelTest):
    def test_height_can_be_recorded_before_alignment_from_step_four(self):
        p=self.panel;p.update_state()
        self.assertFalse(p.touch_button.isEnabled())
        self.assertIn('障碍',p.reason4.text())
        p.clearance4.setChecked(True);p.update_state()
        self.assertTrue(p.clearance.isChecked())            # both boxes stay identical
        self.assertEqual(self.controller.calls[-1][0],'declare_clearance')
        self.assertTrue(p.touch_button.isEnabled())         # no alignment needed first
        self.assertTrue(all(b.isEnabled() for b in p.z_jog_buttons.values()))
        self.assertFalse(p.reason4.isVisibleTo(p))
        p.clearance.setChecked(False);self.assertFalse(p.clearance4.isChecked())

    def test_disabled_steps_explain_why(self):
        p=self.panel
        self.controller.state.update(connection=False,state='disconnected');p.update_state()
        for label in (p.reason3,p.reason4,p.reason5,p.reason6):
            self.assertIn('连接',label.text())
        self.controller.state.update(connection=True,state='ready');p.update_state()
        self.assertIn('第 3 步',p.reason5.text())
        self.controller.state.update(calibrated=True);p.update_state()
        self.assertIn('第 4 步',p.reason6.text())
        self.controller.state.update(pen_up=-15,pen_down=-10);p.update_state()
        self.assertEqual('',p.reason6.text())

    def test_anchor_message_keeps_recorded_height(self):
        p=self.panel;p.geometry_id=lambda:'page';p.update_state()
        self.controller.state.update(pen_up=-15,pen_down=-10)
        p.preset_confirmed.setChecked(True);p.reference_action='anchor_preset';p.reference_clicked(1,2)
        self.assertIn('保留',p.log.text())


class NoBlinkDuringStatusQuery(PanelTest):
    def test_status_query_in_flight_does_not_grey_buttons(self):
        p=self.panel;p.clearance.setChecked(True)
        self.controller.state.update(calibrated=True,pen_up=-15,pen_down=-10)
        for pending in (False,True,False):
            self.controller.state['status_pending']=pending;p.update_state()
            self.assertTrue(p.send_button.isEnabled());self.assertTrue(p.touch_button.isEnabled())
            self.assertTrue(all(b.isEnabled() for b in p.page_jog_buttons.values()))
        self.controller.state['pending']=True;p.update_state()   # a real command in flight still blocks
        self.assertFalse(p.send_button.isEnabled())

    def test_test_step_sits_under_write_step_and_is_optional(self):
        p=self.panel
        self.assertEqual([s.title for s in p.steps[4:]],['开始书写','试写一个小方框'])
        self.controller.state.update(calibrated=True,pen_up=-15,pen_down=-10);p.update_state()
        self.assertEqual(p.step5.state[0],'active');self.assertEqual(p.step6.state[0],'todo')
        self.assertTrue(p.step5.expanded());self.assertTrue(p.step6.expanded())


class PageDirectionJog(PanelTest):
    def jog(self,vector):
        self.controller.calls=[];self.panel.page_jog(*vector);return self.controller.calls[-1][1]
    def test_upright_and_inverted_presets(self):
        p=self.panel
        self.assertEqual(self.jog((1,0)),dict(axis='Y',distance=.2,feed=300))
        self.assertEqual(self.jog((0,1))['axis'],'X');self.assertEqual(self.jog((0,1))['distance'],-.2)
        p.preset_placement.setCurrentIndex(1)
        self.assertEqual(self.jog((1,0)),dict(axis='Y',distance=-.2,feed=300))
        self.assertEqual(self.jog((0,1)),dict(axis='X',distance=.2,feed=300))
        p.coarse.click()
        self.assertEqual(self.jog((0,-1)),dict(axis='X',distance=-1,feed=300))
    def test_active_mapping_overrides_preset(self):
        self.controller.state['page_to_machine']=(1,0,0,1,5,5)
        self.assertEqual(self.jog((1,0))['axis'],'X')
        self.assertEqual(self.jog((0,-1)),dict(axis='Y',distance=-.2,feed=300))
    def test_jog_pad_requires_clearance(self):
        p=self.panel;p.update_state()
        self.assertFalse(any(b.isEnabled() for b in p.page_jog_buttons.values()))
        p.clearance.setChecked(True);p.update_state()
        self.assertTrue(all(b.isEnabled() for b in p.page_jog_buttons.values()))
    def test_clearance_box_follows_controller(self):
        p=self.panel;p.clearance.setChecked(True);p._clearance_sent=-10
        self.controller.state['clearance']=None;p.update_state()
        self.assertFalse(p.clearance.isChecked())

class PresetAnchor(PanelTest):
    def test_machine_preset_anchor_is_confirmed_one_shot_not_authorization(self):
        p=self.panel;p.geometry_id=lambda:'page-1';p.update_state()
        self.assertFalse(p.preset_confirmed.isChecked())
        self.assertFalse(p.preset_button.isEnabled())
        p.preset_confirmed.setChecked(True)
        self.assertTrue(p.preset_button.isEnabled())
        p.reference_action='anchor_preset';p.reference_clicked(12,34)
        self.assertEqual(self.controller.calls,[('anchor_preset',dict(preset='songboshi_2024_paper',page_x=12,page_y=34,geometry_id='page-1',axes_confirmed=True))])
        self.assertFalse(p.preset_confirmed.isChecked())
        self.assertIn('第 4 步',p.log.text())
        p.reference_clicked(13,35)
        self.assertEqual(len(self.controller.calls),1)

    def test_preset_confirmation_cleared_on_connection_and_geometry_changes(self):
        p=self.panel;p.update_state()
        for action in ('connect','disconnect','geometry_changed','manual_reposition'):
            p.preset_confirmed.setChecked(True)
            p.send(action)
            self.assertFalse(p.preset_confirmed.isChecked())
        p.preset_confirmed.setChecked(True)
        self.controller.state['session_id']='new-session';p.update_state()
        self.assertFalse(p.preset_confirmed.isChecked())
        p.preset_confirmed.setChecked(True)
        self.controller.state['geometry_id']='new-page';p.update_state()
        self.assertFalse(p.preset_confirmed.isChecked())
        p.preset_confirmed.setChecked(True)
        self.controller.state['connection']=False;p.update_state()
        self.assertFalse(p.preset_confirmed.isChecked())
        self.assertFalse(p.preset_button.isEnabled())

    def test_preset_rechecks_fresh_idle_at_photo_click(self):
        p=self.panel;p.geometry_id=lambda:'page-1';p.update_state()
        p.preset_confirmed.setChecked(True);p.reference_action='anchor_preset'
        self.controller.state['pending']=True
        p.reference_clicked(1,2)
        self.assertEqual(self.controller.calls,[])
        self.assertFalse(p.preset_confirmed.isChecked())
        self.assertIn('失效',p.log.text())

    def test_single_point_mapping_status_does_not_imply_missing_three_points(self):
        p=self.panel
        self.controller.state.update(calibrated=True,points_count=0)
        p.update_state()
        self.assertIn('单点定位有效',p.details.text())
        self.assertNotIn('参考点：0 / 3',p.details.text())
        self.assertFalse(self.controller.calls)
        self.controller.state.update(calibrated=False,points_count=2)
        p.update_state()
        self.assertIn('参考点：2 / 3',p.details.text())
        self.assertNotIn('单点定位有效',p.details.text())

    def test_inverted_preset_payload_and_switch_invalidates_only_preset_mapping(self):
        p=self.panel;p.geometry_id=lambda:'unchanged-page';p.update_state()
        p.preset_confirmed.setChecked(True);p.reference_action='anchor_preset';p.reference_clicked(3,4)
        self.controller.calls=[]
        p.preset_confirmed.setChecked(True)
        p.preset_placement.setCurrentIndex(1)
        self.assertFalse(p.preset_confirmed.isChecked())
        self.assertEqual(self.controller.calls,[('geometry_changed',dict(geometry_id='unchanged-page'))])
        p.preset_confirmed.setChecked(True);p.reference_action='anchor_preset';p.reference_clicked(5,6)
        self.assertEqual(self.controller.calls[-1],('anchor_preset',dict(preset='songboshi_2024_paper_180',page_x=5,page_y=6,geometry_id='unchanged-page',axes_confirmed=True)))
        self.assertFalse(p.preset_confirmed.isChecked())
        self.assertFalse(any(action in ('jog','authorize','start_job') for action,_ in self.controller.calls))

    def test_placement_selection_preserves_independent_three_point_workflow(self):
        p=self.panel;p.geometry_id=lambda:'page-3';p.update_state()
        p.send('fit_calibration',geometry_id='page-3')
        self.controller.state.update(calibrated=True,points_count=3,page_to_machine=(1,0,0,1,2,3))
        before=self.controller.snapshot()
        self.controller.calls=[]
        p.preset_confirmed.setChecked(True);p.preset_placement.setCurrentIndex(1)
        self.assertFalse(p.preset_confirmed.isChecked())
        self.assertEqual(self.controller.calls,[])
        self.assertEqual(self.controller.snapshot()['page_to_machine'],before['page_to_machine'])
        self.assertEqual(self.controller.snapshot()['calibrated'],True)

    def test_snapshot_preset_sources_survive_manual_reanchor_and_failed_fit(self):
        p=self.panel;p.geometry_id=lambda:'same-page';p.update_state()
        self.controller.state.update(mapping_preset='songboshi_2024_paper',anchor_preset_source=None,calibrated=True)
        p.send('fit_calibration',geometry_id='same-page')
        p.preset_placement.setCurrentIndex(1)
        self.assertEqual(self.controller.calls[-1],('geometry_changed',dict(geometry_id='same-page')))
        self.controller.calls=[]
        self.controller.state.update(mapping_preset=None,anchor_preset_source='songboshi_2024_paper',calibrated=False)
        p.send('manual_reposition');p.send('anchor',page_x=1,page_y=2,axes_confirmed=True)
        self.controller.state.update(mapping_preset='songboshi_2024_paper',anchor_preset_source=None,calibrated=True)
        p.preset_placement.setCurrentIndex(0)
        self.assertEqual(self.controller.calls[-1],('geometry_changed',dict(geometry_id='same-page')))

    def test_successful_fit_snapshot_removes_preset_ownership(self):
        p=self.panel;p.geometry_id=lambda:'same-page';p.update_state()
        self.controller.state.update(mapping_preset='songboshi_2024_paper',anchor_preset_source=None,calibrated=True)
        p.update_state();p.send('fit_calibration',geometry_id='same-page')
        self.controller.state.update(mapping_preset=None,anchor_preset_source=None,calibrated=True,points_count=3)
        p.update_state();self.controller.calls=[]
        p.preset_placement.setCurrentIndex(1)
        self.assertEqual(self.controller.calls,[])

class GuidedJobs(PanelTest):
    def setUp(self):
        self.areas=[]
        self.window=SimpleNamespace(project=Project(width=100,height=100,zones=[ExclusionZone(id='z',points=[(40,40),(60,40),(60,60),(40,60)])]),
                                    show_job_area=self.areas.append)
        super().setUp()
        p=self.panel;p.geometry_id=lambda:'page'
        self.answers=[];p.confirm=lambda title,text,accept='':(self.answers.append(text),self.accept)[1]
        self.accept=True
        self.controller.state.update(calibrated=True,page_to_machine=(0,1,-1,0,50,0),wpos=(40,10,-15),pen_up=-15,pen_down=-10,
                                     clearance_bounds=((30,50),(0,20),(-20,-10)))
        p.update_state();self.controller.calls=[]

    def test_test_square_auto_authorizes_its_own_area(self):
        p=self.panel;p.reference_action='test_square';p.reference_clicked(10,10)
        (auth,a),(start,s)=self.controller.calls
        self.assertEqual((auth,start),('authorize','start_job'))
        self.assertEqual(s['polylines'],[[(8,8),(12,8),(12,12),(8,12),(8,8)]])
        self.assertIs(s['preflight_passed'],True);self.assertEqual(s['geometry_id'],'page')
        # Page (8..12) maps to machine X=50-y, Y=x; current pen (40,10) included, 1 mm pad.
        self.assertEqual(a['work_bounds'],(37,7,43,13))
        self.assertEqual(a['z_bounds'],(-20,-10))
        self.assertIs(a['clearance_verified'],True);self.assertIn('压平',a['validation_note'])
        self.assertEqual(self.areas[-1],None);self.assertEqual(len(self.areas[0]),4)
        self.assertIn('4 毫米',self.answers[0])
        self.assertIsNone(p.reference_action)

    def test_test_square_rejects_protected_area_and_page_edge(self):
        p=self.panel
        for point,expected in [((50,50),'保护区'),((1,50),'边缘')]:
            p.reference_action='test_square';p.reference_clicked(*point)
            self.assertIn(expected,p.log.text())
        self.assertEqual(self.controller.calls,[])

    def test_declined_confirmation_sends_nothing(self):
        self.accept=False;p=self.panel
        p.reference_action='test_square';p.reference_clicked(10,10)
        self.assertEqual(self.controller.calls,[]);self.assertIn('已取消',p.log.text())

    def test_manual_bounds_skip_auto_authorization(self):
        p=self.panel;p.manual_bounds.setChecked(True)
        p.reference_action='test_square';p.reference_clicked(10,10)
        self.assertEqual([c[0] for c in self.controller.calls],['start_job'])

    def test_completion_marks_test_and_page_recommends_test_first(self):
        p=self.panel;p.reference_action='test_square';p.reference_clicked(10,10)
        self.controller.events=[dict(kind='complete',text='')];p.update_state()
        self.assertTrue(p.test_done);self.assertEqual(p.step6.state[0],'done');self.assertIn('试写完成',p.log.text())
        p.run_job([[(20,20),(25,20)]],'page')
        self.assertIn('试写的位置和线条正常',self.answers[-1])
    def test_page_without_test_warns_in_confirmation(self):
        self.panel.run_job([[(20,20),(25,20)]],'page')
        self.assertIn('还没有试写',self.answers[-1])
    def test_buttons_follow_wizard_order(self):
        p=self.panel;p.update_state()
        self.assertTrue(p.test_button.isEnabled());self.assertTrue(p.send_button.isEnabled())
        self.controller.state.update(pen_up=None,pen_down=None);p.update_state()
        self.assertFalse(p.test_button.isEnabled());self.assertFalse(p.send_button.isEnabled())
        self.assertEqual(p.step4.state[0],'active')
        self.assertTrue(p.step4.expanded());self.assertFalse(p.step3.expanded());self.assertFalse(p.step5.expanded())
        p.step3.toggle();self.assertTrue(p.step3.expanded())
        self.controller.state.update(state='running');p.update_state()
        self.assertTrue(p.pause_button.isEnabled());self.assertTrue(p.stop_button.isEnabled());self.assertFalse(p.manual_button.isEnabled())
        self.assertTrue(p.step5.expanded());self.assertFalse(p.step6.expanded())

if __name__=='__main__':unittest.main()
