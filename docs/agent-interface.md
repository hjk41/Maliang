# Local application interfaces

The GUI, local JSON API, CLI and MCP adapter share `ApplicationService` and its
revision, geometry, preflight, immutable plans and device controller. The transport
does not reimplement layout or writing. `pagewriter.interfaces.schema.TOOLS` is the
canonical method/JSON Schema catalog (`maliang-agent tools` prints it).

## Start a local session

```powershell
pip install '.[agent]'
maliang-agent serve --workspace C:\work\maliang --connection C:\work\maliang\connection.json
```

Source checkouts can use `python agent_main.py` in place of `maliang-agent`.
The optional `agent` extra uses the official Python MCP SDK `mcp>=2.3,<3`.
HTTP transport and ordinary CLI clients use the standard library; the shared
application uses native Qt and jsonschema to validate the same published input schemas.
The server creates a native Qt application for fonts and layout, without showing
a window. Do not select Qt's offscreen platform on Windows.

Register a validated local device model configuration with repeatable flags:

```powershell
maliang-agent serve --workspace C:\work\maliang --device-profile model.json --device-profile second-model.json
```

The JSON format and adapter limits are described in [device adapters](device-adapters.md).
Loading only registers model data for `device.profiles`; the selected default
remains the disconnected simulator. It does not connect a port or initiate motion.

The readiness JSON contains the loopback URL and connection-file path. The file
contains the bearer token, PID and protocol version. It is atomically replaced,
restricted to the current owner (Windows DACL or POSIX mode 0600), and removed
when its owning server exits normally. Keep it out of source control and treat
it as a credential. The server holds a private sidecar `.lock` file to prevent
simultaneous starts using the same discovery path. That lock file remains after
shutdown and contains no token. A valid stale discovery file is replaced only
when its old loopback port refuses a connection, or Windows confirms that no
IPv4/IPv6 listener owns that port after a connection timeout. A live endpoint, uncertain
liveness, or invalid existing file causes a clear startup error; choose another
connection path. The default path is `WORKSPACE/.maliang/connection.json`.

## CLI and JSON API

```powershell
maliang-agent --connection C:\work\maliang\connection.json call system.info
maliang-agent --connection C:\work\maliang\connection.json call document.apply --file edits.json
maliang-agent --connection C:\work\maliang\connection.json preview --path preview.png
```

`call METHOD --params JSON` accepts an object; omitted parameters mean `{}`.
`--file PATH` reads UTF-8 JSON; `--file -` reads stdin. Convenience commands are
`tools`, `fonts`, `status`, and `preview`. Output is one JSON envelope and errors
return a nonzero status. `--connection` may precede or follow the subcommand.

```json
{"method":"document.apply","params":{"expected_revision":1,"operations":[{"op":"add_text","x_mm":20,"y_mm":20,"width_mm":80,"runs":[{"text":"Hello 马良","font_id":"maoken","size_pt":12}]}]}}
```

HTTP clients send that object to `POST /v1/call`, with
`Content-Type: application/json` and `Authorization: Bearer TOKEN`.
Responses are `{"ok":true,"result":...}` or
`{"ok":false,"error":{"code":"...","message":"...","details":{}}}`.
The server binds only `127.0.0.1`, selects a random port by default, rejects
foreign Host/Origin values, accepts at most a 1 MiB request body and 16 concurrent
connections, and bounds socket/dispatch waits. No CORS access is granted.
The client ignores proxy environment variables and refuses redirects or
non-loopback connection URLs. HTTP workers queue service operations onto the Qt
owner thread. A timed-out queued operation is cancelled; an operation already
executing may finish, so inspect state before retrying a mutation. Use the same
`request_id` to retry `job.start` safely.

Document edits use operation `op` values `add_text`, `update_text`, `remove`,
`add_zone`, `update_zone`, `page`, `import_photo`, `photo`, and `remove_photo`. Use font IDs from
`fonts.list`, millimetres for geometry, points for type size, and the revision
from `document.get`. Undo/redo also require `expected_revision`.
`document.export` supports SVG and `.pagewriter`. Preview rendering returns a PNG
path plus page/pixel mapping; an omitted path uses a private generated preview.
Explicit document/preview paths must remain in the allowed workspace. MCP also
includes image content for PNGs up to 8 MiB.

`fonts.list` returns compact font summaries with IDs, names, licenses and fallback
IDs. `device.status` includes `event_cursor`; `device.events` with `{"after":0}`
returns a bounded recent event list and the current cursor. Pass the last cursor
as `after` when polling again. Older events can age out of the bounded list.

The discovery file and its `.lock` sidecar, `.maliang` state directory and internal job database are
reserved from document writes, including custom discovery filenames. Save
projects as `.pagewriter` or `.json` and SVG exports as `.svg`; source files such
as `.py` cannot be overwritten through document save/export.
Application package/resources and source tooling are also protected from output
writes. Imported photo originals are read-only references. Unknown existing
output files are not replaced; use a new name, a previously generated output,
or open the project before saving it. Document new/open always invalidate the
prior context, even when page contents happen to match.

The desktop menu **More → Agent interface** enables this API on the current GUI
document and copies MCP configuration. The window and service share the same
controller. Document edits and new device/motion commands wait while an operator
dialog or IME composition is active; pause/cancel controls remain available.
Native operator-only confirmation binds the inspected document/session/position
to a correlated successful range authorization before granting Agent motion.

## MCP stdio

Start the HTTP session first. Configure an MCP host to run the installed console
entry point with these arguments (substitute your private connection path):

```json
{"command":"maliang-agent","args":["mcp","--connection","C:\\work\\maliang\\connection.json"]}
```

This adapter exposes the same schemas and forwards calls to the existing session;
it neither creates a second controller nor opens ports. The official SDK owns
the stdio protocol, initialization and EOF handling. Protocol messages alone go
to stdout; startup errors go to stderr. Installing the package does not edit any
agent's global configuration.

## Device and job boundaries

The default service permits the `simulator_servo` profile. Physical connections
require `serve --allow-hardware`, and physical motion additionally requires the
existing GUI/operator-session grant. No API or MCP tool creates that grant and
no `confirmed=true`, `preflight_passed`, arbitrary serial-write or authorization
bypass is exposed. A headless session is suitable for document work and simulated
jobs. `job.prepare` returns a plan bound to document/session/device capabilities;
`job.start` consumes that plan with an idempotency request ID. Changes can make a
prepared plan stale. Check service errors instead of assuming preparation is a
permanent motion permission.

Physical protocol/model compatibility beyond previously verified hardware remains
unverified. Simulator and transport tests do not establish a new machine's
electrical, coordinate, pen-lift or emergency-stop behavior.
