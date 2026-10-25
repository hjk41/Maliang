# Device profiles and adapters

Maliang separates ordered pen/XY operations from their device encoding. The
Grbl adapter encodes pen transitions through a calibrated Z axis. The offline
simulator uses XY plus explicit servo commands and has no Z coordinate. A
protocol adapter does not establish physical compatibility or calibration.

Built-in profile IDs are `songboshi_2024`, `grbl_generic` and
`simulator_servo`. The Songboshi profile contains the previously observed axis
presets and firmware speed caps. Its working dimensions remain unknown. The
generic Grbl profile is an **unverified configuration**: its maximum speeds,
page size, axis orientation and pen heights are unknown.

## Config-only models using the existing Grbl adapter

Save this example as `my-grbl.json`. The baud rate is a connection setting to
verify against the device manual; it is not evidence of a working device. The
example deliberately supplies no invented speed limits or measured dimensions.

```json
{
  "id": "my_grbl_unverified",
  "name": "My Grbl model (unverified)",
  "driver": "grbl",
  "description": "Unverified local configuration; calibrate and authorize each session.",
  "support_status": "unverified_configuration",
  "capabilities": {
    "axes": ["X", "Y", "Z"],
    "pen_actuation": "z_axis",
    "transport": "serial",
    "simulation": false,
    "pause_resume": true,
    "bounded_jog": true,
    "physical_completion": false,
    "requires_runtime_calibration": true,
    "manual_reposition_feedback": false,
    "cancellation_semantics": "hold_and_lock_device_queue_not_cleared",
    "completion_detection": "fresh_controller_idle"
  },
  "serial_baud": 115200,
  "axis_presets": {},
  "max_write_feed": null,
  "max_travel_feed": null,
  "max_z_feed": null,
  "max_page_size": null
}
```

`load_profile(path)` reads one UTF-8 JSON object, validates it and registers it
for the current process. It returns an immutable `DeviceProfile` object:

```python
from pagewriter.devices import load_profile, register_profile, create_controller

profile = load_profile("my-grbl.json")
assert profile.id == "my_grbl_unverified"
metadata = profile.to_dict()  # JSON-compatible dictionary
controller = create_controller(profile.id)  # Does not connect or move.
```

`register_profile(dictionary)` has the same validation and return type without
reading a file. Registration never opens serial, loads executable code, restores
calibration, or grants motion. A registered ID cannot overwrite an existing ID;
restart the process to load changed model data under that ID. Startup entry
points may load a profile file explicitly; registration is otherwise process
local and no files are auto-discovered.

## Accepted JSON schema

Unknown top-level and capability fields are rejected. Files are limited to
65,536 bytes. Numbers must be finite; JSON cannot contain NaN or Infinity.

| Field | Required | Accepted value |
| --- | --- | --- |
| `id` | yes | String matching `[a-z][a-z0-9_-]{0,63}`; unique in the process |
| `name` | yes | Nonblank string, at most 120 characters |
| `driver` | yes | Exactly `"grbl"` for config-only custom models |
| `description` | yes | String, at most 2,000 characters |
| `support_status` | no | Exactly `"unverified_configuration"`; this is also the default |
| `capabilities` | yes | Object described below |
| `serial_baud` | no | Integer from 1,200 through 4,000,000; omitted/null defaults to 115200 |
| `axis_presets` | no | Object with at most 20 named four-number linear mappings; omitted defaults to `{}` |
| `max_write_feed` | no | Positive number at most 1,000,000 mm/min, or null |
| `max_travel_feed` | no | Positive number at most 1,000,000 mm/min, or null |
| `max_z_feed` | no | Positive number at most 1,000,000 mm/min, or null |
| `max_page_size` | no | `[width_mm, height_mm]`, each positive and at most 2,000, or null |

`capabilities` requires `axes: ["X","Y","Z"]`,
`pen_actuation: "z_axis"`, and `transport: "serial"`. Optional flags must be
JSON booleans: `simulation` defaults to false and must remain false;
`physical_completion` defaults to false and must remain false;
`requires_runtime_calibration` defaults to true and must remain true;
`pause_resume` and `bounded_jog` default to true. Config-only data cannot claim
measured physical completion or disable runtime calibration.

`manual_reposition_feedback` defaults to false and must remain false: controller
coordinates do not measure a hand-pushed pen carriage. Custom Grbl profiles may
omit `cancellation_semantics` and `completion_detection`; if supplied they must
be exactly `hold_and_lock_device_queue_not_cleared` and `fresh_controller_idle`.
These are also their defaults. The simulator instead advertises
`clear_virtual_queue` and `virtual_observation`, while still reporting no
physical manual-reposition feedback. Cancellation metadata must never be read
as a guaranteed physical stop.

Each optional axis preset maps a nonempty name of at most 100 characters to
`[a,b,c,d]`, where `machine_x = a*page_x + c*page_y + tx` and
`machine_y = b*page_x + d*page_y + ty`. The two column lengths must each be
between 0.2 and 5, and `abs(a*d-b*c)` must be at least 0.04. Translation is
captured from the live session. An empty preset object requires normal
three-point calibration; copying another model's axes does not verify them.

Optional speed/page limits are model configuration supplied by the operator.
An omitted/null limit remains **unknown**, and never inherits Songboshi limits.
Actual runtime mapping, pen heights, work/Z bounds and inspected clearance still
require fresh device evidence and operator authorization. Known configured
speed caps constrain runtime authorization. A different protocol or a different
pen actuator requires another adapter implementation rather than extra JSON.

## Adding a protocol adapter in trusted Python source

The controller factory selects `DeviceProfile.driver` through the built-in
`_DRIVER_FACTORIES` registry in `pagewriter/devices/__init__.py`. Its current
keys are `grbl` and `servo_simulator`. The `simulation` capability describes a
device; it does not select its implementation. There are no dynamic imports
from profile files and no plugin loading from JSON.

To add an adapter:

1. Implement the `DeviceController` contract in a new trusted source module.
   Give that adapter its own transport, ordered IR encoder and completion
   evidence. Construction/import must not open hardware. Preserve exclusive
   endpoint ownership, runtime calibration/authorization, worker motion lease
   checks, correlated start/rejection/completion events and no automatic replay.
2. Add one lazy constructor function and one entry under a distinct driver ID
   in `_DRIVER_FACTORIES`. The lazy constructor keeps optional dependencies
   from loading before the adapter is selected.
3. Add a built-in `DeviceProfile` with that driver ID and accurate capabilities;
   use unknown limits and an unverified status where measurements are absent.
4. Test factory selection and encoding/completion with memory transports before
   any separately authorized physical validation. Keep legacy imports intact.
5. Only if config-only models are needed, extend `register_profile` with a
   driver-specific validated data schema and document it. Until that schema is
   implemented, custom JSON remains restricted to the existing Grbl/Z adapter.

## Grants, completion and interrupted jobs

The controller publishes `authorization_id` only after the corresponding
candidate profile validates. Queuing a replacement invalidates the prior
authorization immediately; a rejected candidate leaves no active profile.
Agent motion authority is an expiring runtime lease bound to the successful
authorization, connection, geometry and mapping. It is never restored from disk.
Stale coordinates during an existing job affect readiness for a new action,
without erasing an unchanged scope grant. Pending starts/resumes recheck the
lease on the worker; revocation and expiry cannot silently dispatch them later.

Acknowledgement and completion remain separate. Grbl completion requires a
fresh final Idle observation after the final acknowledgement; the simulator
reports only a virtual completion. Cancellation never claims a measured
physical stop.

Jobs left nonterminal by another service runtime are shown as unknown and never
replayed. Explicit internal operator reconciliation records
`interrupted_resolved`, not completion, with an inspection note. A physical
historical job requires a fresh, calibrated, authorized real controller using
the same profile and a current operator grant. Switching to the simulator cannot
resolve physical uncertainty. If that device is unavailable, keep the record
unresolved until it can be inspected.
