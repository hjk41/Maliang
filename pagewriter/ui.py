from __future__ import annotations

import base64
import copy
import math
from dataclasses import replace
from pathlib import Path

from PySide6.QtCore import Qt, QPointF, QRectF, QSettings, QTimer
from PySide6.QtGui import (QAction, QColor, QFont, QFontDatabase, QImage, QKeySequence,
                          QPainter, QPainterPath, QPen, QPixmap, QPolygonF, QImageReader,
                          QTextCharFormat, QTextCursor, QTransform)
from PySide6.QtWidgets import (QApplication, QCheckBox, QComboBox, QDialog,
    QDialogButtonBox, QDoubleSpinBox, QFileDialog, QFontComboBox, QFormLayout,
    QGraphicsItem, QGraphicsPathItem, QGraphicsPixmapItem, QGraphicsRectItem, QGraphicsScene,
    QGraphicsView, QGroupBox, QHBoxLayout, QLabel, QListWidget, QListWidgetItem,
    QMainWindow, QMenu, QMessageBox, QPushButton, QScrollArea, QSizePolicy, QSlider, QSplitter,
    QStackedWidget, QStyle, QStyleOptionGraphicsItem, QTextEdit, QToolBar, QToolButton, QVBoxLayout, QWidget)

from .inline_edit import hit_position, paint_cursor, lines_for, cursor_geometry
from .rectify import RectifyDialog
from . import __version__
from .resources import resource_path
from . import stroke_fonts as builtin_fonts

from .core import (TextBlock, ExclusionZone, Project, build_block_path, preflight, composed_local_path,
                   effective_column_height, sync_block_flow, Drawing, drawing_local_path, build_drawing_path,
                   drawing_local_paths, drawing_strokes, apply_erase,
                   image_to_data,
                   export_svg, offline_gcode, save_project, load_project, starter_project, blank_project, PAPER_SIZES,
                   path_subpaths, font_warnings, layout_info, prepare_stroke_document)


BLUE = QColor("#245cad")
RED = QColor("#db5965")
VENDOR_FONT_DIR = Path(r"C:\Program Files (x86)\写字机软件\gcodeFonts")
VENDOR_FONTS = [("快写（推荐）", "超连笔快写-3500字_kvenjoy_gantry.gfont"),
                ("快写 · 完整字库", "超连笔快写_kvenjoy_gantry.gfont"),
                ("乐行书", "乐行书_kvenjoy_乐乐.gfont"),
                ("乐行书 2", "乐行书2_kvenjoy_乐乐.gfont"),
                ("楷书 · 田英章", "田英章楷书_kvenjoy_瑞瑞.gfont")]


def ink_pen(color, width):
    pen = QPen(color, width)
    pen.setCapStyle(Qt.PenCapStyle.RoundCap)
    pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
    return pen


def spin(low=-1000, high=1000, value=0, suffix=" mm", decimals=2):
    widget = QDoubleSpinBox()
    widget.setRange(low, high)
    widget.setDecimals(decimals)
    widget.setValue(value)
    widget.setSuffix(suffix)
    widget.setKeyboardTracking(False)
    return widget


class ResizeHandle(QGraphicsRectItem):
    """Screen-sized square (or round direction handle) on a selected box; its owner applies the change."""
    SIZE = 9

    def __init__(self, owner, role, cursor):
        size = self.SIZE+4 if role == "rotate" else self.SIZE
        super().__init__(-size/2, -size/2, size, size, owner)
        self.owner, self.role = owner, role
        self.setFlag(QGraphicsItem.GraphicsItemFlag.ItemIgnoresTransformations)
        self.setPen(QPen(BLUE, 1.5))
        self.setBrush(QColor("white"))
        self.setCursor(cursor)
        self.setZValue(50)
        self.setToolTip("拖动改变文字方向（吸附到 15°，按住 Shift 自由旋转）" if role == "rotate" else "拖动调整大小")

    def paint(self, painter, option, widget=None):
        if self.role != "rotate":
            return super().paint(painter, option, widget)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(self.pen())
        painter.setBrush(self.brush())
        painter.drawEllipse(self.rect())
        # Small arrow pointing to the text's "up".
        painter.setBrush(BLUE)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.drawPolygon(QPolygonF([QPointF(0, -3.5), QPointF(3, 1.5), QPointF(-3, 1.5)]))

    def mousePressEvent(self, event):
        if event.button() != Qt.MouseButton.LeftButton:
            return event.ignore()
        self.owner.begin_resize()
        event.accept()

    def mouseMoveEvent(self, event):
        self.owner.drag_resize(self.role, event.scenePos(), event.modifiers())

    def mouseReleaseEvent(self, event):
        self.owner.end_resize()


class ResizableMixin:
    """Selected boxes show handles (not in the writing stage); geometry is committed on release."""

    def sync_handles(self):
        show = self.isSelected() and getattr(self.window, "stage", 1) != 2 and self.scene() is not None
        handles = getattr(self, "handles", {})
        wanted = self.handle_positions() if show else {}
        for role in [r for r in handles if r not in wanted]:
            handle = handles.pop(role)
            if handle.scene() is not None:
                handle.scene().removeItem(handle)
        for role, (point, cursor) in wanted.items():
            if role not in handles:
                handles[role] = ResizeHandle(self, role, cursor)
            handles[role].setPos(point)
        self.handles = handles

    def itemChange(self, change, value):
        if change == QGraphicsItem.GraphicsItemChange.ItemSelectedHasChanged:
            self.setZValue(self.base_z+10 if self.isSelected() else self.base_z)
            self.sync_handles()
        return super().itemChange(change, value)

    def begin_resize(self):
        self.resize_inverse = self.sceneTransform().inverted()[0]
        self.resize_result = None
        if hasattr(self, "frame"):   # boxes that can turn (text, pictures)
            self.rotate_center = self.frame().center()
            self.rotate_center_scene = self.mapToScene(self.rotate_center)

    ROTATE_OFFSET = 6.0  # mm above the top edge, along the object's own "up"

    def rotate_anchor(self):
        f = self.frame()
        return QPointF(f.width()/2, -self.ROTATE_OFFSET)

    @staticmethod
    def snap_angle(angle, free=False):
        angle = angle % 360
        angle = round(angle) if free else round(angle/15)*15
        return float(angle % 360)

    def drag_rotate(self, scene_point, modifiers):
        center, local_center = self.rotate_center_scene, self.rotate_center
        dx, dy = scene_point.x()-center.x(), scene_point.y()-center.y()
        if math.hypot(dx, dy) < 1e-6:
            return
        # The handle marks the text's "up"; for rotation r, local (0,-1) points to (sin r, -cos r).
        free = bool(modifiers is not None and modifiers & Qt.KeyboardModifier.ShiftModifier)
        angle = self.snap_angle(math.degrees(math.atan2(dx, -dy)), free)
        offset = QTransform().rotate(angle).map(local_center)
        position = QPointF(center.x()-offset.x(), center.y()-offset.y())
        self.setRotation(angle)
        self.setPos(position)
        self.resize_result = ("rotate", angle, position)
        self.window.statusBar().showMessage(f"方向 {angle:g}°（0° 为正常朝上；按住 Shift 可任意角度）")

    def paint_frame(self, painter):
        """One dashed frame on the box plus the stalk to the direction handle."""
        pen = QPen(QColor("#3d7be0"), 1, Qt.PenStyle.DashLine)
        pen.setCosmetic(True)
        painter.setPen(pen)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        frame = self.frame()
        painter.drawRect(frame)
        if getattr(self.window, "stage", 1) != 2:
            painter.setPen(QPen(QColor("#3d7be0"), 0))
            painter.drawLine(QPointF(frame.width()/2, 0), self.rotate_anchor())

    def end_resize(self):
        # Commit after the event returns: refresh() rebuilds the scene, which
        # deletes this item and the handle that is still delivering the event.
        result, self.resize_result = self.resize_result, None
        if result is not None:
            commit = self.commit_resize
            QTimer.singleShot(0, lambda: commit(result))


class TextItem(ResizableMixin, QGraphicsPathItem):
    # Selection frame and caret extend this far beyond the text; keep them inside
    # boundingRect so dragging repaints the whole area (no trails).
    PAINT_MARGIN = 1.5
    base_z = 5

    def __init__(self, window, block, collision=False):
        super().__init__(build_block_path(replace(block, x=0, y=0, rotation=0)))
        self.window, self.block_id, self.block = window, block.id, block
        self.column_rect = None
        self.guides = None
        # Columns may have grown downward to flow around protected zones.
        self.set_columns(block.width, effective_column_height(block) if block.column_height > 0 else 0)
        self.setPos(block.x, block.y)
        self.setRotation(block.rotation)
        self.setPen(ink_pen(RED if collision else BLUE, window.project.pen_width))
        self.setBrush(Qt.BrushStyle.NoBrush)
        self.setFlags(QGraphicsItem.GraphicsItemFlag.ItemIsMovable |
                      QGraphicsItem.GraphicsItemFlag.ItemIsSelectable)
        self.setZValue(5)
        self.setToolTip(f"{block.name}\n单击原位编辑；使用移动工具拖动排版；选中后拖动小方块调整宽高")

    def set_columns(self, width, height):
        self.prepareGeometryChange()
        block = self.block
        self.frame_width = width
        if height > 0:
            self.column_rect = QRectF(0, 0, width, height)
            guides = QPainterPath()
            column_width = (width-(block.columns-1)*block.column_gap)/block.columns
            for index in range(block.columns):
                guides.addRect(index*(column_width+block.column_gap),0,column_width,height)
            if self.guides is None:
                self.guides = QGraphicsPathItem(self)
                self.guides.setPen(QPen(QColor("#a8b6ca"),.16,Qt.PenStyle.DashLine))
                self.guides.setBrush(Qt.BrushStyle.NoBrush)
                self.guides.setAcceptedMouseButtons(Qt.MouseButton.NoButton)
                self.guides.setZValue(-.5)
            self.guides.setPath(guides)
        else:
            self.column_rect = None

    def frame(self):
        """The box itself in local mm: its width by the column (or text) height."""
        if getattr(self, "column_rect", None) is not None:
            return QRectF(self.column_rect)
        width = getattr(self, "frame_width", None) or self.block.width
        return QRectF(0, 0, width, max(2., self.path().boundingRect().bottom()))

    def handle_positions(self):
        f = self.frame()
        result = {"left": (QPointF(0, f.height()/2), Qt.CursorShape.SizeHorCursor),
                  "right": (QPointF(f.width(), f.height()/2), Qt.CursorShape.SizeHorCursor),
                  "rotate": (self.rotate_anchor(), Qt.CursorShape.OpenHandCursor)}
        if self.column_rect is not None:
            result["bottom"] = (QPointF(f.width()/2, f.height()), Qt.CursorShape.SizeVerCursor)
            result["corner"] = (QPointF(f.width(), f.height()), Qt.CursorShape.SizeFDiagCursor)
        return result

    def begin_resize(self):
        super().begin_resize()
        self.resize_start = (self.block.width, self.block.column_height, QPointF(self.pos()))

    def drag_resize(self, role, scene_point, modifiers=None):
        if role == "rotate":
            return self.drag_rotate(scene_point, modifiers)
        width0, height0, origin = self.resize_start
        local = self.resize_inverse.map(scene_point)
        block = self.block
        min_width = (block.columns-1)*block.column_gap+2*block.columns
        width, height, shift = width0, height0, 0.
        if role in ("right", "corner"):
            width = max(min_width, round(local.x(), 1))
        elif role == "left":
            width = max(min_width, round(width0-local.x(), 1))
            shift = width0-width
        if role in ("bottom", "corner") and height0 > 0:
            height = max(2., round(local.y(), 1))
        position = origin+QTransform().rotate(block.rotation).map(QPointF(shift, 0))
        # Preview reflows around protected zones exactly as the result will.
        candidate = replace(block, width=width, column_height=height, x=position.x(), y=position.y())
        try:
            sync_block_flow(candidate, self.window.project)
            path = composed_local_path(candidate)
        except ValueError:
            return
        self.set_columns(width, effective_column_height(candidate) if height > 0 else 0)
        self.setPath(path)
        self.setPos(position)
        self.resize_result = ("size", width, height, QPointF(self.pos()))
        self.sync_handles()
        self.window.statusBar().showMessage(f"宽 {width:.1f} mm" + (f"，栏高 {height:.1f} mm" if height0 > 0 else ""))

    def commit_resize(self, result):
        window = self.window
        window.record()
        block = window.block_by_id(self.block_id)
        if result[0] == "rotate":
            _, angle, position = result
            block.rotation, block.x, block.y = angle, position.x(), position.y()
            window.refresh()
            window.statusBar().showMessage(f"文字方向已设为 {angle:g}°")
            return
        _, width, height, position = result
        block.width, block.x, block.y = width, position.x(), position.y()
        if block.column_height > 0:
            block.column_height = height
        window.refresh()
        window.statusBar().showMessage(f"文字框已调整为宽 {width:.1f} mm" + (f"、栏高 {height:.1f} mm" if block.column_height > 0 else ""))

    def shape(self):
        # Include blank line space in the hit area so text can be selected easily.
        shape = QPainterPath()
        shape.setFillRule(Qt.FillRule.WindingFill)
        shape.addRect(self.path().boundingRect().adjusted(-1, -1, 1, 1))
        if getattr(self,"column_rect",None):
            shape.addRect(self.column_rect)
        return shape

    def content_rect(self):
        rect = super().boundingRect()
        if getattr(self,"column_rect",None):
            rect = rect.united(self.column_rect)
        return rect

    def boundingRect(self):
        # Frame, caret and the direction stalk must all lie inside, so moving
        # or resizing repaints everything that was drawn (no trails).
        m = self.PAINT_MARGIN
        rect = self.content_rect().united(self.frame())
        anchor = self.rotate_anchor()
        rect = rect.united(QRectF(anchor.x()-.5, anchor.y(), 1, -anchor.y()))
        return rect.adjusted(-m, -m, m, m)

    def paint(self, painter, option, widget=None):
        # Qt's own selection highlight follows the (padded) boundingRect and
        # would show a second, larger frame; draw one frame on the real box.
        plain = QStyleOptionGraphicsItem(option)
        plain.state &= ~QStyle.StateFlag.State_Selected
        super().paint(painter, plain, widget)
        paint_cursor(self, painter)
        if self.isSelected():
            pen = QPen(QColor("#3d7be0"), 1, Qt.PenStyle.DashLine)
            pen.setCosmetic(True)
            painter.setPen(pen)
            painter.setBrush(Qt.BrushStyle.NoBrush)
            frame = self.frame()
            painter.drawRect(frame)
            if getattr(self.window, "stage", 1) != 2:
                painter.setPen(QPen(QColor("#3d7be0"), 0))
                painter.drawLine(QPointF(frame.width()/2, 0), self.rotate_anchor())

    def mousePressEvent(self, event):
        if self.window.canvas.editing_id == self.block_id:
            self.window.canvas.place_cursor(self, event.pos(), bool(event.modifiers() & Qt.KeyboardModifier.ShiftModifier))
            event.accept()
            return
        self.window.select_block(self.block_id)
        self.start_pos = self.pos()
        super().mousePressEvent(event)

    def mouseReleaseEvent(self, event):
        super().mouseReleaseEvent(event)
        if self.window.canvas.editing_id == self.block_id:
            event.accept()
            return
        if self.pos() != self.start_pos:
            self.window.record()
            block = self.window.block_by_id(self.block_id)
            block.x, block.y = self.pos().x(), self.pos().y()
            self.window.refresh()

    def mouseDoubleClickEvent(self, event):
        self.window.select_block(self.block_id)
        self.window.canvas.editing_id = self.block_id
        self.window.canvas.place_cursor(self, event.pos())
        event.accept()


def is_axis_rect(points):
    return len(points) == 4 and all(
        abs(a[0]-b[0]) < 1e-9 or abs(a[1]-b[1]) < 1e-9 for a, b in zip(points, points[1:]+points[:1]))


class ZoneItem(ResizableMixin, QGraphicsPathItem):
    base_z = 3
    MIN_SIZE = .5
    RECT_HANDLES = {"tl": (0, 0, Qt.CursorShape.SizeFDiagCursor), "t": (.5, 0, Qt.CursorShape.SizeVerCursor),
                    "tr": (1, 0, Qt.CursorShape.SizeBDiagCursor), "r": (1, .5, Qt.CursorShape.SizeHorCursor),
                    "br": (1, 1, Qt.CursorShape.SizeFDiagCursor), "b": (.5, 1, Qt.CursorShape.SizeVerCursor),
                    "bl": (0, 1, Qt.CursorShape.SizeBDiagCursor), "l": (0, .5, Qt.CursorShape.SizeHorCursor)}

    def __init__(self, window, zone):
        super().__init__()
        self.window, self.zone_id = window, zone.id
        self.rect_mode = is_axis_rect([tuple(p) for p in zone.points])
        self.set_points([tuple(map(float, p)) for p in zone.points])
        self.setPen(QPen(RED, .25, Qt.PenStyle.DashLine))
        self.setBrush(QColor(219, 89, 101, 34))
        self.setZValue(3)
        self.setFlags(QGraphicsItem.GraphicsItemFlag.ItemIsSelectable |
                      QGraphicsItem.GraphicsItemFlag.ItemIsMovable)
        self.setToolTip(f"禁写区：{zone.name}；拖动移动，选中后拖动小方块调整大小，Delete 删除")

    def set_points(self, points):
        self.points = points
        path = QPainterPath()
        path.addPolygon(QPolygonF([QPointF(*p) for p in points]))
        path.closeSubpath()
        self.setPath(path)

    def handle_positions(self):
        if self.rect_mode:
            r = self.path().boundingRect()
            return {role: (QPointF(r.left()+fx*r.width(), r.top()+fy*r.height()), cursor)
                    for role, (fx, fy, cursor) in self.RECT_HANDLES.items()}
        return {f"v{i}": (QPointF(*p), Qt.CursorShape.SizeAllCursor) for i, p in enumerate(self.points)}

    def begin_resize(self):
        super().begin_resize()
        self.resize_start = list(self.points)

    def drag_resize(self, role, scene_point, modifiers=None):
        local = self.resize_inverse.map(scene_point)
        x, y = round(local.x(), 2), round(local.y(), 2)
        if self.rect_mode:
            xs, ys = [p[0] for p in self.resize_start], [p[1] for p in self.resize_start]
            left, top, right, bottom = min(xs), min(ys), max(xs), max(ys)
            if "l" in role: left = min(x, right-self.MIN_SIZE)
            if "r" in role: right = max(x, left+self.MIN_SIZE)
            if "t" in role: top = min(y, bottom-self.MIN_SIZE)
            if "b" in role: bottom = max(y, top+self.MIN_SIZE)
            points = [(left, top), (right, top), (right, bottom), (left, bottom)]
        else:
            points = list(self.resize_start)
            points[int(role[1:])] = (x, y)
        self.set_points(points)
        offset = self.pos()
        self.resize_result = [(px+offset.x(), py+offset.y()) for px, py in points]
        self.sync_handles()

    def commit_resize(self, points):
        window = self.window
        window.record()
        zone = next(z for z in window.project.zones if z.id == self.zone_id)
        zone.points = points
        window.refresh()
        window.statusBar().showMessage("保护区大小已调整")

    def mousePressEvent(self, event):
        self.window.select_block(self.zone_id)
        self.start_pos = self.pos()
        super().mousePressEvent(event)

    def mouseReleaseEvent(self, event):
        super().mouseReleaseEvent(event)
        delta = self.pos() - self.start_pos
        if delta.manhattanLength() > 0:
            self.window.record()
            zone = next(z for z in self.window.project.zones if z.id == self.zone_id)
            zone.points = [(x + delta.x(), y + delta.y()) for x, y in zone.points]
            self.window.refresh()


class DrawingItem(ResizableMixin, QGraphicsPathItem):
    """A picture written as single strokes; move, resize (keeps proportions) and turn like text."""
    PAINT_MARGIN = 1.5
    base_z = 6

    def __init__(self, window, drawing, collision=False):
        self.window, self.drawing_id, self.drawing = window, drawing.id, drawing
        self.preview_scale = 1.0
        super().__init__(drawing_local_path(drawing))
        self.setPos(drawing.x, drawing.y)
        self.setRotation(drawing.rotation)
        self.setPen(ink_pen(RED if collision else BLUE, window.project.pen_width))
        self.setBrush(Qt.BrushStyle.NoBrush)
        self.setFlags(QGraphicsItem.GraphicsItemFlag.ItemIsMovable | QGraphicsItem.GraphicsItemFlag.ItemIsSelectable)
        self.setZValue(self.base_z)
        self.setToolTip(f"{drawing.name}\n拖动换位置；拖右下角小方块改大小；拖上方圆形把手改方向")

    def frame(self):
        d = self.drawing
        return QRectF(0, 0, d.width*self.preview_scale, d.height*self.preview_scale)

    def boundingRect(self):
        m = self.PAINT_MARGIN
        anchor = self.rotate_anchor()
        rect = super().boundingRect().united(self.frame()).united(QRectF(anchor.x()-.5, anchor.y(), 1, -anchor.y()))
        return rect.adjusted(-m, -m, m, m)

    def shape(self):
        shape = QPainterPath()
        shape.addRect(self.frame())   # click anywhere inside the picture
        return shape

    def paint(self, painter, option, widget=None):
        plain = QStyleOptionGraphicsItem(option)
        plain.state &= ~QStyle.StateFlag.State_Selected
        super().paint(painter, plain, widget)
        if self.isSelected():
            self.paint_frame(painter)

    def handle_positions(self):
        f = self.frame()
        return {"corner": (f.bottomRight(), Qt.CursorShape.SizeFDiagCursor),
                "right": (QPointF(f.width(), f.height()/2), Qt.CursorShape.SizeHorCursor),
                "rotate": (self.rotate_anchor(), Qt.CursorShape.OpenHandCursor)}

    def drag_resize(self, role, scene_point, modifiers=None):
        if role == "rotate":
            return self.drag_rotate(scene_point, modifiers)
        d = self.drawing
        local = self.resize_inverse.map(scene_point)
        wanted = local.x() if role == "right" else max(local.x(), local.y()*d.width/max(d.height, 1e-6))
        width = round(min(2000., max(2., wanted)), 1)
        # Fast preview by scaling; the strokes are regenerated on release.
        self.prepareGeometryChange()
        self.preview_scale = width/d.width
        self.setPath(QTransform.fromScale(self.preview_scale, self.preview_scale).map(drawing_local_path(d)))
        self.resize_result = ("size", width)
        self.sync_handles()
        self.window.statusBar().showMessage(f"图画宽 {width:.1f} mm，高约 {d.height*self.preview_scale:.1f} mm")

    def commit_resize(self, result):
        window = self.window
        window.record()
        drawing = window.drawing_by_id(self.drawing_id)
        if result[0] == "rotate":
            _, angle, position = result
            drawing.rotation, drawing.x, drawing.y = angle, position.x(), position.y()
        else:
            drawing.width = result[1]
        window.refresh()

    def mousePressEvent(self, event):
        self.window.select_block(self.drawing_id)
        self.start_pos = self.pos()
        super().mousePressEvent(event)

    def mouseReleaseEvent(self, event):
        super().mouseReleaseEvent(event)
        if self.pos() != self.start_pos:
            self.window.record()
            drawing = self.window.drawing_by_id(self.drawing_id)
            drawing.x, drawing.y = self.pos().x(), self.pos().y()
            self.window.refresh()


class BackgroundItem(QGraphicsPixmapItem):
    def __init__(self, window, pixmap):
        super().__init__(pixmap)
        self.window = window
        self.setZValue(1)
        self.setOpacity(window.project.image_opacity)
        self.setFlag(QGraphicsItem.GraphicsItemFlag.ItemIsMovable, not window.project.image_locked)

    def mousePressEvent(self, event):
        self.start_pos = self.pos()
        super().mousePressEvent(event)

    def mouseReleaseEvent(self, event):
        super().mouseReleaseEvent(event)
        if self.pos() != self.start_pos:
            self.window.record()
            self.window.project.image_offset = [self.pos().x(), self.pos().y()]
            self.window.refresh()


class Canvas(QGraphicsView):
    def __init__(self, window):
        super().__init__()
        self.window = window
        self.setScene(QGraphicsScene(self))
        self.setRenderHints(QPainter.RenderHint.Antialiasing | QPainter.RenderHint.SmoothPixmapTransform)
        self.setBackgroundBrush(QColor("#e7ebf1"))
        self.setDragMode(QGraphicsView.DragMode.ScrollHandDrag)
        self.setTransformationAnchor(QGraphicsView.ViewportAnchor.AnchorUnderMouse)
        self.editing_id = None
        self.selecting_text = False
        self.preedit_text = ""
        self.preedit_attributes = []
        self.setAttribute(Qt.WidgetAttribute.WA_InputMethodEnabled, True)
        self.mode = "select"
        self.points = []
        self.temporary = None
        # Magnifier while picking a pen position on the photo, like the corner picker.
        self.loupe = QLabel(self.viewport())
        self.loupe.setFixedSize(self.LOUPE, self.LOUPE)
        self.loupe.setStyleSheet("border:2px solid #ffd54a;background:#222")
        self.loupe.hide()

    LOUPE = 180

    def update_loupe(self, view_pos):
        if self.mode != "reference":
            self.loupe.hide()
            return
        center = self.mapToScene(view_pos.toPoint())
        radius = self.LOUPE/2/(self.transform().m11()*4)   # four times the current view magnification
        pixmap = QPixmap(self.LOUPE, self.LOUPE)
        pixmap.fill(QColor("#2d3138"))
        painter = QPainter(pixmap)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        self.scene().render(painter, QRectF(0, 0, self.LOUPE, self.LOUPE),
                            QRectF(center.x()-radius, center.y()-radius, 2*radius, 2*radius))
        painter.setPen(QPen(QColor("#ffd54a"), 1))
        half = self.LOUPE/2
        painter.drawLine(QPointF(half, 0), QPointF(half, half-6))
        painter.drawLine(QPointF(half, half+6), QPointF(half, self.LOUPE))
        painter.drawLine(QPointF(0, half), QPointF(half-6, half))
        painter.drawLine(QPointF(half+6, half), QPointF(self.LOUPE, half))
        painter.end()
        self.loupe.setPixmap(pixmap)
        width = self.viewport().width()
        left = view_pos.x() > width-self.LOUPE-40 and view_pos.y() < self.LOUPE+40
        self.loupe.move(10 if left else width-self.LOUPE-10, 10)
        self.loupe.show()
        self.loupe.raise_()

    def leaveEvent(self, event):
        self.loupe.hide()
        super().leaveEvent(event)

    def place_cursor(self, item, point, extend=False):
        block = self.window.block_by_id(item.block_id)
        cursor = self.window.editor.textCursor()
        cursor.setPosition(hit_position(block, point), QTextCursor.MoveMode.KeepAnchor if extend else QTextCursor.MoveMode.MoveAnchor)
        self.window.editor.setTextCursor(cursor)
        self.selecting_text = True
        self.setDragMode(QGraphicsView.DragMode.NoDrag)
        self.setCursor(Qt.CursorShape.IBeamCursor)
        self.setFocus(Qt.FocusReason.MouseFocusReason)
        self.refresh_ime()
        self.window.statusBar().showMessage("原位编辑中：直接输入或拖选文字；移动排版请使用左侧移动工具。")
        self.scene().update()

    def start_editing(self, block_id, select_all=False):
        """Put a text box into editing and make sure the IME (e.g. Chinese pinyin) is on."""
        item = next((i for i in self.window.text_items if i.block_id == block_id), None)
        if item is None:
            return False
        if self.mode != "select":
            self.set_mode("select")
        self.window.select_block(block_id)
        self.editing_id = block_id
        cursor = self.window.editor.textCursor()
        if select_all:
            cursor.select(QTextCursor.SelectionType.Document)
        else:
            cursor.movePosition(QTextCursor.MoveOperation.End)
        self.window.editor.setTextCursor(cursor)
        self.setDragMode(QGraphicsView.DragMode.NoDrag)
        self.setCursor(Qt.CursorShape.IBeamCursor)
        self.setFocus(Qt.FocusReason.OtherFocusReason)
        self.refresh_ime()
        self.scene().update()
        return True

    @staticmethod
    def refresh_ime():
        # Windows asks "does this widget take IME input?" when focus arrives,
        # often before editing starts; ask again so pinyin input switches on.
        QApplication.inputMethod().update(Qt.InputMethodQuery.ImQueryAll)

    def inputMethodQuery(self, query):
        if query == Qt.InputMethodQuery.ImEnabled:
            return bool(self.editing_id) or self.mode == "select"
        if self.editing_id:
            if query == Qt.InputMethodQuery.ImCursorRectangle:
                block=self.window.block_by_id(self.editing_id)
                item=next((i for i in self.window.text_items if i.block_id==self.editing_id),None)
                if block and item:
                    rect=cursor_geometry(block,self.window.editor.textCursor().position())
                    if rect:return self.mapFromScene(item.mapRectToScene(rect)).boundingRect()
            return self.window.editor.inputMethodQuery(query)
        return super().inputMethodQuery(query)

    def inputMethodEvent(self, event):
        if self.editing_id:
            self.preedit_text = event.preeditString()
            self.preedit_attributes = list(event.attributes())
            previous_guard=self.window.guard
            # QTextEdit emits textChanged for temporary preedit layout changes.
            # Only committed insertion/replacement belongs in project history.
            if not event.commitString() and not event.replacementLength():
                self.window.guard=True
            try:
                QApplication.sendEvent(self.window.editor, event)
            finally:
                self.window.guard=previous_guard
            self.scene().update()
        else:
            super().inputMethodEvent(event)

    def set_mode(self, mode):
        if mode == "select" and getattr(self.window, "stage", 1) == 2:
            mode = "view"
        self.mode = mode
        if mode != "reference" and hasattr(self, "loupe"):
            self.loupe.hide()
        button = getattr(self.window, "erase_button", None)
        if button is not None and button.isChecked() != (mode == "erase"):
            button.blockSignals(True); button.setChecked(mode == "erase"); button.blockSignals(False)
        if mode != "select":
            self.editing_id = None
            self.preedit_text = ""
            self.preedit_attributes = []
        self.points = []
        self.setDragMode(QGraphicsView.DragMode.ScrollHandDrag if mode in {"select","move","view"}
                         else QGraphicsView.DragMode.NoDrag)
        self.setCursor(Qt.CursorShape.ArrowCursor if mode in {"select","move","view"} else Qt.CursorShape.CrossCursor)
        self.window.statusBar().showMessage({"select": "单击页面文字直接输入；要换位置请用「移动文字」", "move": "拖动文字换位置；点「编辑文字」恢复输入",
            "rect": "在照片上拖出一个矩形，圈住不能写的地方", "polygon": "逐点单击；双击完成；Esc 取消", "reference": "点击照片中的位置：右上角放大镜显示十字线处；Ctrl+滚轮可放大画面",
            "view": "书写时页面只供查看；对准和试写时会提示点照片",
            "erase": "按住左键在选中的图画上拖动，擦掉经过的线条；再点「擦除线条」或按 Esc 结束"}[mode])

    def mousePressEvent(self, event):
        if self.mode == "reference":
            p = self.mapToScene(event.position().toPoint())
            if 0 <= p.x() <= self.window.project.width and 0 <= p.y() <= self.window.project.height:
                self.set_mode("select")
                self.window.device_panel.reference_clicked(p.x(), p.y())
            event.accept()
            return
        if self.mode == "view":
            event.accept()
            return
        if self.mode == "erase":
            if event.button() == Qt.MouseButton.LeftButton:
                self.window.erase_at(self.mapToScene(event.position().toPoint()), start=True)
            event.accept()
            return
        if event.button() == Qt.MouseButton.LeftButton and isinstance(self.itemAt(event.position().toPoint()), ResizeHandle):
            self.resizing = True
            return super().mousePressEvent(event)
        if self.mode == "select":
            point=self.mapToScene(event.position().toPoint())
            item=next((item for item in reversed(self.window.text_items) if item.shape().contains(item.mapFromScene(point))),None)
            if item and event.button() == Qt.MouseButton.LeftButton:
                if self.window.selected_id != item.block_id:
                    self.window.select_block(item.block_id)
                self.editing_id=item.block_id
                self.place_cursor(item,item.mapFromScene(point),bool(event.modifiers() & Qt.KeyboardModifier.ShiftModifier))
                event.accept()
                return
            if not any(item.block_id == self.editing_id and item.shape().contains(item.mapFromScene(self.mapToScene(event.position().toPoint()))) for item in self.window.text_items):
                self.editing_id = None
                self.preedit_text = ""
                self.preedit_attributes = []
            return super().mousePressEvent(event)
        if self.mode == "move":
            return super().mousePressEvent(event)
        p = self.mapToScene(event.position().toPoint())
        if event.button() == Qt.MouseButton.LeftButton:
            if self.mode == "rect":
                self.points = [p]
            else:
                self.points.append(p)
            event.accept()

    def mouseMoveEvent(self, event):
        if self.editing_id and self.selecting_text and event.buttons() & Qt.MouseButton.LeftButton:
            item = next((i for i in self.window.text_items if i.block_id == self.editing_id), None)
            if item:
                self.place_cursor(item, item.mapFromScene(self.mapToScene(event.position().toPoint())), True)
                event.accept()
                return
        p = self.mapToScene(event.position().toPoint())
        self.window.coordinates.setText(f"页面  X {p.x():.1f}  Y {p.y():.1f} mm")
        self.update_loupe(event.position())
        if self.mode == "erase":
            if event.buttons() & Qt.MouseButton.LeftButton:
                self.window.erase_at(p)
            event.accept()
            return
        if self.mode not in {"select","move"} and self.points:
            if self.temporary:
                self.scene().removeItem(self.temporary)
            path = QPainterPath(self.points[0])
            if self.mode == "rect":
                path.addRect(QRectF(self.points[0], p).normalized())
            else:
                for q in self.points[1:]:
                    path.lineTo(q)
                path.lineTo(p)
            self.temporary = self.scene().addPath(path, QPen(RED, .3, Qt.PenStyle.DashLine))
            self.temporary.setZValue(20)
        return super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        self.selecting_text = False
        if self.mode == "erase":
            self.window.erase_finish()
            event.accept()
            return
        if getattr(self, "resizing", False):
            self.resizing = False
            return super().mouseReleaseEvent(event)
        if self.editing_id:
            event.accept()
            return
        if self.mode == "rect" and self.points:
            rect = QRectF(self.points[0], self.mapToScene(event.position().toPoint())).normalized()
            if rect.width() > .5 and rect.height() > .5:
                self.window.add_zone([(rect.left(), rect.top()), (rect.right(), rect.top()),
                                      (rect.right(), rect.bottom()), (rect.left(), rect.bottom())])
            elif self.temporary:
                self.scene().removeItem(self.temporary)
            self.temporary = None
            self.set_mode("select")
            return
        return super().mouseReleaseEvent(event)

    def mouseDoubleClickEvent(self, event):
        if self.mode in {"view","reference"}:
            event.accept()
            return
        if self.mode == "select":
            point=self.mapToScene(event.position().toPoint())
            item=next((item for item in reversed(self.window.text_items) if item.shape().contains(item.mapFromScene(point))),None)
            if item:
                self.window.select_block(item.block_id)
                self.editing_id=item.block_id
                self.place_cursor(item,item.mapFromScene(point))
                event.accept()
                return
        if self.mode == "polygon":
            if len(self.points) >= 3:
                self.window.add_zone([(p.x(), p.y()) for p in self.points])
            elif self.temporary:
                self.scene().removeItem(self.temporary)
            self.temporary = None
            self.set_mode("select")
            return
        return super().mouseDoubleClickEvent(event)

    def keyPressEvent(self, event):
        if self.editing_id and event.key() != Qt.Key.Key_Escape:
            QApplication.sendEvent(self.window.editor, event)
            self.scene().update()
            return
        if event.key() == Qt.Key.Key_Escape and self.mode == "erase":
            return self.set_mode("select")
        if event.key() == Qt.Key.Key_Escape:
            self.editing_id = None
            self.preedit_text = ""
            self.preedit_attributes = []
            self.setDragMode(QGraphicsView.DragMode.ScrollHandDrag)
            self.setCursor(Qt.CursorShape.ArrowCursor)
            self.scene().update()
            if self.temporary:
                self.scene().removeItem(self.temporary)
                self.temporary = None
            self.set_mode("select")
        elif event.key() == Qt.Key.Key_Delete and self.mode != "view":
            self.window.delete_selected()
        else:
            super().keyPressEvent(event)

    def wheelEvent(self, event):
        if event.modifiers() & Qt.KeyboardModifier.ControlModifier:
            self.window.zoom.setValue(self.window.zoom.value() + (10 if event.angleDelta().y() > 0 else -10))
            event.accept()
        else:
            super().wheelEvent(event)


class WritingEdit(QTextEdit):
    def __init__(self,window):
        super().__init__()
        self.window = window

    def keyPressEvent(self,event):
        if event.key() == Qt.Key.Key_U and event.modifiers() & Qt.KeyboardModifier.ControlModifier:
            self.window.apply_underline()
            event.accept()
            return
        super().keyPressEvent(event)


class MainWindow(QMainWindow):
    def __init__(self, blank=False):
        super().__init__()
        self.setWindowTitle(f"马良 · Maliang {__version__}")
        self.resize(1500, 980)
        # The app starts on an empty A4 page; the demo project is kept for tests.
        self.project = blank_project() if blank else starter_project()
        self.filename = None
        self.selected_id = self.project.blocks[0].id if self.project.blocks else None
        self.undo_stack, self.redo_stack = [], []
        self.guard = False
        self.dirty = False
        self.replay_parts = []
        self.replay_timer = QTimer(self)
        self.replay_timer.timeout.connect(self.advance_replay)
        self.replay_index = 0
        self.build_ui()
        self.refresh()
        QTimer.singleShot(0, self.fit_page)

    STAGES = ("① 照片", "② 批注", "③ 书写")
    STAGE_HINTS = ("导入书页照片，量好尺寸，圈出不能写的地方。",
                   "单击页面文字直接输入；拖选文字后用上方工具栏改字体和字号。",
                   "按左侧步骤连接写字机、对准、校准笔高度，然后书写。")

    def build_ui(self):
        self.setStyleSheet("""
            QMainWindow { background: #f5f7fb; }
            QWidget { color:#25324b; font-family:'Microsoft YaHei UI'; font-size:12px; }
            QToolBar { background:white; border:0; spacing:8px; padding:8px 12px; }
            QPushButton, QToolButton { background:#fff; border:1px solid #d7deea; border-radius:6px; padding:7px 11px; }
            QPushButton:hover, QToolButton:hover { background:#edf3ff; border-color:#8caee4; }
            QPushButton:pressed { background:#dfeaff; }
            QPushButton:checked { background:#e3edff; border-color:#7197d8; color:#245cad; }
            QPushButton:disabled { color:#a5b0c2; }
            QPushButton[stage="true"] { border:0; border-radius:16px; padding:7px 18px; font-size:14px; color:#5d6b82; }
            QPushButton[stage="true"]:checked { background:#245cad; color:white; font-weight:700; }
            QPushButton[primary="true"] { background:#245cad; color:white; border-color:#245cad; font-weight:600; padding:9px 12px; }
            QPushButton[primary="true"]:hover { background:#2f6bc2; }
            QGroupBox { background:white; border:1px solid #e0e5ef; border-radius:8px; margin-top:14px; padding:14px 9px 9px; font-weight:600; }
            QGroupBox::title { subcontrol-origin:margin; left:12px; padding:0 4px; }
            QLineEdit,QDoubleSpinBox,QComboBox,QTextEdit,QListWidget { background:white; border:1px solid #d7deea; border-radius:4px; padding:4px; }
            QLabel[hint="true"] { color:#5d6b82; font-weight:400; }
            QStatusBar { background:white; }
            QScrollArea { border:0; background:transparent; }
        """)
        self.advanced_widgets = []
        toolbar = QToolBar("项目")
        toolbar.setMovable(False)
        self.addToolBar(toolbar)
        toolbar.addWidget(QLabel("<b style='font-size:19px;color:#245cad'>马良</b>　"))
        self.stage = 1
        self.stage_buttons = []
        for index,label in enumerate(self.STAGES):
            button = QPushButton(label)
            button.setCheckable(True)
            button.setProperty("stage",True)
            button.clicked.connect(lambda checked=False,i=index:self.set_stage(i))
            self.stage_buttons.append(button)
            toolbar.addWidget(button)
        spacer = QWidget()
        spacer.setSizePolicy(QSizePolicy.Policy.Expanding,QSizePolicy.Policy.Preferred)
        toolbar.addWidget(spacer)
        actions = {}
        for key,label,callback,shortcut in [("new","新建",self.new_project,"Ctrl+N"),("open","打开",self.open_project,"Ctrl+O"),
                ("save","保存",self.save,"Ctrl+S"),("save_as","另存为…",lambda:self.save(True),"Ctrl+Shift+S"),
                ("undo","↶ 撤销",self.undo,"Ctrl+Z"),("redo","↷ 重做",self.redo,"Ctrl+Y")]:
            action = QAction(label,self)
            action.triggered.connect(callback)
            action.setShortcut(shortcut)
            self.addAction(action)
            actions[key] = action
        for key in ("open","save","undo","redo"):
            toolbar.addAction(actions[key])
        more = QToolButton()
        more.setText("更多 ▾")
        more.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        menu = QMenu(more)
        menu.addAction(actions["new"])
        menu.addAction(actions["save_as"])
        examples_menu = menu.addMenu("打开分栏示例")
        for label,filename in [("双栏 · 阅读札记","双栏阅读札记.pagewriter"),("三栏 · 留白摘记","三栏留白摘记.pagewriter"),
                               ("双栏 · 划线批注","双栏划线批注.pagewriter")]:
            action = examples_menu.addAction(label)
            action.triggered.connect(lambda checked=False,name=filename:self.open_example(name))
        menu.addSeparator()
        menu.addAction("导出页面 SVG 路径…",self.export_svg_file)
        menu.addAction("离线 G-code 预览…",self.preview_gcode)
        menu.addSeparator()
        self.advanced_action = menu.addAction("显示高级功能")
        self.advanced_action.setCheckable(True)
        self.advanced_action.toggled.connect(self.apply_advanced)
        menu.addSeparator()
        self.font_licenses_action = menu.addAction("内置字体与许可…",self.show_font_licenses)
        more.setMenu(menu)
        toolbar.addWidget(more)

        stylebar = QToolBar("文字样式")
        stylebar.setMovable(False)
        self.addToolBarBreak(Qt.ToolBarArea.TopToolBarArea)
        self.addToolBar(Qt.ToolBarArea.TopToolBarArea, stylebar)
        self.stylebar = stylebar
        stylebar.addWidget(QLabel("文字样式 "))
        self.stroke_fonts = QComboBox()
        self.stroke_fonts.addItem("选择单线字体", None)
        self.stroke_fonts.setToolTip("内置单线字体可直接书写；也可导入已授权的本地 gfont 字库。")
        self.stroke_fonts.currentIndexChanged.connect(self.choose_stroke_font)
        self.stroke_fonts.setMinimumWidth(190)
        self.stroke_fonts.setMaximumWidth(320)
        stylebar.addWidget(self.stroke_fonts)
        self.font_box = QFontComboBox()
        self.font_box.setMaximumWidth(220)
        self.font_box.setToolTip("普通字体只能预览，写字机不能书写。")
        self.font_box.currentFontChanged.connect(self.apply_font)
        self.system_font_action = stylebar.addWidget(self.font_box)
        self.font_size = spin(2,120,12," pt",1)
        self.font_size.valueChanged.connect(self.apply_size)
        stylebar.addWidget(QLabel("字号"))
        stylebar.addWidget(self.font_size)
        bold = QPushButton("粗体")
        bold.clicked.connect(self.apply_bold)
        self.bold_action = stylebar.addWidget(bold)
        self.underline_button = QPushButton("下划线 U")
        self.underline_button.setCheckable(True)
        self.underline_button.setToolTip("Ctrl+U；仅切换选区的下划线，无选区时作用于后续输入。")
        self.underline_button.clicked.connect(self.apply_underline)
        stylebar.addWidget(self.underline_button)
        import_stroke = QPushButton("导入字体文件…")
        import_stroke.clicked.connect(self.import_stroke_font)
        self.import_font_action = stylebar.addWidget(import_stroke)
        self.selection_label = QLabel("选区：无")
        self.selection_label.setProperty("hint",True)
        stylebar.addWidget(self.selection_label)
        underline_action = QAction("切换下划线",self)
        underline_action.setShortcut("Ctrl+U")
        underline_action.triggered.connect(lambda checked=False:self.apply_underline())
        self.addAction(underline_action)
        self.populate_vendor_fonts()

        self.editor = WritingEdit(self)
        self.editor.setAcceptRichText(False)
        self.editor.setUndoRedoEnabled(False)
        self.editor.textChanged.connect(self.text_changed)
        self.editor.cursorPositionChanged.connect(self.cursor_changed)
        self.editor.setParent(self)
        self.editor.hide()

        splitter = QSplitter()
        self.setCentralWidget(splitter)
        self.stage_stack = QStackedWidget()
        self.stage_stack.setMinimumWidth(320)
        self.stage_stack.setMaximumWidth(440)

        # ① Photo and page
        photo_page, pv = self.stage_page("① 照片与页面", self.STAGE_HINTS[0])
        photo = QGroupBox("书页照片")
        pf = QVBoxLayout(photo)
        btn = QPushButton("导入照片")
        btn.setProperty("primary",True)
        btn.clicked.connect(self.import_image)
        pf.addWidget(btn)
        rotation_row=QHBoxLayout()
        self.rotate_photo_ccw=QPushButton("⟲ 向左转")
        self.rotate_photo_cw=QPushButton("向右转 ⟳")
        for button,degrees in [(self.rotate_photo_ccw,-90),(self.rotate_photo_cw,90)]:
            button.setToolTip("把照片转 90°，让它按正常阅读方向显示；不会修改原照片。转完需要重新对齐四个角。")
            button.clicked.connect(lambda checked=False,angle=degrees:self.rotate_image(angle))
            rotation_row.addWidget(button)
        pf.addLayout(rotation_row)
        row = QHBoxLayout()
        rectify = QPushButton("对齐四个角")
        rectify.setToolTip("照片拍斜了时，依次点书页的左上、右上、右下、左下角。")
        rectify.clicked.connect(self.rectify)
        row.addWidget(rectify)
        clearrect = QPushButton("还原")
        clearrect.clicked.connect(self.reset_rectify)
        row.addWidget(clearrect)
        pf.addLayout(row)
        self.opacity = QSlider(Qt.Orientation.Horizontal)
        self.opacity.setRange(0,100)
        self.opacity.valueChanged.connect(self.image_changed)
        pf.addWidget(QLabel("照片深浅"))
        pf.addWidget(self.opacity)
        self.image_lock = QCheckBox("锁定照片位置")
        self.image_lock.toggled.connect(self.image_changed)
        pf.addWidget(self.image_lock)
        self.advanced_widgets.append(self.image_lock)
        pv.addWidget(photo)
        page = QGroupBox("书页实际尺寸")
        form = QFormLayout(page)
        self.paper = QComboBox()
        for name, w, h in PAPER_SIZES:
            self.paper.addItem(f"{name}", (w, h))
        self.paper.addItem("自定义", None)
        self.paper_landscape = QCheckBox("横放")
        row = QHBoxLayout(); row.addWidget(self.paper, 1); row.addWidget(self.paper_landscape)
        form.addRow("纸张", row)
        self.page_w = spin(20,1000,148)
        self.page_h = spin(20,1000,210)
        form.addRow("宽",self.page_w)
        form.addRow("高",self.page_h)
        form.addRow(self.hint_label("选常用纸张，或用尺子量书页本身的宽和高。"))
        self.paper.activated.connect(self.paper_chosen)
        self.paper_landscape.toggled.connect(self.paper_chosen)
        self.page_w.valueChanged.connect(self.page_changed)
        self.page_h.valueChanged.connect(self.page_changed)
        pv.addWidget(page)
        pen = QGroupBox("笔迹与余量")
        form = QFormLayout(pen)
        self.pen_width = spin(.05,5,.3)
        self.margin = spin(0,20,.4)
        form.addRow("笔迹宽度",self.pen_width)
        form.addRow("额外余量",self.margin)
        self.pen_width.valueChanged.connect(self.page_changed)
        self.margin.valueChanged.connect(self.page_changed)
        pv.addWidget(pen)
        self.advanced_widgets.append(pen)
        tool = QGroupBox("不能写的地方")
        tf = QVBoxLayout(tool)
        tf.addWidget(self.hint_label("把印刷文字、图片、书夹圈起来，书写时会自动避开。"))
        row = QHBoxLayout()
        for label,mode,tip in [("▧ 框选",'rect',"在照片上拖出一个矩形"),("⬡ 描边",'polygon',"逐点单击，双击完成；Esc 取消")]:
            button = QPushButton(label)
            button.setToolTip(tip)
            button.clicked.connect(lambda checked=False,m=mode:self.canvas.set_mode(m))
            row.addWidget(button)
        tf.addLayout(row)
        self.zone_kind = QComboBox()
        self.zone_kind.addItems(["印刷内容（不能落笔）", "书夹 / 障碍（笔也不能经过）"])
        self.zone_kind.setToolTip("选中一个区域后可改类型")
        self.zone_kind.currentIndexChanged.connect(self.zone_kind_changed)
        tf.addWidget(self.zone_kind)
        delete = QPushButton("删除选中的区域")
        delete.clicked.connect(self.delete_selected)
        tf.addWidget(delete)
        pv.addWidget(tool)
        objects_box = QGroupBox("页面对象")
        ob = QVBoxLayout(objects_box)
        self.objects = QListWidget()
        self.objects.setMinimumHeight(110)
        self.objects.itemClicked.connect(self.object_clicked)
        ob.addWidget(self.objects)
        pv.addWidget(objects_box)
        self.advanced_widgets.append(objects_box)
        pv.addStretch()

        # ② Annotation
        edit_page, ev = self.stage_page("② 批注", self.STAGE_HINTS[1])
        add = QPushButton("＋ 添加文字")
        add.setProperty("primary",True)
        add.clicked.connect(self.add_text)
        ev.addWidget(add)
        row = QHBoxLayout()
        for label,mode,tip in [("Ⅰ 编辑文字","select","接着在选中的文字里输入（可用中文输入法）"),("↖ 移动文字","move","拖动文字换位置")]:
            button = QPushButton(label)
            button.setToolTip(tip)
            button.clicked.connect(lambda checked=False,m=mode:self.edit_or_move(m))
            row.addWidget(button)
        ev.addLayout(row)
        add_picture = QPushButton("＋ 添加图画（自动转线稿）")
        add_picture.setToolTip("导入一张图片，转成单线笔迹；涂黑的地方可按灰度排线填充")
        add_picture.clicked.connect(self.add_drawing)
        ev.addWidget(add_picture)
        delete = QPushButton("删除选中的对象")
        delete.clicked.connect(self.delete_selected)
        ev.addWidget(delete)
        self.build_drawing_panel(ev)
        column_group = QGroupBox("分栏")
        self.column_group = column_group
        cf = QFormLayout(column_group)
        self.columns = QComboBox()
        self.columns.addItems(["一栏", "两栏 · 左 → 右", "三栏 · 左 → 中 → 右"])
        self.columns.currentIndexChanged.connect(self.columns_changed)
        self.column_height = spin(0,1000,0)
        self.column_height.setSpecialValueText("不限高（一栏）")
        self.column_gap = spin(0,100,6)
        self.column_gap.setToolTip("一栏时不影响字形；切换到两栏或三栏时使用此间距。")
        self.column_height.valueChanged.connect(self.geometry_changed)
        self.column_gap.valueChanged.connect(self.geometry_changed)
        self.column_width_label = QLabel()
        self.column_status = QLabel()
        self.column_status.setWordWrap(True)
        cf.addRow("栏数",self.columns)
        cf.addRow("栏高",self.column_height)
        cf.addRow("栏间距",self.column_gap)
        self.column_separators = QCheckBox("在栏之间画分割线")
        self.column_separators.setToolTip("真实笔迹，参与碰撞检查和输出；一栏时不画线。")
        self.column_separators.toggled.connect(self.separator_changed)
        cf.addRow(self.column_separators)
        cf.addRow("每栏宽度",self.column_width_label)
        cf.addRow(self.column_status)
        ev.addWidget(column_group)
        geom = QGroupBox("位置与大小")
        self.geom_group = geom
        gf = QFormLayout(geom)
        self.pos_x,self.pos_y = spin(),spin()
        self.block_w = spin(2,1000,65)
        self.angle = spin(-360,360,0," °",1)
        for label,widget in [("左边距 X",self.pos_x),("上边距 Y",self.pos_y),("总宽",self.block_w),("旋转",self.angle)]:
            gf.addRow(label,widget)
            widget.valueChanged.connect(self.geometry_changed)
        self.avoid_zones = QCheckBox("自动避开保护区")
        self.avoid_zones.setToolTip("文字绕开重叠的保护区；放不下时沿文字自身的“下方”加高。旋转过的文字按它自己的方向排。")
        self.avoid_zones.toggled.connect(self.avoid_changed)
        gf.addRow(self.avoid_zones)
        ev.addWidget(geom)
        out = QGroupBox("检查笔迹")
        ov = QVBoxLayout(out)
        self.check_result = QTextEdit()
        self.check_result.setReadOnly(True)
        self.check_result.setMaximumHeight(150)
        ov.addWidget(self.check_result)
        check = QPushButton("重新检查")
        check.clicked.connect(self.refresh)
        ov.addWidget(check)
        self.show_travel = QCheckBox("显示抬笔移动（虚线）")
        self.show_travel.toggled.connect(self.refresh_scene)
        ov.addWidget(self.show_travel)
        replay = QPushButton("▶  逐笔播放 / 停止")
        replay.clicked.connect(self.toggle_replay)
        ov.addWidget(replay)
        self.replay_slider = QSlider(Qt.Orientation.Horizontal)
        self.replay_slider.setRange(0,100)
        self.replay_slider.setValue(100)
        self.replay_slider.valueChanged.connect(self.seek_replay)
        ov.addWidget(self.replay_slider)
        ev.addWidget(out)
        self.advanced_widgets.append(out)
        ev.addStretch()

        # ③ Writing
        from .device_ui import DevicePanel
        self.device_panel = DevicePanel(self)
        write_page = QScrollArea()
        write_page.setWidgetResizable(True)
        write_page.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        write_page.setWidget(self.device_panel)
        for widget in (photo_page, edit_page, write_page):
            self.stage_stack.addWidget(widget)
        splitter.addWidget(self.stage_stack)

        center = QWidget()
        cv = QVBoxLayout(center)
        cv.setContentsMargins(0,8,0,0)
        self.summary = QLabel()
        self.summary.setStyleSheet("padding:10px 16px;background:#fff;border-radius:6px")
        self.summary.linkActivated.connect(lambda link:self.show_check_details())
        cv.addWidget(self.summary)
        self.canvas = Canvas(self)
        cv.addWidget(self.canvas,1)
        bottom = QHBoxLayout()
        self.coordinates = QLabel("页面坐标 · 毫米")
        bottom.addWidget(self.coordinates)
        bottom.addStretch()
        self.mode_label = QLabel()
        self.mode_label.setProperty("hint",True)
        bottom.addWidget(self.mode_label)
        bottom.addSpacing(16)
        self.zoom = QSlider(Qt.Orientation.Horizontal)
        self.zoom.setRange(30,220)
        self.zoom.setValue(100)
        self.zoom.setFixedWidth(110)
        self.zoom.valueChanged.connect(self.set_zoom)
        bottom.addWidget(self.zoom)
        self.zoom_label = QLabel("100%")
        bottom.addWidget(self.zoom_label)
        fit = QPushButton("适合页面")
        fit.clicked.connect(self.fit_page)
        bottom.addWidget(fit)
        cv.addLayout(bottom)
        splitter.addWidget(center)
        splitter.setStretchFactor(1,1)
        splitter.setSizes([400,1100])
        self.apply_advanced(bool(QSettings().value("ui/advanced",False,type=bool)),save=False)
        self.set_stage(1)

    def stage_page(self, title, text):
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        body = QWidget()
        layout = QVBoxLayout(body)
        label = QLabel(title)
        label.setStyleSheet("font-size:16px;font-weight:700;padding:4px 2px")
        layout.addWidget(label)
        layout.addWidget(self.hint_label(text))
        scroll.setWidget(body)
        return scroll, layout

    @staticmethod
    def hint_label(text):
        label = QLabel(text)
        label.setWordWrap(True)
        label.setProperty("hint",True)
        return label

    def set_stage(self, index):
        self.stage = index
        self.stage_stack.setCurrentIndex(index)
        for i,button in enumerate(self.stage_buttons):
            button.setChecked(i == index)
        self.stylebar.setVisible(index == 1)
        if index != 1:
            self.canvas.editing_id = None
        self.canvas.set_mode("select")
        for item in self.canvas.scene().items():
            if isinstance(item, ResizableMixin):
                item.sync_handles()
        self.statusBar().showMessage(self.STAGE_HINTS[index])

    def apply_advanced(self, enabled, save=True):
        enabled = bool(enabled)
        self.advanced = enabled
        if self.advanced_action.isChecked() != enabled:
            self.advanced_action.blockSignals(True)
            self.advanced_action.setChecked(enabled)
            self.advanced_action.blockSignals(False)
        for widget in self.advanced_widgets:
            widget.setVisible(enabled)
        self.import_font_action.setVisible(enabled)
        self.bold_action.setVisible(enabled)
        self.device_panel.set_advanced(enabled)
        if save:
            QSettings().setValue("ui/advanced",enabled)
        if hasattr(self,"canvas"):
            self.cursor_changed()

    def show_check_details(self):
        self.show_text("检查结果",self.check_result.toPlainText())

    def show_job_area(self, rect):
        item = getattr(self,"job_area_item",None)
        if item is not None and item.scene() is not None:
            item.scene().removeItem(item)
        self.job_area_item = None
        if rect is None:
            return
        self.job_area_item = self.canvas.scene().addRect(QRectF(*rect),QPen(BLUE,.35,Qt.PenStyle.DashLine),QColor(36,92,173,18))
        self.job_area_item.setZValue(30)
        self.canvas.ensureVisible(self.job_area_item)
        QApplication.processEvents()

    def populate_vendor_fonts(self):
        """Built-ins are available on a clean install; optional local fonts follow."""
        for descriptor in builtin_fonts.descriptors():
            self.stroke_fonts.addItem(descriptor['name'], descriptor)
            index = self.stroke_fonts.count() - 1
            self.stroke_fonts.setItemData(index, '内置单线字体 · 来源：' + descriptor.get('source_name', ''), Qt.ItemDataRole.ToolTipRole)
        for label,filename in VENDOR_FONTS:
            path = VENDOR_FONT_DIR / filename
            if path.is_file():
                self.stroke_fonts.addItem(label,{"path":str(path),"name":label,"pending":True})

    def load_stroke_font(self, index):
        descriptor = self.stroke_fonts.itemData(index)
        if descriptor and descriptor.get('builtin_id'):
            # A project may carry an older resource fingerprint. Explicitly
            # choosing the bundled face upgrades it to the installed version.
            descriptor = builtin_fonts.descriptor(descriptor['builtin_id'])
            self.stroke_fonts.setItemData(index,descriptor)
        if descriptor and descriptor.get("pending"):
            QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
            try:
                from .gfont import register_font
                descriptor = register_font(descriptor["path"])
            finally:
                QApplication.restoreOverrideCursor()
            self.stroke_fonts.setItemData(index,descriptor)
        return descriptor

    def default_stroke_font(self):
        for index in range(1,self.stroke_fonts.count()):
            try:
                descriptor = self.load_stroke_font(index)
            except Exception:
                continue
            if descriptor:
                return descriptor
        return None

    def block_by_id(self, block_id=None):
        return next((b for b in self.project.blocks if b.id == (block_id or self.selected_id)),None)

    def record(self):
        self.undo_stack.append((copy.deepcopy(self.project),self.selected_id))
        self.undo_stack = self.undo_stack[-100:]
        self.redo_stack.clear()
        self.dirty = True

    def undo(self):
        if not self.undo_stack: return
        self.redo_stack.append((copy.deepcopy(self.project),self.selected_id))
        self.project,self.selected_id = self.undo_stack.pop()
        self.dirty = True
        self.refresh()

    def redo(self):
        if not self.redo_stack: return
        self.undo_stack.append((copy.deepcopy(self.project),self.selected_id))
        self.project,self.selected_id = self.redo_stack.pop()
        self.dirty = True
        self.refresh()

    def sync_list(self):
        self.objects.clear()
        for object_,prefix in [(b,"文  ") for b in self.project.blocks]+[(d,"图  ") for d in self.project.drawings]+[(z,"禁  ") for z in self.project.zones]:
            item = QListWidgetItem(prefix+object_.name)
            item.setData(Qt.ItemDataRole.UserRole,object_.id)
            self.objects.addItem(item)
            if object_.id == self.selected_id:
                self.objects.setCurrentItem(item)

    def object_clicked(self,item):
        self.select_block(item.data(Qt.ItemDataRole.UserRole))
        self.refresh_scene()

    def select_block(self,block_id):
        self.selected_id = block_id
        self.sync_list()
        self.sync_editor()

    def sync_editor(self):
        block = self.block_by_id()
        self.guard = True
        self.editor.setEnabled(block is not None)
        zone = next((z for z in self.project.zones if z.id == self.selected_id),None)
        self.zone_kind.setEnabled(zone is not None)
        self.zone_kind.setCurrentIndex(1 if zone and zone.kind == "obstacle" else 0)
        if block:
            if self.editor.toHtml() != block.html or getattr(self,"editor_bound_block",None) is not block:
                self.editor.document().setDefaultFont(QFont('Microsoft YaHei', 12))
                self.editor.setHtml(block.html)
                prepare_stroke_document(self.editor.document(), block)
                # QTextEdit retains its previous insertion format across setHtml.
                # Read the new document's first character with an independent
                # cursor, then reset only the empty insertion point at its start.
                insertion = QTextCursor(self.editor.document())
                first = QTextCursor(insertion)
                if first.movePosition(QTextCursor.MoveOperation.NextCharacter,QTextCursor.MoveMode.KeepAnchor):
                    initial_format = first.charFormat()
                else:
                    initial_format = QTextCharFormat()
                    initial_format.setFont(self.editor.document().defaultFont())
                    initial_format.setFontUnderline(False)
                self.editor.setTextCursor(insertion)
                self.editor.setCurrentCharFormat(initial_format)
            self.editor_bound_block = block
            self.sync_stroke_fonts(block)
            self.pos_x.setValue(block.x)
            self.pos_y.setValue(block.y)
            self.block_w.setValue(block.width)
            self.angle.setValue(block.rotation)
            self.columns.setCurrentIndex(block.columns-1)
            self.column_height.setMinimum(0 if block.columns == 1 else .5)
            self.column_height.setValue(block.column_height)
            self.column_gap.setValue(block.column_gap)
            self.column_separators.setChecked(block.column_separators)
            self.avoid_zones.setChecked(block.avoid_zones)
            self.update_column_status(block)
        else:
            self.editor.clear()
            self.editor_bound_block = None
            self.column_width_label.clear()
            self.column_status.clear()
        for control in (self.columns,self.column_height,self.column_gap,self.column_separators,self.pos_x,self.pos_y,self.block_w,self.angle):
            control.setEnabled(block is not None)
        self.column_group.setVisible(block is not None)
        self.geom_group.setVisible(block is not None)
        self.guard = False
        self.sync_drawing_panel()
        self.cursor_changed()

    def refresh(self):
        self.guard = True
        for widget,value in [(self.page_w,self.project.width),(self.page_h,self.project.height),
                (self.pen_width,self.project.pen_width),(self.margin,self.project.margin)]:
            widget.setValue(value)
        self.opacity.setValue(round(self.project.image_opacity*100))
        self.image_lock.setChecked(self.project.image_locked)
        self.guard = False
        self.sync_paper_choice()
        self.sync_list()
        self.sync_editor()
        self.refresh_scene()

    def refresh_scene(self):
        if not hasattr(self,"canvas"): return
        self.replay_timer.stop()
        self.canvas.temporary = None
        scene = self.canvas.scene()
        scene.clear()
        self.job_area_item = None
        self.canvas.points = []
        if hasattr(self, 'device_panel'):
            geometry = (self.project.width,self.project.height,self.project.image_data,repr(self.project.image_quad),repr(self.project.image_edges),repr(self.project.image_guides),tuple(self.project.image_offset))
            if getattr(self, '_reference_geometry', None) != geometry:
                self._reference_geometry = geometry
                self.device_panel.send('geometry_changed',geometry_id=self.device_panel.geometry_id())
        w,h = self.project.width,self.project.height
        scene.setSceneRect(-12,-12,w+24,h+24)
        self.paper_item = scene.addRect(0,0,w,h,QPen(QColor("#c5cedc"),.2),QColor("white"))
        self.paper_item.setFlag(QGraphicsItem.GraphicsItemFlag.ItemClipsChildrenToShape)
        self.render_background()
        for x in range(0,int(w)+1,10):
            item = scene.addSimpleText(str(x))
            item.setBrush(QColor("#7c8da6"))
            item.setScale(.19)
            item.setPos(x-1,-5)
        for y in range(0,int(h)+1,10):
            item = scene.addSimpleText(str(y))
            item.setBrush(QColor("#7c8da6"))
            item.setScale(.19)
            item.setPos(-7,y-1)
        self.collisions = preflight(self.project)
        active_block = self.block_by_id()
        if active_block:
            self.update_column_status(active_block)
        bad = {c.block_id for c in self.collisions}
        for zone in self.project.zones:
            item = ZoneItem(self,zone)
            scene.addItem(item)
            item.setSelected(zone.id == self.selected_id)
        self.text_items = []
        self.drawing_items = []
        self.replay_parts = []
        last_point = None
        for drawing in self.project.drawings:
            item = DrawingItem(self,drawing,drawing.id in bad)
            scene.addItem(item)
            item.setSelected(drawing.id == self.selected_id)
            self.drawing_items.append(item)
        for block in list(self.project.blocks)+list(self.project.drawings):
            if isinstance(block, Drawing):
                page_path = build_drawing_path(block)
            else:
                item = TextItem(self,block,block.id in bad)
                scene.addItem(item)
                item.setSelected(block.id == self.selected_id)
                self.text_items.append(item)
                page_path = build_block_path(block)
            for polygon in path_subpaths(page_path):
                if not polygon: continue
                sub = QPainterPath(QPointF(*polygon[0]))
                for point in polygon[1:]: sub.lineTo(QPointF(*point))
                self.replay_parts.append(sub)
                if self.show_travel.isChecked() and last_point is not None:
                    scene.addLine(last_point[0],last_point[1],polygon[0][0],polygon[0][1],
                                  QPen(QColor("#b2a5cf"),.15,Qt.PenStyle.DashLine)).setZValue(4)
                last_point = polygon[-1]
        self.replay_slider.blockSignals(True)
        self.replay_slider.setValue(100)
        self.replay_slider.blockSignals(False)
        self.replay_overlay = None
        count = len(self.project.blocks)
        mode=self.writing_mode_label()
        self.mode_label.setText(mode+" · 蓝色为实际笔迹")
        details = "　<a href='details' style='color:#245cad'>查看详情</a>"
        if self.collisions:
            self.summary.setText(f"<b style='color:#be3b50'>有 {len(self.collisions)} 处问题（红色文字）· 输出已阻止</b>　 {w:g} × {h:g} mm{details}")
            result = "\n".join("• "+c.message for c in self.collisions)
            self.check_result.setPlainText("预检未通过\n\n"+result+"\n\n请移动文字或调整禁区后重新导出。")
        else:
            ready = "还没有文字" if not count else "可以书写" if mode == "单线字库笔迹" else "只能预览：请给文字选单线字体"
            color = "#25826c" if mode == "单线字库笔迹" else "#a8661c"
            self.summary.setText(f"<b style='color:{color}'>✓ 检查通过 · {ready}</b>　 {w:g} × {h:g} mm　 {count} 段文字　 {mode}{details}")
            self.check_result.setPlainText(f"页面几何预检通过\n模式：{mode}\n\n实际落笔路径 + {self.project.pen_width:g} mm 笔宽\n额外保护余量 {self.project.margin:g} mm\n已检查页面边界、矩形和多边形禁区。\n\n照片仅供参考：请手工标出所有印刷区。\n机器坐标与笔尖高度尚未标定。\nSVG 可用于离线查看；真实书写不可用。")
        self.setWindowTitle(("* " if self.dirty else "")+"马良 · Maliang / "+(self.filename.name if self.filename else "未命名项目"))

    def render_background(self):
        if not self.project.image_data: return
        quad = self.project.image_quad
        if quad and (self.project.image_edges or self.project.image_guides):
            return self.render_flattened_background()
        image = QImage.fromData(base64.b64decode(self.project.image_data))
        if image.isNull(): return
        item = BackgroundItem(self,QPixmap.fromImage(image))
        transform = QTransform()
        source = QPolygonF([QPointF(*p) for p in quad]) if quad else QPolygonF([
            QPointF(0,0),QPointF(image.width(),0),QPointF(image.width(),image.height()),QPointF(0,image.height())])
        target = QPolygonF([QPointF(0,0),QPointF(self.project.width,0),
                           QPointF(self.project.width,self.project.height),QPointF(0,self.project.height)])
        if not QTransform.quadToQuad(source,target,transform):
            self.statusBar().showMessage("四点无效，底图未显示；请重新校正")
            return
        item.setTransform(transform)
        offset = getattr(self.project,"image_offset",[0,0])
        item.setPos(*offset)
        item.setParentItem(self.paper_item)

    def render_flattened_background(self):
        """Curved-edge photo: draw a pre-flattened page image, cached across refreshes."""
        p = self.project
        key = (hash(p.image_data),repr(p.image_quad),repr(p.image_edges),repr(p.image_guides),p.width,p.height)
        cached = getattr(self,"_flattened_cache",None)
        if cached is None or cached[0] != key:
            from .dewarp import PageWarp
            image = QImage.fromData(base64.b64decode(p.image_data))
            if image.isNull(): return
            try:warp = PageWarp(p.image_quad,p.image_edges,p.width,p.height,guides=p.image_guides)
            except ValueError:
                self.statusBar().showMessage("弯边校正无效，底图未显示；请重新校正")
                return
            scale = min(10,max(2,max(image.width(),image.height())/max(p.width,p.height)))
            flattened = warp.render(image,max(1,round(p.width*scale)),max(1,round(p.height*scale)))
            cached = (key,QPixmap.fromImage(flattened),scale)
            self._flattened_cache = cached
        _,pixmap,scale = cached
        item = BackgroundItem(self,pixmap)
        item.setTransform(QTransform().scale(1/scale,1/scale))
        item.setPos(*getattr(p,"image_offset",[0,0]))
        item.setParentItem(self.paper_item)

    def set_zoom(self,value):
        self.canvas.resetTransform()
        self.canvas.scale(3.7795275591*value/100,3.7795275591*value/100)
        self.zoom_label.setText(f"{value}%")

    def fit_page(self):
        if not self.canvas.viewport().height(): return
        scale = min(self.canvas.viewport().width()/(self.project.width+24),
                    self.canvas.viewport().height()/(self.project.height+24))
        self.zoom.setValue(max(30,min(220,round(scale/3.7795275591*100))))
        self.canvas.centerOn(self.project.width/2,self.project.height/2)

    def page_changed(self):
        if self.guard:return
        self.sync_paper_choice()
        self.record()
        self.project.width,self.project.height = self.page_w.value(),self.page_h.value()
        self.project.pen_width,self.project.margin = self.pen_width.value(),self.margin.value()
        self.refresh_scene()

    def image_changed(self):
        if self.guard:return
        self.record()
        self.project.image_opacity = self.opacity.value()/100
        self.project.image_locked = self.image_lock.isChecked()
        self.refresh_scene()

    def geometry_changed(self):
        if self.guard:return
        block = self.block_by_id()
        if not block:return
        if self.block_w.value() <= (block.columns-1)*self.column_gap.value():
            self.error("区域总宽必须大于所有栏间距之和。请增加总宽或减小栏间距。")
            self.sync_editor()
            return
        self.record()
        block.x,block.y,block.width,block.rotation = self.pos_x.value(),self.pos_y.value(),self.block_w.value(),self.angle.value()
        block.column_height,block.column_gap = self.column_height.value(),self.column_gap.value()
        self.refresh_scene()

    def columns_changed(self,index):
        if self.guard:return
        block = self.block_by_id()
        if not block:return
        count = index+1
        if block.width <= (count-1)*block.column_gap:
            self.error("当前总宽不足以容纳栏间距，请先增加区域总宽或减小间距。")
            self.sync_editor()
            return
        self.record()
        block.columns = count
        if count > 1 and block.column_height <= 0:
            block.column_height = 60
        self.guard = True
        self.column_height.setMinimum(0 if count == 1 else .5)
        self.column_height.setValue(block.column_height)
        self.column_gap.setEnabled(True)
        self.guard = False
        self.refresh_scene()

    def separator_changed(self,enabled):
        if self.guard:return
        block = self.block_by_id()
        if not block:return
        self.record()
        block.column_separators = enabled
        self.refresh_scene()

    def avoid_changed(self,enabled):
        if self.guard:return
        block = self.block_by_id()
        if not block:return
        self.record()
        block.avoid_zones = enabled
        self.refresh_scene()

    def update_column_status(self,block):
        details = layout_info(block)
        self.column_width_label.setText(f"{details['column_width']:.2f} mm × {block.columns} 栏")
        if details['overflow']:
            self.column_status.setText("文字溢出：最后一栏或单行空间不足，已阻止导出。请增加栏高/总宽或调整字号。")
            self.column_status.setStyleSheet("color:#be3b50;font-weight:600")
        elif details.get('flow_height'):
            self.column_status.setText(f"为避开保护区，栏高已自动加到 {details['flow_height']:.1f} mm（设定 {block.column_height:.1f} mm）。")
            self.column_status.setStyleSheet("color:#a8661c")
        elif block.column_height > 0:
            self.column_status.setText(f"已设置 {block.columns} 栏，当前用到 {details['page_count']} 栏；文字未溢出。")
            self.column_status.setStyleSheet("color:#25826c")
        else:
            self.column_status.setText("单栏不限高，保持旧版排版行为。")
            self.column_status.setStyleSheet("color:#738299")

    def cursor_changed(self):
        if self.guard:return
        cursor = self.editor.textCursor()
        self.saved_cursor = QTextCursor(cursor)
        self.selection_label.setText(f"已选 {len(cursor.selectedText())} 个字" if cursor.hasSelection() else "")
        fmt = cursor.charFormat()
        self.guard = True
        self.underline_button.setChecked(fmt.fontUnderline())
        block=self.block_by_id()
        single=block is not None and getattr(block,'writing_mode','outline')=='single_line'
        self.system_font_action.setVisible(not single and getattr(self,'advanced',False))
        self.font_box.setEnabled(not single)
        if single:
            self.sync_stroke_fonts(block)
            families=fmt.fontFamilies() or []
            token=next((f for f in families if f in block.stroke_fonts),block.stroke_font)
            index=next((i for i in range(1,self.stroke_fonts.count()) if (self.stroke_fonts.itemData(i) or {}).get('family')==token),0)
            self.stroke_fonts.setCurrentIndex(index)
        else:
            self.font_box.setCurrentFont(fmt.font())
        size = fmt.fontPointSize()
        if size <= 0:
            size = fmt.font().pointSizeF()
        if size <= 0:
            size = self.editor.document().defaultFont().pointSizeF()
        self.font_size.setValue(size if size > 0 else 12)
        self.guard = False
        self.canvas.scene().update()

    def apply_format(self,fmt):
        if self.guard or not self.block_by_id():return
        cursor = QTextCursor(getattr(self,"saved_cursor",self.editor.textCursor()))
        self.editor.setTextCursor(cursor)
        self.editor.mergeCurrentCharFormat(fmt)
        self.saved_cursor = QTextCursor(self.editor.textCursor())
        self.canvas.setFocus()

    def sync_stroke_fonts(self, block):
        for token,descriptor in getattr(block,'stroke_fonts',{}).items():
            index=next((i for i in range(1,self.stroke_fonts.count()) if (self.stroke_fonts.itemData(i) or {}).get('family')==token),None)
            if index is None:
                # A recommended font not yet loaded may be the same local file.
                same=lambda data:data.get('pending') and Path(data['path']).resolve()==Path(descriptor.get('path','')).resolve()
                index=next((i for i in range(1,self.stroke_fonts.count()) if same(self.stroke_fonts.itemData(i) or {})),None)
            if index is None:self.stroke_fonts.addItem(descriptor.get('name',token),descriptor)
            else:self.stroke_fonts.setItemData(index,descriptor)

    def writing_mode_label(self):
        modes={getattr(b,'writing_mode','outline') for b in self.project.blocks}
        if modes=={'single_line'}:return '单线字库笔迹'
        if 'single_line' in modes:return '部分文字是普通字体（不能书写）'
        return '普通字体（只能预览，不能书写）'

    def import_stroke_font(self):
        name,_ = QFileDialog.getOpenFileName(self,"导入已授权单线字库","","字库 (*.gfont)")
        if not name:return
        try:
            from .gfont import register_font
            descriptor=register_font(name)
            self.stroke_fonts.addItem(descriptor['name'],descriptor)
            self.stroke_fonts.setCurrentIndex(self.stroke_fonts.count()-1)
        except Exception as error:self.error(str(error))

    def choose_stroke_font(self,index):
        if self.guard:return
        block=self.block_by_id()
        if not self.stroke_fonts.itemData(index):return
        try:
            descriptor=self.load_stroke_font(index)
            families=builtin_fonts.layout_families(descriptor)
        except Exception as error:return self.error(str(error))
        self.preferred_stroke_font=descriptor
        if block is None:return
        self.record()
        token=descriptor['family']
        block.stroke_fonts[token]=copy.deepcopy(descriptor)
        block.stroke_font=token
        block.writing_mode='single_line'
        fmt=QTextCharFormat();fmt.setFontFamilies(families);fmt.setFontKerning(False);self.apply_format(fmt)
        self.refresh_scene()
        self.cursor_changed()

    def apply_font(self,font):
        if self.block_by_id() and getattr(self.block_by_id(),'writing_mode','outline')=='single_line':return
        if font.family().startswith('Maliang Stroke '):
            # Registered metric proxies can also appear in Qt's generic font
            # picker. Selecting one must use its pen paths, never empty outlines.
            index=next((i for i in range(1,self.stroke_fonts.count())
                if (self.stroke_fonts.itemData(i) or {}).get('family')==font.family()),None)
            if index is not None:
                return self.choose_stroke_font(index)
            return
        fmt = QTextCharFormat()
        fmt.setFontFamilies([font.family()])
        self.apply_format(fmt)

    def apply_size(self,size):
        fmt = QTextCharFormat()
        fmt.setFontPointSize(size)
        self.apply_format(fmt)

    def apply_bold(self):
        fmt = QTextCharFormat()
        fmt.setFontWeight(QFont.Weight.Bold if self.editor.fontWeight() < QFont.Weight.Bold else QFont.Weight.Normal)
        self.apply_format(fmt)

    def apply_underline(self,checked=None):
        if self.guard or not self.block_by_id():return
        if checked is None:
            checked = not self.underline_button.isChecked()
        fmt = QTextCharFormat()
        fmt.setFontUnderline(bool(checked))
        self.apply_format(fmt)
        self.underline_button.setChecked(bool(checked))

    def text_changed(self):
        if self.guard:return
        block = self.block_by_id()
        if not block:return
        self.record()
        block.html = self.editor.toHtml()
        self.refresh_scene()

    def edit_or_move(self, mode):
        self.canvas.set_mode(mode)
        if mode == "select":
            # 编辑文字: continue typing in the selected box, or the only one.
            target = self.block_by_id() or (self.project.blocks[0] if len(self.project.blocks) == 1 else None)
            if target and self.canvas.start_editing(target.id):
                return
            self.statusBar().showMessage("点一下页面上的文字开始输入；没有文字时先点「＋ 添加文字」")

    def add_text(self):
        import uuid
        font = copy.deepcopy(getattr(self,"preferred_stroke_font",None) or self.default_stroke_font())
        self.record()
        family = font["family"] if font else "Microsoft YaHei"
        block = TextBlock(id=uuid.uuid4().hex,name=f"添注 {len(self.project.blocks)+1}",
                          html=f'<p style="font-family:\'{family}\';font-size:12pt;">在这里写下你的批注</p>',x=20,y=30,width=70)
        if font:
            block.writing_mode,block.stroke_font,block.stroke_fonts = "single_line",family,{family:font}
        self.project.blocks.append(block)
        self.selected_id = block.id
        self.refresh()
        # Ready to type at once; the placeholder is selected so typing replaces it.
        self.canvas.start_editing(block.id, select_all=True)

    def add_zone(self,points):
        import uuid
        self.record()
        zone = ExclusionZone(id=uuid.uuid4().hex,name=f"印刷保护区 {len(self.project.zones)+1}",points=points)
        self.project.zones.append(zone)
        self.selected_id = zone.id
        self.refresh()

    def zone_kind_changed(self,index):
        if self.guard:return
        zone = next((z for z in self.project.zones if z.id == self.selected_id),None)
        if not zone:return
        self.record()
        zone.kind = "obstacle" if index else "ink"
        self.refresh_scene()

    def delete_selected(self):
        if not self.selected_id:return
        self.record()
        self.project.blocks = [b for b in self.project.blocks if b.id != self.selected_id]
        self.project.zones = [z for z in self.project.zones if z.id != self.selected_id]
        self.project.drawings = [d for d in self.project.drawings if d.id != self.selected_id]
        self.selected_id = None
        self.refresh()

    # --- Pictures written as line art -------------------------------------
    # (attribute, label, kind, minimum, maximum, step, tooltip)
    DRAWING_FIELDS = [
        ("x", "左边距 X", "mm", -2000, 2000, .5, ""),
        ("y", "上边距 Y", "mm", -2000, 2000, .5, ""),
        ("width", "宽度", "mm", 2, 2000, 1, "高度按图片比例"),
        ("rotation", "方向", "deg", -360, 360, 15, "也可以拖图画上方的圆形把手"),
        ("threshold", "线条深浅", "int", 1, 255, 8, "比这个灰度深的像素当作线条（越大，浅色线也会被描出来）"),
        ("detail", "简化", "px", 0, 5, .25, "越大线条越简单、点越少；0 为不简化"),
        ("min_stroke", "去掉短于", "mm", 0, 10, .2, "比这更短的碎线不写"),
        ("fill_cutoff", "填充从多深开始", "gray", 0, 255, 8, "比这个灰度深的大块区域才排线；越大，浅灰区域也会被填"),
        ("fill_spacing", "最密线距", "mm", .1, 5, .1, "最黑处相邻两条排线的距离"),
        ("fill_levels", "灰度分档", "int", 1, 4, 1, "1=只要够深就同样密；2–4=越深越密（档数越多越细腻）"),
        ("fill_angle", "排线角度", "deg", 0, 180, 15, ""),
        ("fill_min_width", "最窄填充", "mm", 0, 10, .1, "比这窄的深色区域当作线条，不排线（程序也会按图中线条粗细自动放宽）"),
    ]

    SLIDER_FIELDS = {"threshold", "detail", "min_stroke", "fill_cutoff", "fill_spacing", "fill_levels", "fill_angle", "fill_min_width"}

    def build_drawing_panel(self, layout):
        group = QGroupBox("图画")
        self.drawing_group = group
        form = QFormLayout(group)
        self.drawing_controls, self.drawing_value_labels = {}, {}
        self.drawing_apply_timer = QTimer(self)
        self.drawing_apply_timer.setSingleShot(True)
        self.drawing_apply_timer.setInterval(300)   # convert once dragging pauses
        self.drawing_apply_timer.timeout.connect(self.apply_pending_drawing_change)
        self.pending_drawing_change = None
        for name, label, kind, low, high, step, tip in self.DRAWING_FIELDS:
            if name == "fill_cutoff":
                self.drawing_fill = QCheckBox("填充涂黑的区域（排线）")
                self.drawing_fill.toggled.connect(lambda value: self.drawing_changed("fill", value))
                form.addRow(self.drawing_fill)
            if name in self.SLIDER_FIELDS:
                control = QSlider(Qt.Orientation.Horizontal)
                control.setRange(0, round((high-low)/step))
                control.setProperty("field", (low, step, kind))
                value_label = QLabel()
                value_label.setMinimumWidth(64)
                control.valueChanged.connect(lambda index, n=name: self.drawing_slider_moved(n, index))
                row = QHBoxLayout(); row.addWidget(control, 1); row.addWidget(value_label)
                self.drawing_value_labels[name] = value_label
                form.addRow(label, row)
            else:
                control = spin(low, high, low, {"mm": " mm", "deg": " °"}[kind], 1)
                control.setSingleStep(step)
                control.valueChanged.connect(lambda value, n=name: self.drawing_changed(n, value))
                form.addRow(label, control)
            control.setToolTip(tip)
            self.drawing_controls[name] = control
        # Eraser: brush over strokes on the canvas to remove them.
        self.erase_button = QPushButton("擦除线条（在图画上拖动）")
        self.erase_button.setCheckable(True)
        self.erase_button.toggled.connect(lambda on: self.canvas.set_mode("erase" if on else "select"))
        form.addRow(self.erase_button)
        self.eraser_size = QSlider(Qt.Orientation.Horizontal)
        self.eraser_size.setRange(2, 40)             # 0.25 mm steps: 0.5 .. 10 mm
        self.eraser_size.setValue(8)
        self.eraser_label = QLabel("2 mm")
        self.eraser_size.valueChanged.connect(lambda v: self.eraser_label.setText(f"{v/4:g} mm"))
        row = QHBoxLayout(); row.addWidget(self.eraser_size, 1); row.addWidget(self.eraser_label)
        form.addRow("橡皮大小", row)
        self.erase_restore = QPushButton("恢复被擦掉的线条")
        self.erase_restore.clicked.connect(self.restore_erased)
        form.addRow(self.erase_restore)
        self.drawing_stats = self.hint_label("")
        form.addRow(self.drawing_stats)
        group.setVisible(False)
        layout.addWidget(group)

    def slider_value(self, name, index=None):
        slider = self.drawing_controls[name]
        low, step, _ = slider.property("field")
        return round(low+(slider.value() if index is None else index)*step, 4)

    def show_slider_value(self, name, value):
        kind = self.drawing_controls[name].property("field")[2]
        text = {"mm": f"{value:g} mm", "deg": f"{value:g}°", "px": f"{value:g} 像素"}.get(kind, f"{value:g}")
        if name == "fill_levels":
            text = "同样密" if value == 1 else f"{int(value)} 档"
        self.drawing_value_labels[name].setText(text)

    def drawing_slider_moved(self, name, index):
        value = self.slider_value(name, index)
        self.show_slider_value(name, value)
        if self.guard: return
        self.pending_drawing_change = (name, value)
        self.drawing_apply_timer.start()

    def apply_pending_drawing_change(self):
        if self.pending_drawing_change:
            name, value = self.pending_drawing_change
            self.pending_drawing_change = None
            self.drawing_changed(name, value)

    def erase_at(self, scene_point, start=False):
        item = next((i for i in getattr(self, "drawing_items", []) if i.drawing_id == self.selected_id), None)
        if not item:
            if start: self.statusBar().showMessage("先选中一张图画，再在它上面拖动擦除")
            return
        drawing = item.drawing
        if start:
            self.record()
            self.erase_pending = []
        local = item.mapFromScene(scene_point)
        radius = self.eraser_size.value()/4/2
        self.erase_pending.append([round(local.x()/drawing.width, 5), round(local.y()/drawing.width, 5), round(radius/drawing.width, 5)])
        strokes = apply_erase(drawing_local_paths(drawing), drawing.erase+self.erase_pending, drawing.width)
        item.setPath(drawing_local_path(drawing, strokes))

    def erase_finish(self):
        drawing = self.drawing_by_id()
        if drawing and getattr(self, "erase_pending", None):
            drawing.erase = drawing.erase+self.erase_pending
            self.erase_pending = []
            self.refresh_scene()
            self.sync_drawing_panel()

    def restore_erased(self):
        drawing = self.drawing_by_id()
        if drawing and drawing.erase:
            self.record()
            drawing.erase = []
            self.refresh_scene()
            self.sync_drawing_panel()

    def drawing_by_id(self, drawing_id=None):
        return next((d for d in self.project.drawings if d.id == (drawing_id or self.selected_id)), None)

    def drawing_changed(self, name, value):
        if self.guard: return
        drawing = self.drawing_by_id()
        if not drawing: return
        if name == "fill_cutoff":
            value = round(1-value/255, 4)          # shown as gray level, stored as darkness
        elif name in ("threshold", "fill_levels"):
            value = int(value)
        elif name != "fill":
            value = float(value)
        if getattr(drawing, name) == value: return
        self.record()
        setattr(drawing, name, value)
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            self.refresh_scene()
        finally:
            QApplication.restoreOverrideCursor()
        self.sync_drawing_panel()

    def sync_drawing_panel(self):
        drawing = self.drawing_by_id()
        self.drawing_group.setVisible(drawing is not None)
        if not drawing: return
        self.guard = True
        for name, control in self.drawing_controls.items():
            value = getattr(drawing, name)
            if name == "fill_cutoff":
                value = round((1-value)*255)          # stored as darkness, shown as gray level
            if name in self.SLIDER_FIELDS:
                low, step, _ = control.property("field")
                control.setValue(round((value-low)/step))
                self.show_slider_value(name, value)
            else:
                control.setValue(value)
            if name.startswith("fill_"):
                control.setEnabled(drawing.fill)
        self.drawing_fill.setChecked(drawing.fill)
        self.erase_restore.setEnabled(bool(drawing.erase))
        self.guard = False
        from . import lineart
        strokes = drawing_strokes(drawing)
        ink = sum(lineart.length(line) for line in strokes)
        travel = sum(math.dist(a[-1], b[0]) for a, b in zip(strokes, strokes[1:]))
        seconds = ink/lineart.WRITE_FEED*60+travel/lineart.TRAVEL_FEED*60+len(strokes)*lineart.PEN_CYCLE_S
        hatch = "，含排线" if drawing.stats.get("hatched") else ""
        erased = f"，已擦 {len(drawing.erase)} 处" if drawing.erase else ""
        self.drawing_stats.setText(f"高约 {drawing.height:.1f} mm · 共 {len(strokes)} 笔{hatch}{erased} · 落笔 {ink/1000:.2f} 米 · "
                                   f"预计约 {max(1, round(seconds))} 秒（估算）")

    def add_drawing(self):
        name, _ = QFileDialog.getOpenFileName(self, "选择要转成线稿的图片", "", "图片或 SVG (*.png *.jpg *.jpeg *.bmp *.webp *.gif *.svg)")
        if name:
            self.add_drawing_from_file(name)

    def add_drawing_from_file(self, filename):
        import uuid
        if str(filename).lower().endswith(".svg"):
            # Vector file: keep its own paths, no rasterising.
            from . import svgimport
            try:
                vector = svgimport.parse(str(filename))
            except (ValueError, OSError) as exc:
                return self.error(f"无法读取 SVG：{exc}")
            return self.place_drawing(Drawing(id=f"drawing-{uuid.uuid4().hex[:8]}", name=Path(filename).stem[:30] or "图画",
                                              vector=vector), "SVG 已按原矢量导入。")
        reader = QImageReader(str(filename))
        reader.setAutoTransform(True)
        image = reader.read()
        if image.isNull(): return self.error("无法读取图片")
        if max(image.width(), image.height()) > 1600:   # keeps the project small; conversion uses <= 700 px anyway
            image = image.scaled(1600, 1600, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation)
        return self.place_drawing(Drawing(id=f"drawing-{uuid.uuid4().hex[:8]}", name=Path(filename).stem[:30] or "图画",
                                          image_data=image_to_data(image)), "图画已转成线稿。")

    def place_drawing(self, drawing, message):
        drawing.width = round(min(50., self.project.width/2), 1)
        drawing.x, drawing.y = round((self.project.width-drawing.width)/2, 1), round(self.project.height*.3, 1)
        self.record()
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            drawing_local_path(drawing)
        except ValueError as exc:
            QApplication.restoreOverrideCursor()
            return self.error(str(exc))
        QApplication.restoreOverrideCursor()
        self.project.drawings.append(drawing)
        self.selected_id = drawing.id
        self.refresh()
        self.statusBar().showMessage(message+"可拖动、改大小和方向；右侧可调排线。")
        return drawing

    def import_image(self):
        name,_ = QFileDialog.getOpenFileName(self,"导入书页照片","","图片 (*.png *.jpg *.jpeg *.bmp *.webp)")
        if not name:return
        reader = QImageReader(name)
        reader.setAutoTransform(True)
        image = reader.read()
        if image.isNull():return self.error("无法读取图片")
        from PySide6.QtCore import QBuffer,QByteArray,QIODevice
        data = QByteArray()
        buffer = QBuffer(data)
        buffer.open(QIODevice.OpenModeFlag.WriteOnly)
        image.save(buffer,"PNG")
        self.record()
        self.project.image_data = base64.b64encode(bytes(data)).decode("ascii")
        self.project.image_quad = None
        self.project.image_edges = None
        self.project.image_guides = None
        self.project.image_offset = [0,0]
        self.refresh()
        self.statusBar().showMessage("照片已嵌入项目。请确认实际页宽/页高，必要时四点校正。")

    def rotate_image(self,degrees):
        """Rotate embedded pixels only; old photo and machine mappings are invalid."""
        if not self.project.image_data:return self.error("请先导入底图")
        if degrees not in (-90,90):raise ValueError("底图旋转仅支持顺/逆时针 90°")
        image=QImage.fromData(base64.b64decode(self.project.image_data))
        if image.isNull():return self.error("无法读取项目底图")
        from .core import image_to_data
        rotated=image.transformed(QTransform().rotate(degrees))
        data=image_to_data(rotated)
        self.record()
        self.project.image_data=data
        self.project.image_quad=None
        self.project.image_edges=None
        self.project.image_guides=None
        self.project.image_offset=[0,0]
        # refresh_scene compares photo geometry and invalidates runtime mapping.
        self.refresh()
        direction="顺时针" if degrees>0 else "逆时针"
        self.statusBar().showMessage(f"底图已{direction}旋转 90°；原文件未改，旧四点与机器定位已失效，请重新四点校正并核对实际页宽/页高。")

    def rectify(self):
        if not self.project.image_data:return self.error("请先导入底图")
        image = QImage.fromData(base64.b64decode(self.project.image_data))
        dlg = RectifyDialog(self,image,self.project.image_quad,(self.project.width,self.project.height),self.project.image_edges,self.project.image_guides)
        if dlg.exec() == QDialog.DialogCode.Accepted:
            cross_products = []
            for i in range(4):
                a,b,c = dlg.points[i],dlg.points[(i+1)%4],dlg.points[(i+2)%4]
                cross_products.append((b[0]-a[0])*(c[1]-b[1])-(b[1]-a[1])*(c[0]-b[0]))
            if not (all(v > 1 for v in cross_products) or all(v < -1 for v in cross_products)):
                return self.error("四点必须围成凸四边形，请按左上、右上、右下、左下重新选择")
            source = QPolygonF([QPointF(*p) for p in dlg.points])
            target = QPolygonF([QPointF(0,0),QPointF(self.project.width,0),QPointF(self.project.width,self.project.height),QPointF(0,self.project.height)])
            transform = QTransform()
            if not QTransform.quadToQuad(source,target,transform):return self.error("四点共线或退化，请重新选择")
            self.record()
            self.project.image_quad = list(dlg.points)
            edges = getattr(dlg,"edges",None)
            self.project.image_edges = {side:[list(p) for p in values] for side,values in edges.items()} if edges else None
            guides = getattr(dlg,"guides",None)
            self.project.image_guides = [[list(p) for p in guide] for guide in guides] if guides else None
            self.project.image_offset = [0,0]
            self.refresh()

    def reset_rectify(self):
        self.record()
        self.project.image_quad = None
        self.project.image_edges = None
        self.project.image_guides = None
        self.project.image_offset = [0,0]
        self.refresh()

    def maybe_save(self):
        if not self.dirty:return True
        answer = QMessageBox.question(self,"保存项目","当前项目已修改。是否保存？",QMessageBox.StandardButton.Save | QMessageBox.StandardButton.Discard | QMessageBox.StandardButton.Cancel)
        if answer == QMessageBox.StandardButton.Cancel:return False
        if answer == QMessageBox.StandardButton.Save:return self.save()
        return True

    def paper_chosen(self, *_):
        if self.guard:
            return
        size = self.paper.currentData() or (self.page_w.value(), self.page_h.value())   # 自定义: just turn it
        w, h = sorted(size, reverse=self.paper_landscape.isChecked())
        self.guard = True
        self.page_w.setValue(w); self.page_h.setValue(h)
        self.guard = False
        self.page_changed()

    def sync_paper_choice(self):
        """Show which preset (and orientation) the current width/height match, else 自定义."""
        w, h = self.page_w.value(), self.page_h.value()
        index = next((i for i, (_, pw, ph) in enumerate(PAPER_SIZES)
                      if {round(pw, 1), round(ph, 1)} == {round(w, 1), round(h, 1)}), self.paper.count()-1)
        for widget in (self.paper, self.paper_landscape):
            widget.blockSignals(True)
        self.paper.setCurrentIndex(index)
        if index < len(PAPER_SIZES):
            self.paper_landscape.setChecked(w > h)
        for widget in (self.paper, self.paper_landscape):
            widget.blockSignals(False)

    def new_project(self):
        if not self.maybe_save():return
        self.project = blank_project(self.project.width, self.project.height)   # keep the current paper size
        self.filename = None
        self.selected_id = None
        self.undo_stack.clear()
        self.redo_stack.clear()
        self.dirty = False
        self.refresh()
        self.set_stage(0)
        self.fit_page()

    def open_project(self):
        if not self.maybe_save():return
        name,_ = QFileDialog.getOpenFileName(self,"打开书页项目","","书页项目 (*.pagewriter *.json)")
        if name:self.open_path(Path(name))

    def open_example(self,name):
        if self.maybe_save():
            self.open_path(resource_path("samples", name))

    def open_path(self,path):
        try:
            project = load_project(path)
        except Exception as exc:return self.error(str(exc))
        self.project,self.filename = project,Path(path)
        self.selected_id = project.blocks[0].id if project.blocks else None
        self.undo_stack.clear()
        self.redo_stack.clear()
        self.dirty = False
        self.refresh()
        self.set_stage(1 if project.blocks else 0)
        self.fit_page()
        warnings = font_warnings(project)
        if warnings:
            self.error("\n".join(warnings))

    def save(self,as_new=False):
        path = self.filename
        if as_new or path is None:
            name,_ = QFileDialog.getSaveFileName(self,"保存便携书页项目",str(path or "我的书页.pagewriter"),"书页项目 (*.pagewriter)")
            if not name:return False
            path = Path(name)
            if not path.suffix:path = path.with_suffix(".pagewriter")
        try:
            save_project(self.project,path)
        except Exception as exc:
            self.error(str(exc))
            return False
        self.filename = path
        self.dirty = False
        self.refresh_scene()
        self.statusBar().showMessage("项目已保存，底图与字形路径已嵌入，可复制到其他电脑。")
        return True

    def export_svg_file(self):
        try:svg = export_svg(self.project)
        except Exception as exc:return self.error(str(exc))
        name,_ = QFileDialog.getSaveFileName(self,"导出页面毫米路径","添注路径.svg","SVG (*.svg)")
        if name:
            Path(name).write_text(svg,encoding="utf-8")
            self.statusBar().showMessage("SVG 已导出。此文件为页面几何，不含机器标定。")

    def show_text(self,title,text):
        dlg = QDialog(self)
        dlg.setWindowTitle(title)
        dlg.resize(820,650)
        layout = QVBoxLayout(dlg)
        edit = QTextEdit()
        edit.setReadOnly(True)
        edit.setPlainText(str(text))
        edit.setFont(QFont("Consolas",10))
        layout.addWidget(edit)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.rejected.connect(dlg.reject)
        layout.addWidget(buttons)
        dlg.exec()

    def show_font_licenses(self):
        entries = builtin_fonts.catalog()
        sections = ['马良 · 内置字体与许可', '', '字体名称 / 原始来源 / 许可证', '']
        sections.extend(f'{entry["name"]} / {entry.get("source_name", "")} / {entry["license"]}' for entry in entries)
        for entry in entries:
            try:
                license_body = builtin_fonts.license_text(entry['id'])
            except (OSError, ValueError) as exc:
                return self.error(f'无法读取字体许可：{entry["name"]}\n{exc}')
            sections.extend(['', '='*64, entry['name'],
                f'原始来源：{entry.get("source_name", "")}  |  {entry["license"]}',
                '='*64, '', license_body])
        self.show_text('内置字体与许可', '\n'.join(sections))

    def preview_gcode(self):
        try:code = offline_gcode(self.project)
        except Exception as exc:return self.error(str(exc))
        self.show_text("离线页面坐标预览 · 禁止发送机器",code)

    def protocol_demo(self):
        try:
            from .protocol import simulate_job
            text = simulate_job(["G21","G90","G1 X1 Y1 F60"])
        except Exception as exc:text = "模拟接口："+str(exc)
        self.show_text("离线协议状态机演示 · 未打开串口",text)

    def toggle_replay(self):
        if self.replay_timer.isActive():
            self.replay_timer.stop()
            return
        self.replay_index = 0
        self.replay_timer.start(45)
        self.seek_replay(0)

    def advance_replay(self):
        self.replay_index += max(1,len(self.replay_parts)//250)
        value = min(100,round(self.replay_index/max(1,len(self.replay_parts))*100))
        self.replay_slider.setValue(value)
        if value == 100:self.replay_timer.stop()

    def seek_replay(self,value):
        if self.replay_overlay:
            self.canvas.scene().removeItem(self.replay_overlay)
            self.replay_overlay = None
        for item in getattr(self,"text_items",[]):item.setVisible(value == 100)
        if value < 100:
            path = QPainterPath()
            for sub in self.replay_parts[:round(len(self.replay_parts)*value/100)]:path.addPath(sub)
            self.replay_overlay = self.canvas.scene().addPath(path,ink_pen(BLUE,self.project.pen_width))
            self.replay_overlay.setZValue(10)

    def error(self,message):
        QMessageBox.warning(self,"马良",message)

    def closeEvent(self,event):
        if self.maybe_save():
            self.replay_timer.stop()
            self.device_panel.shutdown()
            event.accept()
        else:event.ignore()
