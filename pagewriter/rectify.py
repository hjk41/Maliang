"""Four-corner photo picker: zoom/pan, draggable corners, magnifier and live preview.

Pixel coordinates are independent of page/machine coordinates.
"""
import math
from PySide6.QtCore import Qt, QPointF, QRectF, QTimer
from .dewarp import MAX_EDGE_POINTS, MAX_GUIDE_POINTS, MAX_GUIDES, SIDES, PageWarp, normalize_edges, normalize_guides
from PySide6.QtGui import QColor, QImage, QPainter, QPainterPath, QPen, QPixmap, QPolygonF, QTransform
from PySide6.QtWidgets import (QApplication, QDialog, QDialogButtonBox, QGraphicsEllipseItem, QGraphicsItem,
    QGraphicsPathItem, QGraphicsRectItem, QGraphicsScene, QGraphicsSimpleTextItem, QGraphicsView, QHBoxLayout,
    QLabel, QPushButton, QVBoxLayout, QWidget)

CORNERS = ('左上', '右上', '右下', '左下')
YELLOW = QColor('#ffd54a')
RED = QColor('#db5965')
BLUE = QColor('#3d7be0')
ORANGE = QColor('#f0a020')
GREEN = QColor('#3fb950')
LOUPE = 180
HANDLE_HIT = 14


def order_corners(points):
    """Return TL, TR, BR, BL for any click order of a convex quadrilateral."""
    cx = sum(p[0] for p in points)/len(points)
    cy = sum(p[1] for p in points)/len(points)
    ordered = sorted(points, key=lambda p: math.atan2(p[1]-cy, p[0]-cx))
    start = min(range(len(ordered)), key=lambda i: ordered[i][0]+ordered[i][1])
    return ordered[start:]+ordered[:start]


def is_convex(points):
    if len(points) != 4:
        return False
    cross = []
    for i in range(4):
        a, b, c = points[i], points[(i+1) % 4], points[(i+2) % 4]
        cross.append((b[0]-a[0])*(c[1]-b[1])-(b[1]-a[1])*(c[0]-b[0]))
    return all(v > 1 for v in cross) or all(v < -1 for v in cross)


class PickerView(QGraphicsView):
    def __init__(self, dialog):
        super().__init__()
        self.dialog = dialog
        self.setScene(QGraphicsScene(self))
        self.setRenderHints(QPainter.RenderHint.Antialiasing | QPainter.RenderHint.SmoothPixmapTransform)
        self.setTransformationAnchor(QGraphicsView.ViewportAnchor.AnchorUnderMouse)
        self.setResizeAnchor(QGraphicsView.ViewportAnchor.AnchorViewCenter)
        self.setBackgroundBrush(QColor('#3a3f47'))
        self.setMouseTracking(True)
        self.viewport().setMouseTracking(True)
        self.setCursor(Qt.CursorShape.CrossCursor)
        self.panning = None
        self.dragging = None
        self.auto_fit = True
        self.loupe = QLabel(self.viewport())
        self.loupe.setFixedSize(LOUPE, LOUPE)
        self.loupe.setStyleSheet('border:2px solid #ffd54a;background:#222')
        self.loupe.hide()

    def scale_factor(self):
        return self.transform().m11()

    def fit(self):
        self.auto_fit = True
        rect = self.dialog.image_rect
        pad = max(rect.width(), rect.height())*.03
        self.fitInView(rect.adjusted(-pad, -pad, pad, pad), Qt.AspectRatioMode.KeepAspectRatio)

    def zoom(self, factor):
        fit = min(self.viewport().width()/max(1, self.dialog.image_rect.width()),
                  self.viewport().height()/max(1, self.dialog.image_rect.height()))
        target = max(fit*.5, min(40, self.scale_factor()*factor))
        factor = target/self.scale_factor()
        if abs(factor-1) > 1e-6:
            self.scale(factor, factor)
            self.auto_fit = False

    def wheelEvent(self, event):
        steps = event.angleDelta().y()/120
        if steps:
            self.zoom(1.25**steps)
        event.accept()
        self.refresh_loupe(event.position())

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if self.auto_fit:
            self.fit()

    def handle_at(self, view_pos):
        """Corner index, ('edge', side, i) or ('ghost', side, i); corners win ties."""
        best, found = HANDLE_HIT, None
        for key, (x, y) in self.dialog.handle_positions():
            point = self.mapFromScene(QPointF(x, y))
            distance = math.hypot(point.x()-view_pos.x(), point.y()-view_pos.y())
            if distance < best-.5:
                best, found = distance, key
        return found

    def mousePressEvent(self, event):
        position = event.position()
        key = self.handle_at(position)
        if event.button() == Qt.MouseButton.RightButton and isinstance(key, tuple) and key[0] in ('edge', 'guide'):
            (self.dialog.remove_edge_point if key[0] == 'edge' else self.dialog.remove_guide_point)(key[1], key[2])
            event.accept()
            return
        if event.button() in (Qt.MouseButton.RightButton, Qt.MouseButton.MiddleButton):
            self.panning = position
            self.setCursor(Qt.CursorShape.ClosedHandCursor)
            self.loupe.hide()
            event.accept()
            return
        if event.button() == Qt.MouseButton.LeftButton:
            if self.dialog.drawing_guide is not None and (key is None or (isinstance(key, tuple) and key[0] == 'ghost')):
                key = self.dialog.add_guide_point(self.mapToScene(position.toPoint()))
            elif isinstance(key, tuple) and key[0] == 'ghost':
                key = self.dialog.add_edge_point(key[1], key[2], self.mapToScene(position.toPoint()))
            if key is None and len(self.dialog.points) < 4:
                key = self.dialog.add_point(self.mapToScene(position.toPoint()))
            if key is not None:
                self.dragging = key
                self.dialog.select(key)
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        position = event.position()
        if self.panning is not None:
            delta = position-self.panning
            self.panning = position
            self.horizontalScrollBar().setValue(self.horizontalScrollBar().value()-round(delta.x()))
            self.verticalScrollBar().setValue(self.verticalScrollBar().value()-round(delta.y()))
            self.auto_fit = False
            return
        if self.dragging is not None:
            self.dialog.move_point(self.dragging, self.mapToScene(position.toPoint()))
        self.setCursor(Qt.CursorShape.SizeAllCursor if self.handle_at(position) is not None else Qt.CursorShape.CrossCursor)
        self.refresh_loupe(position)

    def mouseReleaseEvent(self, event):
        if self.panning is not None:
            self.panning = None
            self.setCursor(Qt.CursorShape.CrossCursor)
        if self.dragging is not None:
            self.dragging = None
            self.dialog.finish_edit()
        event.accept()

    def mouseDoubleClickEvent(self, event):
        if self.dialog.drawing_guide is not None and event.button() == Qt.MouseButton.LeftButton:
            self.dialog.finish_guide()
        event.accept()

    def leaveEvent(self, event):
        self.loupe.hide()
        super().leaveEvent(event)

    def keyPressEvent(self, event):
        if not self.dialog.handle_key(event):
            super().keyPressEvent(event)

    def refresh_loupe(self, view_pos):
        if self.panning is not None:
            return
        center = self.mapToScene(view_pos.toPoint())
        self.loupe.setPixmap(self.dialog.loupe_pixmap(center, self.scale_factor()))
        width = self.viewport().width()
        left = view_pos.x() > width-LOUPE-40 and view_pos.y() < LOUPE+40
        self.loupe.move(10 if left else width-LOUPE-10, 10)
        self.loupe.show()
        self.loupe.raise_()


class RectifyDialog(QDialog):
    """Return ``points`` (source pixels TL, TR, BR, BL; may lie outside the photo),
    ``edges`` (optional curved-edge points per side) and ``guides`` (optional
    traced text lines that should be straight); unused parts are ``None``."""

    def __init__(self, parent, image, points=None, page_size=None, edges=None, guides=None):
        super().__init__(parent)
        self.setWindowTitle('对齐四个角 · 照片校正（不改变机器坐标）')
        self.setWindowFlags(Qt.WindowType.Window | Qt.WindowType.WindowTitleHint |
                            Qt.WindowType.WindowMaximizeButtonHint | Qt.WindowType.WindowCloseButtonHint)
        screen = (parent.screen() if parent is not None else QApplication.primaryScreen()).availableGeometry()
        self.resize(int(screen.width()*.9), int(screen.height()*.9))
        self.image = image
        self.image_rect = QRectF(0, 0, image.width(), image.height())
        margin = max(image.width(), image.height())*.25
        self.bounds = self.image_rect.adjusted(-margin, -margin, margin, margin)
        self.page_size = page_size or (148, 210)
        self.selected = None
        self.preview_scale = min(1, 900/max(1, image.width(), image.height()))
        self.preview_source = image.scaled(max(1, round(image.width()*self.preview_scale)), max(1, round(image.height()*self.preview_scale)),
                                           Qt.AspectRatioMode.IgnoreAspectRatio, Qt.TransformationMode.SmoothTransformation)
        self.preview_timer = QTimer(self)
        self.preview_timer.setSingleShot(True)
        self.preview_timer.timeout.connect(self.update_preview)

        layout = QHBoxLayout(self)
        self.view = PickerView(self)
        layout.addWidget(self.view, 1)
        side = QWidget()
        side.setFixedWidth(290)
        sv = QVBoxLayout(side)
        sv.setContentsMargins(4, 0, 0, 0)
        title = QLabel('对齐四个角')
        title.setStyleSheet('font-size:16px;font-weight:700')
        sv.addWidget(title)
        self.prompt = QLabel()
        self.prompt.setWordWrap(True)
        self.prompt.setStyleSheet('font-size:14px;color:#245cad;font-weight:600;padding:6px 0')
        sv.addWidget(self.prompt)
        self.corner_list = QLabel()
        sv.addWidget(self.corner_list)
        tips = QLabel('· 滚轮放大缩小，按住右键拖动画面\n'
                      '· 圆点可以拖动微调，方向键逐像素移动（Shift 加快）\n'
                      '· 点的顺序不限，会自动识别左上 / 右上 / 右下 / 左下\n'
                      '· 角被挡住或拍到照片外时，可点在灰色区域，沿书页边缘的延长线估计')
        tips.setWordWrap(True)
        tips.setStyleSheet('color:#5d6b82')
        sv.addWidget(tips)
        bend = QLabel('<b>书页弯曲时</b><br>'
                      '· <b>参考线（推荐）</b>：点「沿文字加参考线」，沿一行本该水平的文字或印刷横线，从一头到另一头点 3～5 个点，'
                      '双击最后一点结束。可以在弯曲的地方多加几条。<br>'
                      '· <b>弯边</b>：把边中间的空心 ◇ 拖到弯曲的书边上。书角常会翘起，比文字弯得厉害，所以优先用参考线。<br>'
                      '· 右键点实心的点可删除。')
        bend.setWordWrap(True)
        bend.setStyleSheet('color:#5d6b82;padding-top:6px')
        sv.addWidget(bend)
        self.guide_button = QPushButton('＋ 沿文字加参考线')
        self.guide_button.clicked.connect(self.toggle_guide)
        sv.addWidget(self.guide_button)
        row = QHBoxLayout()
        self.undo_button = QPushButton('撤销上一点')
        self.undo_button.clicked.connect(self.undo_point)
        reset = QPushButton('重新选择')
        reset.clicked.connect(self.reset)
        row.addWidget(self.undo_button)
        row.addWidget(reset)
        sv.addLayout(row)
        self.straighten_button = QPushButton('清除弯边和参考线')
        self.straighten_button.clicked.connect(self.straighten)
        sv.addWidget(self.straighten_button)
        row = QHBoxLayout()
        for label, callback in [('放大', lambda: self.view.zoom(1.5)), ('缩小', lambda: self.view.zoom(1/1.5)), ('适合窗口', self.view.fit)]:
            button = QPushButton(label)
            button.clicked.connect(callback)
            row.addWidget(button)
        sv.addLayout(row)
        sv.addWidget(QLabel('<b>校正后的样子</b>（文字应横平竖直）'))
        self.preview = QLabel()
        self.preview.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.preview.setMinimumHeight(260)
        self.preview.setStyleSheet('background:#eef1f6;border-radius:6px')
        sv.addWidget(self.preview, 1)
        self.message = QLabel()
        self.message.setWordWrap(True)
        self.message.setStyleSheet('color:#a8661c')
        sv.addWidget(self.message)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        self.ok = buttons.button(QDialogButtonBox.StandardButton.Ok)
        self.ok.setText('应用')
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText('取消')
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        sv.addWidget(buttons)
        layout.addWidget(side)

        scene = self.view.scene()
        scene.setSceneRect(self.bounds)
        scene.addRect(self.image_rect, QPen(Qt.PenStyle.NoPen), QColor('#2d3138'))
        scene.addPixmap(QPixmap.fromImage(image))
        pen = QPen(YELLOW, 2)
        pen.setCosmetic(True)
        self.outline = QGraphicsPathItem()
        self.outline.setPen(pen)
        self.outline.setZValue(5)
        scene.addItem(self.outline)
        pen = QPen(GREEN, 2)
        pen.setCosmetic(True)
        self.guide_outline = QGraphicsPathItem()
        self.guide_outline.setPen(pen)
        self.guide_outline.setZValue(6)
        scene.addItem(self.guide_outline)
        self.handles = []
        self.points = [tuple(map(float, p)) for p in points] if points and len(points) == 4 else []
        self.edge_points = {side: list(values) for side, values in normalize_edges(edges).items()} if self.points else {}
        self.guide_points = normalize_guides(guides) if self.points else []
        self.drawing_guide = None
        self.warp = None
        self.refresh()
        QTimer.singleShot(0, self.view.fit)

    # Point model ---------------------------------------------------------
    @property
    def edges(self):
        result = {side: list(values) for side, values in self.edge_points.items() if values}
        return result or None

    @property
    def guides(self):
        return [list(g) for g in self.guide_points if len(g) >= 2] or None

    def clamp(self, point):
        x = min(max(point.x(), self.bounds.left()), self.bounds.right())
        y = min(max(point.y(), self.bounds.top()), self.bounds.bottom())
        return (x, y)

    def handle_positions(self):
        result = list(enumerate(self.points))
        if self.warp is not None:
            for side in SIDES:
                result += [(('edge', side, i), p) for i, p in enumerate(self.edge_points.get(side, []))]
        if len(self.points) == 4:
            for g, guide in enumerate(self.guide_points):
                result += [(('guide', g, i), p) for i, p in enumerate(guide)]
        if self.warp is not None and self.drawing_guide is None:
            for side in SIDES:
                if len(self.edge_points.get(side, [])) < MAX_EDGE_POINTS:
                    result += [(('ghost', side, i), p) for i, p in enumerate(self.warp.edge_midpoints(side))]
        return result

    def add_point(self, point):
        if len(self.points) >= 4:
            return None
        self.points.append(self.clamp(point))
        self.refresh()
        return len(self.points)-1

    def add_edge_point(self, side, index, point):
        values = self.edge_points.setdefault(side, [])
        if len(values) >= MAX_EDGE_POINTS:
            return None
        values.insert(index, self.clamp(point))
        self.refresh()
        return ('edge', side, index)

    def remove_edge_point(self, side, index):
        values = self.edge_points.get(side, [])
        if 0 <= index < len(values):
            values.pop(index)
            self.selected = None
            self.refresh()

    def toggle_guide(self):
        if self.drawing_guide is not None:
            self.finish_guide()
        elif len(self.points) == 4 and len(self.guide_points) < MAX_GUIDES:
            self.guide_points.append([])
            self.drawing_guide = len(self.guide_points)-1
            self.selected = None
            self.refresh()

    def add_guide_point(self, point):
        guide = self.guide_points[self.drawing_guide]
        if len(guide) >= MAX_GUIDE_POINTS:
            return None
        guide.append(self.clamp(point))
        key = ('guide', self.drawing_guide, len(guide)-1)
        if len(guide) >= MAX_GUIDE_POINTS:
            self.drawing_guide = None
        self.refresh()
        return key

    def finish_guide(self):
        """End tracing; a line needs at least two points."""
        if self.drawing_guide is not None and len(self.guide_points[self.drawing_guide]) < 2:
            self.guide_points.pop(self.drawing_guide)
            self.selected = None
        self.drawing_guide = None
        self.refresh()

    def remove_guide_point(self, guide, index):
        if 0 <= guide < len(self.guide_points) and 0 <= index < len(self.guide_points[guide]):
            self.guide_points[guide].pop(index)
            if len(self.guide_points[guide]) < 2 and guide != self.drawing_guide:
                self.guide_points.pop(guide)
                if self.drawing_guide is not None and self.drawing_guide > guide:
                    self.drawing_guide -= 1
            self.selected = None
            self.refresh()

    def move_point(self, key, point):
        if isinstance(key, tuple) and key[0] == 'guide':
            self.guide_points[key[1]][key[2]] = self.clamp(point)
        elif isinstance(key, tuple):
            self.edge_points[key[1]][key[2]] = self.clamp(point)
        else:
            self.points[key] = self.clamp(point)
        self.refresh(preview_delay=40)

    def select(self, key):
        self.selected = key
        self.refresh()

    def finish_edit(self):
        if len(self.points) == 4 and not isinstance(self.selected, tuple):
            selected = self.points[self.selected] if self.selected is not None else None
            self.points = order_corners(self.points)
            self.selected = self.points.index(selected) if selected in self.points else None
        self.refresh()

    def undo_point(self):
        if self.drawing_guide is not None:
            guide = self.guide_points[self.drawing_guide]
            if guide:
                guide.pop()
            else:
                self.finish_guide()
            self.selected = None
            self.refresh()
        elif self.points:
            self.points.pop()
            self.edge_points = {}
            self.guide_points = []
            self.selected = None
            self.refresh()

    def reset(self):
        self.points = []
        self.edge_points = {}
        self.guide_points = []
        self.drawing_guide = None
        self.selected = None
        self.refresh()

    def straighten(self):
        self.edge_points = {}
        self.guide_points = []
        self.drawing_guide = None
        if isinstance(self.selected, tuple):
            self.selected = None
        self.refresh()

    def nudge(self, dx, dy):
        key = self.selected
        if key is None:
            return
        if isinstance(key, tuple):
            values = self.guide_points[key[1]] if key[0] == 'guide' and key[1] < len(self.guide_points) else \
                self.edge_points.get(key[1], []) if key[0] == 'edge' else []
            if key[2] >= len(values):
                return
            x, y = values[key[2]]
        elif key < len(self.points):
            x, y = self.points[key]
        else:
            return
        self.move_point(key, QPointF(x+dx, y+dy))

    def handle_key(self, event):
        key = event.key()
        moves = {Qt.Key.Key_Left: (-1, 0), Qt.Key.Key_Right: (1, 0), Qt.Key.Key_Up: (0, -1), Qt.Key.Key_Down: (0, 1)}
        if key in moves:
            step = max(.25, 1/self.view.scale_factor())*(10 if event.modifiers() & Qt.KeyboardModifier.ShiftModifier else 1)
            self.nudge(moves[key][0]*step, moves[key][1]*step)
            return True
        if key == Qt.Key.Key_Backspace:
            self.undo_point()
            return True
        if self.drawing_guide is not None and key in (Qt.Key.Key_Return, Qt.Key.Key_Enter, Qt.Key.Key_Escape):
            self.finish_guide()
            return True
        if key == Qt.Key.Key_Delete and isinstance(self.selected, tuple):
            remove = self.remove_guide_point if self.selected[0] == 'guide' else self.remove_edge_point
            remove(self.selected[1], self.selected[2])
            return True
        return False

    def keyPressEvent(self, event):
        if not self.handle_key(event):
            super().keyPressEvent(event)

    # Drawing -------------------------------------------------------------
    def label_for(self, index):
        if len(self.points) == 4 and is_convex(self.points):
            return CORNERS[index]
        return f'第 {index+1} 点'

    def make_warp(self):
        if not is_convex(self.points):
            return None
        try:
            return PageWarp(self.points, self.edge_points, *self.page_size, guides=self.guide_points)
        except ValueError:
            return None

    def diamond(self, x, y, filled, selected):
        item = QGraphicsRectItem(-5, -5, 10, 10)
        item.setRotation(45)
        item.setFlag(QGraphicsItem.GraphicsItemFlag.ItemIgnoresTransformations)
        item.setPos(x, y)
        item.setZValue(9)
        if filled:
            item.setPen(QPen(QColor('white'), 1.5))
            item.setBrush(BLUE if selected else ORANGE)
        else:
            item.setPen(QPen(YELLOW, 1.5))
            item.setBrush(QColor(20, 24, 32, 150))
        return item

    def refresh(self, preview_delay=0):
        scene = self.view.scene()
        for item in self.handles:
            scene.removeItem(item)
        self.handles = []
        self.warp = self.make_warp()
        for index, (x, y) in enumerate(self.points):
            color = BLUE if index == self.selected else RED
            handle = QGraphicsEllipseItem(-7, -7, 14, 14)
            handle.setPen(QPen(QColor('white'), 2))
            handle.setBrush(color)
            handle.setFlag(QGraphicsItem.GraphicsItemFlag.ItemIgnoresTransformations)
            handle.setPos(x, y)
            handle.setZValue(10)
            tag = QGraphicsRectItem(handle)
            text = QGraphicsSimpleTextItem(self.label_for(index), tag)
            text.setBrush(QColor('white'))
            box = text.boundingRect().adjusted(-4, -2, 4, 2)
            tag.setRect(box)
            tag.setBrush(QColor(20, 24, 32, 190))
            tag.setPen(QPen(Qt.PenStyle.NoPen))
            tag.setPos(10, -box.height()-6)
            scene.addItem(handle)
            self.handles.append(handle)
        path = QPainterPath()
        if self.warp is not None:
            outline = (self.warp.edge_polyline('top') + self.warp.edge_polyline('right') +
                       self.warp.edge_polyline('bottom')[::-1] + self.warp.edge_polyline('left')[::-1])
            path.moveTo(*outline[0])
            for point in outline[1:]:
                path.lineTo(*point)
            path.closeSubpath()
            for key, (x, y) in self.handle_positions():
                if isinstance(key, tuple) and key[0] != 'guide':
                    item = self.diamond(x, y, key[0] == 'edge', key == self.selected)
                    scene.addItem(item)
                    self.handles.append(item)
        elif self.points:
            path.moveTo(*self.points[0])
            for point in self.points[1:]:
                path.lineTo(*point)
            if len(self.points) == 4:
                path.closeSubpath()
        self.outline.setPath(path)
        guides_path = QPainterPath()
        for g, guide in enumerate(self.guide_points):
            line = self.warp.guide_polyline(g) if self.warp is not None else None
            line = line or (guide if len(guide) >= 2 else None)
            if line:
                guides_path.moveTo(*line[0])
                for point in line[1:]:
                    guides_path.lineTo(*point)
            for i, (x, y) in enumerate(guide):
                dot = QGraphicsEllipseItem(-5, -5, 10, 10)
                dot.setFlag(QGraphicsItem.GraphicsItemFlag.ItemIgnoresTransformations)
                dot.setPos(x, y)
                dot.setZValue(9)
                dot.setPen(QPen(QColor('white'), 1.5))
                dot.setBrush(BLUE if ('guide', g, i) == self.selected else GREEN)
                scene.addItem(dot)
                self.handles.append(dot)
        self.guide_outline.setPath(guides_path)
        count = len(self.points)
        valid = self.warp is not None
        bent = sum(len(v) for v in self.edge_points.values())
        guides = len([g for g in self.guide_points if len(g) >= 2])
        if self.drawing_guide is not None:
            placed = len(self.guide_points[self.drawing_guide])
            self.prompt.setText(f'正在加第 {self.drawing_guide+1} 条参考线（已点 {placed} 点）：沿一行本该水平的文字，'
                                '从一头点到另一头，双击最后一点或按回车结束。')
        elif count < 4:
            self.prompt.setText(f'第 {count+1} / 4 点：点击书页的一个角\n（建议顺序：{"→".join(CORNERS)}）')
        elif not valid:
            self.prompt.setText('这四个点围不成书页形状，请拖动调整，或撤销后重选。')
        elif bent or guides:
            parts = ([f'{guides} 条参考线'] if guides else [])+([f'{bent} 个弯边点'] if bent else [])
            self.prompt.setText(f'已用 {"、".join(parts)} 拉平。看预览里的文字和网格是否横平竖直，满意后点「应用」。')
        else:
            self.prompt.setText('四个角都选好了。书页弯曲的话，用「沿文字加参考线」；看预览满意后点「应用」。')
        self.corner_list.setText('　'.join(f"{'●' if i < count else '○'} {self.label_for(i) if i < count else CORNERS[i]}" for i in range(4)))
        self.undo_button.setEnabled(bool(count))
        self.straighten_button.setEnabled(bool(bent or self.guide_points))
        self.guide_button.setText('完成这条参考线' if self.drawing_guide is not None else '＋ 沿文字加参考线')
        self.guide_button.setEnabled(self.drawing_guide is not None or (valid and len(self.guide_points) < MAX_GUIDES))
        self.ok.setEnabled(valid and self.drawing_guide is None)
        placed = self.points+[p for values in self.edge_points.values() for p in values]+[p for g in self.guide_points for p in g]
        outside = any(not self.image_rect.contains(QPointF(x, y)) for x, y in placed)
        self.message.setText('有点在照片外面：请确认是沿书页边缘延长后估计的位置。' if outside else '')
        if preview_delay:
            self.preview_timer.start(preview_delay)
        else:
            self.update_preview()

    def preview_size(self):
        width, height = self.page_size
        box_w, box_h = max(120, self.preview.width()-16), max(160, self.preview.height()-16)
        scale = min(box_w/width, box_h/height)
        return max(1, round(width*scale)), max(1, round(height*scale))

    def update_preview(self):
        warp = self.make_warp()
        if warp is None:
            self.preview.setPixmap(QPixmap())
            self.preview.setText('选好四个角后显示')
            return
        width, height = self.preview_size()
        result = warp.render(self.preview_source, width, height, source_scale=self.preview_scale, cell=12)
        painter = QPainter(result)
        painter.setPen(QPen(QColor(61, 123, 224, 110), 1, Qt.PenStyle.DashLine))
        for i in range(1, 6):
            painter.drawLine(round(width*i/6), 0, round(width*i/6), height)
            painter.drawLine(0, round(height*i/6), width, round(height*i/6))
        painter.end()
        self.preview.setText('')
        self.preview.setPixmap(QPixmap.fromImage(result))

    def loupe_pixmap(self, center, view_scale):
        """Four times the current view magnification around the cursor."""
        radius = max(3., LOUPE/2/(view_scale*4))
        crop = QImage(LOUPE, LOUPE, QImage.Format.Format_RGB32)
        crop.fill(QColor('#2d3138'))
        painter = QPainter(crop)
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, False)
        zoom = LOUPE/(2*radius)
        painter.setTransform(QTransform().scale(zoom, zoom).translate(radius-center.x(), radius-center.y()))
        painter.drawImage(0, 0, self.image)
        painter.resetTransform()
        for key, (x, y) in self.handle_positions():
            if isinstance(key, tuple) and key[0] == 'ghost':
                continue
            px, py = (x-center.x()+radius)*zoom, (y-center.y()+radius)*zoom
            if -10 <= px <= LOUPE+10 and -10 <= py <= LOUPE+10:
                painter.setPen(QPen(QColor('white'), 2))
                painter.setBrush(BLUE if key == self.selected else RED if not isinstance(key, tuple) else GREEN if key[0] == 'guide' else ORANGE)
                painter.drawEllipse(QPointF(px, py), 5, 5)
        painter.setPen(QPen(YELLOW, 1))
        half = LOUPE/2
        painter.drawLine(QPointF(half, 0), QPointF(half, half-6))
        painter.drawLine(QPointF(half, half+6), QPointF(half, LOUPE))
        painter.drawLine(QPointF(0, half), QPointF(half-6, half))
        painter.drawLine(QPointF(half+6, half), QPointF(LOUPE, half))
        painter.end()
        return QPixmap.fromImage(crop)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if hasattr(self, 'preview'):
            self.preview_timer.start(30)
