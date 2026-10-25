"""Private, atomic local connection discovery; bearer tokens are credentials."""
import csv
import ctypes
from ctypes import wintypes
import json
import os
from pathlib import Path
import socket
import subprocess
import tempfile
from urllib.parse import urlsplit

from .common import InterfaceError


def _private(path):
    if os.name == "nt":
        flags = subprocess.CREATE_NO_WINDOW
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        system_directory = ctypes.create_unicode_buffer(32768)
        get_system_directory = kernel.GetSystemDirectoryW
        get_system_directory.argtypes = [wintypes.LPWSTR, wintypes.UINT]
        get_system_directory.restype = wintypes.UINT
        if not get_system_directory(system_directory, len(system_directory)):
            raise ctypes.WinError(ctypes.get_last_error())
        # Avoid command lookup in a caller's potentially untrusted workspace.
        whoami = str(Path(system_directory.value) / "whoami.exe")
        identity = subprocess.check_output([whoami, "/user", "/fo", "csv", "/nh"],
                                           text=True, creationflags=flags)
        sid = next(csv.reader(identity.strip().splitlines()))[1]
        # Replace the DACL, rather than adding an ACE to any explicit defaults
        # inherited from a managed workspace/temp directory.
        advapi = ctypes.WinDLL("advapi32", use_last_error=True)
        convert = advapi.ConvertStringSecurityDescriptorToSecurityDescriptorW
        convert.argtypes = [wintypes.LPCWSTR, wintypes.DWORD,
                            ctypes.POINTER(ctypes.c_void_p), ctypes.POINTER(wintypes.DWORD)]
        convert.restype = wintypes.BOOL
        set_security = advapi.SetFileSecurityW
        set_security.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, ctypes.c_void_p]
        set_security.restype = wintypes.BOOL
        kernel.LocalFree.argtypes = [ctypes.c_void_p]
        kernel.LocalFree.restype = ctypes.c_void_p
        descriptor = ctypes.c_void_p()
        if not convert(f"D:P(A;;FA;;;{sid})", 1, ctypes.byref(descriptor), None):
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            if not set_security(str(path), 0x80000004, descriptor):
                raise ctypes.WinError(ctypes.get_last_error())
        finally:
            kernel.LocalFree(descriptor)
    else:
        path.chmod(0o600)


def write_connection(path, value):
    path = Path(path).expanduser().resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(temporary)
    try:
        # Restrict permissions before writing any credential bytes.
        _private(temporary)
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
            descriptor = None
            json.dump(value, stream, ensure_ascii=False)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if descriptor is not None:
            os.close(descriptor)
        temporary.unlink(missing_ok=True)
    return path


def read_connection(path):
    try:
        path = Path(path).expanduser().resolve()
        if path.stat().st_size > 65536:
            raise ValueError("oversized connection")
        data = json.loads(path.read_text(encoding="utf-8-sig"))
        parsed = urlsplit(data["url"])
        if (parsed.scheme != "http" or parsed.hostname != "127.0.0.1" or
                not parsed.port or parsed.username or parsed.password or
                parsed.path not in ("", "/") or parsed.query or parsed.fragment or
                not isinstance(data["token"], str) or len(data["token"]) < 32):
            raise ValueError("invalid loopback discovery")
        return data
    except (OSError, ValueError, TypeError, KeyError) as exc:
        raise InterfaceError("connection_error", "Cannot read a valid local connection file.") from exc


def _windows_has_listener(port):
    """Return bool, or None if Windows listener enumeration cannot be verified.

    Some Windows firewall configurations silently drop a local connection to a
    nonexistent port. A timeout then needs an OS listener check, not a guess.
    Scan IPv4 and IPv6 conservatively, including non-loopback bound addresses.
    """
    class Row4(ctypes.Structure):
        _fields_ = [(name, wintypes.DWORD) for name in
                    ("state", "local_address", "local_port", "remote_address", "remote_port", "pid")]

    class Row6(ctypes.Structure):
        _fields_ = [("local_address", ctypes.c_ubyte * 16), ("local_scope", wintypes.DWORD),
                    ("local_port", wintypes.DWORD), ("remote_address", ctypes.c_ubyte * 16),
                    ("remote_scope", wintypes.DWORD), ("remote_port", wintypes.DWORD),
                    ("state", wintypes.DWORD), ("pid", wintypes.DWORD)]

    try:
        function = ctypes.WinDLL("iphlpapi", use_last_error=True).GetExtendedTcpTable
        function.argtypes = [ctypes.c_void_p, ctypes.POINTER(wintypes.DWORD), wintypes.BOOL,
                             wintypes.ULONG, ctypes.c_int, wintypes.ULONG]
        function.restype = wintypes.DWORD
        for family, row_type in ((2, Row4), (23, Row6)):  # AF_INET, Windows AF_INET6
            size = wintypes.DWORD()
            result = function(None, ctypes.byref(size), False, family, 3, 0)  # OWNER_PID_LISTENER
            if result not in (0, 122):  # ERROR_INSUFFICIENT_BUFFER
                return None
            for _ in range(3):
                buffer = ctypes.create_string_buffer(max(4, size.value))
                result = function(buffer, ctypes.byref(size), False, family, 3, 0)
                if result != 122:
                    break
            if result:
                return None
            count = wintypes.DWORD.from_buffer(buffer).value
            if 4 + count * ctypes.sizeof(row_type) > len(buffer):
                return None
            rows = (row_type * count).from_buffer(buffer, 4)
            if any(socket.ntohs(row.local_port & 0xffff) == port for row in rows):
                return True
        return False
    except (OSError, AttributeError, ValueError):
        return None


class ConnectionClaim:
    """Hold a per-path OS lock; refuse to replace live or unknown discovery.

    The empty lock file deliberately remains after unlocking. Removing it would
    let a third process lock a new inode while another waits on the old inode.
    """
    def __init__(self, path):
        self.path = Path(path).expanduser().resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.file = None
        lock_path = self.path.with_name(self.path.name + ".lock")
        try:
            self.file = lock_path.open("a+b")
            _private(lock_path)
            self.file.seek(0, os.SEEK_END)
            if not self.file.tell():
                self.file.write(b"\0")
                self.file.flush()
            self.file.seek(0)
            try:
                if os.name == "nt":
                    import msvcrt
                    msvcrt.locking(self.file.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(self.file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as exc:
                raise InterfaceError("connection_in_use", "Connection file is owned by another running server; choose a different file.") from exc
            if self.path.exists():
                try:
                    existing = read_connection(self.path)
                except InterfaceError as exc:
                    raise InterfaceError("connection_exists", "Connection path already contains an invalid discovery file; choose a different file.") from exc
                port = urlsplit(existing["url"]).port
                try:
                    with socket.create_connection(("127.0.0.1", port), timeout=.25):
                        pass
                except ConnectionRefusedError:
                    pass  # No listener: a stale valid discovery can be replaced.
                except OSError as exc:
                    if os.name != "nt" or _windows_has_listener(port) is not False:
                        raise InterfaceError("connection_unavailable", "Cannot verify that the existing endpoint is stopped; choose a different connection file.") from exc
                else:
                    raise InterfaceError("connection_in_use", "Connection file points to a live local server; choose a different file.")
        except Exception:
            self.close()
            raise

    def close(self):
        if self.file is not None:
            self.file.close()  # Closing releases flock/Windows byte-range locks.
            self.file = None
