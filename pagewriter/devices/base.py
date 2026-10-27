"""Shared application-facing contract; implementations own their worker/transport."""
from typing import Callable, Protocol
from dataclasses import asdict, is_dataclass
import copy
import json
import math
import threading
import time

PEN_CALIBRATION_STROKE_MM = 5.0


class RuntimeMotionLease:
    """Nonpersistent worker fence; only scope lifetime, no Qt or document access."""
    def __init__(self,deadline):
        if not math.isfinite(deadline): raise ValueError('Invalid motion lease deadline')
        self.deadline=deadline
        self.revoked=threading.Event()

    def revoke(self): self.revoked.set()

    def check(self):
        if self.revoked.is_set() or time.monotonic()>=self.deadline:
            raise ValueError('Operator motion lease revoked or expired before dispatch')


def validate_motion_lease(kwargs):
    if 'motion_lease' in kwargs:
        lease=kwargs['motion_lease']
        if not isinstance(lease,RuntimeMotionLease): raise ValueError('Invalid runtime motion lease')
        lease.check()


def runtime_binding(controller, state=None):
    """Canonical runtime authority shared by job intent and worker validation."""
    state=controller.snapshot() if state is None else state
    profile=getattr(controller,'profile',None)
    if is_dataclass(profile): profile=asdict(profile)
    elif isinstance(profile,dict): profile=copy.deepcopy(profile)
    else: profile=None
    return dict(session_id=state.get('session_id'),geometry_id=state.get('geometry_id'),
        device_profile=state.get('device_profile'),capabilities=state.get('capabilities',{}),
        page_to_machine=state.get('page_to_machine'),pen_up=state.get('pen_up'),pen_down=state.get('pen_down'),
        pen_touch=state.get('pen_touch'),wco=state.get('wco'),clearance=state.get('clearance'),
        authorization=profile,authorization_id=state.get('authorization_id'),authorized=state.get('authorized',False))


def validate_job_context(controller, kwargs):
    """Close the snapshot-to-worker race without trusting client preflight."""
    validate_motion_lease(kwargs)
    state=controller.snapshot()
    if 'expected_binding' in kwargs:
        canonical=lambda value: json.dumps(value,sort_keys=True,allow_nan=False)
        if canonical(kwargs['expected_binding'])!=canonical(runtime_binding(controller,state)):
            raise ValueError('Job device context changed before dispatch')
    for argument,key in [('expected_session_id','session_id'),('expected_mapping','page_to_machine'),('expected_position','wpos')]:
        if argument in kwargs:
            expected=kwargs[argument]; actual=state.get(key)
            if isinstance(expected,(list,tuple)) and actual is not None: actual=tuple(actual); expected=tuple(expected)
            if expected!=actual: raise ValueError('Job device context changed before dispatch')


class DeviceController(Protocol):
    def start(self): ...
    def close(self) -> None: ...
    def submit(self, action: str, **kwargs) -> None: ...
    def snapshot(self) -> dict: ...
    def drain_events(self) -> list[dict]: ...
    def subscribe_events(self, listener: Callable[[dict], None]) -> Callable[[], None]: ...
    def tick(self) -> None: ...
