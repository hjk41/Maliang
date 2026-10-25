"""Transport boundary and process-local exclusive physical endpoint ownership."""
from __future__ import annotations
import gc
import threading
import weakref
from typing import Protocol


class Transport(Protocol):
    def write(self, payload: bytes) -> int: ...
    def read(self, count: int) -> bytes: ...
    def close(self) -> None: ...
    @property
    def in_waiting(self) -> int: ...


_owners = weakref.WeakValueDictionary()
_ownership_lock = threading.Lock()


def claim_endpoint(endpoint, owner):
    # Windows accepts both COM3 and \\.\COM3. Give both one ownership key.
    key = str(endpoint).strip().removeprefix('\\\\.\\').casefold()
    if not key:
        raise ValueError('Empty physical endpoint')
    gc.collect()  # Discard unreachable controllers (for example injected clock closures).
    with _ownership_lock:
        current = _owners.get(key)
        if current is not None and current is not owner:
            raise ValueError('Device endpoint already owned by another controller')
        _owners[key] = owner
    return key


def release_endpoint(key, owner):
    with _ownership_lock:
        if _owners.get(key) is owner:
            del _owners[key]


class SerialTransport:
    """Open with DTR/RTS low, exposing raw serial only for legacy integrations."""
    def __init__(self, port, owner, factory=None, baudrate=115200):
        self.owner = weakref.ref(owner)
        self.key = claim_endpoint(port,owner)
        self.serial = None
        try:
            if factory is None:
                import serial
                factory = serial.Serial
            self.serial = factory(port=None,baudrate=baudrate,bytesize=8,parity='N',stopbits=1,timeout=0,write_timeout=0)
            self.serial.dtr=False
            self.serial.rts=False
            self.serial.port=port
            self.serial.open()
        except Exception:
            self.close()
            raise

    @property
    def in_waiting(self): return self.serial.in_waiting
    def read(self,count): return self.serial.read(count)
    def write(self,payload): return self.serial.write(payload)
    def close(self):
        try:
            if self.serial is not None: self.serial.close()
        finally:
            release_endpoint(self.key,self.owner())


class MemoryTransport:
    """An isolated alternate encoder sink; cannot open a hardware endpoint."""
    def __init__(self): self.sent=[]; self.closed=False
    def write(self,payload):
        if self.closed: raise RuntimeError('Transport closed')
        self.sent.append(payload)
        return len(payload)
    @property
    def in_waiting(self): return 0
    def read(self,count): return b''
    def close(self): self.closed=True
