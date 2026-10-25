"""Guided writing wizard. All transport work is owned by the controller thread."""
import math
import time
from PySide6.QtCore import Qt, QTimer, QPointF
from PySide6.QtGui import QPainterPath, QTransform
from PySide6.QtWidgets import (QWidget, QVBoxLayout, QHBoxLayout, QGridLayout, QFormLayout, QLabel, QPushButton,
    QLineEdit, QDoubleSpinBox, QCheckBox, QMessageBox, QComboBox, QFrame, QGroupBox, QButtonGroup)

from .devices import create_controller, get_profile, list_profiles

PRESETS = get_profile('songboshi_2024').presets
TEST_SQUARE_MM = 4.0
IDLE_STATES = {'idle', 'ready', 'complete'}
STATE_LABELS = {'disconnected': '未连接', 'unknown': '等待确认（已锁定）', 'idle': '空闲', 'ready': '就绪',
                'complete': '完成', 'running': '正在书写', 'pausing': '正在暂停', 'paused': '已暂停',
                'stopped': '已停止', 'failed': '故障锁定', 'connecting': '连接中'}
STYLE = """
QFrame#wizardStep { background:white; border:1px solid #e0e5ef; border-radius:8px; }
QFrame#wizardStep[status="active"] { border:2px solid #7197d8; }
QPushButton[primary="true"] { background:#245cad; color:white; border-color:#245cad; font-weight:600; padding:9px 12px; }
QPushButton[primary="true"]:hover { background:#2f6bc2; }
QPushButton[primary="true"]:disabled { background:#b9c7dd; border-color:#b9c7dd; color:#f3f6fb; }
QPushButton[flat_link="true"] { border:0; background:transparent; color:#245cad; text-decoration:underline; padding:2px; }
QPushButton[flat_link="true"]:disabled { color:#a5b0c2; }
QLabel[hint="true"] { color:#5d6b82; }
QLabel#wizardLog { background:#f1f5fc; border-radius:6px; padding:8px 10px; color:#25324b; }
"""


def hint(text):
    label = QLabel(text)
    label.setWordWrap(True)
    label.setProperty('hint', True)
    return label


def spin(low, high, value, suffix=' mm', decimals=2):
    widget = QDoubleSpinBox()
    widget.setRange(low, high)
    widget.setDecimals(decimals)
    widget.setValue(value)
    widget.setSuffix(suffix)
    return widget


def fmt(value):
    return '—' if value is None else f'{value:.2f}'


class Step(QFrame):
    MARKS = '①②③④⑤⑥'

    def __init__(self, number, title):
        super().__init__()
        self.setObjectName('wizardStep')
        self.number, self.title, self.state = number, title, None
        self.user_expanded = None
        outer = QVBoxLayout(self)
        outer.setContentsMargins(12, 10, 12, 10)
        outer.setSpacing(7)
        self.header = QLabel()
        self.header.setCursor(Qt.CursorShape.PointingHandCursor)
        self.header.setToolTip('点击展开 / 收起')
        self.header.mousePressEvent = lambda event: self.toggle()
        outer.addWidget(self.header)
        self.body = QWidget()
        self.box = QVBoxLayout(self.body)
        self.box.setContentsMargins(0, 0, 0, 2)
        self.box.setSpacing(7)
        outer.addWidget(self.body)
        self.auto_expanded = True
        self.set_status('todo')

    def add(self, item):
        (self.box.addLayout if hasattr(item, 'addWidget') and not isinstance(item, QWidget) else self.box.addWidget)(item)
        return item

    def expanded(self):
        return self.auto_expanded if self.user_expanded is None else self.user_expanded

    def toggle(self):
        self.user_expanded = not self.expanded()
        self.refresh()

    def set_expanded(self, expanded):
        """Automatic state; a manual toggle holds until the step status changes."""
        if expanded != self.auto_expanded:
            self.auto_expanded = expanded
        self.refresh()

    def set_status(self, status, detail=''):
        if (status, detail) == self.state:
            return
        if self.state is None or status != self.state[0]:
            self.user_expanded = None
        self.state = (status, detail)
        self.refresh()

    def refresh(self):
        status, detail = self.state
        mark = '✓' if status == 'done' else self.MARKS[self.number-1]
        color = {'done': '#25826c', 'active': '#245cad', 'todo': '#8793a6'}[status]
        arrow = '▾' if self.expanded() else '▸'
        extra = f"　<span style='color:#738299;font-weight:400;font-size:12px'>{detail}</span>" if detail else ''
        self.header.setText(f"<span style='font-size:14px;font-weight:700;color:{color}'>{mark}  {self.title}</span>{extra}"
                            f"<span style='color:#a5b0c2'>　{arrow}</span>")
        self.body.setVisible(self.expanded())
        if self.property('status') != status:
            self.setProperty('status', status)
            self.style().unpolish(self)
            self.style().polish(self)


class DevicePanel(QWidget):
    def __init__(self, window, controller=None, controller_selector=None):
        super().__init__()
        self.window = window
        if controller is None:
            controller = create_controller()
        self.controller = controller
        self.controller_selector = controller_selector
        self.controller.start()
        self.reference_action = None
        self.buttons = {}
        self.current_job = None
        self.test_done = self.page_done = False
        self.advanced = False
        self._preset_request_pending = False
        self._preset_placement_index = 0
        self._preset_context = None
        self._clearance_sent = -math.inf
        self.setStyleSheet(STYLE)
        layout = QVBoxLayout(self)
        layout.setSpacing(10)
        self.status = QLabel('写字机未连接')
        self.status.setWordWrap(True)
        layout.addWidget(self.status)

        # Advanced values are created first; the simple steps read them.
        self.xy_clearance = spin(.1, 10, 10)
        self.z_clearance = spin(.1, 5, 5)
        self.step = spin(.05, 1, .2)
        self.feed = spin(1, 300, 300, ' mm/min', 0)
        self.lift = spin(.5, 5, 2)
        self.press = spin(0, 5, 3)
        self.pen_direction = QComboBox()
        self.pen_direction.addItem('Z+ 降笔（本机已确认）', 1)
        self.pen_direction.addItem('Z− 降笔', -1)

        self.step1 = Step(1, '连接写字机')
        self.profile_selector = QComboBox()
        for profile in list_profiles():
            self.profile_selector.addItem(profile['name'],profile['id'])
            self.profile_selector.setItemData(self.profile_selector.count()-1,profile['description'],Qt.ItemDataRole.ToolTipRole)
        profile_id = self.controller.snapshot().get('device_profile','songboshi_2024')
        self.profile_selector.setCurrentIndex(max(0,self.profile_selector.findData(profile_id)))
        self.profile_selector.currentIndexChanged.connect(self.profile_changed)
        self.step1.add(self.profile_selector)
        row = QHBoxLayout()
        self.port = QComboBox()
        self.port.setEditable(True)
        self.port.addItem('COM3')
        self.refresh_ports()
        self.connect_button = self.make_button('连接', self.connect_device, primary=True)
        self.disconnect_button = self.make_button('断开', lambda: self.send('disconnect'))
        row.addWidget(self.port, 1)
        row.addWidget(self.connect_button)
        row.addWidget(self.disconnect_button)
        self.step1.add(row)
        self.connection_hint = hint('先关掉原厂软件。连接只读取状态，笔不会动。')
        self.step1.add(self.connection_hint)
        self.virtual_controls = QWidget()
        virtual_row = QHBoxLayout(self.virtual_controls)
        virtual_row.setContentsMargins(0,0,0,0)
        self.virtual_prepare_button = QPushButton('准备当前虚拟页面')
        self.virtual_prepare_button.clicked.connect(self.prepare_virtual)
        virtual_row.addWidget(self.virtual_prepare_button)
        self.virtual_lift_button = QPushButton('舵机抬笔')
        self.virtual_lower_button = QPushButton('舵机落笔')
        self.virtual_lift_button.clicked.connect(lambda: self.send('lift_pen'))
        self.virtual_lower_button.clicked.connect(lambda: self.send('lower_pen'))
        virtual_row.addWidget(self.virtual_lift_button)
        virtual_row.addWidget(self.virtual_lower_button)
        self.step1.add(self.virtual_controls)
        self.virtual_controls.hide()

        self.step2 = Step(2, '书怎么放')
        self.preset_placement = QComboBox()
        self.preset_placement.addItem('正着放', 'songboshi_2024_paper')
        self.preset_placement.addItem('倒着放（转了 180°）', 'songboshi_2024_paper_180')
        self.preset_placement.setItemData(0, '机器方向：页面右 = Y+，页面下 = X−', Qt.ItemDataRole.ToolTipRole)
        self.preset_placement.setItemData(1, '机器方向：页面右 = Y−，页面下 = X+', Qt.ItemDataRole.ToolTipRole)
        self.preset_placement.currentIndexChanged.connect(self.preset_placement_changed)
        self.step2.add(self.preset_placement)
        self.preset_confirmed = QCheckBox('我看过了：书确实这样放，已压平')
        self.preset_confirmed.toggled.connect(self.update_state)
        self.step2.add(self.preset_confirmed)
        self.step2.add(hint('屏幕上的照片总是按正常方向显示，倒放由程序换算，不用旋转照片。'))

        self.step3 = Step(3, '对准笔尖')
        self.step3.add(hint('装笔时让笔尖离纸约 1 毫米、不要碰到纸。可先用手把笔头推到要对准的地方附近（1 厘米内），再连接。'))
        self.clearance = QCheckBox('笔尖周围没有障碍')
        self.clearance.setToolTip('笔尖周围 1 厘米、上下 5 毫米内没有书夹、手或其他东西；笔只会在这个范围内移动。')
        self.clearance.toggled.connect(self.clearance_toggled)
        self.step3.add(self.clearance)
        self.step3.add(hint('勾选表示：笔尖周围 1 厘米、上下 5 毫米内没有书夹、手等东西。'))
        pad = QGridLayout()
        pad.setSpacing(4)
        self.page_jog_buttons = {}
        for vector, label, cell in [((0, -1), '↑ 上', (0, 1)), ((-1, 0), '← 左', (1, 0)),
                                    ((1, 0), '右 →', (1, 2)), ((0, 1), '↓ 下', (2, 1))]:
            button = QPushButton(label)
            button.setFixedSize(72, 34)
            button.setProperty('control_role', 'jog')
            button.clicked.connect(lambda checked=False, v=vector: self.page_jog(*v))
            self.page_jog_buttons[vector] = button
            pad.addWidget(button, *cell)
        sizes = QVBoxLayout()
        sizes.setSpacing(2)
        self.fine = QPushButton('细 0.2')
        self.coarse = QPushButton('粗 1')
        self.fine.setToolTip('每次移动 0.2 毫米')
        self.coarse.setToolTip('每次移动 1 毫米')
        group = QButtonGroup(self)
        for button, value in [(self.fine, .2), (self.coarse, 1)]:
            button.setCheckable(True)
            button.setFixedSize(72, 24)
            button.setStyleSheet('padding:1px')
            group.addButton(button)
            button.clicked.connect(lambda checked=False, v=value: self.step.setValue(v))
            sizes.addWidget(button)
        self.fine.setChecked(True)
        self.step.valueChanged.connect(self.sync_step_buttons)
        pad.addLayout(sizes, 1, 1)
        centered = QHBoxLayout()
        centered.addStretch()
        centered.addLayout(pad)
        centered.addStretch()
        self.step3.add(centered)
        self.preset_button = self.make_button('笔尖对准后，在照片上点同一个位置', lambda: self.pick('anchor_preset'), primary=True)
        self.preset_button.setProperty('control_role', 'anchor_preset')
        self.step3.add(self.preset_button)
        self.step3.add(hint('选好认的位置，比如字的拐角、线的端点。'))
        self.manual_button = self.make_button('我用手推过笔头', lambda: self.send('manual_reposition'), link=True)
        self.manual_button.setToolTip('手推没有位置反馈：对准和笔高度会失效，需要重新勾选周围空间并对准。')
        self.step3.add(self.manual_button)
        self.reason3 = self.reason_label(self.step3)

        self.step4 = Step(4, '校准笔的高度')
        self.step4.add(hint('可以在对准之前或之后做。一点一点降笔，直到笔尖刚好碰到纸面，再点「记下」；之后笔会自动抬到离纸 2 毫米，方便对准。会留下一个小点，可先把笔移到不显眼的地方。'))
        # Same confirmation as step 3, so this step can be done first.
        self.clearance4 = QCheckBox('笔尖周围没有障碍')
        self.clearance4.setToolTip(self.clearance.toolTip())
        self.clearance4.toggled.connect(self.clearance_toggled)
        self.step4.add(self.clearance4)
        row = QHBoxLayout()
        self.z_jog_buttons = {}
        for direction in (-1, 1):
            button = QPushButton()
            button.setMinimumHeight(34)
            button.setProperty('control_role', 'jog')
            button.clicked.connect(lambda checked=False, d=direction: self.send('jog', axis='Z', distance=d*min(self.z_step.currentData(), .25), feed=min(self.feed.value(), 30)))
            self.z_jog_buttons[direction] = button
            row.addWidget(button, 1)
        self.step4.add(row)
        row = QHBoxLayout()
        self.z_step = QComboBox()
        for value in (.05, .1, .25):
            self.z_step.addItem(f'每次 {value:g} mm', value)
        self.z_step.setCurrentIndex(1)
        self.height_label = QLabel('当前高度：未知')
        self.height_label.setWordWrap(True)
        row.addWidget(self.z_step)
        row.addWidget(self.height_label, 1)
        self.step4.add(row)
        self.touch_button = self.make_button('刚好碰到纸，记下这个高度', self.record_touch, primary=True)
        self.step4.add(self.touch_button)
        self.touch_result = hint('')
        self.step4.add(self.touch_result)
        self.lift_button = self.make_button('抬笔', lambda: self.send('lift_pen'))
        self.lift_button.setToolTip('把笔抬到记下的抬笔高度（只会向离开纸面的方向移动）。')
        self.step4.add(self.lift_button)
        self.reason4 = self.reason_label(self.step4)
        self.update_z_labels()
        self.pen_direction.currentIndexChanged.connect(self.stroke_settings_changed)

        self.step5 = Step(5, '开始书写')
        self.send_button = self.make_button('开始书写', self.start_job, primary=True)
        self.step5.add(self.send_button)
        self.reason5 = self.reason_label(self.step5)
        row = QHBoxLayout()
        self.pause_button = self.make_button('暂停', lambda: self.send('pause'))
        self.resume_button = self.make_button('继续', lambda: self.send('resume'))
        self.stop_button = self.make_button('停止', lambda: self.send('stop'))
        for button in (self.pause_button, self.resume_button, self.stop_button):
            row.addWidget(button)
        self.step5.add(row)
        self.reset_button = self.make_button('取消设备里剩下的动作…', self.confirm_reset)
        self.step5.add(self.reset_button)

        # Optional check, shown under the write step.
        self.step6 = Step(6, '试写一个小方框')
        self.step6.add(hint(f'可选，建议在正式书写前做：在照片上点一块空白，程序会在那里写一个 {TEST_SQUARE_MM:g} 毫米的小方框，用来检查位置和墨迹。'))
        self.test_button = self.make_button('选位置并试写', lambda: self.pick('test_square'))
        self.step6.add(self.test_button)
        self.reason6 = self.reason_label(self.step6)
        self.steps = [self.step1, self.step2, self.step3, self.step4, self.step5, self.step6]
        for item in self.steps:
            layout.addWidget(item)

        self.log = QLabel('按顺序完成下面的步骤。')
        self.log.setObjectName('wizardLog')
        self.log.setWordWrap(True)
        layout.insertWidget(1, self.log)

        self.advanced_box = QGroupBox('高级设置')
        av = QVBoxLayout(self.advanced_box)
        self.details = QLabel()
        self.details.setWordWrap(True)
        av.addWidget(self.details)
        form = QFormLayout()
        form.addRow('净空 XY ±', self.xy_clearance)
        form.addRow('净空 Z ±', self.z_clearance)
        form.addRow('点动步长', self.step)
        form.addRow('点动速度', self.feed)
        av.addLayout(form)
        self.recheck_button = self.make_button('现场重新确认净空范围…', lambda: self.declare_clearance(True))
        av.addWidget(self.recheck_button)
        row = QHBoxLayout()
        self.axis_jog_buttons = {}
        for axis in 'XY':
            for direction in (-1, 1):
                button = QPushButton(f'{axis} {"−" if direction < 0 else "+"}')
                button.setProperty('control_role', 'jog')
                button.clicked.connect(lambda checked=False, a=axis, d=direction: self.send('jog', axis=a, distance=d*min(self.step.value(), 1), feed=self.feed.value()))
                self.axis_jog_buttons[(axis, direction)] = button
                row.addWidget(button)
        av.addWidget(QLabel('按机器轴点动'))
        av.addLayout(row)
        av.addWidget(QLabel('<b>笔高度</b>'))
        form = QFormLayout()
        form.addRow('移动时离纸', self.lift)
        form.addRow('书写时多压', self.press)
        form.addRow('降笔方向', self.pen_direction)
        self.pen_stroke = spin(.1, 5, 4)
        self.pen_stroke.valueChanged.connect(self.stroke_settings_changed)
        form.addRow('固定抬落行程', self.pen_stroke)
        av.addLayout(form)
        self.stroke_confirmed = QCheckBox('已核对降笔方向与弹簧行程')
        self.stroke_confirmed.setToolTip('只设置高度，不运动。')
        self.stroke_confirmed.toggled.connect(self.update_state)
        av.addWidget(self.stroke_confirmed)
        self.stroke_button = self.make_button('以当前为抬笔，设置固定落笔行程', self.set_pen_stroke)
        self.stroke_button.setProperty('control_role', 'pen_stroke')
        self.stroke_button.setToolTip('读取当前 Z 为抬笔高度，并按所选方向设置落笔高度；此按钮不发送移动命令。')
        av.addWidget(self.stroke_button)
        self.capture_up_button = self.make_button('采集当前 Z 为抬笔高度', lambda: self.send('capture_pen', kind='up'))
        self.capture_down_button = self.make_button('采集当前 Z 为落笔高度', lambda: self.send('capture_pen', kind='down'))
        av.addWidget(self.capture_up_button)
        av.addWidget(self.capture_down_button)
        av.addWidget(QLabel('<b>三点对准（书斜放时）</b>'))
        av.addWidget(hint('用软件点动把笔尖依次移到三个不共线的位置，每次点照片对应点。手推没有位置反馈，不能用来采点。'))
        self.capture_button = self.make_button('点动后：点击照片采集参考点', lambda: self.pick('capture_point'))
        self.fit_button = self.make_button('用三个参考点完成对准', lambda: self.send('fit_calibration', geometry_id=self.geometry_id()))
        self.anchor_button = self.make_button('沿用已验证方向：点击照片重新单点对准', lambda: self.pick('anchor'))
        for button in (self.capture_button, self.fit_button, self.anchor_button):
            av.addWidget(button)
        av.addWidget(QLabel('<b>书写范围与速度</b>'))
        self.manual_bounds = QCheckBox('改用手动工作边界（需先授权）')
        self.manual_bounds.toggled.connect(self.update_state)
        av.addWidget(self.manual_bounds)
        bounds = QFormLayout()
        self.bounds = {}
        for key, label, value in [('left', 'X 最小', 0), ('top', 'Y 最小', 0), ('right', 'X 最大', 100),
                                  ('bottom', 'Y 最大', 100), ('zmin', 'Z 最小', -1), ('zmax', 'Z 最大', 1)]:
            field = spin(-1000, 1000, value)
            self.bounds[key] = field
            bounds.addRow(label, field)
        av.addLayout(bounds)
        speeds = QFormLayout()
        self.write_feed = spin(1, 4000, 3000, ' mm/min', 0)
        self.travel_feed = spin(1, 4000, 4000, ' mm/min', 0)
        self.z_feed = spin(1, 5000, 3000, ' mm/min', 0)
        speeds.addRow('书写 XY 速度', self.write_feed)
        speeds.addRow('抬笔移动 XY 速度', self.travel_feed)
        speeds.addRow('抬落 Z 速度', self.z_feed)
        av.addLayout(speeds)
        self.validation = QLineEdit()
        self.validation.setPlaceholderText('现场核对行程、净空及笔尖的记录')
        av.addWidget(self.validation)
        self.verified = QCheckBox('以上边界、Z 范围和净空已现场验证')
        av.addWidget(self.verified)
        self.authorize_button = self.make_button('授权当前会话书写', self.authorize)
        av.addWidget(self.authorize_button)
        layout.addWidget(self.advanced_box)
        self.advanced_box.setVisible(False)
        layout.addStretch()
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.update_state)
        self.timer.start(100)
        self.populate_profile_presets()

    def populate_profile_presets(self):
        profile=get_profile(self.controller.snapshot().get('device_profile','songboshi_2024'))
        selected=self.preset_placement.currentData()
        self.preset_placement.blockSignals(True)
        self.preset_placement.clear()
        labels={'songboshi_2024_paper':'正着放','songboshi_2024_paper_180':'倒着放（转了 180°）'}
        for preset in profile.presets:
            self.preset_placement.addItem(labels.get(preset,preset),preset)
        if self.preset_placement.findData(selected)>=0: self.preset_placement.setCurrentIndex(self.preset_placement.findData(selected))
        self.preset_placement.blockSignals(False)

    def profile_changed(self, index):
        profile_id=self.profile_selector.itemData(index)
        state=self.controller.snapshot()
        if state.get('connection'):
            self.profile_selector.blockSignals(True)
            self.profile_selector.setCurrentIndex(max(0,self.profile_selector.findData(state.get('device_profile','songboshi_2024'))))
            self.profile_selector.blockSignals(False)
            return
        if self.controller_selector is not None:
            self.controller=self.controller_selector(profile_id)
        else:
            self.controller.close()
            self.controller=create_controller(profile_id)
        self.controller.start()
        self.populate_profile_presets()
        self.current_job=None; self.test_done=self.page_done=False
        self.clear_preset_confirmation()
        self.update_state()

    def connect_device(self):
        if self.profile_selector.currentData()=='simulator_servo':
            self.send('connect')
            self.prepare_virtual()
        else:
            self.send('connect',port=self.port.currentText().strip())

    def prepare_virtual(self):
        if self.window is None:
            self.log.setText('请先打开一个页面。')
            return
        project=self.window.project
        self.send('prepare_virtual',geometry_id=self.geometry_id(),work_bounds=(0,0,project.width,project.height),
                  write_feed=self.write_feed.value(),travel_feed=self.travel_feed.value())

    def make_button(self, label, callback, primary=False, link=False):
        button = QPushButton(label)
        button.clicked.connect(callback)
        if primary:
            button.setProperty('primary', True)
        if link:
            button.setProperty('flat_link', True)
            button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.buttons[label] = button
        return button

    def set_advanced(self, enabled):
        self.advanced = bool(enabled)
        self.advanced_box.setVisible(self.advanced)
        self.update_state()

    def refresh_ports(self):
        lister = getattr(type(self.controller), 'device_list', None)
        if lister is None:
            return
        try:
            ports = [item['port'] for item in lister()]
        except Exception:
            return
        for name in ports:
            if self.port.findText(name) < 0:
                self.port.addItem(name)
        if self.port.currentText() not in ports and len(ports) == 1:
            self.port.setCurrentText(ports[0])

    def sync_step_buttons(self, value):
        self.fine.setChecked(abs(value-.2) < 1e-9)
        self.coarse.setChecked(abs(value-1) < 1e-9)

    def stroke_settings_changed(self, *args):
        self.stroke_confirmed.setChecked(False)
        self.update_z_labels()
        self.update_state()

    def update_z_labels(self):
        down = self.pen_direction.currentData()
        for direction, button in self.z_jog_buttons.items():
            action = '▼ 降笔' if direction == down else '▲ 抬笔'
            button.setText(f'{action}一点（Z{"+" if direction > 0 else "−"}）')

    @staticmethod
    def reason_label(step):
        label = hint('')
        label.setStyleSheet('color:#a8661c')
        step.add(label)
        return label

    def set_clearance_boxes(self, checked):
        for box in (self.clearance, self.clearance4):
            box.blockSignals(True)
            box.setChecked(checked)
            box.blockSignals(False)

    def clearance_toggled(self, checked):
        # Steps 3 and 4 show the same confirmation; keep both boxes identical.
        self.set_clearance_boxes(checked)
        if checked:
            self._clearance_sent = time.monotonic()
            self.declare_clearance(False)

    def page_linear(self):
        mapping = self.controller.snapshot().get('page_to_machine')
        if mapping: return tuple(mapping[:4])
        profile=get_profile(self.controller.snapshot().get('device_profile','songboshi_2024'))
        preset=profile.presets.get(self.preset_placement.currentData())
        if preset is None: raise ValueError('当前设备没有已验证方向，请先三点对准。')
        return preset

    def page_jog(self, dx, dy):
        """Jog along a page direction; the dominant machine axis carries the step."""
        a, b, c, d = self.page_linear()
        mx, my = a*dx+c*dy, b*dx+d*dy
        axis, amount = ('X', mx) if abs(mx) >= abs(my) else ('Y', my)
        self.send('jog', axis=axis, distance=math.copysign(min(self.step.value(), 1), amount), feed=self.feed.value())

    def set_pen_stroke(self):
        self.send('set_pen_stroke', distance=self.pen_stroke.value(), direction=self.pen_direction.currentData(), confirmed=self.stroke_confirmed.isChecked())

    def record_touch(self):
        self.send('set_pen_touch', lift=self.lift.value(), press=self.press.value(), direction=self.pen_direction.currentData(), confirmed=True)
        self.send('lift_pen')
        self.log.setText('已记下碰纸高度，正在把笔抬起。')

    def confirm(self, title, text, accept='确认开始'):
        box = QMessageBox(QMessageBox.Icon.Question, title, text, parent=self)
        ok = box.addButton(accept, QMessageBox.ButtonRole.AcceptRole)
        cancel = box.addButton('取消', QMessageBox.ButtonRole.RejectRole)
        box.setDefaultButton(cancel)
        box.exec()
        return box.clickedButton() is ok

    def declare_clearance(self, renew):
        if renew and not self.confirm('重新确认净空', '以当前笔位重新确认周围空间？对准、笔高度和书写授权都会失效。', '重新确认'):
            return
        self.send('declare_clearance', xy_mm=self.xy_clearance.value(), z_mm=self.z_clearance.value(), confirmed_new_range=renew)

    def authorization(self, work_bounds, z_bounds, note, verified):
        return dict(work_bounds=work_bounds, z_bounds=z_bounds, write_feed=self.write_feed.value(),
                    travel_feed=self.travel_feed.value(), z_feed=self.z_feed.value(), max_write_feed=4000,
                    max_travel_feed=4000, max_z_feed=5000, clearance_verified=verified, validation_note=note)

    def authorize(self):
        v = {key: field.value() for key, field in self.bounds.items()}
        self.send('authorize', **self.authorization((v['left'], v['top'], v['right'], v['bottom']), (v['zmin'], v['zmax']),
                                                    self.validation.text(), self.verified.isChecked()))

    def start_job(self):
        if self.window is None:
            return
        from .core import preflight, all_subpaths
        project = self.window.project
        if any(getattr(block, 'writing_mode', 'outline') != 'single_line' for block in project.blocks):
            self.log.setText('真实书写只能用单线字体。请在「批注」里给所有文字选择单线字体。')
            return
        if preflight(project):
            self.log.setText('页面检查没通过（文字碰到保护区或超出页面），请先在「批注」里调整。')
            return
        paths = all_subpaths(project)
        if not paths:
            self.log.setText('页面上还没有要写的文字。')
            return
        self.run_job(paths, 'page')

    def test_square_paths(self, x, y):
        from .core import zone_path, stroke_envelope
        project = self.window.project
        half = TEST_SQUARE_MM/2
        points = [(x-half, y-half), (x+half, y-half), (x+half, y+half), (x-half, y+half), (x-half, y-half)]
        if not all(0 <= px <= project.width and 0 <= py <= project.height for px, py in points):
            self.log.setText('这里太靠近页面边缘，请换一块空白。')
            return None
        path = QPainterPath(QPointF(*points[0]))
        for point in points[1:]:
            path.lineTo(QPointF(*point))
        envelope = stroke_envelope(path, project.pen_width+2*project.margin)
        if any(envelope.intersects(zone_path(zone)) for zone in project.zones):
            self.log.setText('这里会碰到保护区，请换一块空白。')
            return None
        return [points]

    def job_bounds(self, paths, pad=1.0):
        """Machine work area for this job: its own strokes plus the current pen position."""
        state = self.controller.snapshot()
        mapping, position, clearance = state.get('page_to_machine'), state.get('wpos'), state.get('clearance_bounds')
        if not mapping or position is None:
            self.log.setText('当前坐标/页面映射未知，拒绝发送。')
            return None
        if not clearance:
            self.log.setText('请先在第 3 步勾选「笔尖周围没有障碍」。')
            return None
        transform = QTransform(*mapping)
        inverse, ok = transform.inverted()
        if not ok:
            self.log.setText('页面映射不可逆。')
            return None
        page = [point for path in paths for point in path]
        pen = inverse.map(QPointF(position[0], position[1]))
        page.append((pen.x(), pen.y()))
        machine = [transform.map(QPointF(*point)) for point in page]
        xs, ys = [p.x() for p in machine], [p.y() for p in machine]
        px, py = [p[0] for p in page], [p[1] for p in page]
        page_rect = (min(px)-pad, min(py)-pad, max(px)-min(px)+2*pad, max(py)-min(py)+2*pad)
        return (min(xs)-pad, min(ys)-pad, max(xs)+pad, max(ys)+pad), tuple(clearance[2]), page_rect

    def run_job(self, paths, kind):
        if self.controller.snapshot().get('simulation'):
            if not self.initial_travel_clear(paths): return
            self.current_job=kind; self.page_done=False
            self.prepare_virtual()
            self.send('start_job',polylines=paths,geometry_id=self.geometry_id(),preflight_passed=True)
            self.log.setText('正在离线模拟，未连接真实设备。')
            return
        if not self.initial_travel_clear(paths):
            return
        bounds = self.job_bounds(paths)
        if bounds is None:
            return
        machine, z_range, page_rect = bounds
        checks = ['书已压平，对准后没有再动过', '照片上蓝色虚线框里没有书夹、手或其他东西']
        if kind == 'test':
            title, text = '试写确认', f'将在点选位置写一个 {TEST_SQUARE_MM:g} 毫米小方框。'
        else:
            title, text = '开始书写', '将按页面上的蓝色笔迹书写。'
            checks.append('笔有墨，试写的位置和线条正常' if self.test_done else '还没有试写——建议先用下面第 6 步试写一个小方框')
        text += '\n\n请确认：\n' + '\n'.join('•  '+item for item in checks) + '\n\n书写中可随时点「暂停」或「停止」。'
        show = getattr(self.window, 'show_job_area', None)
        if show:
            show(page_rect)
        try:
            accepted = self.confirm(title, text)
        finally:
            if show:
                show(None)
        if not accepted:
            self.log.setText('已取消，没有发送。')
            return
        if not self.manual_bounds.isChecked():
            note = f"{time.strftime('%Y-%m-%d %H:%M:%S')} 书写前确认：" + '；'.join(checks)
            self.send('authorize', **self.authorization(machine, z_range, note, True))
        self.current_job = kind
        self.page_done = False
        self.send('start_job', polylines=paths, geometry_id=self.geometry_id(), preflight_passed=True)
        self.log.setText('正在试写…' if kind == 'test' else '正在书写…')

    def initial_travel_clear(self, paths):
        from .core import zone_path, stroke_envelope
        state = self.controller.snapshot()
        mapping = state.get('page_to_machine')
        position = state.get('wpos')
        if not mapping or position is None or not paths:
            self.log.setText('当前坐标/页面映射未知，拒绝发送。')
            return False
        inverse, ok = QTransform(*mapping).inverted()
        if not ok:
            self.log.setText('页面映射不可逆。')
            return False
        page = inverse.map(QPointF(position[0], position[1]))
        route = QPainterPath(page)
        route.lineTo(QPointF(*paths[0][0]))
        project = self.window.project
        envelope = stroke_envelope(route, project.pen_width+2*project.margin)
        if any(zone.kind == 'obstacle' and envelope.intersects(zone_path(zone)) for zone in project.zones):
            self.log.setText('当前位置到第一笔的抬笔路线穿越实体障碍，请安全重新定位。')
            return False
        return True

    def send(self, action, **kwargs):
        if action in {'connect', 'disconnect', 'geometry_changed', 'manual_reposition'}:
            self.clear_preset_confirmation()
        if action in {'connect', 'disconnect', 'geometry_changed'}:
            self._preset_request_pending = False
        try:
            if action == 'start_job' and self.window is not None and hasattr(self.window, 'ensure_application_service'):
                self.window.ensure_application_service().jobs.submit_operator_paths(
                    kwargs['polylines'], kwargs['geometry_id'], kind=self.current_job or 'page')
            else:
                self.controller.submit(action, **kwargs)
        except Exception as error:
            self.log.setText(self.translate_error(str(error)))

    def geometry_id(self):
        from .document_service import geometry_identity
        return geometry_identity(self.window.project)

    def preset_placement_changed(self, index):
        state = self.controller.snapshot()
        if state.get('connection') and state.get('state') not in IDLE_STATES:
            self.preset_placement.blockSignals(True)
            self.preset_placement.setCurrentIndex(self._preset_placement_index)
            self.preset_placement.blockSignals(False)
            self.log.setText('请先结束当前设备操作，再更改书的摆放。')
            return
        self._preset_placement_index = index
        self.clear_preset_confirmation()
        if state.get('mapping_preset') or state.get('anchor_preset_source') or self._preset_request_pending:
            self.send('geometry_changed', geometry_id=self.geometry_id())
            self._preset_request_pending = False
            self.log.setText('书的摆放改了，之前的对准和笔高度已失效；请确认后重新对准。')
        else:
            self.log.setText('请确认书确实这样放，再勾选第 2 步。')
        self.update_state()

    def clear_preset_confirmation(self):
        if not hasattr(self, 'preset_confirmed'):
            return
        self.preset_confirmed.blockSignals(True)
        self.preset_confirmed.setChecked(False)
        self.preset_confirmed.blockSignals(False)
        if self.reference_action == 'anchor_preset':
            self.reference_action = None

    @staticmethod
    def fresh_idle(state):
        # A routine status query in flight is not "busy": the controller waits
        # for its reply before running the action (see WAIT_FOR_STATUS).
        return bool(state.get('connection') and state.get('state') in IDLE_STATES and not state.get('pending')
                    and state.get('coordinate_age', math.inf) <= 2)

    def pick(self, action):
        if action == 'anchor_preset' and not (self.preset_confirmed.isChecked() and self.fresh_idle(self.controller.snapshot())):
            self.log.setText('请等写字机空闲，并确认第 2 步书的摆放。')
            return
        self.reference_action = action
        if self.window is not None:
            self.window.canvas.editing_id = None
            self.window.canvas.set_mode('reference')
            if action == 'test_square':
                self.window.statusBar().showMessage('点击照片中的一块空白，作为试写小方框的中心')
        self.log.setText('请在照片上点击位置。' if action != 'test_square' else '请在照片上点一块空白。')

    def reference_clicked(self, x, y):
        if self.reference_action == 'anchor_preset':
            self.update_state()
        action = self.reference_action
        if action == 'anchor_preset':
            confirmed = self.preset_confirmed.isChecked()
            fresh = self.fresh_idle(self.controller.snapshot())
            self.clear_preset_confirmation()
            if not (confirmed and fresh):
                self.log.setText('本次摆放确认已失效；请重新勾选第 2 步，等写字机空闲后再点。')
                return
            self.send('anchor_preset', preset=self.preset_placement.currentData(), page_x=x, page_y=y,
                      geometry_id=self.geometry_id(), axes_confirmed=True)
            self._preset_request_pending = True
            pens = self.controller.snapshot().get('pen_up') is not None
            self.log.setText('已对准（笔没有移动），已记下的笔高度保留。' if pens else '已对准（笔没有移动）。请继续第 4 步校准笔的高度。')
            return
        if action == 'test_square':
            self.reference_action = None
            paths = self.test_square_paths(x, y)
            if paths:
                self.run_job(paths, 'test')
            return
        if action is None:
            self.log.setText('参考点操作已失效，请重新选择定位方式。')
            return
        kwargs = {'page_x': x, 'page_y': y}
        if action == 'anchor':
            kwargs['axes_confirmed'] = True
        self.send(action, **kwargs)
        self.log.setText(f'页面参考点：{x:.3f}, {y:.3f} mm；等待控制器确认。')

    def confirm_reset(self):
        if self.confirm('取消设备里剩下的动作', '确认笔尖和书都安全后，取消写字机里还没执行的动作？之后需要重新对准和设置笔高度。', '取消剩下的动作'):
            self.send('confirm_reset')

    def drain_messages(self):
        messages, problem = [], None
        for event in self.controller.drain_events():
            if not isinstance(event, dict):
                messages.append(str(event))
                continue
            kind, text = event.get('kind'), event.get('text', '')
            if kind == 'rejected' and event.get('action') == 'anchor_preset':
                self._preset_request_pending = False
            if kind in {'rx', 'tx'}:
                continue
            if kind == 'complete':
                if event.get('simulation'):
                    self.test_done = self.current_job == 'test' or self.test_done
                    self.page_done = self.current_job == 'page' or self.page_done
                    self.current_job = None
                    messages.append('离线模拟完成（未连接真实设备）。')
                    continue
                if self.current_job == 'test':
                    self.test_done = True
                    messages.append('试写完成。位置对、线条完整吗？不对就回到第 3 / 4 步调整。')
                elif self.current_job == 'page':
                    self.page_done = True
                    messages.append('书写完成。')
                self.current_job = None
                continue
            if text:
                message = self.translate_error(text)
                if kind in {'rejected', 'fault'} and problem is None:
                    problem = message
                messages.append(message)
        if problem or messages:
            self.log.setText(problem or messages[-1])

    def update_state(self):
        try:
            self.drain_messages()
            state = self.controller.snapshot()
            simulation=state.get('simulation',False)
            self.virtual_controls.setVisible(simulation)
            for step in (self.step2,self.step3,self.step4): step.setVisible(not simulation)
            self.profile_selector.setEnabled(not state.get('connection',False))
            self.connection_hint.setText('纯内存 XY / 舵机模拟；笔的位置和完成状态均为虚拟模型。' if simulation else get_profile(state.get('device_profile','songboshi_2024')).description)
            if simulation:
                self.update_simulation_state(state)
                return
            self.port.setVisible(True)
            profile = get_profile(state.get('device_profile','songboshi_2024'))
            has_preset = bool(profile.presets)
            self.step2.setVisible(has_preset)
            if not has_preset:
                self.advanced_box.setVisible(True)
            if state.get('mapping_preset') or state.get('anchor_preset_source'):
                self._preset_request_pending = False
            geometry = self.geometry_id() if self.window is not None else None
            context = (state.get('connection'), state.get('session_id'), state.get('geometry_id'), geometry, self.preset_placement.currentData())
            if self._preset_context is not None and context != self._preset_context:
                self.clear_preset_confirmation()
            self._preset_context = context
            connected = state.get('connection', False)
            machine_state = state.get('state')
            busy = state.get('pending', False)
            fresh = state.get('coordinate_age', math.inf) <= 2
            # Not including the status query in flight every 0.5 s: sampling
            # it made every button blink grey now and then.
            idle = connected and machine_state in IDLE_STATES and not busy and fresh
            calibrated = state.get('calibrated', False)
            up, down, touch = state.get('pen_up'), state.get('pen_down'), state.get('pen_touch')
            pens = up is not None and down is not None
            if 'clearance' in state and state['clearance'] is None and self.clearance.isChecked() and time.monotonic()-self._clearance_sent > 1:
                self.set_clearance_boxes(False)
            cleared = self.clearance.isChecked()
            if not (calibrated and pens):
                self.test_done = False
            position = state.get('wpos')
            label = STATE_LABELS.get(machine_state, '等待设备确认')
            where = '' if position is None else '<br><span style="color:#738299">笔位置  ' + '  '.join(f'{a} {v:.2f}' for a, v in zip('XYZ', position)) + ' mm</span>'
            self.status.setText(f"<b>写字机：已连接</b> · {label}{where}" if connected else "<b>写字机：未连接</b>")
            reference = '单点定位有效' if calibrated and state.get('points_count', 0) == 0 else f"参考点：{state.get('points_count', 0)} / 3"
            progress = state.get('progress', (0, 0))
            self.details.setText(f"{reference}；页面映射：{'有效' if calibrated else '无效'}\n碰纸 Z：{fmt(touch)}；抬笔 Z：{fmt(up)}；落笔 Z：{fmt(down)}\n"
                                 f"净空：{state.get('clearance') or '未声明'}；授权：{'有效' if state.get('authorized') else '无'}\n确认进度：{progress[0]} / {progress[1]}")
            if position is None:
                self.height_label.setText('当前高度：未知')
            else:
                room = state.get('clearance_remaining')
                extra = ''
                if room:
                    below, above = room[2]
                    extra = f"　还能往下 {above if self.pen_direction.currentData() > 0 else below:.2f} mm"
                self.height_label.setText(f'当前高度 Z {position[2]:.2f} mm{extra}')
            if touch is not None and pens:
                self.touch_result.setText(f'已记下：碰纸高度 Z {touch:.2f}。移动时抬到 {up:.2f}（离纸 {abs(touch-up):g} mm），'
                                          f'书写时落到 {down:.2f}（多压 {abs(down-touch):g} mm，靠弹簧缓冲）。')
            elif pens:
                self.touch_result.setText(f'已设置：抬笔 Z {up:.2f}，落笔 Z {down:.2f}。')
            else:
                self.touch_result.setText('')
            self.touch_result.setVisible(bool(self.touch_result.text()))
            done = [connected, self.preset_confirmed.isChecked() or calibrated, calibrated, pens, self.page_done, self.test_done]
            details = [label if connected else '', self.preset_placement.currentText() if done[1] else '',
                       '已对准' if calibrated else '', f'碰纸 Z {touch:.2f}' if touch is not None and pens else ('已设置' if pens else ''),
                       f'已写 {progress[0]} / {progress[1]}' if progress[1] else '', '已试写' if self.test_done else '可选，建议先做']
            # Step 6 (test) is optional: it never becomes the current step.
            active = next((i for i, value in enumerate(done[:5]) if not value), 4)
            running = machine_state in {'running', 'pausing', 'paused'}
            for index, item in enumerate(self.steps):
                item.set_status('done' if done[index] else 'active' if index == active else 'todo', details[index])
                write_open = active == 4 or running or machine_state == 'stopped'
                item.set_expanded(index == active or (index == 4 and write_open) or (index == 5 and active == 4 and not running))
            self.connect_button.setEnabled(not connected)
            self.disconnect_button.setEnabled(connected)
            self.port.setEnabled(not connected)
            self.preset_placement.setEnabled(not connected or idle)
            self.clearance.setEnabled(connected)
            self.clearance4.setEnabled(connected)
            for button in [*self.page_jog_buttons.values(), *self.axis_jog_buttons.values(), *self.z_jog_buttons.values()]:
                button.setEnabled(idle and cleared)
            self.preset_button.setEnabled(idle and self.preset_confirmed.isChecked())
            self.manual_button.setEnabled(connected and not running and not busy)
            self.touch_button.setEnabled(idle and cleared)
            self.lift_button.setEnabled(idle and pens)
            self.test_button.setEnabled(idle and calibrated and pens and self.window is not None)
            authorized_ok = state.get('authorized', False) if self.manual_bounds.isChecked() else True
            self.send_button.setEnabled(idle and calibrated and pens and authorized_ok)
            self.pause_button.setEnabled(machine_state == 'running')
            self.resume_button.setEnabled(machine_state == 'paused')
            self.stop_button.setEnabled(connected and running)
            self.reset_button.setEnabled(connected)
            self.reset_button.setVisible(self.advanced or (connected and machine_state in {'stopped', 'unknown'}))
            self.stroke_button.setEnabled(idle and self.stroke_confirmed.isChecked())
            for button in (self.capture_up_button, self.capture_down_button, self.recheck_button, self.capture_button):
                button.setEnabled(idle)
            self.fit_button.setEnabled(idle and state.get('points_count', 0) >= 3)
            self.anchor_button.setEnabled(idle and state.get('anchor_available', False))
            self.authorize_button.setEnabled(idle and calibrated and pens)
            # Say why a step's buttons are grey instead of leaving it a puzzle.
            def why(*checks):
                return next((message for ok, message in checks if not ok), '')
            base = [(connected, '先在第 1 步连接写字机。'),
                    (idle or running, '写字机还在动或状态没刷新，稍等一下。')]
            reasons = {
                self.reason3: why(*base, (cleared, '先勾选「笔尖周围没有障碍」，箭头才能移动笔。'),
                                  (self.preset_confirmed.isChecked() or calibrated, '先在第 2 步确认书的摆放，才能在照片上点位置。')),
                self.reason4: why(*base, (cleared, '先勾选上面的「笔尖周围没有障碍」。')),
                self.reason5: '' if running else why(*base, (calibrated, '先完成第 3 步对准。'), (pens, '先完成第 4 步笔的高度。'),
                                                     (authorized_ok, '高级设置里选了手动边界，需先授权。')),
                self.reason6: '' if running else why(*base, (calibrated, '先完成第 3 步对准。'), (pens, '先完成第 4 步笔的高度。')),
            }
            for label, text in reasons.items():
                label.setText(text)
                label.setVisible(bool(text))
            self.preset_placement.setEnabled(has_preset and (not connected or idle))
            self.preset_confirmed.setEnabled(has_preset)
            self.preset_button.setEnabled(has_preset and idle and self.preset_confirmed.isChecked())
            if not has_preset and not calibrated:
                for button in self.page_jog_buttons.values(): button.setEnabled(False)
                self.reason3.setText('当前配置无已验证轴向预设；请用高级设置的机器轴点动与三点对准。')
                self.reason3.setVisible(True)
        except Exception as error:
            self.log.setText(self.translate_error(str(error)))

    def update_simulation_state(self,state):
        connected=state.get('connection',False)
        machine_state=state.get('state')
        idle=connected and machine_state in IDLE_STATES and not state.get('pending')
        prepared=state.get('calibrated') and state.get('authorized')
        position=state.get('wpos')
        where='' if position is None else f" · X {position[0]:.2f} / Y {position[1]:.2f} mm"
        self.status.setText(f"<b>离线模拟器</b> · {STATE_LABELS.get(machine_state,machine_state)}{where} · 舵机笔 {state.get('pen_state','up')}")
        self.details.setText('虚拟 XY 坐标；舵机抬落笔；没有 Z 轴。')
        self.port.setVisible(False); self.port.setEnabled(False)
        self.connect_button.setEnabled(not connected); self.disconnect_button.setEnabled(connected)
        self.virtual_prepare_button.setEnabled(connected and not state.get('pending') and machine_state in IDLE_STATES|{'stopped'})
        self.virtual_lift_button.setEnabled(idle and bool(prepared)); self.virtual_lower_button.setEnabled(idle and bool(prepared))
        self.send_button.setEnabled(idle and bool(prepared) and self.window is not None)
        self.test_button.setEnabled(idle and bool(prepared) and self.window is not None)
        self.pause_button.setEnabled(machine_state=='running'); self.resume_button.setEnabled(machine_state=='paused')
        self.stop_button.setEnabled(connected and machine_state in {'running','waiting_idle','paused'})
        self.reset_button.hide(); self.advanced_box.hide()
        self.reason5.setText('' if prepared else '连接模拟器后，点「准备当前虚拟页面」。')
        self.reason5.setVisible(not prepared); self.reason6.hide()
        progress=state.get('progress',(0,0))
        self.step1.set_status('done' if connected else 'active','纯内存设备')
        self.step5.set_status('active' if prepared else 'todo',f'模拟确认 {progress[0]} / {progress[1]}')
        self.step5.set_expanded(True)
        self.step6.set_expanded(False)

    TRANSLATIONS = {
        'Confirmed finite stroke and direction +/-1 required': '请勾选已核对降笔方向与弹簧行程，并选择有效方向和行程。',
        'Confirmed touch with finite lift/press and direction +/-1 required': '离纸 / 多压距离无效，请在高级设置中检查。',
        'Declare inspected clearance first': '请先在第 3 步勾选「笔尖周围没有障碍」。',
        'Declare physical clearance first': '请先在第 3 步勾选「笔尖周围没有障碍」。',
        'Pen stroke exceeds declared Z envelope': '落笔行程超出这次确认的上下范围；请减小行程，或重新确认周围空间。',
        'Pen touch heights exceed declared Z envelope': '笔离纸太远：按设定的离纸 / 多压距离会超出这次确认的上下 5 毫米。请点「我用手推过笔头」，把笔装到离纸约 1 毫米后重新对准。',
        'Pen heights required before lifting': '还没有记下笔的高度。',
        'Lift must move away from the paper': '笔已经在抬笔高度之上，不用再抬。',
        'Disconnected': '写字机未连接。',
        'Fresh Idle coordinates required': '写字机还在动或状态没刷新，请稍等再点。',
        'Fresh millimetre work coordinates and WCO required': '写字机还在动或状态没刷新，请稍等再点。',
        'Fresh Idle and empty command/status queues required': '写字机还在动或状态没刷新，请稍等再点。',
        'Drain status query before jogging': '写字机还在动或状态没刷新，请稍等再点。',
        'Jog exceeds inspected displacement envelope or single step': '已经到这次确认范围的边上（1 厘米 / 上下 5 毫米）。要走更远，请点「我用手推过笔头」，用手推过去再重新对准。',
        'Jog exceeds initial step or cumulative clearance': '已经到这次确认范围的边上，请现场复核。',
        'Runtime XY and Z captures required': '请先完成对准（第 3 步）和笔高度（第 4 步）。',
        'Current runtime authorization required': '书写准备已失效，请重新点「开始」。',
        'Current position is outside runtime authorized bounds': '笔现在的位置超出书写范围，请重新对准。',
        'Confirmed previous axes required': '没有可沿用的已验证方向，请重新对准。',
        'Manual movement is not measured by the controller': '手推没有位置反馈：对准和笔高度已失效。请重新勾选周围空间，再对准一次。',
        'Manual movement is not measured by the controller; inspect and declare clearance again': '已记下你手推过笔头：对准和笔高度已失效。请重新勾选周围空间，再对准一次。',
        'New physical range needs explicit confirmation; calibration will be invalidated': '已经确认过周围空间；如需重新确认，请用高级设置里的「现场重新确认净空范围」。',
        'Wait for outstanding status before starting job': '等状态刷新后再开始。',
        'Connection found active device; inspect and explicitly recover': '连接时发现写字机正在动作。请等它停下后断开再连接。',
    }

    @classmethod
    def translate_error(cls, text):
        return cls.TRANSLATIONS.get(text, '设备提示：'+text)

    def shutdown(self):
        self.timer.stop()
        self.controller.close()
