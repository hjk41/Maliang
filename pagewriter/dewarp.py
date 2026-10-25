"""Flatten a photographed page from four corners plus optional curves.

Page mm (x, y) -> "rectified" mm through a Coons-style patch -> photo pixels
through the four-corner homography. Curve points are stored in photo pixels;
in rectified space a straight edge is the page border itself, so with no
curve points the patch is the identity and the result is exactly the classic
four-point perspective warp.

Two kinds of curves:
- edges: the paper's own top/right/bottom/left boundary;
- guides: a line of text or a printed rule that should be straight and
  horizontal. A paper edge often curls much more than the text next to it,
  so guides along the content are usually the better reference near a spine.

Horizontal curves (top edge, guides, bottom edge) are interpolated piecewise
in y; left/right edges add a horizontal Coons correction. With only the top
and bottom edges this is exactly the bilinearly blended Coons patch. Each
curve is parameterised by arc length (paper does not stretch). This is a 2D
model, not a 3D reconstruction.
"""
import bisect
import math
from PySide6.QtCore import QPointF, QRect, QRectF
from PySide6.QtGui import QColor, QImage, QPainter, QPolygonF, QTransform

SIDES = ('top', 'right', 'bottom', 'left')
MAX_EDGE_POINTS = 5
MAX_GUIDES = 6
MAX_GUIDE_POINTS = 6
MIN_GUIDE_GAP = 1.0  # mm between neighbouring horizontal curves


def _catmull_rom(p0, p1, p2, p3, count):
    """Centripetal Catmull-Rom samples from p1 (inclusive) to p2 (exclusive)."""
    def knot(t, a, b):
        return t + max(math.dist(a, b), 1e-9)**.5
    t0 = 0.
    t1 = knot(t0, p0, p1)
    t2 = knot(t1, p1, p2)
    t3 = knot(t2, p2, p3)

    def mix(a, b, ta, tb, t):
        wa, wb = (tb-t)/(tb-ta), (t-ta)/(tb-ta)
        return (wa*a[0]+wb*b[0], wa*a[1]+wb*b[1])
    result = []
    for k in range(count):
        t = t1+(t2-t1)*k/count
        a1, a2, a3 = mix(p0, p1, t0, t1, t), mix(p1, p2, t1, t2, t), mix(p2, p3, t2, t3, t)
        b1, b2 = mix(a1, a2, t0, t2, t), mix(a2, a3, t1, t3, t)
        result.append(mix(b1, b2, t1, t2, t))
    return result


class ArcCurve:
    """Smooth curve through ordered nodes, evaluated by arc-length fraction."""

    def __init__(self, nodes, samples=48):
        self.nodes = [tuple(map(float, n)) for n in nodes]
        if len(self.nodes) == 2:
            points = self.nodes
        else:
            ext = [(2*self.nodes[0][0]-self.nodes[1][0], 2*self.nodes[0][1]-self.nodes[1][1]), *self.nodes,
                   (2*self.nodes[-1][0]-self.nodes[-2][0], 2*self.nodes[-1][1]-self.nodes[-2][1])]
            points = []
            for i in range(1, len(ext)-2):
                points.extend(_catmull_rom(ext[i-1], ext[i], ext[i+1], ext[i+2], samples))
            points.append(self.nodes[-1])
        self.points = points
        self.lengths = [0.]
        for a, b in zip(points, points[1:]):
            self.lengths.append(self.lengths[-1]+math.dist(a, b))
        self.length = self.lengths[-1]

    def at(self, fraction):
        if self.length <= 0:
            return self.points[0]
        s = min(max(fraction, 0.), 1.)*self.length
        i = min(max(bisect.bisect_right(self.lengths, s)-1, 0), len(self.points)-2)
        span = self.lengths[i+1]-self.lengths[i]
        w = 0. if span <= 0 else (s-self.lengths[i])/span
        a, b = self.points[i], self.points[i+1]
        return (a[0]+(b[0]-a[0])*w, a[1]+(b[1]-a[1])*w)

    def fraction_of(self, point):
        """Arc-length fraction of the sample nearest to ``point``."""
        index = min(range(len(self.points)), key=lambda i: math.dist(self.points[i], point))
        return 0. if self.length <= 0 else self.lengths[index]/self.length


def normalize_edges(edges):
    if not edges:
        return {}
    return {side: [tuple(map(float, p)) for p in edges.get(side, [])] for side in SIDES if edges.get(side)}


def normalize_guides(guides):
    return [[tuple(map(float, p)) for p in guide] for guide in (guides or []) if len(guide) >= 2]


class PageWarp:
    def __init__(self, corners, edges, width, height, guides=None):
        self.width, self.height = float(width), float(height)
        if len(corners) != 4 or self.width <= 0 or self.height <= 0:
            raise ValueError('需要四个角和正的页面尺寸')
        page = QPolygonF([QPointF(0, 0), QPointF(width, 0), QPointF(width, height), QPointF(0, height)])
        self.homography = QTransform()
        if not QTransform.quadToQuad(page, QPolygonF([QPointF(*p) for p in corners]), self.homography):
            raise ValueError('四点共线或退化')
        self.inverse, ok = self.homography.inverted()
        if not ok:
            raise ValueError('四点映射不可逆')
        self.edges = normalize_edges(edges)
        self.guides = normalize_guides(guides)
        self.flat = not any(self.edges.values()) and not self.guides
        w, h = self.width, self.height
        ends = {'top': ((0, 0), (w, 0), 0), 'bottom': ((0, h), (w, h), 0),
                'left': ((0, 0), (0, h), 1), 'right': ((w, 0), (w, h), 1)}
        self.curves = {}
        for side, (start, end, axis) in ends.items():
            inner = sorted((self.rectify_pixel(p) for p in self.edges.get(side, [])), key=lambda q: q[axis])
            inner = [q for q in inner if 0 < q[axis] < (w if axis == 0 else h)]
            self.curves[side] = ArcCurve([start, *inner, end])
        levels = [(0., self.curves['top'], -1)]
        for index, guide in enumerate(self.guides):
            level = self.guide_level(guide)
            if level is not None:
                levels.append((level[0], level[1], index))
        levels.sort(key=lambda item: item[0])
        self.levels = [levels[0]]
        for item in levels[1:]:
            if item[0]-self.levels[-1][0] >= MIN_GUIDE_GAP and self.height-item[0] >= MIN_GUIDE_GAP:
                self.levels.append(item)
        self.levels.append((self.height, self.curves['bottom'], -1))
        self.used_guides = {index for _, _, index in self.levels if index >= 0}

    def guide_level(self, guide):
        """Extend a traced line straight to both page sides.

        Returns (page y the line belongs to, full-width curve). The line's
        true height is taken from its flatter end, which is usually the part
        of the page away from the bend.
        """
        w = self.width
        points = sorted((self.rectify_pixel(p) for p in guide), key=lambda q: q[0])
        points = [p for i, p in enumerate(points) if i == 0 or p[0]-points[i-1][0] > 1e-6]
        if len(points) < 2:
            return None

        def slope(a, b):
            return (b[1]-a[1])/(b[0]-a[0])
        left_slope, right_slope = slope(points[0], points[1]), slope(points[-2], points[-1])
        left = (0., points[0][1]-left_slope*points[0][0])
        right = (w, points[-1][1]+right_slope*(w-points[-1][0]))
        nodes = [left, *[p for p in points if 0 < p[0] < w], right]
        level = left[1] if abs(left_slope) <= abs(right_slope) else right[1]
        if not 0 < level < self.height:
            return None
        return level, ArcCurve(nodes)

    def rectify_pixel(self, point):
        mapped = self.inverse.map(QPointF(*point))
        return (mapped.x(), mapped.y())

    def rectified(self, x, y):
        w, h = self.width, self.height
        u, v = x/w, y/h
        k = max(0, min(len(self.levels)-2, bisect.bisect_right([level for level, _, _ in self.levels], y)-1))
        (y0, upper, _), (y1, lower, _) = self.levels[k], self.levels[k+1]
        t = (y-y0)/(y1-y0)
        a, b = upper.at(u), lower.at(u)
        left, right = self.curves['left'].at(v), self.curves['right'].at(v)
        return ((1-t)*a[0]+t*b[0]+(1-u)*left[0]+u*(right[0]-w),
                (1-t)*a[1]+t*b[1]+(1-u)*(left[1]-v*h)+u*(right[1]-v*h))

    def guide_polyline(self, index, count=64):
        """Photo-pixel polyline of a guide extended across the page, or None if unused."""
        curve = next((c for _, c, i in self.levels if i == index), None)
        if curve is None:
            return None
        result = []
        for i in range(count+1):
            mapped = self.homography.map(QPointF(*curve.at(i/count)))
            result.append((mapped.x(), mapped.y()))
        return result

    def to_image(self, x, y):
        if not self.flat:
            x, y = self.rectified(x, y)
        mapped = self.homography.map(QPointF(x, y))
        return (mapped.x(), mapped.y())

    def edge_polyline(self, side, count=64):
        """Photo-pixel polyline of one boundary, for drawing the outline."""
        curve = self.curves[side]
        result = []
        for i in range(count+1):
            mapped = self.homography.map(QPointF(*curve.at(i/count)))
            result.append((mapped.x(), mapped.y()))
        return result

    def edge_midpoints(self, side):
        """Photo-pixel midpoints between consecutive nodes: where a new point can be pulled out."""
        curve = self.curves[side]
        fractions = [curve.fraction_of(node) for node in curve.nodes]
        result = []
        for a, b in zip(fractions, fractions[1:]):
            mapped = self.homography.map(QPointF(*curve.at((a+b)/2)))
            result.append((mapped.x(), mapped.y()))
        return result

    def render(self, image, out_width, out_height, source_scale=1., cell=24):
        """Draw the flattened page into an ``out_width`` x ``out_height`` image.

        The page is split into small cells; each cell is drawn with its own
        perspective transform, so curved edges become straight.
        """
        result = QImage(out_width, out_height, QImage.Format.Format_ARGB32_Premultiplied)
        result.fill(QColor('#2d3138'))
        painter = QPainter(result)
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        columns = max(1, math.ceil(out_width/cell)) if not self.flat else 1
        rows = max(1, math.ceil(out_height/cell)) if not self.flat else 1
        xs = [round(out_width*i/columns) for i in range(columns+1)]
        ys = [round(out_height*j/rows) for j in range(rows+1)]
        sx, sy = self.width/out_width, self.height/out_height
        corners = [[self.to_image(x*sx, y*sy) for x in xs] for y in ys]
        bounds = QRectF(0, 0, image.width(), image.height())
        for j in range(rows):
            for i in range(columns):
                quad = [corners[j][i], corners[j][i+1], corners[j+1][i+1], corners[j+1][i]]
                source = QPolygonF([QPointF(x*source_scale, y*source_scale) for x, y in quad])
                target = QPolygonF([QPointF(xs[i], ys[j]), QPointF(xs[i+1], ys[j]), QPointF(xs[i+1], ys[j+1]), QPointF(xs[i], ys[j+1])])
                transform = QTransform()
                if not QTransform.quadToQuad(source, target, transform):
                    continue
                area = source.boundingRect().adjusted(-2, -2, 2, 2).intersected(bounds)
                if area.isEmpty():
                    continue
                painter.resetTransform()
                painter.setClipRect(QRect(xs[i], ys[j], xs[i+1]-xs[i], ys[j+1]-ys[j]))
                painter.setTransform(transform)
                painter.drawImage(area.topLeft(), image, area)
        painter.end()
        return result
