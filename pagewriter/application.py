"""One application boundary for desktop, local API, CLI and MCP.

Document/layout commands run on the owning Qt thread. Only controllers own
transports. Public methods never accept caller-supplied preflight/authorization.
"""
from __future__ import annotations
import copy
import math
from pathlib import Path
import threading
import uuid
import queue
from collections import deque

from . import __version__, core, stroke_fonts
from .document_service import DocumentService, geometry_identity
from .service_errors import ServiceError, fields, number
from .rendering import render_page


def json_safe(value):
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if isinstance(value, dict):
        return {str(key): json_safe(v) for key, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [json_safe(v) for v in value]
    return value


class ApplicationService:
    def __init__(self, project=None, controller=None, window=None, workspace=None,
                 state_dir=None, allow_hardware=False):
        from .devices import create_controller
        from .job_service import JobManager
        self.thread_id, self.runtime_id = threading.get_ident(), uuid.uuid4().hex
        self.window, self.allow_hardware, self.closed = window, bool(allow_hardware), False
        self.workspace = Path(workspace or Path.cwd()).resolve()
        if not self.workspace.is_dir():
            raise ServiceError('INVALID_ARGUMENT', '工作目录不存在')
        self.state_dir = Path(state_dir or self.workspace / '.maliang').resolve()
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.controller = controller or create_controller('simulator_servo')
        self.controller.start()
        self._owns_controller = window is None
        self.documents = DocumentService(project, window, self.workspace, self._document_changed)
        if self.state_dir != self.workspace:
            self.documents.reserved_paths.add(self.state_dir)
        try:
            self.jobs = JobManager(self.documents, self.controller, self.state_dir, self.runtime_id)
        except Exception:
            if self._owns_controller:
                self.controller.close()
            raise
        self._event_queue, self._events, self._event_cursor = queue.SimpleQueue(), deque(maxlen=200), 0
        self._event_unsubscribe = None
        self._subscribe()
        if window is not None:
            window.device_panel.controller_selector = self.select_controller

    def _owner(self):
        if self.closed:
            raise ServiceError('SERVICE_CLOSED', '应用服务已关闭')
        if threading.get_ident() != self.thread_id:
            raise ServiceError('WRONG_THREAD', '文档操作须由 Qt 主线程分派')

    def _document_changed(self, changed_geometry):
        self.jobs.document_changed(changed_geometry)
        if changed_geometry and self.controller.snapshot().get('connection'):
            self.controller.submit('geometry_changed', geometry_id=geometry_identity(self.documents.project))

    def describe_methods(self):
        from .interfaces.schema import TOOLS
        return [{'name': name, **copy.deepcopy(spec)} for name, spec in TOOLS.items()]

    def poll(self):
        if self.closed:
            return
        self._owner()
        self.jobs.poll()
        while True:
            try:
                event = self._event_queue.get_nowait()
            except queue.Empty:
                break
            self._event_cursor += 1
            self._events.append({'cursor': self._event_cursor, **json_safe(event)})

    def _subscribe(self):
        if self._event_unsubscribe:
            self._event_unsubscribe()
        def receive(event):
            if event.get('kind') not in {'tx', 'rx', 'ack'}:
                self._event_queue.put(dict(event))
        self._event_unsubscribe = self.controller.subscribe_events(receive) if hasattr(self.controller, 'subscribe_events') else None

    def reserve_path(self, path):
        self.documents.reserved_paths.add(Path(path).resolve())

    def device_events(self, after=0):
        if type(after) is not int or after < 0:
            raise ServiceError('INVALID_ARGUMENT', 'after 须为非负整数游标')
        return {'events': [e for e in self._events if e['cursor'] > after], 'cursor': self._event_cursor}

    def _editable(self):
        state = self.controller.snapshot().get('state')
        if state in {'running', 'pausing', 'paused'}:
            raise ServiceError('DEVICE_BUSY', '作业进行中，请先暂停后取消，或等待结束再编辑')
        self._gui_idle()

    def _gui_idle(self):
        if self.window is not None:
            from PySide6.QtWidgets import QApplication
            if QApplication.activeModalWidget() is not None or getattr(self.window.canvas, 'preedit_text', ''):
                raise ServiceError('GUI_BUSY', '用户正在确认操作或输入法组词，请稍后重读版本再编辑')

    def dispatch(self, method, params=None):
        self._owner()
        from .interfaces.schema import TOOLS
        if method not in TOOLS:
            raise ServiceError('METHOD_NOT_FOUND', '未知应用操作', {'method': method})
        params = {} if params is None else params
        schema = TOOLS[method]['inputSchema']
        fields(params, schema['properties'], schema.get('required', ()))
        from jsonschema import Draft202012Validator, ValidationError
        try:
            Draft202012Validator(schema).validate(params)
        except ValidationError as exc:
            raise ServiceError('INVALID_ARGUMENT', '参数不符合接口结构', {'path': list(exc.absolute_path)}) from exc
        self.poll()
        self.documents.observe()
        handlers = {
            'system.info': self.info, 'document.get': self.documents.get,
            'document.new': self.documents.new, 'document.open': self.documents.open,
            'document.save': self.documents.save, 'document.apply': self.documents.apply,
            'document.undo': lambda **kw: self.documents.history(False, **kw),
            'document.redo': lambda **kw: self.documents.history(True, **kw),
            'document.export': self.export, 'fonts.list': self.fonts,
            'preview.render': self.preview, 'document.preflight': self.preflight,
            'device.profiles': self.profiles, 'device.list': self.device_list,
            'device.connect': self.connect, 'device.disconnect': self.disconnect,
            'device.status': self.device_status, 'device.events': self.device_events, 'device.command': self.device_command,
            'job.prepare': self.jobs.prepare, 'job.start': self.jobs.start, 'job.status': self.jobs.status,
            'job.pause': lambda **kw: self.jobs.control('pause', **kw),
            'job.resume': lambda **kw: self.jobs.control('resume', **kw),
            'job.cancel': lambda **kw: self.jobs.control('cancel', **kw),
        }
        if method in {'document.new', 'document.open', 'document.apply', 'document.undo', 'document.redo'}:
            self._editable()
        elif method in {'device.connect', 'device.disconnect', 'job.start', 'job.resume'} or (
                method == 'device.command' and params.get('command') != 'poll'):
            self._gui_idle()
        try:
            return json_safe(handlers[method](**params))
        except ServiceError:
            raise
        except (ValueError, TypeError, KeyError, OverflowError) as exc:
            raise ServiceError('INVALID_ARGUMENT', str(exc)) from exc
        except OSError as exc:
            raise ServiceError('IO_ERROR', str(exc)) from exc

    def info(self):
        return {'version': __version__, 'api_version': 1, 'runtime_id': self.runtime_id,
                'revision': self.documents.revision, 'workspace': str(self.workspace),
                'hardware_connections_allowed': self.allow_hardware,
                'motion_authorized': self._motion_status(),
                'gui_attached': self.window is not None}

    def fonts(self):
        return {'fonts': [{key: copy.deepcopy(entry[key]) for key in
                 ('id', 'name', 'source_name', 'license', 'glyph_count', 'fallback_ids')}
                 for entry in stroke_fonts.catalog()]}

    def profiles(self):
        from .devices import list_profiles
        return {'profiles': list_profiles()}

    def device_list(self):
        from .controller import MachineController
        return {'ports': MachineController.device_list(), 'simulation_profile': 'simulator_servo'}

    def select_controller(self, profile_id):
        from .devices import create_controller, get_profile
        self._owner()
        get_profile(profile_id)
        state = self.controller.snapshot()
        if state.get('connection') or state.get('pending'):
            raise ServiceError('DEVICE_BUSY', '请先断开当前设备后切换机型')
        old = self.controller
        new = create_controller(profile_id)
        self.jobs.set_controller(new)
        self.controller = new
        self._subscribe()
        old.close()
        new.start()
        if self.window is not None:
            panel = self.window.device_panel
            panel.controller = new
            panel.profile_selector.blockSignals(True)
            panel.profile_selector.setCurrentIndex(panel.profile_selector.findData(profile_id))
            panel.profile_selector.blockSignals(False)
            panel.populate_profile_presets()
            panel.current_job = None; panel.test_done = panel.page_done = False
            panel.clear_preset_confirmation(); panel.update_state()
        return new

    def connect(self, profile_id='simulator_servo', port=None):
        from .devices import get_profile
        profile = get_profile(profile_id)
        if not profile.capabilities.simulation and not self.allow_hardware:
            raise ServiceError('HARDWARE_DISABLED', '此服务只允许离线模拟；实机连接须由操作员启用')
        if self.controller.snapshot().get('connection'):
            raise ServiceError('DEVICE_BUSY', '当前已有连接，请先断开')
        current = self.controller.snapshot().get('device_profile')
        if current != profile_id:
            self.select_controller(profile_id)
        if not profile.capabilities.simulation and (not isinstance(port, str) or not port.strip()):
            raise ServiceError('INVALID_ARGUMENT', '实机连接需要明确串口名称')
        self.controller.submit('connect', **({'port': port} if port is not None and not profile.capabilities.simulation else {}))
        geometry = geometry_identity(self.documents.project)
        self.controller.submit('geometry_changed', geometry_id=geometry)
        if profile.capabilities.simulation:
            self.controller.submit('prepare_virtual', geometry_id=geometry,
                                   work_bounds=(0., 0., self.documents.project.width, self.documents.project.height))
        if self.window is not None:
            panel = self.window.device_panel
            # Update selection without generating a second controller switch.
            if hasattr(panel, 'profile_selector'):
                index = panel.profile_selector.findData(profile_id)
                if index >= 0:
                    panel.profile_selector.blockSignals(True); panel.profile_selector.setCurrentIndex(index); panel.profile_selector.blockSignals(False)
        return {'accepted': True, 'profile_id': profile_id, 'simulation': profile.capabilities.simulation}

    def disconnect(self):
        self.jobs.revoke_motion()
        self.controller.submit('disconnect')
        return {'accepted': True}

    def _motion_status(self):
        try:
            if hasattr(self.jobs, 'grant_status'):
                return self.jobs.grant_status(readonly=True)
            return self.jobs.require_motion()
        except ServiceError as exc:
            return {'granted': False, 'reason': exc.code}

    def device_status(self):
        state = self.controller.snapshot()
        state['agent_motion'] = self._motion_status()
        state['event_cursor'] = self._event_cursor
        return state

    def device_command(self, command, params=None):
        rules = {
            'poll': (set(), set()), 'manual_reposition': (set(), set()),
            'jog': ({'axis', 'distance', 'feed'}, {'axis', 'distance'}),
            'lift_pen': (set(), set()), 'lower_pen': (set(), set()),
            'capture_point': ({'page_x', 'page_y'}, {'page_x', 'page_y'}),
            'fit_calibration': (set(), set()),
            'anchor': ({'page_x', 'page_y'}, {'page_x', 'page_y'}),
            'prepare_virtual': (set(), set()),
        }
        if command not in rules:
            raise ServiceError('UNSUPPORTED_COMMAND', '该命令不对 Agent 开放', {'commands': sorted(rules)})
        args = {} if params is None else copy.deepcopy(params)
        fields(args, *rules[command])
        state = self.controller.snapshot()
        simulation = state.get('simulation', False)
        if command == 'prepare_virtual':
            if not simulation:
                raise ServiceError('UNSUPPORTED_CAPABILITY', '只有离线模拟器可以准备虚拟定位')
            args = {'geometry_id': geometry_identity(self.documents.project),
                    'work_bounds': (0., 0., self.documents.project.width, self.documents.project.height)}
        if command in {'jog', 'lift_pen', 'lower_pen'}:
            self.jobs.require_motion()
        if command == 'jog':
            caps = state.get('capabilities', {})
            if args.get('axis') not in caps.get('axes', []):
                raise ServiceError('UNSUPPORTED_CAPABILITY', '设备没有此轴')
            number(args['distance'], 'distance', -1, 1)
            number(args.get('feed', 60), 'feed', .01, 300)
        if command in {'capture_point', 'anchor'}:
            number(args['page_x'], 'page_x', 0, self.documents.project.width)
            number(args['page_y'], 'page_y', 0, self.documents.project.height)
        if command in {'fit_calibration', 'anchor'}:
            args['geometry_id'] = geometry_identity(self.documents.project)
        if command == 'anchor':
            # Reuse only operator-confirmed axes preserved by manual_reposition.
            if not state.get('anchor_available'):
                raise ServiceError('NEEDS_ANCHOR', '没有已确认的轴向，请在界面完成初次定位')
            args['axes_confirmed'] = True
        if command == 'manual_reposition':
            self.jobs.revoke_motion()
        self.controller.submit(command, **args)
        return {'accepted': True, 'command': command, 'simulation': simulation}

    def preflight(self):
        issues = [vars(c) for c in core.preflight(self.documents.project)]
        return {'revision': self.documents.revision, 'passed': not issues, 'issues': issues,
                'physical_position_verified': False, 'simulation': self.controller.snapshot().get('simulation', False)}

    def preview(self, path=None, scale=4., show_zones=True):
        number(scale, 'scale', .5, 20)
        if type(show_zones) is not bool:
            raise ServiceError('INVALID_ARGUMENT', 'show_zones 须为布尔值')
        target = self.documents.output_path(path, 'preview') if path is not None else self.state_dir / ('preview-' + uuid.uuid4().hex + '.png')
        if target.suffix.lower() != '.png':
            raise ServiceError('INVALID_ARGUMENT', '预览路径须为 PNG')
        result = render_page(self.documents.project, target, scale, show_zones)
        self.documents.output_paths[target] = 'preview'
        result['revision'] = self.documents.revision
        result['preflight'] = self.preflight()
        return result

    def export(self, path, format):
        current = getattr(self.window, 'filename', None) if self.window else self.documents.filename
        target = self.documents.path(path, writing=True)
        if format == 'svg':
            if target.suffix.lower() != '.svg':
                raise ServiceError('INVALID_ARGUMENT', 'SVG 导出路径须为 .svg')
            target = self.documents.output_path(target, 'svg')
            content = core.export_svg(self.documents.project)
            temporary = target.with_name(target.name + '.tmp')
            temporary.write_text(content, encoding='utf-8'); temporary.replace(target)
        elif format == 'pagewriter':
            if target.suffix.lower() not in {'.pagewriter', '.json'}:
                raise ServiceError('INVALID_ARGUMENT', '文档路径须为 .pagewriter 或 .json')
            target = self.documents.output_path(target, 'document', current)
            core.save_project(self.documents.project, target)
        else:
            raise ServiceError('INVALID_ARGUMENT', '支持 svg 或 pagewriter')
        self.documents.output_paths[target] = 'document' if format == 'pagewriter' else 'svg'
        return {'path': str(target), 'revision': self.documents.revision}

    def close(self):
        if self.closed:
            return
        self.jobs.close()
        if self._event_unsubscribe:
            self._event_unsubscribe()
        if self._owns_controller:
            self.controller.close()
        self.closed = True
