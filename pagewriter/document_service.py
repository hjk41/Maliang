"""Atomic document commands shared by native editing and agent interfaces.

The stored v1 project and Qt layout engine remain authoritative. Public text
runs use Unicode scalar strings and font IDs, never Qt's UTF-16 offsets.
"""
from __future__ import annotations
import copy
import hashlib
import json
import math
import uuid
from dataclasses import fields as dataclass_fields
from html import escape
from pathlib import Path

from PySide6.QtCore import QPointF, Qt
from PySide6.QtGui import QImageReader, QTransform

from . import core, stroke_fonts
from .service_errors import ServiceError, fields, number


DRAWING_FIELDS = {'x_mm': 'x', 'y_mm': 'y', 'width_mm': 'width',
    'rotation_degrees': 'rotation', 'threshold': 'threshold', 'detail': 'detail',
    'min_stroke_mm': 'min_stroke', 'fill': 'fill', 'fill_cutoff': 'fill_cutoff',
    'fill_spacing_mm': 'fill_spacing', 'fill_angle_degrees': 'fill_angle',
    'fill_levels': 'fill_levels', 'fill_min_width_mm': 'fill_min_width'}


def geometry_identity(project):
    data = [project.width, project.height, project.image_data, project.image_quad,
            project.image_edges, project.image_guides, project.image_offset]
    return hashlib.sha256(json.dumps(data, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def fingerprint(project):
    """Exclude lazily generated glyph/layout caches from editing revisions."""
    # Do not deep-copy megabytes of frozen paths just to discard them.
    data = {f.name: getattr(project, f.name) for f in dataclass_fields(project)
            if f.name not in {'blocks', 'drawings', 'zones'}}
    excluded = {'frozen_key', 'frozen_path', 'fonts', 'frozen_overflow', 'frozen_layout',
                'flow_exclusions', 'flow_clearance', 'stroke_fonts'}
    data['blocks'] = []
    for block in project.blocks:
        value = {f.name: getattr(block, f.name) for f in dataclass_fields(block) if f.name not in excluded}
        value['stroke_fonts'] = {family: {k: v for k, v in descriptor.items()
            if k not in {'glyphs', 'glyph_sources'}} for family, descriptor in block.stroke_fonts.items()}
        data['blocks'].append(value)
    data['drawings'] = [{f.name: getattr(d, f.name) for f in dataclass_fields(d)
                         if f.name not in {'frozen_key', 'paths', 'stats', 'height'}} for d in project.drawings]
    data['zones'] = [{f.name: getattr(z, f.name) for f in dataclass_fields(z)} for z in project.zones]
    return hashlib.sha256(json.dumps(data, sort_keys=True, ensure_ascii=False, allow_nan=False).encode()).hexdigest()


class DocumentService:
    def __init__(self, project=None, window=None, workspace=None, on_change=None):
        self.window, self.workspace, self.on_change = window, Path(workspace or Path.cwd()).resolve(), on_change
        self._project = project or core.blank_project()
        self.filename = None
        self.reserved_paths = {self.workspace / '.maliang'}
        package = Path(__file__).resolve().parent
        self.protected_paths = {package, self.workspace / '.git'}
        source = package.parent
        if (source / 'pyproject.toml').is_file() and (source / 'main.py').is_file():
            self.protected_paths.update(source / name for name in ('tools', 'tests', '.github', 'licenses', '.git'))
        self.output_paths = {}
        self.revision, self.undo_stack, self.redo_stack = 1, [], []
        self._signature = fingerprint(self.project)
        self._geometry = geometry_identity(self.project)

    @property
    def project(self):
        return self.window.project if self.window is not None else self._project

    def path(self, value, writing=False):
        if not isinstance(value, (str, Path)) or not str(value):
            raise ServiceError('INVALID_ARGUMENT', '需要有效文件路径')
        path = Path(value)
        path = (self.workspace / path).resolve() if not path.is_absolute() else path.resolve()
        if not path.is_relative_to(self.workspace):
            raise ServiceError('PATH_OUTSIDE_WORKSPACE', '文件须位于本次服务的工作目录内')
        if any(path == item or path.is_relative_to(item) for item in self.reserved_paths):
            raise ServiceError('RESERVED_PATH', '此路径属于服务内部状态，不能作为文档或图片文件')
        if writing and any(path == item or path.is_relative_to(item) for item in self.protected_paths):
            raise ServiceError('PROTECTED_PATH', '不能通过文档接口覆盖应用程序、资源或原始参考图')
        return path

    def output_path(self, value, kind, current=None):
        path = self.path(value, writing=True)
        if path.exists() and path != current and self.output_paths.get(path) != kind:
            raise ServiceError('FILE_EXISTS', '目标文件已存在；请使用新文件名或先打开要保存的项目')
        return path

    def observe(self):
        signature = fingerprint(self.project)
        if signature != self._signature:
            geometry = geometry_identity(self.project)
            changed_geometry = geometry != self._geometry
            self.revision += 1
            self._signature, self._geometry = signature, geometry
            if self.on_change:
                self.on_change(changed_geometry)
        return self.revision

    def check_revision(self, expected=None, required=False):
        self.observe()
        if expected is None and required:
            raise ServiceError('INVALID_ARGUMENT', '写操作需要 expected_revision')
        if expected is not None and (type(expected) is not int or expected != self.revision):
            raise ServiceError('REVISION_CONFLICT', '文档已改变，请先重新读取', {'revision': self.revision})

    def _commit(self, project, selected_id=None, new_context=False):
        core._validate_project(project)
        ids = [o.id for o in [*project.blocks, *project.zones, *project.drawings]]
        if len(ids) != len(set(ids)):
            raise ServiceError('INVALID_ARGUMENT', '文档对象 ID 必须唯一')
        geometry = geometry_identity(project)
        changed_geometry = new_context or geometry != self._geometry
        if self.window is not None:
            self.window.record()
            self.window.project = project
            if selected_id is not None:
                self.window.selected_id = selected_id
            elif getattr(self.window, 'selected_id', None) not in ids:
                self.window.selected_id = ''
            self.window.refresh()
        else:
            self.undo_stack.append(copy.deepcopy(self.project))
            self.undo_stack = self.undo_stack[-100:]
            self.redo_stack.clear()
            self._project = project
        self.revision += 1
        self._signature, self._geometry = fingerprint(project), geometry
        if self.on_change:
            self.on_change(changed_geometry)

    def new(self, width_mm=210, height_mm=297, expected_revision=None):
        self.check_revision(expected_revision)
        number(width_mm, 'width_mm', .1, 2000); number(height_mm, 'height_mm', .1, 2000)
        self._commit(core.blank_project(width_mm, height_mm), new_context=True)
        self.filename = None
        if self.window is not None:
            self.window.filename = None
        return self.get()

    def open(self, path, expected_revision=None):
        self.check_revision(expected_revision)
        path = self.path(path)
        if path.stat().st_size > 80_000_000:
            raise ServiceError('INVALID_ARGUMENT', '项目文件过大')
        project = core.load_project(path)
        self._commit(project, project.blocks[0].id if project.blocks else '', new_context=True)
        self.filename = path
        if self.window is not None:
            self.window.filename, self.window.dirty = path, False
        return self.get()

    def save(self, path=None):
        current = getattr(self.window, 'filename', None) if self.window else self.filename
        target = self.path(path) if path is not None else current
        if target is None:
            raise ServiceError('INVALID_ARGUMENT', '未命名文档需要保存路径')
        target = self.path(target, writing=True)
        if target.suffix.lower() not in {'.pagewriter', '.json'}:
            raise ServiceError('INVALID_ARGUMENT', '文档保存路径须为 .pagewriter 或 .json')
        target = self.output_path(target, 'document', current)
        core.save_project(self.project, target)
        self.output_paths[target] = 'document'
        self.filename = target
        if self.window is not None:
            self.window.filename, self.window.dirty = target, False
        return {'path': str(target), 'revision': self.observe()}

    def external_context_changed(self):
        """A GUI new/open action starts a fresh context even for equal content."""
        self._signature, self._geometry = fingerprint(self.project), geometry_identity(self.project)
        self.revision += 1
        if self.on_change:
            self.on_change(True)

    def history(self, redo=False, expected_revision=None):
        self.check_revision(expected_revision, required=True)
        if self.window is not None:
            (self.window.redo if redo else self.window.undo)()
        else:
            source, target = (self.redo_stack, self.undo_stack) if redo else (self.undo_stack, self.redo_stack)
            if source:
                target.append(copy.deepcopy(self.project)); self._project = source.pop()
        self.observe()
        return self.get()

    def _runs(self, block):
        doc = core.make_document(block)
        result, paragraph, first = [], doc.begin(), True
        while paragraph.isValid():
            if not first:
                result.append({'text': '\n', 'font_id': None, 'size_pt': 12., 'underline': False})
            first = False
            it = paragraph.begin()
            while not it.atEnd():
                fragment = it.fragment()
                if fragment.isValid():
                    fmt = fragment.charFormat()
                    descriptor = stroke_fonts.resolve_descriptor(block.stroke_fonts, fmt.fontFamilies(), block.stroke_font)
                    result.append({'text': fragment.text().replace('\u2028', '\n'), 'font_id': descriptor.get('builtin_id') if descriptor else None,
                                   'family': descriptor['family'] if descriptor else (fmt.fontFamilies() or [''])[0],
                                   'size_pt': fmt.fontPointSize() or doc.defaultFont().pointSizeF(),
                                   'underline': fmt.fontUnderline()})
                it += 1
            paragraph = paragraph.next()
        return result

    def get(self):
        self.observe()
        project = self.project
        blocks = []
        for block in project.blocks:
            runs = self._runs(block)
            blocks.append({'id': block.id, 'name': block.name, 'x_mm': block.x, 'y_mm': block.y,
                           'width_mm': block.width, 'rotation_degrees': block.rotation, 'runs': runs,
                           'text': ''.join(r['text'] for r in runs), 'columns': block.columns,
                           'column_height_mm': block.column_height, 'column_gap_mm': block.column_gap,
                           'column_separators': block.column_separators, 'avoid_zones': block.avoid_zones,
                           'writing_mode': block.writing_mode})
        return {'revision': self.revision, 'width_mm': project.width, 'height_mm': project.height,
                'pen_width_mm': project.pen_width, 'margin_mm': project.margin,
                'blocks': blocks, 'zones': [{'id': z.id, 'name': z.name, 'kind': z.kind, 'points_mm': z.points} for z in project.zones],
                'drawings': [self._drawing_info(d) for d in project.drawings],
                'photo': {'present': bool(project.image_data), 'quad': project.image_quad,
                          'edges': project.image_edges, 'guides': project.image_guides,
                          'opacity': project.image_opacity, 'offset_mm': project.image_offset}}

    def _drawing_info(self, drawing):
        from . import lineart
        paths = core.drawing_strokes(drawing)
        ink = sum(lineart.length(path) for path in paths)
        travel = sum(math.dist(a[-1], b[0]) for a, b in zip(paths, paths[1:]))
        return {'id': drawing.id, 'name': drawing.name,
                **{key: getattr(drawing, attr) for key, attr in DRAWING_FIELDS.items()},
                'height_mm': drawing.height, 'source_type': 'svg' if drawing.vector else 'image',
                'erase_circles_mm': [[u*drawing.width, v*drawing.width, r*drawing.width]
                                     for u, v, r in drawing.erase],
                'stats': {**drawing.stats, 'strokes': len(paths), 'ink_mm': ink, 'travel_mm': travel,
                          'seconds': ink/lineart.WRITE_FEED*60 + travel/lineart.TRAVEL_FEED*60
                                     + len(paths)*lineart.PEN_CYCLE_S}}

    def _set_runs(self, block, runs):
        if not isinstance(runs, list) or not 1 <= len(runs) <= 10000:
            raise ServiceError('INVALID_ARGUMENT', 'runs 需要非空列表，最多10000段')
        spans, fonts, length = [], {}, 0
        for run in runs:
            fields(run, {'text', 'font_id', 'size_pt', 'underline'}, {'text', 'font_id', 'size_pt'})
            if not isinstance(run['text'], str) or any(0xd800 <= ord(c) <= 0xdfff for c in run['text']):
                raise ServiceError('INVALID_ARGUMENT', '文字须为有效 Unicode')
            length += len(run['text'])
            if length > 200000:
                raise ServiceError('INVALID_ARGUMENT', '文字过长')
            size = number(run['size_pt'], 'size_pt', 1, 500)
            underline = run.get('underline', False)
            if type(underline) is not bool:
                raise ServiceError('INVALID_ARGUMENT', 'underline 须为布尔值')
            font = stroke_fonts.descriptor(run['font_id'])
            fonts[font['family']] = font
            style = f'font-family:"{font["family"]}";font-size:{size:g}pt;'
            if underline:
                style += 'text-decoration:underline;'
            text = escape(run['text'].replace('\r\n', '\n').replace('\r', '\n')).replace('\n', '<br/>')
            spans.append(f'<span style=\'{style}\'>{text}</span>')
        block.html = '<p style="white-space:pre-wrap;margin:0">' + ''.join(spans) + '</p>'
        block.writing_mode = 'single_line'
        block.stroke_font = next(iter(fonts))
        block.stroke_fonts = fonts
        block.frozen_key = ''; block.frozen_path = []; block.fonts = []

    def _image(self, path, auto_transform=False):
        path = self.path(path)
        self.protected_paths.add(path)
        if path.stat().st_size > 40_000_000:
            raise ServiceError('INVALID_ARGUMENT', '图片文件过大')
        reader = QImageReader(str(path))
        reader.setAutoTransform(auto_transform)
        size = reader.size()
        if not size.isValid() or size.width() * size.height() > 40_000_000:
            raise ServiceError('INVALID_ARGUMENT', '图片尺寸无效或过大')
        image = reader.read()
        if image.isNull():
            raise ServiceError('INVALID_ARGUMENT', '无法读取图片')
        return image

    def _import_drawing(self, path):
        path = self.path(path)
        self.protected_paths.add(path)
        if path.suffix.lower() == '.svg':
            from xml.etree.ElementTree import ParseError
            from . import svgimport
            if path.stat().st_size > 4_000_000:
                raise ServiceError('INVALID_ARGUMENT', 'SVG 文件过大')
            try:
                drawing = core.Drawing(vector=svgimport.parse(path))
            except (ValueError, ParseError, RecursionError) as exc:
                raise ServiceError('INVALID_ARGUMENT', '无法读取 SVG 图画') from exc
        else:
            image = self._image(path, auto_transform=True)
            if max(image.width(), image.height()) > 1600:
                image = image.scaled(1600, 1600, Qt.AspectRatioMode.KeepAspectRatio,
                                     Qt.TransformationMode.SmoothTransformation)
            drawing = core.Drawing(image_data=core.image_to_data(image))
        drawing.id = uuid.uuid4().hex
        drawing.name = path.stem[:30] or '图画'
        return drawing

    def _photo(self, project, op):
        if 'path' in op:
            project.image_data = core.image_to_data(self._image(op['path']))
            project.image_quad = project.image_edges = project.image_guides = None
            project.image_offset = [0., 0.]
        if 'rotation_degrees' in op:
            angle = op['rotation_degrees']
            if type(angle) is not int or angle % 90:
                raise ServiceError('INVALID_ARGUMENT', '照片旋转须为90度的整数倍')
            if not project.image_data:
                raise ServiceError('INVALID_ARGUMENT', '当前没有照片')
            project.image_data = core.image_to_data(core.data_to_image(project.image_data).transformed(QTransform().rotate(angle)))
            project.image_quad = project.image_edges = project.image_guides = None
            project.image_offset = [0., 0.]
        for key, attr in [('quad', 'image_quad'), ('edges', 'image_edges'), ('guides', 'image_guides'),
                          ('opacity', 'image_opacity'), ('offset_mm', 'image_offset')]:
            if key in op:
                setattr(project, attr, copy.deepcopy(op[key]))
        if project.image_quad is not None:
            core._points(project.image_quad, length=4, label='照片四角')
            image = core.data_to_image(project.image_data)
            quad = project.image_quad
            if any(not 0 <= x <= image.width() or not 0 <= y <= image.height() for x, y in quad):
                raise ServiceError('INVALID_ARGUMENT', '照片四角超出原图')
            crosses = [(quad[(i+1)%4][0]-quad[i][0])*(quad[(i+2)%4][1]-quad[(i+1)%4][1])
                       -(quad[(i+1)%4][1]-quad[i][1])*(quad[(i+2)%4][0]-quad[(i+1)%4][0]) for i in range(4)]
            if not (all(c > .01 for c in crosses) or all(c < -.01 for c in crosses)):
                raise ServiceError('INVALID_ARGUMENT', '照片四角须依次构成非退化凸四边形')

    def apply(self, operations, expected_revision):
        self.check_revision(expected_revision, required=True)
        if not isinstance(operations, list) or not 1 <= len(operations) <= 1000:
            raise ServiceError('INVALID_ARGUMENT', 'operations 需要1到1000项')
        project = copy.deepcopy(self.project)
        selected, added, changed_drawings = None, [], set()
        text_fields = {'id', 'name', 'x_mm', 'y_mm', 'width_mm', 'rotation_degrees', 'runs',
                       'columns', 'column_height_mm', 'column_gap_mm', 'column_separators', 'avoid_zones'}
        geometry_fields = {'x_mm': 'x', 'y_mm': 'y', 'width_mm': 'width', 'rotation_degrees': 'rotation',
                           'column_height_mm': 'column_height', 'column_gap_mm': 'column_gap'}
        for operation in operations:
            if not isinstance(operation, dict):
                raise ServiceError('INVALID_ARGUMENT', '操作须为对象')
            op = dict(operation)
            kind = op.pop('op', None)
            if kind in ('add_text', 'update_text'):
                fields(op, text_fields, {'runs'} if kind == 'add_text' else {'id'})
                if kind == 'add_text':
                    block = core.TextBlock(id=op.get('id', uuid.uuid4().hex), name=op.get('name', '批注'))
                    project.blocks.append(block); added.append(block.id)
                else:
                    block = next((b for b in project.blocks if b.id == op['id']), None)
                    if block is None:
                        raise ServiceError('NOT_FOUND', '文字对象不存在')
                for key, attr in geometry_fields.items():
                    if key in op:
                        setattr(block, attr, number(op[key], key))
                for key in ('name', 'columns', 'column_separators', 'avoid_zones'):
                    if key in op:
                        setattr(block, key, op[key])
                if 'runs' in op:
                    self._set_runs(block, op['runs'])
                selected = block.id
            elif kind in ('add_zone', 'update_zone'):
                fields(op, {'id', 'name', 'kind', 'points_mm'}, {'points_mm'} if kind == 'add_zone' else {'id'})
                if kind == 'add_zone':
                    zone = core.ExclusionZone(id=op.get('id', uuid.uuid4().hex)); project.zones.append(zone); added.append(zone.id)
                else:
                    zone = next((z for z in project.zones if z.id == op['id']), None)
                    if zone is None:
                        raise ServiceError('NOT_FOUND', '禁写区不存在')
                for key, attr in [('name', 'name'), ('kind', 'kind'), ('points_mm', 'points')]:
                    if key in op:
                        setattr(zone, attr, copy.deepcopy(op[key]))
                selected = zone.id
            elif kind in ('import_drawing', 'update_drawing', 'erase_drawing', 'restore_drawing'):
                if kind == 'import_drawing':
                    fields(op, {'path', 'id', 'name', *DRAWING_FIELDS}, {'path'})
                    drawing = self._import_drawing(op['path'])
                    drawing.width = min(50., project.width/2)
                    drawing.x = (project.width-drawing.width)/2
                    drawing.y = project.height*.3
                    if 'id' in op:
                        drawing.id = op['id']
                    project.drawings.append(drawing); added.append(drawing.id)
                else:
                    allowed = {'id', 'name', *DRAWING_FIELDS} if kind == 'update_drawing' else (
                        {'id', 'circles_mm'} if kind == 'erase_drawing' else {'id'})
                    fields(op, allowed, {'id', 'circles_mm'} if kind == 'erase_drawing' else {'id'})
                    drawing = next((d for d in project.drawings if d.id == op['id']), None)
                    if drawing is None:
                        raise ServiceError('NOT_FOUND', '图画对象不存在')
                if kind in ('import_drawing', 'update_drawing'):
                    for key, attr in DRAWING_FIELDS.items():
                        if key in op:
                            setattr(drawing, attr, op[key])
                    if 'name' in op:
                        drawing.name = op['name']
                elif kind == 'erase_drawing':
                    circles = op['circles_mm']
                    if not isinstance(circles, list) or not 1 <= len(circles) <= 20000:
                        raise ServiceError('INVALID_ARGUMENT', '擦除需要1到20000个圆')
                    for circle in circles:
                        if not isinstance(circle, (list, tuple)) or len(circle) != 3:
                            raise ServiceError('INVALID_ARGUMENT', '擦除圆须为 [x, y, radius]')
                        x, y, radius = (number(value, 'circle') for value in circle)
                        if not 0 < radius <= drawing.width:
                            raise ServiceError('INVALID_ARGUMENT', '擦除半径须大于0且不超过图画宽度')
                        drawing.erase.append([x/drawing.width, y/drawing.width, radius/drawing.width])
                else:
                    drawing.erase = []
                selected = drawing.id
                changed_drawings.add(drawing.id)
            elif kind == 'remove':
                fields(op, {'id'}, {'id'})
                found = False
                for attr in ('blocks', 'zones', 'drawings'):
                    old = getattr(project, attr); new = [o for o in old if o.id != op['id']]
                    found |= len(old) != len(new); setattr(project, attr, new)
                if not found:
                    raise ServiceError('NOT_FOUND', '对象不存在')
            elif kind == 'page':
                fields(op, {'width_mm', 'height_mm', 'pen_width_mm', 'margin_mm'})
                for key, attr in [('width_mm', 'width'), ('height_mm', 'height'), ('pen_width_mm', 'pen_width'), ('margin_mm', 'margin')]:
                    if key in op:
                        setattr(project, attr, number(op[key], key))
            elif kind in ('import_photo', 'photo'):
                fields(op, {'path', 'quad', 'edges', 'guides', 'opacity', 'offset_mm', 'rotation_degrees'}, {'path'} if kind == 'import_photo' else ())
                self._photo(project, op)
            elif kind == 'remove_photo':
                fields(op, set())
                project.image_data = ''
                project.image_quad = project.image_edges = project.image_guides = None
                project.image_offset = [0., 0.]
            else:
                raise ServiceError('INVALID_ARGUMENT', '未知文档操作', {'op': kind})
        # Convert staged drawings before changing the live editor or its undo
        # stack. Bad sources/settings must roll back the entire batch.
        if changed_drawings:
            core._validate_project(project)
            for drawing in project.drawings:
                if drawing.id in changed_drawings:
                    core.drawing_local_paths(drawing)
        if selected not in {o.id for o in [*project.blocks, *project.zones, *project.drawings]}:
            selected = None
        self._commit(project, selected)
        return {'revision': self.revision, 'added_ids': added}
