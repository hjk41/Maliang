"""JSON-first command line entry point for local service and MCP forwarding."""
import argparse
import json
from pathlib import Path
import signal
import sys

from .client import ApiClient
from .common import InterfaceError, failure, parse_json
from .schema import TOOLS


def _json(value):
    print(json.dumps(value, ensure_ascii=False, allow_nan=False), flush=True)


class _ArgumentParser(argparse.ArgumentParser):
    def error(self, message):
        raise InterfaceError("invalid_arguments", message)


def _parser():
    parser = _ArgumentParser(prog="maliang-agent", description="Maliang local application interfaces")
    parser.add_argument("--connection", metavar="PATH", help="Private local API discovery file")
    commands = parser.add_subparsers(dest="action", required=True)
    for name in ("call", "serve", "mcp", "tools", "fonts", "status", "preview"):
        command = commands.add_parser(name)
        command.add_argument("--connection", metavar="PATH", default=argparse.SUPPRESS)
        if name == "call":
            command.add_argument("method")
            group = command.add_mutually_exclusive_group()
            group.add_argument("--params", help="JSON object (default {})")
            group.add_argument("--file", help="UTF-8 JSON object file, or - for stdin")
        elif name == "serve":
            command.add_argument("--workspace", type=Path, default=Path.cwd())
            command.add_argument("--device-profile", type=Path, action="append", default=[], metavar="FILE",
                                 help="Load a validated device model JSON; repeat to register multiple models")
            command.add_argument("--allow-hardware", action="store_true",
                                 help="Permit connection to physical ports; motion still requires an operator grant")
            command.add_argument("--port", type=int, default=0)
        elif name == "preview":
            command.add_argument("--path")
            command.add_argument("--scale", type=float, default=4.0)
    return parser


def serve(args):
    from PySide6.QtCore import QTimer
    from PySide6.QtWidgets import QApplication
    from pagewriter.application import ApplicationService
    from pagewriter.devices import load_profile
    from .http import start_api
    for profile_path in args.device_profile:
        try:
            load_profile(profile_path.expanduser().resolve())
        except (OSError, ValueError, TypeError) as exc:
            raise InterfaceError("invalid_device_profile", "Cannot load a valid device profile.",
                                 {"path": str(profile_path), "reason": str(exc)}) from exc
    # Native platform fonts/layout need QApplication even without a visible window.
    app = QApplication.instance() or QApplication(["maliang-agent"])
    app.setApplicationName("Maliang Agent")
    app.setQuitOnLastWindowClosed(False)
    workspace = args.workspace.expanduser().resolve()
    workspace.mkdir(parents=True, exist_ok=True)
    connection = Path(args.connection) if args.connection else workspace / ".maliang" / "connection.json"
    service = ApplicationService(workspace=workspace, allow_hardware=args.allow_hardware)
    api = None
    timer = QTimer()
    timer.setInterval(20)
    timer.timeout.connect(service.poll)
    previous = {}
    try:
        api = start_api(service, connection_file=connection, port=args.port)
        for signum in (signal.SIGINT, signal.SIGTERM):
            previous[signum] = signal.signal(signum, lambda *_: app.quit())
        timer.start()
        _json({"ok": True, "result": {"ready": True, "url": api.url,
                                        "connection": str(api.connection_file)}})
        return app.exec()
    finally:
        timer.stop()
        if api is not None:
            api.close()
        service.close()
        for signum, handler in previous.items():
            signal.signal(signum, handler)


def main(argv=None):
    # Python's Windows console encoding can otherwise reject document Unicode.
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    args = None
    arguments = list(sys.argv[1:] if argv is None else argv)
    try:
        args = _parser().parse_args(arguments)
        if args.action == "serve":
            return serve(args)
        if args.action == "tools":
            _json({"ok": True, "result": [{"name": name, **spec} for name, spec in TOOLS.items()]})
            return 0
        if not args.connection:
            raise InterfaceError("connection_required", "Pass --connection PATH for this command.")
        if args.action == "mcp":
            from .mcp import run_mcp
            run_mcp(args.connection)
            return 0
        if args.action == "call":
            try:
                raw = (sys.stdin.read() if args.file == "-" else
                       Path(args.file).read_text(encoding="utf-8-sig")) if args.file else args.params or "{}"
                params = parse_json(raw)
                if not isinstance(params, dict):
                    raise ValueError("object required")
            except (OSError, ValueError, UnicodeError) as exc:
                raise InterfaceError("invalid_params", "Input must be a UTF-8 JSON object.") from exc
            method = args.method
        else:
            method = {"fonts": "fonts.list", "status": "system.info", "preview": "preview.render"}[args.action]
            params = ({"scale": args.scale, **({"path": args.path} if args.path else {})}
                      if args.action == "preview" else {})
        result = ApiClient(args.connection).call(method, params)
        _json(result)
        return 0 if result.get("ok") else 1
    except InterfaceError as exc:
        envelope = {"ok": False, "error": exc.as_dict()}
    except KeyboardInterrupt:
        return 130
    except Exception:
        envelope = failure("internal_error", "Agent interface failed.")
    # MCP stdout is reserved for protocol messages, including startup failures.
    if (args is not None and args.action == "mcp") or (args is None and "mcp" in arguments):
        print(json.dumps(envelope, ensure_ascii=False), file=sys.stderr, flush=True)
    else:
        _json(envelope)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
