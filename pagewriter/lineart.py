"""Raster image -> pen strokes (box-local mm polylines).

- Lines: dark pixels are thinned to a one-pixel skeleton (Zhang-Suen) and
  traced into single strokes, simplified (Ramer-Douglas-Peucker) and short
  bits dropped. Nothing is outlined twice.
- Fill (optional): areas that are dark *and* wider than ``fill_min_width``
  (so ordinary outlines are not filled) are hatched with parallel lines.
  Line density follows the gray level: with N levels, every hatch line has
  its own darkness threshold (an ordered-dither pattern), so darker areas
  receive more lines. Skeleton strokes inside filled areas are dropped.
"""
import base64
import math
import numpy as np
from PySide6.QtCore import Qt
from PySide6.QtGui import QImage

MAX_PX = 700
WRITE_FEED, TRAVEL_FEED, PEN_CYCLE_S = 3000, 4000, .15   # mm/min, mm/min, s per lift+drop
_NB4 = [(-1, 0), (0, 1), (1, 0), (0, -1)]
_NB_DIAG = [(-1, 1), (1, 1), (1, -1), (-1, -1)]
# Order in which hatch lines switch on as areas get darker (1D ordered dither).
_DITHER = {1: [0], 2: [0, 1], 3: [0, 2, 1], 4: [0, 2, 1, 3]}


def darkness(image_data, max_px=MAX_PX):
    """0 (white) .. 1 (black) array from an embedded PNG/JPEG, longest side <= max_px."""
    image = QImage.fromData(base64.b64decode(image_data))
    if image.isNull():
        raise ValueError("无法读取图画图片")
    if image.hasAlphaChannel():
        flat = QImage(image.size(), QImage.Format.Format_ARGB32)
        flat.fill(Qt.GlobalColor.white)
        from PySide6.QtGui import QPainter
        painter = QPainter(flat)
        painter.drawImage(0, 0, image)
        painter.end()
        image = flat
    scale = min(1., max_px/max(image.width(), image.height()))
    if scale < 1:
        image = image.scaled(max(1, round(image.width()*scale)), max(1, round(image.height()*scale)),
                             Qt.AspectRatioMode.IgnoreAspectRatio, Qt.TransformationMode.SmoothTransformation)
    gray = image.convertToFormat(QImage.Format.Format_Grayscale8)
    raw = np.frombuffer(gray.constBits(), dtype=np.uint8, count=gray.bytesPerLine()*gray.height())
    pixels = raw.reshape(gray.height(), gray.bytesPerLine())[:, :gray.width()]
    return 1-pixels.astype(np.float32)/255


def thin(mask):
    """Zhang-Suen thinning of a boolean array."""
    img = np.pad(mask.astype(np.uint8), 1)
    changed = True
    while changed:
        changed = False
        for step in (0, 1):
            p = img
            P2, P3, P4, P5 = p[:-2, 1:-1], p[:-2, 2:], p[1:-1, 2:], p[2:, 2:]
            P6, P7, P8, P9 = p[2:, 1:-1], p[2:, :-2], p[1:-1, :-2], p[:-2, :-2]
            ring = [P2, P3, P4, P5, P6, P7, P8, P9, P2]
            B = sum(r.astype(np.int16) for r in ring[:8])
            A = sum(((ring[i] == 0) & (ring[i+1] == 1)).astype(np.int16) for i in range(8))
            keep = (p[1:-1, 1:-1] == 1) & (B >= 2) & (B <= 6) & (A == 1)
            if step == 0:
                m = keep & (P2*P4*P6 == 0) & (P4*P6*P8 == 0)
            else:
                m = keep & (P2*P4*P8 == 0) & (P2*P6*P8 == 0)
            if m.any():
                img[1:-1, 1:-1][m] = 0
                changed = True
    return img[1:-1, 1:-1].astype(bool)


def trace(skeleton):
    """Skeleton pixels -> polylines (x, y), split only at real junctions and ends."""
    pts = set(zip(*np.nonzero(skeleton)))

    def nbrs(p):
        # 4-neighbours, plus a diagonal only when no 4-neighbour already links
        # to it; plain 8-connectivity makes staircases look like junctions.
        y, x = p
        result = [(y+dy, x+dx) for dy, dx in _NB4 if (y+dy, x+dx) in pts]
        result += [(y+dy, x+dx) for dy, dx in _NB_DIAG
                   if (y+dy, x+dx) in pts and (y+dy, x) not in pts and (y, x+dx) not in pts]
        return result
    degree = {p: len(nbrs(p)) for p in pts}
    used, lines = set(), []
    key = lambda a, b: (a, b) if a < b else (b, a)
    for start in sorted(pts, key=lambda p: degree[p] == 2):
        for n in nbrs(start):
            if key(start, n) in used:
                continue
            used.add(key(start, n))
            line, prev, cur = [start], start, n
            while True:
                line.append(cur)
                if degree[cur] != 2 or cur == start:
                    break
                nxt = [q for q in nbrs(cur) if q != prev and key(cur, q) not in used]
                if not nxt:
                    break
                used.add(key(cur, nxt[0]))
                prev, cur = cur, nxt[0]
            lines.append([(x, y) for y, x in line])
    return lines


def simplify(points, eps):
    if len(points) < 3 or eps <= 0:
        return points
    arr = np.asarray(points, float)
    a, d = arr[0], arr[-1]-arr[0]
    n = math.hypot(*d)
    rel = arr-a
    dist = np.abs(d[0]*rel[:, 1]-d[1]*rel[:, 0])/n if n else np.hypot(rel[:, 0], rel[:, 1])
    i = int(np.argmax(dist))
    if dist[i] > eps:
        return simplify(points[:i+1], eps)[:-1]+simplify(points[i:], eps)
    return [points[0], points[-1]]


def _morph(mask, radius, grow):
    out = mask.copy()
    for _ in range(int(radius)):
        p = np.pad(out, 1, constant_values=not grow)
        stack = [p[1+dy:p.shape[0]-1+dy, 1+dx:p.shape[1]-1+dx] for dy in (-1, 0, 1) for dx in (-1, 0, 1)]
        out = np.logical_or.reduce(stack) if grow else np.logical_and.reduce(stack)
    return out


def hatch(dark, region, mm_per_px, spacing_mm, angle_deg, cutoff, levels):
    """Parallel segments (px) inside ``region``; darker pixels get more lines."""
    h, w = dark.shape
    if not region.any() or spacing_mm <= 0:
        return []
    spacing = spacing_mm/mm_per_px
    theta = math.radians(angle_deg)
    along, across = np.array([math.cos(theta), math.sin(theta)]), np.array([-math.sin(theta), math.cos(theta)])
    corners = np.array([[0, 0], [w, 0], [0, h], [w, h]], float)
    offsets, extents = corners@across, corners@along
    pattern = _DITHER[max(1, min(4, int(levels)))]
    count = len(pattern)
    steps = np.arange(extents.min(), extents.max(), .5)
    segments, index = [], 0
    for offset in np.arange(offsets.min()+spacing/2, offsets.max(), spacing):
        # This line is drawn where darkness reaches its own threshold.
        level = cutoff+(1-cutoff)*pattern[index % count]/count
        index += 1
        xs = steps*along[0]+offset*across[0]
        ys = steps*along[1]+offset*across[1]
        inside = (xs >= 0) & (xs < w) & (ys >= 0) & (ys < h)
        on = np.zeros(len(steps), bool)
        xi, yi = xs[inside].astype(int), ys[inside].astype(int)
        on[inside] = region[yi, xi] & (dark[yi, xi] >= level)
        edges = np.diff(np.concatenate([[0], on.astype(np.int8), [0]]))
        for a, b in zip(np.nonzero(edges == 1)[0], np.nonzero(edges == -1)[0]):
            if b-a >= 2:
                segments.append([(float(xs[a]), float(ys[a])), (float(xs[b-1]), float(ys[b-1]))])
    # Alternate directions so the pen zig-zags instead of returning each time.
    return [s if i % 2 == 0 else s[::-1] for i, s in enumerate(segments)]


def order_strokes(lines):
    """Greedy nearest-end ordering (strokes may be reversed) to cut pen-up travel."""
    if not lines:
        return []
    starts = np.array([l[0] for l in lines], float)
    ends = np.array([l[-1] for l in lines], float)
    left = np.ones(len(lines), bool)
    pos, out = np.zeros(2), []
    for _ in range(len(lines)):
        ds = np.where(left, np.hypot(*(starts-pos).T), np.inf)
        de = np.where(left, np.hypot(*(ends-pos).T), np.inf)
        i, j = int(np.argmin(ds)), int(np.argmin(de))
        if de[j] < ds[i]:
            line, idx = lines[j][::-1], j
        else:
            line, idx = lines[i], i
        left[idx] = False
        out.append(line)
        pos = np.array(line[-1], float)
    return out


def length(line):
    return sum(math.dist(a, b) for a, b in zip(line, line[1:]))


def convert(image_data, width_mm, threshold=128, detail=1.0, min_stroke=.6,
            fill=False, fill_cutoff=.6, fill_spacing=.8, fill_angle=45., fill_levels=3, fill_min_width=1.5):
    """Strokes in box-local mm plus statistics."""
    dark = darkness(image_data)
    h, w = dark.shape
    mm = width_mm/w
    ink = dark >= 1-threshold/255
    filled = np.zeros_like(ink)
    lines = []
    skeleton = thin(ink)
    if fill:
        candidate = dark >= fill_cutoff
        # How many erosions each pixel survives ~ its distance to the nearest edge.
        depth, layer = np.zeros(candidate.shape, np.int16), candidate
        for i in range(1, 60):
            layer = _morph(layer, 1, False)
            if not layer.any():
                break
            depth[layer] = i
        # Typical half-width of the drawing's own strokes, measured on the skeleton.
        on_lines = depth[skeleton & candidate]
        stroke = float(np.median(on_lines)) if on_lines.size else 0.
        # Fill only areas clearly thicker than both the strokes and fill_min_width.
        radius = max(1, round(fill_min_width/2/mm), int(math.ceil(2*stroke+1)))
        core = depth >= radius
        filled = _morph(core, radius, True) & candidate
        lines += hatch(dark, filled, mm, fill_spacing, fill_angle, fill_cutoff, fill_levels)
        skeleton &= ~_morph(filled, 1, False)
    for line in trace(skeleton):
        line = simplify(line, detail)
        if length(line)*mm >= min_stroke:
            lines.append(line)
    strokes = order_strokes([[(x*mm, y*mm) for x, y in line] for line in lines])
    ink_mm = sum(length(s) for s in strokes)
    travel = sum(math.dist(a[-1], b[0]) for a, b in zip(strokes, strokes[1:]))
    seconds = ink_mm/WRITE_FEED*60+travel/TRAVEL_FEED*60+len(strokes)*PEN_CYCLE_S
    return {"paths": [[(round(x, 4), round(y, 4)) for x, y in s] for s in strokes], "height": h*mm,
            "strokes": len(strokes), "ink_mm": ink_mm, "travel_mm": travel, "seconds": seconds,
            "hatched": bool(filled.any())}
