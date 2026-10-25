"""Authenticated loopback HTTP with queued execution on the Qt owner thread."""
from dataclasses import dataclass, field
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import secrets
import socket
import threading
import time

from PySide6.QtCore import QCoreApplication, QObject, QThread, Qt, Signal, Slot

from .common import dispatch_envelope, failure, parse_json
from .discovery import ConnectionClaim, write_connection

MAX_BODY = 1024 * 1024
REQUEST_TIMEOUT = 30.0


@dataclass
class _Call:
    method: str
    params: dict
    done: threading.Event = field(default_factory=threading.Event)
    lock: threading.Lock = field(default_factory=threading.Lock)
    cancelled: bool = False
    result: object = None


class MainThreadDispatcher(QObject):
    """Create on the service's Qt thread; workers submit and wait boundedly."""
    queued = Signal(object)

    def __init__(self, service):
        super().__init__()
        app = QCoreApplication.instance()
        if app is None or QThread.currentThread() != app.thread():
            raise RuntimeError("start_api must run on the Qt application thread")
        self.service = service
        self.closed = False
        self.queued.connect(self._execute, Qt.ConnectionType.QueuedConnection)

    @Slot(object)
    def _execute(self, call):
        with call.lock:
            if call.cancelled or self.closed:
                call.result = failure("service_closed", "Application service is closed.")
                call.done.set()
                return
            call.result = dispatch_envelope(self.service, call.method, call.params)
            call.done.set()

    def submit(self, method, params, timeout=REQUEST_TIMEOUT):
        if self.closed:
            return failure("service_closed", "Application service is closed.")
        if QThread.currentThread() == self.thread():
            return dispatch_envelope(self.service, method, params)
        call = _Call(method, params)
        self.queued.emit(call)
        if not call.done.wait(timeout):
            # Avoid blocking on an executing operation's lock. A queued call is
            # cancelled; an already executing operation may still complete.
            if call.lock.acquire(blocking=False):
                try:
                    call.cancelled = True
                finally:
                    call.lock.release()
            return failure("request_timeout", "Application call timed out; inspect state before retrying.")
        return call.result


class _Server(ThreadingHTTPServer):
    daemon_threads = True
    block_on_close = False
    allow_reuse_address = False

    def __init__(self, address, dispatcher, token):
        self.dispatcher, self.token = dispatcher, token
        self.slots = threading.BoundedSemaphore(16)
        super().__init__(address, _Handler)
        self.authority = f"127.0.0.1:{self.server_address[1]}"

    def process_request(self, request, client_address):
        if not self.slots.acquire(blocking=False):
            body = json.dumps(failure("server_busy", "Local API connection limit reached.")).encode()
            try:
                request.settimeout(.1)
                request.sendall(b"HTTP/1.1 503 Service Unavailable\r\nContent-Type: application/json\r\n"
                                b"Connection: close\r\nContent-Length: " + str(len(body)).encode() + b"\r\n\r\n" + body)
            except OSError:
                pass
            request.close()
            return
        try:
            super().process_request(request, client_address)
        except Exception:
            self.slots.release()
            raise

    def process_request_thread(self, request, address):
        try:
            super().process_request_thread(request, address)
        finally:
            self.slots.release()

    def handle_error(self, request, client_address):
        pass  # No request bodies, credentials or traceback on a GUI console.


class _Handler(BaseHTTPRequestHandler):
    server_version = "MaliangLocal/1"
    sys_version = ""

    def setup(self):
        super().setup()
        self.connection.settimeout(REQUEST_TIMEOUT)
        self.deadline = time.monotonic() + REQUEST_TIMEOUT
        # A socket idle timeout alone permits a slow sender to hold a worker
        # forever. Enforce a wall-clock deadline across headers, body and call.
        self.deadline_timer = threading.Timer(REQUEST_TIMEOUT, self._expire)
        self.deadline_timer.daemon = True
        self.deadline_timer.start()

    def _expire(self):
        try:
            self.connection.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass

    def finish(self):
        self.deadline_timer.cancel()
        super().finish()

    def log_message(self, *args):
        pass

    def send_error(self, code, message=None, explain=None):
        self._reply(code, failure("invalid_request", "Invalid HTTP request."))

    def _reply(self, status, body):
        try:
            raw = json.dumps(body, ensure_ascii=False, allow_nan=False).encode("utf-8")
        except (TypeError, ValueError, UnicodeError, RecursionError, OverflowError):
            status, raw = 500, json.dumps(failure("serialization_error", "Invalid service response.")).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(raw)

    def do_POST(self):
        if self.headers.get_all("Host") != [self.server.authority]:
            return self._reply(403, failure("forbidden_host", "Host must match this loopback endpoint."))
        origins = self.headers.get_all("Origin") or []
        if origins and origins != [f"http://{self.server.authority}"]:
            return self._reply(403, failure("forbidden_origin", "Origin is not allowed."))
        expected = f"Bearer {self.server.token}"
        auth = self.headers.get_all("Authorization") or []
        if len(auth) != 1 or not secrets.compare_digest(auth[0].encode("utf-8"), expected.encode("ascii")):
            return self._reply(401, failure("unauthorized", "A valid bearer token is required."))
        if self.path != "/v1/call":
            return self._reply(404, failure("not_found", "Unknown endpoint."))
        if self.headers.get("Transfer-Encoding") or len(self.headers.get_all("Content-Length") or []) != 1:
            return self._reply(400, failure("invalid_request", "A single Content-Length is required."))
        try:
            length = int(self.headers["Content-Length"])
            if not 0 < length <= MAX_BODY:
                return self._reply(413, failure("request_too_large", "Request body exceeds the size limit."))
            if self.headers.get_content_type() != "application/json":
                return self._reply(415, failure("invalid_content_type", "Use application/json."))
            raw = self.rfile.read(length)
            if len(raw) != length:
                raise ValueError("short body")
            body = parse_json(raw)
            if (not isinstance(body, dict) or set(body) - {"method", "params"} or
                    not isinstance(body.get("method"), str) or not body["method"] or
                    not isinstance(body.get("params", {}), dict)):
                raise ValueError("invalid envelope")
        except (ValueError, UnicodeError, RecursionError):
            return self._reply(400, failure("invalid_request", "Expected a JSON object with method and object params."))
        except (TimeoutError, OSError):
            return self._reply(408, failure("request_timeout", "Request body timed out."))
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            return self._reply(408, failure("request_timeout", "Request deadline expired."))
        result = self.server.dispatcher.submit(body["method"], body.get("params", {}), timeout=remaining)
        return self._reply(200, result)

    def do_GET(self):
        self._reply(405, failure("method_not_allowed", "Use POST /v1/call."))

    do_OPTIONS = do_GET
    do_PUT = do_GET
    do_DELETE = do_GET
    do_PATCH = do_GET


class ApiHandle:
    def __init__(self, service, connection_file=None, port=0):
        if not isinstance(port, int) or not 0 <= port <= 65535:
            raise ValueError("port must be between 0 and 65535")
        self.dispatcher = MainThreadDispatcher(service)
        self.token = secrets.token_urlsafe(32)
        self.server = _Server(("127.0.0.1", port), self.dispatcher, self.token)
        self.url = f"http://{self.server.authority}"
        self.connection_file = None
        self._connection_claim = None
        self._closed = False
        self._thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": .1},
                                        name="maliang-local-api", daemon=True)
        try:
            if connection_file:
                connection_path = Path(connection_file).expanduser().resolve()
                reserve = getattr(service, "reserve_path", None)
                if reserve is not None:
                    reserve(connection_path)
                    reserve(connection_path.with_name(connection_path.name + ".lock"))
                self._connection_claim = ConnectionClaim(connection_path)
                self.connection_file = write_connection(connection_path, {
                    "url": self.url, "token": self.token, "pid": os.getpid(), "protocol": "maliang-local-v1",
                    "created_at": datetime.now(timezone.utc).isoformat()})
            self._thread.start()
        except Exception:
            self.server.server_close()
            if self._connection_claim is not None:
                self._connection_claim.close()
            raise

    def close(self):
        if self._closed:
            return
        self._closed = True
        self.dispatcher.closed = True
        self.server.shutdown()
        self.server.server_close()
        self._thread.join(timeout=2)
        if self.connection_file:
            try:
                data = json.loads(self.connection_file.read_text(encoding="utf-8"))
                if data.get("token") == self.token:
                    self.connection_file.unlink(missing_ok=True)
            except (OSError, ValueError):
                pass
        if self._connection_claim is not None:
            self._connection_claim.close()


def start_api(service, connection_file=None, port=0):
    """Start on the Qt main thread. Handle.close() stops only this API."""
    return ApiHandle(service, connection_file=connection_file, port=port)
