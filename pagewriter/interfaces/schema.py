"""Canonical tool discovery shared by local API, CLI and MCP."""


def obj(properties=None, required=()):
    return {"type": "object", "properties": properties or {},
            "required": list(required), "additionalProperties": False}


STR = {"type": "string"}
NUM = {"type": "number"}
INT = {"type": "integer"}
BOOL = {"type": "boolean"}
POINT = {"type": "array", "items": NUM, "minItems": 2, "maxItems": 2}
QUAD = {"type": "array", "items": POINT, "minItems": 4, "maxItems": 4}
OUTPUT_SCHEMA = {"type": "object", "required": ["ok"], "additionalProperties": False,
    "properties": {"ok": BOOL, "result": {}, "error": {"type": "object", "required": ["code", "message"],
        "properties": {"code": STR, "message": STR, "details": {}}}},
    "oneOf": [{"properties": {"ok": {"const": True}}, "required": ["result"]},
              {"properties": {"ok": {"const": False}}, "required": ["error"]}]}
RUN = obj({"text": STR, "font_id": STR, "size_pt": NUM, "underline": BOOL},
          ("text", "font_id", "size_pt"))
TEXT = {"id": STR, "name": STR, "x_mm": NUM, "y_mm": NUM, "width_mm": NUM,
        "runs": {"type": "array", "items": RUN}, "columns": INT,
        "column_height_mm": NUM, "column_gap_mm": NUM, "column_separators": BOOL,
        "rotation_degrees": NUM, "avoid_zones": BOOL}
ZONE = {"id": STR, "name": STR, "kind": {"enum": ["ink", "obstacle"]},
        "points_mm": {"type": "array", "items": POINT, "minItems": 3}}
PHOTO = {"quad": {"anyOf": [QUAD, {"type": "null"}]},
         "edges": {"anyOf": [obj({side: {"type": "array", "items": POINT, "maxItems": 5}
                       for side in ("top", "right", "bottom", "left")}), {"type": "null"}]},
         "guides": {"anyOf": [{"type": "array", "maxItems": 6, "items": {
             "type": "array", "items": POINT, "minItems": 2, "maxItems": 6}}, {"type": "null"}]},
         "opacity": NUM, "offset_mm": POINT,
         "rotation_degrees": {"type": "integer", "multipleOf": 90}}


def operation(kind, properties, required=()):
    return obj({"op": {"const": kind}, **properties}, ("op", *required))


OPERATIONS = {"type": "array", "minItems": 1, "maxItems": 1000, "items": {"oneOf": [
    operation("add_text", TEXT, ("x_mm", "y_mm", "width_mm", "runs")),
    operation("update_text", TEXT, ("id",)),
    operation("remove", {"id": STR}, ("id",)),
    operation("add_zone", ZONE, ("kind", "points_mm")),
    operation("update_zone", ZONE, ("id",)),
    operation("page", {"width_mm": NUM, "height_mm": NUM,
                       "pen_width_mm": NUM, "margin_mm": NUM}),
    operation("import_photo", {"path": STR, **PHOTO}, ("path",)),
    operation("photo", PHOTO),
    operation("remove_photo", {}),
]}}


def spec(description, properties=None, required=(), read_only=False):
    return {"description": description, "inputSchema": obj(properties, required),
            "readOnlyHint": read_only}


TOOLS = {
    "system.info": spec("Application version, session and interface capabilities.", read_only=True),
    "document.get": spec("Current structured document and revision.", read_only=True),
    "document.new": spec("Create a blank document.", {"width_mm": NUM, "height_mm": NUM, "expected_revision": INT}),
    "document.open": spec("Open a document inside the allowed workspace.", {"path": STR, "expected_revision": INT}, ("path",)),
    "document.save": spec("Save the current document inside the workspace.", {"path": STR}),
    "document.apply": spec("Atomically apply structured document operations at the expected revision.",
                           {"operations": OPERATIONS, "expected_revision": INT},
                           ("operations", "expected_revision")),
    "document.undo": spec("Undo a document edit.", {"expected_revision": INT}, ("expected_revision",)),
    "document.redo": spec("Redo a document edit.", {"expected_revision": INT}, ("expected_revision",)),
    "document.export": spec("Export SVG or a document file inside the workspace.",
                            {"path": STR, "format": {"enum": ["svg", "pagewriter"]}}, ("path", "format")),
    "fonts.list": spec("List stroke font IDs and capabilities.", read_only=True),
    "preview.render": spec("Render the document to a workspace PNG with millimetre mapping.",
                           {"path": STR, "scale": NUM, "show_zones": BOOL}),
    "document.preflight": spec("Check writing geometry and active device constraints.", read_only=True),
    "device.profiles": spec("List protocol profiles and verification limits.", read_only=True),
    "device.list": spec("Enumerate available device ports without opening them.", read_only=True),
    "device.connect": spec("Connect a simulator or a permitted device. Does not grant physical motion.",
                           {"profile_id": STR, "port": STR}),
    "device.disconnect": spec("Disconnect the active device."),
    "device.status": spec("Current connection and device state.", read_only=True),
    "device.events": spec("Read the bounded device event stream after a cursor.",
                          {"after": {"type": "integer", "minimum": 0}}, read_only=True),
    "device.command": spec("Submit an allowlisted device command under existing operator grants.",
                           {"command": STR, "params": {"type": "object"}}, ("command",)),
    "job.prepare": spec("Create an immutable preflighted job plan tied to this session and document.",
                        {"expected_revision": INT}),
    "job.start": spec("Start an existing plan idempotently; physical motion needs an operator grant.",
                      {"plan_id": STR, "request_id": STR}, ("plan_id", "request_id")),
    "job.status": spec("Read job progress.", {"job_id": STR}, read_only=True),
    "job.pause": spec("Pause an active job.", {"job_id": STR}),
    "job.resume": spec("Resume a paused job under current grants.", {"job_id": STR}),
    "job.cancel": spec("Cancel an active job.", {"job_id": STR}),
}
