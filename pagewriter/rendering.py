"""Native Qt page preview, using the same geometry as the desktop and export."""
import math
from PySide6.QtCore import QPointF, QRectF
from PySide6.QtGui import QColor, QImage, QPainter, QPen, QPolygonF, QTransform
from . import core


def render_page(project, path, scale=4., show_zones=True):
    if not .5 <= scale <= 20 or project.width * project.height * scale * scale > 40_000_000:
        raise ValueError('预览尺寸过大或比例无效')
    width, height = max(1, math.ceil(project.width*scale)), max(1, math.ceil(project.height*scale))
    image = QImage(width, height, QImage.Format.Format_ARGB32)
    image.fill(QColor('white'))
    painter = QPainter(image)
    try:
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        # Use actual integer image extents so the reported mapping is exact.
        sx, sy = width/project.width, height/project.height
        painter.scale(sx, sy)
        painter.setClipRect(QRectF(0, 0, project.width, project.height))
        if project.image_data:
            photo = core.data_to_image(project.image_data)
            painter.save(); painter.setOpacity(project.image_opacity)
            painter.translate(*project.image_offset)
            if project.image_quad and (project.image_edges or project.image_guides):
                from .dewarp import PageWarp
                warp = PageWarp(project.image_quad, project.image_edges, project.width, project.height, guides=project.image_guides)
                photo = warp.render(photo, width, height)
                painter.drawImage(QRectF(0, 0, project.width, project.height), photo)
            else:
                quad = project.image_quad or [[0,0], [photo.width(),0], [photo.width(),photo.height()], [0,photo.height()]]
                target = [[0,0], [project.width,0], [project.width,project.height], [0,project.height]]
                transform = QTransform()
                if not QTransform.quadToQuad(QPolygonF([QPointF(*p) for p in quad]), QPolygonF([QPointF(*p) for p in target]), transform):
                    raise ValueError('照片映射退化')
                painter.setTransform(transform, True)
                painter.drawImage(QPointF(0, 0), photo)
            painter.restore()
        core.sync_flow(project)
        if show_zones:
            for zone in project.zones:
                painter.setPen(QPen(QColor('#b96a58'), .15))
                painter.setBrush(QColor(210, 100, 70, 35))
                painter.drawPath(core.zone_path(zone))
        painter.setPen(QPen(QColor('#245cad'), project.pen_width))
        painter.setBrush(QColor(0, 0, 0, 0))
        for _, ink in core.ink_items(project):
            painter.drawPath(ink)
    finally:
        painter.end()
    if not image.save(str(path), 'PNG'):
        raise ValueError('无法保存预览文件')
    return {'path': str(path), 'width_px': width, 'height_px': height,
            'page_width_mm': project.width, 'page_height_mm': project.height,
            'pixel_to_page_mm': [project.width/width, 0, 0, project.height/height, 0, 0],
            'page_to_pixel': [width/project.width, 0, 0, height/project.height, 0, 0],
            'origin': 'top_left', 'x_axis': 'right', 'y_axis': 'down'}
