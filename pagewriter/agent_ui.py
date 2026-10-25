"""Desktop attachment for the shared service and operator-only motion grants."""
from pathlib import Path
import json
import time
import uuid
import copy
from dataclasses import asdict, is_dataclass

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import (QApplication, QDialog, QFileDialog, QFormLayout, QHBoxLayout,
    QInputDialog, QLabel, QLineEdit, QMessageBox, QPushButton, QVBoxLayout)

from .service_errors import ServiceError


def grant_context(service):
    """Physical and document evidence inspected by an operator dialog."""
    from .document_service import fingerprint
    service.documents.observe()
    state = service.controller.snapshot()
    keys = ('session_id', 'device_profile', 'geometry_id', 'page_to_machine', 'wpos',
            'wco', 'pen_up', 'pen_down', 'pen_touch', 'clearance', 'capabilities')
    return {'revision': service.documents.revision, 'fingerprint': fingerprint(service.documents.project),
            'state': {key: copy.deepcopy(state.get(key)) for key in keys}}


def check_grant_context(service, controller, expected):
    if service.controller is not controller or grant_context(service) != expected:
        raise ServiceError('STALE_CONFIRMATION', '确认期间设备、位置或页面已改变；请重新核对范围')


def check_authorization(controller, authorization_id, params):
    state = controller.snapshot()
    if state.get('authorization_id') != authorization_id:
        raise ServiceError('AUTHORIZATION_PENDING', '等待本次范围授权确认')
    profile = getattr(controller, 'profile', None)
    profile = asdict(profile) if is_dataclass(profile) else profile
    expected = json.dumps(params, sort_keys=True, allow_nan=False)
    actual = json.dumps({key: profile.get(key) for key in params}, sort_keys=True, allow_nan=False) if isinstance(profile, dict) else ''
    if not state.get('authorized') or actual != expected:
        raise ServiceError('AUTHORIZATION_MISMATCH', '设备未接受本次指定范围，未授予 Agent 权限')


def ensure_service(window):
    service = getattr(window, 'application_service', None)
    if service is None or service.closed:
        from .application import ApplicationService
        workspace = getattr(window, '_agent_workspace', Path.cwd())
        service = ApplicationService(window=window, controller=window.device_panel.controller,
            workspace=workspace, allow_hardware=True)
        window.application_service = service
        timer = QTimer(window)
        timer.setInterval(50)
        def poll():
            try:
                service.poll()
            except ServiceError as exc:
                window.statusBar().showMessage(exc.message)
        timer.timeout.connect(poll); timer.start()
        window.application_service_timer = timer
    return service


def start_api(window, connection_file=None, port=0):
    handle = getattr(window, 'agent_api', None)
    if handle is not None:
        return handle
    from .interfaces.http import start_api as start
    service = ensure_service(window)
    connection = connection_file or service.state_dir / 'connection.json'
    handle = start(service, connection_file=connection, port=port)
    window.agent_api = handle
    return handle


def stop_api(window):
    handle = getattr(window, 'agent_api', None)
    if handle is not None:
        handle.close(); window.agent_api = None
    service = getattr(window, 'application_service', None)
    if service is not None:
        service.jobs.revoke_motion()


def shutdown(window):
    stop_api(window)
    timer = getattr(window, 'application_service_timer', None)
    if timer is not None:
        timer.stop()
    service = getattr(window, 'application_service', None)
    if service is not None:
        service.close()


class AgentDialog(QDialog):
    def __init__(self, window):
        super().__init__(window)
        self.window = window
        self.setWindowTitle('Agent 接口')
        self.resize(650, 330)
        layout = QVBoxLayout(self)
        description = QLabel('Agent 与当前编辑器共享页面、预览和作业。接口只监听本机；连接文件包含凭据，请仅交给你信任的 Agent。')
        description.setWordWrap(True); layout.addWidget(description)
        form = QFormLayout()
        self.workspace = QLineEdit(str(getattr(window, '_agent_workspace', Path.cwd())))
        service = getattr(window, 'application_service', None)
        if service is not None:
            self.workspace.setText(str(service.workspace)); self.workspace.setReadOnly(True)
        browse = QPushButton('选择目录')
        self.browse_button = browse
        browse.setEnabled(service is None)
        def choose():
            selected = QFileDialog.getExistingDirectory(self, '允许 Agent 访问的工作目录', self.workspace.text())
            if selected: self.workspace.setText(selected)
        browse.clicked.connect(choose)
        row = QHBoxLayout(); row.addWidget(self.workspace, 1); row.addWidget(browse)
        form.addRow('工作目录', row)
        self.connection = QLineEdit(); self.connection.setReadOnly(True)
        form.addRow('连接文件', self.connection)
        layout.addLayout(form)
        row = QHBoxLayout()
        self.start_button = QPushButton('启用本机接口')
        self.stop_button = QPushButton('关闭接口并撤销授权')
        copy_button = QPushButton('复制 MCP 配置')
        row.addWidget(self.start_button); row.addWidget(self.stop_button); row.addWidget(copy_button)
        layout.addLayout(row)
        self.status = QLabel(); self.status.setWordWrap(True); layout.addWidget(self.status)
        self.grant_button = QPushButton('授权 Agent 操作当前页面范围 · 15分钟')
        self.grant_button.clicked.connect(self.grant_page)
        layout.addWidget(self.grant_button)
        resolve = QPushButton('核实并归档上次未确认结束的作业…')
        resolve.clicked.connect(self.resolve_unknown); layout.addWidget(resolve)
        close = QPushButton('返回编辑'); close.clicked.connect(self.accept); layout.addWidget(close)
        self.start_button.clicked.connect(self.start)
        self.stop_button.clicked.connect(lambda: (stop_api(window), self.refresh()))
        copy_button.clicked.connect(self.copy_config)
        self.refresh()

    def refresh(self):
        handle = getattr(self.window, 'agent_api', None)
        self.start_button.setEnabled(handle is None)
        self.stop_button.setEnabled(handle is not None)
        self.connection.setText(str(handle.connection_file) if handle is not None else '')
        service = getattr(self.window, 'application_service', None)
        grant = service._motion_status() if service is not None else {'granted': False}
        virtual = service.controller.snapshot().get('simulation') if service is not None else False
        self.grant_button.setEnabled(handle is not None and not virtual)
        if virtual:
            message = '当前为离线舵机模拟器，不连接实物。'
        elif grant.get('granted'):
            message = '当前会话范围授权有效；作业仍须通过页面预检。'
        else:
            message = '排版、预览接口可用。实机动作需要先完成定位和笔高，再在这里授权。'
        self.status.setText('接口已启用。' + message if handle is not None else '接口未启用。点击启用后可让 Agent 读取与编辑当前页面。')

    def start(self):
        try:
            workspace = Path(self.workspace.text()).expanduser().resolve()
            if not workspace.is_dir():
                raise ServiceError('INVALID_ARGUMENT', '请选择已存在的工作目录')
            self.window._agent_workspace = workspace
            start_api(self.window)
            self.workspace.setReadOnly(True)
            self.browse_button.setEnabled(False)
            self.refresh()
        except Exception as exc:
            self.status.setText(str(exc))

    def copy_config(self):
        handle = getattr(self.window, 'agent_api', None)
        if handle is None:
            self.status.setText('请先启用接口。'); return
        import sys
        if getattr(sys, 'frozen', False):
            command, args = str(Path(sys.executable).with_name('MaliangAgent.exe')), []
        else:
            executable = Path(sys.executable)
            if executable.name.lower() == 'pythonw.exe':
                executable = executable.with_name('python.exe')
            source_entry = Path(__file__).resolve().parents[1] / 'agent_main.py'
            command = str(executable)
            args = [str(source_entry)] if source_entry.is_file() else ['-m', 'pagewriter.interfaces.cli']
        config = {'mcpServers': {'maliang': {'command': command,
                    'args': args + ['mcp', '--connection', str(handle.connection_file)]}}}
        QApplication.clipboard().setText(json.dumps(config, ensure_ascii=False, indent=2))
        self.status.setText('已复制 MCP 配置；凭据保存在私有连接文件中。')

    def grant_page(self):
        service = ensure_service(self.window)
        try:
            if service.controller.snapshot().get('simulation'):
                self.status.setText('离线模拟器无需实机范围授权。'); return
            if getattr(self.window, 'agent_api', None) is None:
                raise ServiceError('SERVICE_CLOSED', '请先启用 Agent 接口')
            state = service.controller.snapshot()
            if not (state.get('connection') and state.get('idle') and state.get('state') in {'ready', 'complete'}
                    and state.get('coordinate_age', float('inf')) <= 2 and not state.get('pending')
                    and not state.get('status_pending') and not state.get('queued_commands')):
                raise ServiceError('DEVICE_NOT_READY', '请等待当前设备空闲，再完成定位和范围确认')
            failures = service.dispatch('document.preflight')['issues']
            if failures:
                raise ServiceError('PREFLIGHT_FAILED', '请先处理页面预检问题')
            from .core import all_subpaths
            paths = all_subpaths(self.window.project)
            if not paths or any(b.writing_mode != 'single_line' for b in self.window.project.blocks):
                raise ServiceError('PREFLIGHT_FAILED', '请先添加已通过检查的单线文字或线稿')
            panel = self.window.device_panel
            bounds = panel.job_bounds(paths)
            if bounds is None:
                raise ServiceError('DEVICE_NOT_READY', '请先完成当前设备的定位、笔高和周围空间确认')
            machine, z_range, page_rect = bounds
            controller = service.controller
            context = grant_context(service)
            handle = self.window.agent_api
            self.window.show_job_area(page_rect)
            try:
                answer = QMessageBox.question(self, '授权当前页面范围',
                    '请核对页面蓝色范围内没有书夹或其他障碍，书页和笔高已确认。\n\n'
                    '授权后15分钟内，Agent 可在该范围内开始已预检作业和有限点动；断线、手推或重新定位后授权失效。')
            finally:
                self.window.show_job_area(None)
            if answer != QMessageBox.StandardButton.Yes:
                return
            check_grant_context(service, controller, context)
            params = panel.authorization(machine, z_range, time.strftime('%Y-%m-%d %H:%M:%S') + ' GUI Agent 范围授权', True)
            authorization_id = uuid.uuid4().hex
            controller.submit('authorize', **params, authorization_id=authorization_id)
            deadline = time.monotonic() + 3
            def finish():
                if not self.isVisible() or service.closed or getattr(self.window, 'agent_api', None) is not handle:
                    return
                try:
                    check_grant_context(service, controller, context)
                    check_authorization(controller, authorization_id, params)
                    service.jobs.grant_motion(900)
                    self.refresh()
                except ServiceError as exc:
                    if exc.code in {'AUTHORIZATION_PENDING', 'DEVICE_NOT_READY'} and time.monotonic() < deadline:
                        QTimer.singleShot(100, finish)
                    else:
                        self.status.setText('授权未完成：' + exc.message)
            QTimer.singleShot(100, finish)
        except Exception as exc:
            self.status.setText(str(exc))

    def resolve_unknown(self):
        service = ensure_service(self.window)
        try:
            jobs = [j for j in service.jobs.status()['jobs'] if j['state'] == 'unknown']
            if not jobs:
                self.status.setText('没有待核实的历史作业。'); return
            job_id, ok = QInputDialog.getItem(self, '选择待核实作业', '作业编号', [j['job_id'] for j in jobs], editable=False)
            if not ok: return
            note, ok = QInputDialog.getText(self, '记录现场核实', '请检查设备与纸面，并记录实际状态；归档不会重新执行或宣称已写完。')
            if ok:
                service.jobs.resolve_unknown(job_id, note)
                self.status.setText('已记录核实结果，旧作业不会自动重放。')
        except Exception as exc:
            self.status.setText(str(exc))
