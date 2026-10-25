"""Offline BtWriter/Grbl protocol model. This module never imports or opens serial.

The tested machine is not calibrated for writing. A default MachineProfile cannot
compile executable output. ``ok`` acknowledges a line; completion requires a fresh
status query after the last acknowledgement and a subsequent Idle report. Faults
stop the finite job without resetting, homing, unlocking, moving, or replaying it.
The simulator is a protocol test, never evidence of physical writing validation.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
import math
import re
import time
from typing import Callable, Iterable


@dataclass(frozen=True)
class Event:
    kind: str
    text: str
    fields: dict[str, object] = field(default_factory=dict)


class LineParser:
    """Bounded incremental ASCII parser; handles arbitrary packet boundaries."""

    def __init__(self, max_buffer: int = 65536):
        self.buffer = bytearray()
        self.max_buffer = max_buffer

    def feed(self, data: bytes) -> list[Event]:
        self.buffer.extend(data)
        if len(self.buffer) > self.max_buffer:
            self.buffer.clear()
            return [Event("malformed", "receive buffer exceeded")]
        events = []
        while b"\n" in self.buffer:
            raw, _, rest = self.buffer.partition(b"\n")
            self.buffer[:] = rest
            try:
                text = raw.rstrip(b"\r").decode("ascii").strip()
            except UnicodeDecodeError:
                events.append(Event("malformed", "non-ASCII device reply"))
                continue
            if text:
                events.append(self.parse_line(text))
        return events

    @staticmethod
    def parse_line(text: str) -> Event:
        if text == "ok":
            return Event("ack", text)
        if text.startswith("error:"):
            return Event("error", text)
        if text.startswith("ALARM:"):
            return Event("alarm", text)
        if text.startswith(("Grbl ", "BtWriter ")):
            return Event("reset", text)
        if text.startswith("<") and text.endswith(">"):
            parts = text[1:-1].split("|")
            state = parts[0]
            if state.split(":")[0] not in {"Idle", "Run", "Hold", "Jog", "Alarm", "Door", "Check", "Home", "Sleep"}:
                return Event("malformed", text)
            fields: dict[str, object] = {"state": state}
            try:
                for part in parts[1:]:
                    key, value = part.split(":", 1)
                    if key in {"MPos", "WPos", "WCO"}:
                        coords = tuple(float(v) for v in value.split(","))
                        if len(coords) != 3 or not all(math.isfinite(v) for v in coords):
                            raise ValueError("invalid coordinates")
                        fields[key] = coords
                    else:
                        fields[key] = value
            except ValueError:
                return Event("malformed", text)
            return Event("status", text, fields)
        if (text.startswith("[") and text.endswith("]")) or re.fullmatch(r"\$\d+=[-+\d.eE]+", text):
            return Event("info", text)
        return Event("unexpected", text)


class JobState(str, Enum):
    READY = "ready"
    RUNNING = "running"
    WAITING_IDLE = "waiting_idle"
    COMPLETE = "complete"
    FAILED = "failed"
    CANCELLED = "cancelled"


class SimulatedTransport:
    """Exclusive in-memory transport. No socket, serial port, or OS device."""

    def __init__(self):
        self.owner: object | None = None
        self.sent: list[bytes] = []

    def claim(self, owner: object) -> None:
        if self.owner is not None and self.owner is not owner:
            raise RuntimeError("transport already has an owner")
        self.owner = owner

    def write(self, owner: object, payload: bytes) -> None:
        if self.owner is not owner:
            raise RuntimeError("not the transport owner")
        self.sent.append(payload)

    def release(self, owner: object) -> None:
        if self.owner is owner:
            self.owner = None


class JobSession:
    """One line in flight. Caller explicitly pumps send_next() and poll_status().

    Duplicate replies before sending the next line are rejected. Because Grbl
    replies have no sequence IDs, a duplicated ack delayed until after a later
    command is sent cannot be distinguished on the wire; do not replay a job
    after a transport fault. This is an offline state-machine implementation.
    """

    def __init__(self, lines: Iterable[str], transport: SimulatedTransport | None = None,
                 timeout: float = 10.0, clock: Callable[[], float] = time.monotonic):
        self.lines = [self._normalize(line) for line in lines if line.strip()]
        if not self.lines:
            raise ValueError("empty job")
        if timeout <= 0 or not math.isfinite(timeout):
            raise ValueError("invalid timeout")
        self.transport = transport or SimulatedTransport()
        self.transport.claim(self)
        self.clock, self.timeout = clock, timeout
        self.parser = LineParser()
        self.state = JobState.READY
        self.sent_count = self.ack_count = 0
        self.in_flight = False
        self.awaiting_status = False
        self.deadline: float | None = None
        self.failure = ""
        self.events: list[Event] = []

    @staticmethod
    def _normalize(line: str) -> str:
        line = line.strip()
        if not line or any(ord(c) < 32 or ord(c) > 126 for c in line):
            raise ValueError("commands must be single printable ASCII lines")
        # Finite offline motion jobs cannot sneak control-plane operations in.
        codes = [float(code) for code in re.findall(r"G\s*(\d+(?:\.\d+)?)", line, re.I)]
        if ("$" in line or any(c in line for c in "!?~")
                or any(int(code) in {10, 28, 30, 92} for code in codes)):
            raise ValueError("control/configuration command prohibited in finite job")
        return line

    def send_next(self) -> bytes | None:
        if self.state in {JobState.COMPLETE, JobState.FAILED, JobState.CANCELLED, JobState.WAITING_IDLE} or self.in_flight:
            return None
        if self.sent_count >= len(self.lines):
            return None
        payload = self.lines[self.sent_count].encode("ascii") + b"\n"
        self.transport.write(self, payload)
        self.sent_count += 1
        self.in_flight = True
        self.state = JobState.RUNNING
        self.deadline = self.clock() + self.timeout
        return payload

    def poll_status(self) -> bytes | None:
        # First version deliberately has no status polling while commands are
        # in flight. Grbl replies carry no query IDs, so an outstanding earlier
        # query could otherwise deliver a delayed Idle after the final ack and
        # be mistaken for the completion query's answer.
        if self.state != JobState.WAITING_IDLE:
            return None
        if self.state == JobState.WAITING_IDLE and self.parser.buffer:
            # Finish any already-started report before issuing the completion
            # query; a partial stale Idle must not satisfy the fresh barrier.
            return None
        self.transport.write(self, b"?")
        if self.state == JobState.WAITING_IDLE:
            self.awaiting_status = True
        return b"?"

    def feed(self, data: bytes) -> list[Event]:
        events = self.parser.feed(data)
        self.events.extend(events)
        for event in events:
            if self.state in {JobState.FAILED, JobState.CANCELLED}:
                continue
            if event.kind in {"error", "alarm", "reset", "malformed", "unexpected"}:
                self.fail(event.text)
            elif event.kind == "ack":
                if not self.in_flight or self.state != JobState.RUNNING:
                    self.fail("unexpected or duplicate acknowledgement")
                    continue
                self.ack_count += 1
                self.in_flight = False
                self.deadline = None
                if self.ack_count == len(self.lines):
                    self.state = JobState.WAITING_IDLE
                    self.awaiting_status = False
                    self.deadline = self.clock() + self.timeout
            elif event.kind == "status":
                status = str(event.fields["state"]).split(":")[0]
                if status in {"Alarm", "Door", "Sleep"}:
                    self.fail("unsafe device state: " + status)
                elif self.state == JobState.WAITING_IDLE and self.awaiting_status:
                    self.awaiting_status = False
                    if status == "Idle":
                        self.state = JobState.COMPLETE
                        self.deadline = None
                        self.transport.release(self)
        return events

    def check_timeout(self) -> bool:
        if self.deadline is not None and self.clock() >= self.deadline:
            self.fail("reply or final Idle timeout")
            return True
        return False

    def fail(self, reason: str) -> None:
        self.failure = reason
        self.state = JobState.FAILED
        self.in_flight = self.awaiting_status = False
        self.deadline = None
        self.transport.release(self)

    def disconnect(self) -> None:
        self.fail("transport disconnected; no automatic replay")

    def cancel(self) -> None:
        if self.state in {JobState.COMPLETE, JobState.FAILED, JobState.CANCELLED}:
            return
        self.state = JobState.CANCELLED
        self.in_flight = self.awaiting_status = False
        self.deadline = None
        self.transport.release(self)
        # Cancel only stops this sender. It does not claim queued motion stopped.


@dataclass
class MachineProfile:
    """Explicit WORK-coordinate profile, wholly separate from photo rectification."""

    name: str = "未标定设备（禁止真实书写）"
    page_to_machine: tuple[float, float, float, float, float, float] | None = None
    xy_verified: bool = False
    z_verified: bool = False
    clearance_verified: bool = False
    work_bounds: tuple[float, float, float, float] | None = None
    z_bounds: tuple[float, float] | None = None
    z_up: float | None = None
    z_down: float | None = None
    write_feed: float | None = None
    z_feed: float | None = None
    max_write_feed: float | None = None
    max_z_feed: float | None = None
    validation_note: str = ""
    travel_feed: float | None = None
    max_travel_feed: float | None = None

    def validate(self) -> None:
        if not (self.xy_verified and self.z_verified and self.clearance_verified and self.validation_note.strip()):
            raise ValueError("机器坐标、Z 接触/净空和现场验证记录均须完成；当前禁止生成机器作业")
        sequences = (self.page_to_machine, self.work_bounds, self.z_bounds)
        if any(v is None for v in sequences):
            raise ValueError("缺少机器映射或已验证行程")
        if len(self.page_to_machine) != 6 or len(self.work_bounds) != 4 or len(self.z_bounds) != 2:
            raise ValueError("标定结构不正确")
        values = [*self.page_to_machine, *self.work_bounds, *self.z_bounds, self.z_up, self.z_down,
                  self.write_feed, self.z_feed, self.max_write_feed, self.max_z_feed]
        if any(v is None or not math.isfinite(v) for v in values):
            raise ValueError("标定包含缺失或非有限数值")
        a, b, c, d, _, _ = self.page_to_machine
        if abs(a * d - b * c) < 1e-9:
            raise ValueError("坐标映射不可逆")
        left, top, right, bottom = self.work_bounds
        if right <= left or bottom <= top or self.z_bounds[1] <= self.z_bounds[0]:
            raise ValueError("行程范围无效")
        if self.z_up == self.z_down or not all(self.z_bounds[0] <= z <= self.z_bounds[1] for z in (self.z_up, self.z_down)):
            raise ValueError("Z 抬落高度无效或超出已验证范围")
        if not (0 < self.write_feed <= self.max_write_feed and 0 < self.z_feed <= self.max_z_feed):
            raise ValueError("速度超出已验证范围")
        if (self.travel_feed is None) != (self.max_travel_feed is None):
            raise ValueError("空走速度和上限须成对提供")
        if self.travel_feed is not None:
            if not all(math.isfinite(v) for v in (self.travel_feed,self.max_travel_feed)) or not 0<self.travel_feed<=self.max_travel_feed:
                raise ValueError("空走速度超出已验证范围")

    def map_point(self, point: tuple[float, float]) -> tuple[float, float]:
        if self.page_to_machine is None:
            raise ValueError("没有页面到机器的标定")
        x, y = point
        a, b, c, d, tx, ty = self.page_to_machine
        return a * x + c * y + tx, b * x + d * y + ty


def compile_machine_job(polylines: Iterable[Iterable[tuple[float, float]]], profile: MachineProfile,
                        *, preflight_passed: bool = False) -> list[str]:
    """Compile only an explicitly validated profile, never a guessed axis mapping.

    Caller must pass the current geometry preflight. Output has no homing,
    unlock, zero-setting, reset, or unsolicited return-to-origin operation.
    """
    profile.validate()
    if not preflight_passed:
        raise ValueError("须先通过当前页面的碰撞/越界检查")
    from .ir import compile_paths
    operations = compile_paths(polylines, mapping=profile.page_to_machine,
        work_bounds=profile.work_bounds, write_feed=profile.write_feed,
        travel_feed=profile.travel_feed, preflight_passed=preflight_passed)
    return encode_job(operations,profile)


def encode_job(operations, profile):
    """Grbl/Z adapter for the generic ordered pen/motion IR."""
    from .ir import Move, Pen
    lines = ['G21', 'G90', 'G94']
    for operation in operations:
        if isinstance(operation, Pen):
            height = profile.z_down if operation.down else profile.z_up
            lines.append(f'G1 Z{height:.4f} F{profile.z_feed:.3f}')
        elif isinstance(operation, Move):
            lines.append(f'G1 X{operation.x:.4f} Y{operation.y:.4f} F{operation.feed:.3f}')
        else:
            raise ValueError('Unsupported motion operation')
    return lines



def simulate_job(lines: Iterable[str] | None = None) -> str:
    """Run a finite deterministic in-memory demonstration; never touches COM."""
    session = JobSession(lines if lines is not None else ["G21", "G90", "G1 X1 Y1 F60"])
    log = ["离线协议模拟器 · 内存传输 · 未连接串口"]
    while session.ack_count < len(session.lines):
        payload = session.send_next()
        if payload is None:
            raise RuntimeError(session.failure or "simulator stalled")
        log.append("发送 " + payload.decode().rstrip())
        session.feed(b"<Run|MPos:0,0,15|WCO:0,0,15>\r\n")
        session.feed(b"o")
        session.feed(b"k\n")
        log.append(f"确认 {session.ack_count}/{len(session.lines)}")
    log.append("全部 ok：仍未完成，等待新的状态查询回复")
    session.poll_status()
    session.feed(b"<Idle|MPos:1,1,15|WCO:0,0,15>\r\n")
    log.append("收到查询后的 Idle：模拟作业完成")
    log.append("此结果仅验证状态机；真实方向、行程、Z 接触高度及书写质量仍未标定。")
    return "\n".join(log)
