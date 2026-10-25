"""Read an SVG's own vector geometry (no rasterising).

Returns polylines normalised so the drawing is 1 unit wide:
- ``lines``: every visible shape outline / stroked path, curves flattened finely;
- ``fills``: closed polygons of filled shapes with their darkness (0..1), so
  they can optionally be hatched with gray-dependent density.
Supported: path (M L H V C S Q T A Z, absolute and relative), line, polyline,
polygon, rect, circle, ellipse, nested <g>/<svg> with transform and viewBox.
<defs>, <clipPath>, <mask>, <symbol>, <use>, text and images are skipped.
"""
import math
import re
import xml.etree.ElementTree as ET
from PySide6.QtCore import QPointF, QRectF
from PySide6.QtGui import QColor, QPainterPath, QTransform

WORK = 1000.0   # flatten at this width so small icons keep smooth curves
SKIP = {"defs", "clipPath", "mask", "symbol", "use", "text", "image", "style", "metadata", "title", "desc", "pattern", "marker"}
_NUM = re.compile(r"[-+]?(?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?")


def _tag(element):
    return element.tag.split("}")[-1]


def _length(value, default=0.):
    match = _NUM.match((value or "").strip())
    return float(match.group()) if match else default


def _transform(text):
    result = QTransform()
    for name, args in re.findall(r"(\w+)\s*\(([^)]*)\)", text or ""):
        v = [float(x) for x in _NUM.findall(args)]
        t = QTransform()
        if name == "matrix" and len(v) == 6:
            t = QTransform(v[0], v[1], v[2], v[3], v[4], v[5])
        elif name == "translate" and v:
            t.translate(v[0], v[1] if len(v) > 1 else 0)
        elif name == "scale" and v:
            t.scale(v[0], v[1] if len(v) > 1 else v[0])
        elif name == "rotate" and v:
            if len(v) == 3:
                t.translate(v[1], v[2]); t.rotate(v[0]); t.translate(-v[1], -v[2])
            else:
                t.rotate(v[0])
        elif name == "skewX" and v:
            t.shear(math.tan(math.radians(v[0])), 0)
        elif name == "skewY" and v:
            t.shear(0, math.tan(math.radians(v[0])))
        result = t*result      # SVG: rightmost transform applies first
    return result


def _style(element, inherited):
    style = dict(inherited)
    for key in ("fill", "stroke", "display", "visibility", "fill-opacity", "opacity"):
        if element.get(key) is not None:
            style[key] = element.get(key).strip()
    for part in (element.get("style") or "").split(";"):
        if ":" in part:
            key, value = part.split(":", 1)
            style[key.strip()] = value.strip()
    return style


def _darkness(color):
    if not color or color in ("none", "transparent"):
        return None
    if color.startswith("url("):
        return .5                     # gradients/patterns: treat as mid gray
    c = QColor(color)
    if not c.isValid():
        return 1.
    return 1-(.299*c.redF()+.587*c.greenF()+.114*c.blueF())


def _arc(path, x0, y0, rx, ry, phi, large, sweep, x, y):
    """SVG elliptical arc (endpoint form) appended as short line segments."""
    if rx == 0 or ry == 0:
        return path.lineTo(x, y)
    rx, ry = abs(rx), abs(ry)
    c, s = math.cos(math.radians(phi)), math.sin(math.radians(phi))
    dx, dy = (x0-x)/2, (y0-y)/2
    x1, y1 = c*dx+s*dy, -s*dx+c*dy
    scale = x1*x1/(rx*rx)+y1*y1/(ry*ry)
    if scale > 1:
        rx, ry = rx*math.sqrt(scale), ry*math.sqrt(scale)
    num = rx*rx*ry*ry-rx*rx*y1*y1-ry*ry*x1*x1
    coef = math.sqrt(max(0., num/(rx*rx*y1*y1+ry*ry*x1*x1))) * (-1 if large == sweep else 1)
    cx1, cy1 = coef*rx*y1/ry, -coef*ry*x1/rx
    cx, cy = c*cx1-s*cy1+(x0+x)/2, s*cx1+c*cy1+(y0+y)/2
    angle = lambda ux, uy: math.atan2(uy, ux)
    start = angle((x1-cx1)/rx, (y1-cy1)/ry)
    delta = angle((-x1-cx1)/rx, (-y1-cy1)/ry)-start
    if sweep and delta < 0:
        delta += 2*math.pi
    elif not sweep and delta > 0:
        delta -= 2*math.pi
    steps = max(4, int(abs(delta)/(math.pi/48)))
    for i in range(1, steps+1):
        t = start+delta*i/steps
        px, py = rx*math.cos(t), ry*math.sin(t)
        path.lineTo(c*px-s*py+cx, s*px+c*py+cy)


def _path_data(d):
    path = QPainterPath()
    tokens = re.findall(r"[MmLlHhVvCcSsQqTtAaZz]|" + _NUM.pattern, d or "")
    i, cmd, x, y, sx, sy = 0, None, 0., 0., 0., 0.
    last_ctrl, last_cmd = None, None
    def num():
        nonlocal i
        value = float(tokens[i]); i += 1
        return value
    while i < len(tokens):
        if re.fullmatch(r"[A-Za-z]", tokens[i]):
            cmd = tokens[i]; i += 1
        elif cmd is None:
            break
        rel = cmd.islower()
        C = cmd.upper()
        ox, oy = (x, y) if rel else (0., 0.)
        try:
            if C == "Z":
                path.closeSubpath(); x, y = sx, sy; last_ctrl = None
                last_cmd = C
                continue
            if C == "M":
                x, y = num()+ox, num()+oy; path.moveTo(x, y); sx, sy = x, y
                cmd = "l" if rel else "L"
            elif C == "L":
                x, y = num()+ox, num()+oy; path.lineTo(x, y)
            elif C == "H":
                x = num()+(x if rel else 0); path.lineTo(x, y)
            elif C == "V":
                y = num()+(y if rel else 0); path.lineTo(x, y)
            elif C == "C":
                x1, y1, x2, y2 = num()+ox, num()+oy, num()+ox, num()+oy
                x, y = num()+ox, num()+oy
                path.cubicTo(x1, y1, x2, y2, x, y); last_ctrl = (x2, y2)
            elif C == "S":
                x1, y1 = (2*x-last_ctrl[0], 2*y-last_ctrl[1]) if last_cmd in ("C", "S") and last_ctrl else (x, y)
                x2, y2 = num()+ox, num()+oy
                x, y = num()+ox, num()+oy
                path.cubicTo(x1, y1, x2, y2, x, y); last_ctrl = (x2, y2)
            elif C == "Q":
                x1, y1 = num()+ox, num()+oy
                x, y = num()+ox, num()+oy
                path.quadTo(x1, y1, x, y); last_ctrl = (x1, y1)
            elif C == "T":
                x1, y1 = (2*x-last_ctrl[0], 2*y-last_ctrl[1]) if last_cmd in ("Q", "T") and last_ctrl else (x, y)
                x, y = num()+ox, num()+oy
                path.quadTo(x1, y1, x, y); last_ctrl = (x1, y1)
            elif C == "A":
                rx, ry, phi, large, sweep = num(), num(), num(), num(), num()
                nx, ny = num()+ox, num()+oy
                _arc(path, x, y, rx, ry, phi, bool(large), bool(sweep), nx, ny); x, y = nx, ny
        except (IndexError, ValueError):
            break
        last_cmd = C
    return path


def _shape(element, tag):
    g = lambda name, default=0.: _length(element.get(name), default)
    path = QPainterPath()
    if tag == "path":
        return _path_data(element.get("d"))
    if tag == "rect":
        w, h = g("width"), g("height")
        if w > 0 and h > 0:
            rx = g("rx", g("ry")); ry = g("ry", rx)
            if rx > 0 or ry > 0:
                path.addRoundedRect(QRectF(g("x"), g("y"), w, h), min(rx, w/2), min(ry, h/2))
            else:
                path.addRect(QRectF(g("x"), g("y"), w, h))
    elif tag == "circle":
        r = g("r")
        if r > 0: path.addEllipse(QPointF(g("cx"), g("cy")), r, r)
    elif tag == "ellipse":
        if g("rx") > 0 and g("ry") > 0: path.addEllipse(QPointF(g("cx"), g("cy")), g("rx"), g("ry"))
    elif tag == "line":
        path.moveTo(g("x1"), g("y1")); path.lineTo(g("x2"), g("y2"))
    elif tag in ("polyline", "polygon"):
        v = [float(n) for n in _NUM.findall(element.get("points") or "")]
        if len(v) >= 4:
            path.moveTo(v[0], v[1])
            for k in range(2, len(v)-1, 2):
                path.lineTo(v[k], v[k+1])
            if tag == "polygon": path.closeSubpath()
    return path


def _hatch_polygon(polygon, spacing, angle, levels, cutoff, darkness, index_offset=0):
    """Even-odd scanline hatch of one polygon; line i drawn if darkness reaches its dither level."""
    pattern = {1: [0], 2: [0, 1], 3: [0, 2, 1], 4: [0, 2, 1, 3]}[max(1, min(4, int(levels)))]
    theta = math.radians(angle)
    ca, sa = math.cos(theta), math.sin(theta)
    # Rotate so hatch lines are horizontal in (u, v).
    pts = [(x*ca+y*sa, -x*sa+y*ca) for x, y in polygon]
    vs = [v for _, v in pts]
    segments = []
    k0 = math.ceil(min(vs)/spacing)
    for k in range(k0, int(max(vs)/spacing)+1):
        level = cutoff+(1-cutoff)*pattern[k % len(pattern)]/len(pattern)
        if darkness < level:
            continue
        v = k*spacing
        xs = []
        for (u1, v1), (u2, v2) in zip(pts, pts[1:]+pts[:1]):
            if (v1 <= v < v2) or (v2 <= v < v1):
                xs.append(u1+(v-v1)*(u2-u1)/(v2-v1))
        xs.sort()
        for a, b in zip(xs[::2], xs[1::2]):
            if b-a > 1e-6:
                seg = [(a*ca-v*sa, a*sa+v*ca), (b*ca-v*sa, b*sa+v*ca)]
                segments.append(seg if k % 2 == 0 else seg[::-1])
    return segments


def strokes(vector, width, fill=False, fill_cutoff=.6, fill_spacing=.5, fill_angle=45., fill_levels=3, min_stroke=0., **_):
    """Box-local mm polylines for a parsed SVG at the given width (mm)."""
    lines = [[(x*width, y*width) for x, y in line] for line in vector["lines"]]
    lines = [l for l in lines if sum(math.dist(a, b) for a, b in zip(l, l[1:])) >= min_stroke]
    if fill:
        for polygon, darkness in vector.get("fills", []):
            if darkness >= fill_cutoff:
                lines += _hatch_polygon([(x*width, y*width) for x, y in polygon], fill_spacing, fill_angle,
                                        fill_levels, fill_cutoff, darkness)
    return lines


def parse(source):
    """SVG file path or text -> dict(lines, fills, aspect) normalised to width 1."""
    text = source if "<svg" in str(source)[:2000] else open(source, "rb").read().decode("utf-8", "replace")
    root = ET.fromstring(text)
    if _tag(root) != "svg":
        raise ValueError("不是 SVG 文件")
    view = [float(v) for v in _NUM.findall(root.get("viewBox") or "")]
    width, height = _length(root.get("width"), 0), _length(root.get("height"), 0)
    if len(view) == 4 and view[2] > 0 and view[3] > 0:
        vx, vy, vw, vh = view
    else:
        vx, vy, vw, vh = 0, 0, width or 100, height or 100
    base = QTransform.fromTranslate(-vx, -vy)*QTransform.fromScale(WORK/vw, WORK/vw)
    outlines, fills = [], []

    def walk(element, transform, style):
        tag = _tag(element)
        if tag in SKIP:
            return
        style = _style(element, style)
        if style.get("display") == "none" or style.get("visibility") == "hidden":
            return
        transform = _transform(element.get("transform"))*transform
        if tag in ("g", "svg", "a", "switch"):
            for child in element:
                walk(child, transform, style)
            return
        shape = _shape(element, tag)
        if shape.isEmpty():
            return
        mapped = transform.map(shape)
        stroke = _darkness(style.get("stroke", "none"))
        fill = _darkness(style.get("fill", "black"))        # SVG default fill is black
        if fill is not None and float(style.get("fill-opacity", 1) or 1) > 0:
            for polygon in mapped.toFillPolygons():
                if polygon.count() >= 3:
                    fills.append(([(p.x(), p.y()) for p in polygon], fill))
        if stroke is not None or fill is not None:
            for polygon in mapped.toSubpathPolygons():
                points = [(p.x(), p.y()) for p in polygon]
                if len(points) >= 2:
                    outlines.append(points)
    walk(root, base, {})
    if not outlines:
        raise ValueError("SVG 里没有可写的线条或形状")
    aspect = vh/vw
    norm = lambda pts: [[round(x/WORK, 6), round(y/WORK, 6)] for x, y in pts]
    return {"lines": [norm(p) for p in outlines], "fills": [[norm(p), round(d, 3)] for p, d in fills], "aspect": aspect}
