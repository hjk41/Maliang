"""Transport integration, owner-thread dispatch, and real SDK stdio tests.

No windows are shown and no serial ports are opened.
"""
import asyncio
from concurrent.futures import ThreadPoolExecutor
import http.client
import importlib.util
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

if os.name == "nt":
    os.environ["QT_QPA_PLATFORM"] = "windows"
from PySide6.QtWidgets import QApplication

from pagewriter.interfaces import start_api
from pagewriter.interfaces.client import ApiClient
from pagewriter.interfaces.common import InterfaceError
from pagewriter.interfaces.discovery import read_connection, write_connection
from pagewriter.interfaces.http import MAX_BODY
from pagewriter.interfaces.schema import TOOLS

app = QApplication.instance() or QApplication([])
ROOT = Path(__file__).resolve().parents[1]


class FakeService:
    def __init__(self):
        self.owner = threading.get_ident()
        self.calls = []
        self.preview_path = None
        self.reserved_paths = set()

    def reserve_path(self, path):
        self.reserved_paths.add(Path(path).resolve())

    def dispatch(self, method, params):
        assert threading.get_ident() == self.owner, "service called on worker thread"
        self.calls.append((method, params))
        if method == "document.save":
            raise InterfaceError("path_forbidden", "Path is outside workspace.", {"field": "path"})
        if method == "explode":
            raise RuntimeError("private internal state must not be leaked")
        if method == "preview.render" and self.preview_path:
            return {"path": str(self.preview_path), "width_px": 8, "height_px": 8}
        return {"method": method, "params": params, "unicode": "马良"}


class AgentInterfaceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.connection = Path(self.temp.name) / "connection.json"
        self.service = FakeService()
        self.api = start_api(self.service, self.connection)
        self.pool = ThreadPoolExecutor(max_workers=3)

    def tearDown(self):
        self.api.close()
        self.pool.shutdown(wait=True)
        self.temp.cleanup()

    def pump(self, future, timeout=25):
        deadline = time.monotonic() + timeout
        while not future.done() and time.monotonic() < deadline:
            app.processEvents()
            time.sleep(.005)
        if not future.done():
            self.fail("worker did not finish")
        return future.result()

    def request(self, body=None, token=None, host=None, origin=None, path="/v1/call",
                content_type="application/json", length=None):
        def send():
            connection = http.client.HTTPConnection("127.0.0.1", self.api.server.server_port, timeout=5)
            raw = json.dumps(body or {"method": "system.info", "params": {}}).encode()
            headers = {"Authorization": "Bearer " + (self.api.token if token is None else token),
                       "Host": host or self.api.server.authority, "Content-Type": content_type}
            if origin is not None:
                headers["Origin"] = origin
            if length is not None:
                headers["Content-Length"] = str(length)
            connection.request("POST", path, raw, headers)
            response = connection.getresponse()
            value = response.status, json.loads(response.read())
            connection.close()
            return value
        return self.pump(self.pool.submit(send))

    def test_authenticated_dispatch_is_on_owner_thread(self):
        status, result = self.request({"method": "document.get", "params": {"sample": "中文"}})
        self.assertEqual(status, 200)
        self.assertTrue(result["ok"])
        self.assertEqual(result["result"]["params"]["sample"], "中文")
        self.assertEqual(len(self.service.calls), 1)

    def test_host_origin_token_and_route_guards(self):
        for kwargs, status, code in [
            ({"token": "wrong"}, 401, "unauthorized"),
            ({"host": "evil.example"}, 403, "forbidden_host"),
            ({"origin": "https://evil.example"}, 403, "forbidden_origin"),
            ({"origin": "null"}, 403, "forbidden_origin"),
            ({"path": "/unknown"}, 404, "not_found"),
            ({"content_type": "text/plain"}, 415, "invalid_content_type"),
            ({"length": MAX_BODY + 1}, 413, "request_too_large"),
        ]:
            with self.subTest(kwargs=kwargs):
                actual, result = self.request(**kwargs)
                self.assertEqual(actual, status)
                self.assertEqual(result["error"]["code"], code)
        self.assertEqual(self.service.calls, [])

    def test_envelope_and_service_errors_are_json(self):
        for body in ({"method": "system.info", "params": []}, {"method": 4},
                     {"method": "system.info", "extra": "bad"}):
            self.assertEqual(self.request(body)[0], 400)
        result = self.request({"method": "document.save"})[1]
        self.assertEqual(result["error"], {"code": "path_forbidden",
                         "message": "Path is outside workspace.", "details": {"field": "path"}})
        result = self.request({"method": "explode"})[1]
        self.assertEqual(result["error"]["code"], "internal_error")
        self.assertNotIn("private", json.dumps(result))

    def test_nonfinite_json_never_reaches_service(self):
        def send(value):
            connection = http.client.HTTPConnection("127.0.0.1", self.api.server.server_port, timeout=5)
            connection.request("POST", "/v1/call", '{"method":"system.info","params":{"value":' + value + '}}',
                {"Authorization": "Bearer " + self.api.token, "Content-Type": "application/json"})
            response = connection.getresponse()
            result = response.status, json.loads(response.read())
            connection.close()
            return result
        for value in ("NaN", "Infinity", "-Infinity", "1e9999"):
            status, result = self.pump(self.pool.submit(send, value))
            self.assertEqual(status, 400)
            self.assertEqual(result["error"]["code"], "invalid_request")
        self.assertEqual(self.service.calls, [])

    def test_wall_clock_deadline_closes_slow_header_sender(self):
        def send():
            with socket.create_connection(("127.0.0.1", self.api.server.server_port), timeout=2) as client:
                client.sendall(b"POST /v1/call HTTP/1.1\r\nX-Slow: ")
                start = time.monotonic()
                for _ in range(8):
                    time.sleep(.04)
                    try:
                        client.sendall(b"a")
                    except OSError:
                        break
                try:
                    received = client.recv(4096)
                except (ConnectionAbortedError, ConnectionResetError):
                    received = b""
                return time.monotonic() - start, received
        with patch("pagewriter.interfaces.http.REQUEST_TIMEOUT", .13):
            elapsed, received = self.pump(self.pool.submit(send))
        self.assertLess(elapsed, .6)
        self.assertEqual(received, b"")
        self.assertEqual(self.service.calls, [])

    def test_unserviced_queued_request_times_out_and_never_mutates(self):
        future = self.pool.submit(self.api.dispatcher.submit, "document.new", {}, .02)
        result = future.result(timeout=2)  # Deliberately do not pump Qt yet.
        self.assertEqual(result["error"]["code"], "request_timeout")
        app.processEvents()
        self.assertEqual(self.service.calls, [])

    def test_discovery_is_private_atomic_and_removed_by_owner(self):
        data = read_connection(self.connection)
        self.assertEqual(data["url"], self.api.url)
        self.assertEqual(data["token"], self.api.token)
        if os.name == "nt":
            acl = subprocess.check_output(["icacls", str(self.connection)], text=True,
                                          creationflags=subprocess.CREATE_NO_WINDOW)
            # The inherited mandatory integrity label is not an access ACE.
            access = "\n".join(line for line in acl.splitlines() if "Mandatory Label" not in line)
            self.assertNotIn("(I)", access)
            self.assertNotIn("BUILTIN\\Administrators", access)
            self.assertNotIn("NT AUTHORITY\\SYSTEM", access)
            self.assertNotIn("Everyone", acl)
        else:
            self.assertEqual(self.connection.stat().st_mode & 0o777, 0o600)
        self.api.close()
        self.assertFalse(self.connection.exists())

    def test_client_refuses_non_loopback_discovery(self):
        self.connection.write_text(json.dumps({"url": "http://evil.example:80", "token": "x" * 40}))
        with self.assertRaises(InterfaceError):
            ApiClient(self.connection).call("system.info")

    def test_discovery_collision_live_alias_stale_replacement_and_repeated_start(self):
        self.assertIn(self.connection.resolve(), self.service.reserved_paths)
        self.assertIn(self.connection.with_name(self.connection.name + ".lock").resolve(), self.service.reserved_paths)
        if os.name == "nt":
            from pagewriter.interfaces.discovery import _windows_has_listener
            self.assertIs(_windows_has_listener(self.api.server.server_port), True)
            if socket.has_ipv6:
                with socket.socket(socket.AF_INET6) as listener6:
                    listener6.bind(("::1", 0))
                    listener6.listen(1)
                    self.assertIs(_windows_has_listener(listener6.getsockname()[1]), True)
        original = self.connection.read_bytes()
        with self.assertRaises(InterfaceError) as caught:
            start_api(FakeService(), self.connection)
        self.assertEqual(caught.exception.code, "connection_in_use")
        self.assertEqual(self.connection.read_bytes(), original)
        # Different path, same live endpoint: liveness is TCP only, so it
        # cannot deadlock waiting for this owner thread's Qt dispatcher.
        alias = self.connection.with_name("alias.json")
        write_connection(alias, read_connection(self.connection))
        with self.assertRaises(InterfaceError) as caught:
            start_api(FakeService(), alias)
        self.assertEqual(caught.exception.code, "connection_in_use")
        self.assertEqual(read_connection(alias)["token"], self.api.token)
        # The OS lock also prevents another process replacing discovery.
        result = self.cli("serve", "--workspace", self.temp.name)
        self.assertEqual(result.returncode, 1)
        self.assertEqual(json.loads(result.stdout)["error"]["code"], "connection_in_use")
        self.assertEqual(self.connection.read_bytes(), original)
        stale = self.connection.with_name("stale.json")
        with socket.socket() as unlistened:
            unlistened.bind(("127.0.0.1", 0))
            write_connection(stale, {"url": f"http://127.0.0.1:{unlistened.getsockname()[1]}", "token": "x" * 40})
            replacement = start_api(FakeService(), stale)
            try:
                self.assertEqual(read_connection(stale)["token"], replacement.token)
            finally:
                replacement.close()
        self.api.close()
        restarted = start_api(FakeService(), self.connection)
        try:
            self.api.close()  # Idempotent old close cannot delete a new file.
            self.assertEqual(read_connection(self.connection)["token"], restarted.token)
        finally:
            restarted.close()

    def test_discovery_preserves_unknown_file_and_replacement_not_owned_by_closer(self):
        unknown = self.connection.with_name("user-document.json")
        unknown.write_text('{"user":"content"}', encoding="utf-8")
        with self.assertRaises(InterfaceError) as caught:
            start_api(FakeService(), unknown)
        self.assertEqual(caught.exception.code, "connection_exists")
        self.assertEqual(unknown.read_text(), '{"user":"content"}')
        value = read_connection(self.connection)
        value["token"] = "replacement-owner-token-" + "z" * 32
        write_connection(self.connection, value)
        self.api.close()
        self.assertEqual(read_connection(self.connection)["token"], value["token"])

    def cli(self, *args, input=None):
        def run():
            return subprocess.run([sys.executable, str(ROOT / "agent_main.py"),
                "--connection", str(self.connection), *args], input=input,
                capture_output=True, encoding="utf-8", timeout=20, cwd=ROOT)
        return self.pump(self.pool.submit(run))

    def test_cli_json_params_file_stdin_and_nonzero_error(self):
        result = self.cli("call", "document.get", "--params", '{"text":"马良"}')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["result"]["params"]["text"], "马良")
        result = self.cli("call", "document.get", "--file", "-", input='{"value":2}')
        self.assertEqual(json.loads(result.stdout)["result"]["params"], {"value": 2})
        source = Path(self.temp.name) / "params.json"
        source.write_text('{"value":3}', encoding="utf-8")
        result = self.cli("call", "document.get", "--file", str(source))
        self.assertEqual(json.loads(result.stdout)["result"]["params"], {"value": 3})
        result = self.cli("call", "document.save")
        self.assertEqual(result.returncode, 1)
        self.assertEqual(json.loads(result.stdout)["error"]["code"], "path_forbidden")
        result = self.cli("call", "system.info", "--params", "[]")
        self.assertEqual(result.returncode, 1)
        self.assertEqual(json.loads(result.stdout)["error"]["code"], "invalid_params")

    @unittest.skipUnless(importlib.util.find_spec("mcp"), "optional official MCP SDK not installed")
    def test_real_mcp_stdio_client_discovers_and_calls_shared_tools(self):
        from PySide6.QtGui import QColor, QImage
        self.service.preview_path = Path(self.temp.name) / "preview.png"
        image = QImage(8, 8, QImage.Format.Format_RGB32)
        image.fill(QColor("red"))
        self.assertTrue(image.save(str(self.service.preview_path)))

        async def client():
            from mcp import Client, StdioServerParameters
            params = StdioServerParameters(command=sys.executable,
                args=[str(ROOT / "agent_main.py"), "mcp", "--connection", str(self.connection)], cwd=ROOT)
            async with Client(params, read_timeout_seconds=10) as connection:
                listed = await connection.list_tools()
                self.assertEqual({tool.name for tool in listed.tools}, set(TOOLS))
                for tool in listed.tools:
                    self.assertEqual(tool.input_schema, TOOLS[tool.name]["inputSchema"])
                success = await connection.call_tool("system.info", {})
                self.assertFalse(success.is_error)
                self.assertTrue(success.structured_content["ok"])
                preview = await connection.call_tool("preview.render", {})
                self.assertFalse(preview.is_error)
                self.assertEqual([item.type for item in preview.content], ["text", "image"])
                self.assertEqual(preview.content[1].mime_type, "image/png")
                error = await connection.call_tool("document.save", {})
                self.assertTrue(error.is_error)
                self.assertEqual(error.structured_content["error"]["code"], "path_forbidden")
                invalid = await connection.call_tool("job.start", {})
                self.assertTrue(invalid.is_error)
        self.pump(self.pool.submit(asyncio.run, client()), timeout=35)
        self.assertEqual([call[0] for call in self.service.calls], ["system.info", "preview.render", "document.save"])

    @unittest.skipUnless(importlib.util.find_spec("mcp"), "optional official MCP SDK not installed")
    def test_mcp_eof_clean_exit_and_stdout_has_no_logs(self):
        result = self.cli("mcp", input="")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "")


@unittest.skipUnless(importlib.util.find_spec("pagewriter.application"), "application facade is not installed")
class AgentApplicationEndToEndTests(unittest.TestCase):
    """Own child session: native QApplication, real service and loopback API."""
    def test_repeatable_device_profile_flag_registers_without_connection(self):
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory).resolve()
            connection_file = workspace / "connection.json"
            flags = []
            expected = {"example_model_one", "example_model_two"}
            for identifier in sorted(expected):
                path = workspace / (identifier + ".json")
                path.write_text(json.dumps({"id": identifier, "name": "示例配置 " + identifier,
                    "driver": "grbl", "description": "Unverified test configuration",
                    "capabilities": {"axes": ["X", "Y", "Z"], "pen_actuation": "z_axis",
                                     "transport": "serial"}}), encoding="utf-8")
                flags.extend(["--device-profile", str(path)])
            process = subprocess.Popen([sys.executable, str(ROOT / "agent_main.py"), "serve",
                "--workspace", str(workspace), "--connection", str(connection_file), *flags],
                cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.PIPE, stdin=subprocess.PIPE,
                text=True, encoding="utf-8")
            with ThreadPoolExecutor(max_workers=1) as pool:
                try:
                    ready = json.loads(pool.submit(process.stdout.readline).result(timeout=20))
                    self.assertTrue(ready["ok"], ready)
                    client = ApiClient(connection_file)
                    profiles = client.call("device.profiles")
                    self.assertTrue(profiles["ok"], profiles)
                    self.assertTrue(expected <= {profile["id"] for profile in profiles["result"]["profiles"]})
                    status = client.call("device.status")["result"]
                    self.assertFalse(status["connection"])
                    self.assertEqual(status["device_profile"], "simulator_servo")
                    self.assertTrue(status["simulation"])
                finally:
                    if process.poll() is None:
                        process.terminate()
                    process.communicate(timeout=10)
            bad = workspace / "invalid.json"
            bad.write_text("{}", encoding="utf-8")
            result = subprocess.run([sys.executable, str(ROOT / "agent_main.py"), "serve",
                "--workspace", str(workspace), "--device-profile", str(bad)], cwd=ROOT,
                capture_output=True, text=True, encoding="utf-8", timeout=20)
            self.assertEqual(result.returncode, 1)
            self.assertEqual(json.loads(result.stdout)["error"]["code"], "invalid_device_profile")

    def test_fake_editor_modal_and_preedit_refuse_document_mutations(self):
        from pagewriter import core
        from pagewriter.application import ApplicationService
        from pagewriter.service_errors import ServiceError
        with tempfile.TemporaryDirectory() as directory:
            window = SimpleNamespace(project=core.blank_project(), canvas=SimpleNamespace(preedit_text="拼音"),
                                     device_panel=SimpleNamespace())
            service = ApplicationService(window=window, workspace=directory)
            try:
                revision = service.documents.revision
                with self.assertRaises(ServiceError) as caught:
                    service.dispatch("document.new", {})
                self.assertEqual(caught.exception.code, "GUI_BUSY")
                window.canvas.preedit_text = ""
                with patch.object(QApplication, "activeModalWidget", return_value=object()):
                    with self.assertRaises(ServiceError) as caught:
                        service.dispatch("document.new", {})
                    self.assertEqual(caught.exception.code, "GUI_BUSY")
                self.assertEqual(service.documents.revision, revision)
                self.assertEqual(service.dispatch("document.get", {})["revision"], revision)
            finally:
                service.close()
                service.controller.close()  # Minimal fake editor owns this controller.

    def test_serve_document_preview_export_and_mcp_share_one_session(self):
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory).resolve()
            connection_file = workspace / "connection.json"
            process = subprocess.Popen([sys.executable, str(ROOT / "agent_main.py"), "serve",
                "--workspace", str(workspace), "--connection", str(connection_file)],
                cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.PIPE, stdin=subprocess.PIPE,
                text=True, encoding="utf-8")
            with ThreadPoolExecutor(max_workers=1) as pool:
                try:
                    pending = pool.submit(process.stdout.readline)
                    ready = json.loads(pending.result(timeout=20))
                    self.assertTrue(ready["ok"], ready)
                    self.assertTrue(ready["result"]["ready"])
                    discovery = read_connection(connection_file)
                    self.assertNotIn(discovery["token"], json.dumps(ready))
                    client = ApiClient(connection_file)

                    def call(method, params=None):
                        result = client.call(method, params)
                        self.assertTrue(result.get("ok"), result)
                        return result["result"]

                    info = call("system.info")
                    protected_bytes = connection_file.read_bytes()
                    source = workspace / "source.py"
                    source.write_text("# original source\n", encoding="utf-8")
                    for method, params, expected in [
                        ("document.save", {"path": "connection.json"}, "RESERVED_PATH"),
                        ("document.save", {"path": "connection.json.lock"}, "RESERVED_PATH"),
                        ("document.export", {"path": "connection.json.lock", "format": "pagewriter"}, "RESERVED_PATH"),
                        ("document.export", {"path": "connection.json", "format": "pagewriter"}, "RESERVED_PATH"),
                        ("document.save", {"path": ".maliang/jobs.sqlite3"}, "RESERVED_PATH"),
                        ("document.export", {"path": ".maliang/jobs.sqlite3", "format": "svg"}, "RESERVED_PATH"),
                        ("document.save", {"path": "source.py"}, "INVALID_ARGUMENT"),
                        ("document.export", {"path": "source.py", "format": "svg"}, "INVALID_ARGUMENT"),
                    ]:
                        rejected = client.call(method, params)
                        self.assertFalse(rejected["ok"], (method, params, rejected))
                        self.assertEqual(rejected["error"]["code"], expected)
                    self.assertEqual(connection_file.read_bytes(), protected_bytes)
                    self.assertEqual(source.read_text(encoding="utf-8"), "# original source\n")
                    self.assertTrue((workspace / ".maliang/jobs.sqlite3").is_file())
                    initial_events = call("device.events", {"after": 0})
                    self.assertIn("cursor", initial_events)
                    self.assertIn("event_cursor", call("device.status"))
                    document = call("document.new", {"width_mm": 100, "height_mm": 100})
                    revision = document["revision"]
                    call("document.apply", {"expected_revision": revision, "operations": [
                        {"op": "add_text", "id": "e2e", "x_mm": 10, "y_mm": 10, "width_mm": 60,
                         "runs": [{"text": "中文 Hi", "font_id": "maoken", "size_pt": 12, "underline": True}]}]})
                    document = call("document.get")
                    self.assertEqual(document["blocks"][0]["text"], "中文 Hi")
                    self.assertGreater(document["revision"], revision)
                    conflict = client.call("document.apply", {"expected_revision": revision,
                        "operations": [{"op": "remove", "id": "e2e"}]})
                    self.assertFalse(conflict["ok"])
                    self.assertEqual(conflict["error"]["code"], "REVISION_CONFLICT")
                    preview = call("preview.render", {"path": "preview.png", "scale": 2})
                    self.assertTrue(Path(preview["path"]).is_file())
                    self.assertEqual((preview["width_px"], preview["height_px"]), (200, 200))
                    exported = call("document.export", {"path": "writing.svg", "format": "svg"})
                    self.assertTrue(Path(exported["path"]).is_file())
                    call("document.save", {"path": "writing.pagewriter"})
                    call("document.new", {"width_mm": 120, "height_mm": 130})
                    document = call("document.open", {"path": "writing.pagewriter"})
                    self.assertEqual(document["blocks"][0]["text"], "中文 Hi")
                    outside = client.call("preview.render", {"path": "../outside.png"})
                    self.assertFalse(outside["ok"])
                    self.assertEqual(outside["error"]["code"], "PATH_OUTSIDE_WORKSPACE")
                    if importlib.util.find_spec("mcp"):
                        async def shared():
                            from mcp import Client, StdioServerParameters
                            params = StdioServerParameters(command=sys.executable,
                                args=[str(ROOT / "agent_main.py"), "mcp", "--connection", str(connection_file)], cwd=ROOT)
                            async with Client(params, read_timeout_seconds=10) as mcp:
                                response = await mcp.call_tool("document.get", {})
                                self.assertFalse(response.is_error)
                                self.assertEqual(response.structured_content["result"]["revision"], document["revision"])
                                self.assertEqual(response.structured_content["result"]["blocks"][0]["text"], "中文 Hi")
                                response = await mcp.call_tool("preview.render", {"scale": 1})
                                self.assertFalse(response.is_error)
                                self.assertIn("image", [item.type for item in response.content])
                        asyncio.run(shared())

                    # Use a tiny real stroke-font layout to keep virtual wire
                    # execution short while exercising the whole job boundary.
                    call("document.apply", {"expected_revision": document["revision"], "operations": [
                        {"op": "update_text", "id": "e2e", "runs": [
                            {"text": "i", "font_id": "ems-readability", "size_pt": 12}]}]})
                    call("device.connect", {"profile_id": "simulator_servo"})
                    deadline = time.monotonic() + 10
                    state = call("device.status")
                    while not (state.get("authorized") and state.get("calibrated") and
                               state.get("state") == "ready" and not state.get("queued_commands")):
                        self.assertLess(time.monotonic(), deadline, state)
                        time.sleep(.02)
                        state = call("device.status")
                    self.assertEqual(state["capabilities"]["axes"], ["X", "Y"])
                    self.assertEqual(state["capabilities"]["pen_actuation"], "servo")
                    no_z = client.call("device.command", {"command": "jog", "params": {"axis": "Z", "distance": .1}})
                    self.assertFalse(no_z["ok"])
                    self.assertEqual(no_z["error"]["code"], "UNSUPPORTED_CAPABILITY")
                    plan = call("job.prepare")
                    self.assertTrue(plan["runnable"], plan)
                    started = call("job.start", {"plan_id": plan["plan_id"], "request_id": "e2e-once"})
                    duplicate = call("job.start", {"plan_id": plan["plan_id"], "request_id": "e2e-once"})
                    self.assertEqual(started["job_id"], duplicate["job_id"])
                    deadline = time.monotonic() + 20
                    terminal = {"complete", "failed", "cancelled", "unknown", "rejected"}
                    status = call("job.status", {"job_id": started["job_id"]})
                    while status["state"] not in terminal:
                        self.assertLess(time.monotonic(), deadline, status)
                        time.sleep(.02)
                        status = call("job.status", {"job_id": started["job_id"]})
                    self.assertEqual(status["state"], "complete", status)
                    self.assertTrue(status["simulation"])
                    self.assertEqual(status["completion_evidence"], "virtual_observation")
                    self.assertEqual(len(call("job.status")["jobs"]), 1)
                    events = call("device.events", {"after": initial_events["cursor"]})
                    self.assertGreater(events["cursor"], initial_events["cursor"])
                    self.assertLessEqual(len(events["events"]), 200)
                    self.assertTrue(all(event["cursor"] > initial_events["cursor"] for event in events["events"]))
                    self.assertEqual(call("device.events", {"after": events["cursor"]})["events"], [])
                finally:
                    # This is our own headless child, never an existing user app.
                    if process.poll() is None:
                        process.terminate()
                    process.communicate(timeout=10)


if __name__ == "__main__":
    unittest.main()
