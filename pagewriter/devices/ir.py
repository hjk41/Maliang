"""Ordered, device-independent pen operations in millimetre work coordinates.

The IR has no firmware commands or vertical-axis requirement. An adapter decides
how a pen transition is actuated, and how completion is observed.
"""
from dataclasses import dataclass
import math
from typing import Iterable


@dataclass(frozen=True)
class Pen:
    down: bool


@dataclass(frozen=True)
class Move:
    x: float
    y: float
    feed: float


Operation = Pen | Move


def compile_paths(polylines: Iterable, *, mapping, work_bounds, write_feed,
                  travel_feed=None, preflight_passed=False) -> tuple[Operation, ...]:
    """Validate all geometry before returning any executable operations."""
    if preflight_passed is not True:
        raise ValueError('须先通过当前页面的碰撞/越界检查')
    if len(mapping) != 6 or len(work_bounds) != 4:
        raise ValueError('Invalid work mapping or bounds')
    values = (*mapping, *work_bounds, write_feed, travel_feed or write_feed)
    if not all(math.isfinite(v) for v in values):
        raise ValueError('Nonfinite work mapping, bounds or feed')
    a,b,c,d,tx,ty = mapping
    left,top,right,bottom = work_bounds
    travel_feed = write_feed if travel_feed is None else travel_feed
    if abs(a*d-b*c)<1e-9 or right<=left or bottom<=top or min(write_feed,travel_feed)<=0:
        raise ValueError('Invalid work mapping, bounds or feed')
    result: list[Operation] = [Pen(False)]
    count = 0
    for path in polylines:
        points = []
        for point in path:
            if len(point) != 2:
                raise ValueError('Expected XY point')
            x,y = point
            x,y = a*x+c*y+tx,b*x+d*y+ty
            if not all(math.isfinite(v) for v in (x,y)) or not (left<=x<=right and top<=y<=bottom):
                raise ValueError('路径越出已验证机器工作行程')
            points.append((x,y))
        if len(points)<2:
            continue
        count += 1
        result.extend((Move(*points[0],travel_feed),Pen(True)))
        result.extend(Move(x,y,write_feed) for x,y in points[1:])
        result.append(Pen(False))
    if not count:
        raise ValueError('没有可书写路径')
    return tuple(result)
