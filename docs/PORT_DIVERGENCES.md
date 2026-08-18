# Deliberate divergences from the ported sources

Protocol and transport code is copied verbatim from the hardware-verified projects and kept
byte-identical so it can be diffed and re-synced (see the `extend-exclude` list in
`pyproject.toml`). Anything changed here is listed, with the reason, so it can be ported back
upstream rather than silently forking.

---

## `sony_headsets/transport/bluez.py` — `active_codec()` is now profile-aware

**Changed 2026-08-06. This is a bug present in the upstream project too and worth porting back.**

`active_codec()` iterated every `org.bluez.MediaTransport1` object belonging to the device and
named the codec without checking which profile the transport belonged to. Codec ids are only
meaningful within a profile:

| id | A2DP | HFP |
|---:|---|---|
| 0 | SBC | — |
| 1 | — | CVSD |
| 2 | **AAC** | **mSBC** |

So a headset connected in handsfree mode reported its codec as **"AAC"**, which is wrong and
actively misleading — it looks like a working A2DP link.

This is not hypothetical: a WH-1000XM4 coming back from a reboot frequently negotiates HFP first
and only lands on A2DP a moment later, which is exactly when the Info tab is read.

**Now:** transports are ranked, preferring A2DP over HFP and an active stream over an idle
endpoint. A handsfree link is reported honestly as e.g. `mSBC (handsfree)` rather than being
named as though it were A2DP.

Note this is a *labelling* fix only — it does not change which profile BlueZ negotiates, and the
"prioritize stable connection" MDR setting has no bearing on it either. That setting disables
LDAC (by design); it does not select HFP.

---

## Periodic poll uses `sync()` — matching the reference implementation

**No longer a divergence. Reverted 2026-08-06.**

An earlier version polled with the BlueZ-only `refresh_status()` on the strength of its docstring
warning about MDR traffic. That was wrong once the notification listener was removed (below):
with no listener, the poll is the *only* path by which a change made on the headset itself, or
from Sony's phone app, reaches the UI. `sync()` is what `workers.py` polls with and is the
combination proven on hardware.

The original inconsistency stands and is worth knowing:

The reference implementation's 30 s poll (`_DeviceWorker._poll`, `workers.py:81`) calls
`self._hp.sync()` — a full MDR read of every feature. But `refresh_status()` exists in
`device.py` specifically for polling, and its docstring warns against exactly that:

> *Cheap refresh of battery + codec via BlueZ only — NO MDR traffic. Used for periodic polling:
> on LDAC the SPP config channel has almost no spare bandwidth, so sending a burst of MDR GETs
> flaps the codec/link. The device pushes MDR state changes as NTFYs instead.*

So the code and its own documented intent disagree: the poll does the thing the helper was
written to avoid.

If LDAC ever drops on a 30 s cadence in either project, this is the first thing to look at.

## No notification listener

`Headphones.listen()` exists but the reference GUI never calls it — it appears only in a
docstring example. `hardware-ui` briefly ran it on a background thread to pick up NTFY pushes.
**That was a fabrication and an active bug**, removed 2026-08-06.

A second thread reading frames from the same RFCOMM session races every write: it consumes the
ACK `send_command` is blocking on, and `_apply` mutates `state` while a composite write is reading
it. Symptoms were speak-to-chat switching itself off while its sensitivity was being changed, and
intermittent "device did not confirm the change" for writes that had in fact landed.

One thread owns the session, as in the reference implementation.

---

## Confirmed on hardware, 2026-08-06

- The reboot confirmation and the 15 s auto-reconnect work end to end on a WH-1000XM4.
- **LDAC only re-engages after a full headset power cycle**, not merely after the MDR write.
  Confirmed independent of this app: with the headset in that state, LDAC cannot be selected in
  Plasma's own sound settings either. The headset re-advertises its A2DP capabilities on power-up,
  so BlueZ has nothing to negotiate until then. Both directions of Sound Quality are marked
  `reboots=True` and the 15 s reconnect covers the MDR side; the codec simply will not change
  until the device itself has come back. **Not a bug to chase.**
- A headset returning from a reboot can land on HFP before A2DP, which is what the profile-aware
  codec fix above addresses.

## Connect must not call `sync()` — `handshake()` already does

`Headphones.handshake()` ends with `self.sync()`; its docstring says so: *"Run the confirmed
CONNECT sequence and an initial state sync."* An earlier version of `SonyDevice.connect()` called
`sync()` again afterwards, repeating all 11 state GETs and roughly doubling the time to open a
device (~22 round-trips instead of ~11). The reference implementation's worker calls
`handshake()` only.

## `handshake()` accepts an optional discovery cache

**Divergence, added 2026-08-06. Opt-in: with no cache passed the sequence is byte-identical.**

Opening a device is ~27 protocol round-trips, and about 14 of them re-ask questions whose answers
cannot have changed: the identity fields (model id, serial, device id, version), the advertised
function list, the APO options and the GENERAL_SETTING slot titles.

`Headphones.handshake(cached=...)` skips those when the freshly-read function list matches the
cached one. That list is the fingerprint — it is what a firmware update changes, and everything
skipped is either derived from it or fixed for the life of the device. On mismatch, or with no
cache, the full discovery runs and the cache is rewritten.

Never skipped:

- **The three arming commands.** `ALERT_SET_STATUS` is what registers us for the confirm dialog on
  reboot-inducing settings; without it those changes are silently dropped.
- **`sync()`.** Those are live values.

**No setting is ever cached.** Everything the user can change from Sony's phone app is re-read on
every connect, which is the whole reason values were excluded from caching in the first place. A
test asserts the cache blob contains no setting keys.

Stored under `$XDG_CACHE_HOME/hardware-ui/sony_headsets/<address>.json` — cache, not data:
deleting it costs one slower connect and nothing else.

---

# `dell_monitors`

`protocol/features.py` and `protocol/ddcutil.py` are byte-identical to
`plasma-dell-monitor-support/plasma_dell_monitor/{features,ddcutil_backend}.py`. Everything below
is in the adapter or is a deliberate change, listed so it can be diffed.

## `protocol/calibration.py` — persistence moved out

`probe_range` and `Range` are unchanged. The origin's `load`/`save` wrote to the app's own config
file; here they live in `device.py` and write to
`$XDG_CACHE_HOME/hardware-ui/dell_monitors/calibration.json`. Cache, not config: deleting it costs
one re-run of the probe. The same applies to `input_names.json`.

## `detect_monitors` is not how displays are enumerated

The origin calls `ddcutil detect` at startup. Here `discovery.enumerate_displays()` reads
`/sys/class/drm/*/edid`, which opens nothing and costs microseconds — the project's standing rule.
`detect_monitors` is still used, but only to resolve a display's **I²C bus** when the user opens
it, because no reliable sysfs path exists: on i915, DP connectors carry DDC over AUX and expose no
per-connector `ddc` symlink, and an MST bus (`i2c-15` named `DPMST`) has no link back to its
connector at all. One shared detect covers every monitor, cached for 10 s.

## No periodic re-read; an explicit action instead

The shell polls `Device.refresh()` every 30 s. This module does not implement it. The origin
polls nothing either — every read is a ddcutil invocation that re-probes the bus, and a background
timer doing that competes with every other DDC access on the machine. The origin offers "Re-read
from monitor" in its tray menu; the same action is on the Information tab.

## Read-only MCCS features are shown as readouts, not filtered

The origin drops them (`0xAA` Screen Orientation is its example). They are real state the monitor
reports, the renderer never disables a non-writable row, and hiding them loses information for
nothing. They appear under Information ▸ Reported by the monitor.

## The KVM tab does not repeat the input control

The origin shows Input Source twice — once on Settings and again on the KVM tab, relabelled
"Switch active input" with the switch-back warning. Both write `0x60`. Two capabilities over one
register means the untouched one shows a stale value until the next read, so the KVM tab carries
the warning as a note and points at Settings instead. No functionality is lost; the explanation is
not.

## Input renaming is inline, not a dialog

The origin opens a modal table (`RenameInputsDialog`). Here each input is a `Kind.TEXT` row under
Settings ▸ Input names, which the shared renderer draws with no bespoke UI — and, unlike the
dialog, it is visible without going looking for it. Still app-side only: the monitor's own menu
cannot be renamed over DDC/CI, proven by a full `0x00–0xFF` register diff on a P3424WE.

## Not ported: profiles, copy-to-monitors, export/import, tray, D-Bus CLI

All three of the first group are the same operation — apply a set of values, skip what the target
does not support, clamp the rest — which generalises to every module and belongs in the shell.
The tray and the D-Bus CLI are shell concerns; see `docs/DELL_UI_BEHAVIOUR.md` §15–§16. Recorded
as decisions, not omissions.

## Confirmed on hardware, 2026-08-06 (2× P2425D, DP over a WD22TB4 dock)

- Both panels enumerate from EDID and resolve to `/dev/i2c-15` and `/dev/i2c-16`. Full page in
  **3.3 s** including the shared detect.
- **Set-then-verify with a snapped read-back works**: sharpness 50 → requested 55 → the panel
  landed on **60**, reported as success with the value the monitor actually holds. This is the
  case that made `Device.set()` return the landed value.
- Brightness writes an exact step and confirms exactly (75 → 70 → 75).
- **The merged Colour Preset works across all three opcodes**: Movie and Standard write `0xDC`,
  Warm and Custom Colour write `0x14`, and `0xE2` tracked every one of them. RGB gains survived
  the round trip.
- These panels are attached through a Thunderbolt dock on `DPMST` buses. The origin lists docks
  and MST as unsupported; they work here, slower.

---

# `uvc_cameras`

**Not a port in the sense the rest of this file describes.** No source code was copied. The module
was written against the UVC 1.5 specification and `linux/videodev2.h`; `cameractrls`
(LGPL-3.0-or-later) was read to learn *which* extension unit GUIDs, selectors, offsets and payload
bytes exist, and those values were transcribed. They describe Logitech's, Razer's, Dell's and
AnkerWork's firmware rather than their author's expression — a GUID is a number the device answers
to.

Checked rather than asserted: the only source lines the two projects share are kernel constants from
`linux/videodev2.h` and `asm-generic/ioctl.h`, which any correct transcription produces identically.
And LGPL-3.0-or-later may in any case be conveyed under this project's GPL-3.0, so the licences are
compatible in this direction and the obligation is attribution — see
[README](../README.md#credit-where-it-is-owed).

The divergences below are therefore design decisions rather than patches, but they are recorded for
the same reason: so the difference is deliberate and reviewable.

## Two cameras per camera become one row

`cameractrls` lists every `/dev/video*` node, so a webcam with an infrared sensor for face unlock
appears twice under the same name, and only one of the two is the camera anybody wants. Discovery
keeps the node offering the most pixel formats and remembers the other in the row's `nodes`
property — see [UVC_CAMERAS_UI_BEHAVIOUR](UVC_CAMERAS_UI_BEHAVIOUR.md) §5.

## Vendor controls are gated three ways, not one

`try_xu_control` asks whether a selector answers. That is kept — it is the load-bearing check — but
it is the *third* gate here, after the extension unit's GUID being present in the USB descriptors
and after the model filter where the source declares one (`LOGITECH_BRIO_FOV_DEV_MATCH` and
friends). The order is by cost, so a camera pays only for the checks that can still succeed. §2.

## Vendor ranges are read from the unit, not assumed

A vendor `range` control asks the unit for `GET_MIN` and `GET_MAX` at connect rather than carrying
bounds in the table. A vendor control reports its own range exactly as a V4L2 control does, and
hardcoding one would invent a limit the camera never stated.

## Every write is verified, and that is upstream's rule kept

`V4L2FmtCtrls` compares what the driver returned against what was asked and warns on a
substitution. That behaviour is deliberately preserved for pixel format, resolution and frame rate,
including the `10 / (fps × 10)` interval convention — which is not cosmetic: 7.5 fps is a real UVC
rate and `1/7.5` is not expressible in the integer pair the kernel takes.

## Not carried: the preview, the daemon, the PTZ input bridges

A settings page does not need to be a video player (it would pull in SDL and libturbojpeg), a
background service that reapplies presets is a shell-wide feature rather than a camera one if it is
ever built, and driving pan/tilt from a SpaceNavigator or a MIDI controller is not configuring a
camera. §7.

One consequence worth stating, because it is easy to read as a defect: `cameractrls`' resolution
control *appears* to work where this one seems not to, because its preview is the capturing
application and renegotiates on change. Its effect on any other application is identical to this
one's — none. Measured in §6.

## Confirmed on hardware, 2026-08-18

- **Logitech BRIO** (`046d:085e`) — field of view across all three values on extension unit 10,
  status light on unit 11, both written and read back. Peripheral unit 11 correctly *declines*
  selectors `0x01` and `0x02`, so the mechanical pan/tilt rows never appear on a camera with no
  motor.
- **Realtek Integrated_Webcam_FHD** (`0bda:5570`) — no extension units at all, which is the
  plain-UVC case: the whole page comes from what the driver reported.
- Streaming mode exercised across every format on both cameras, 4096×2160 MJPG and 7.5 fps set and
  read back, and the `EBUSY` path provoked by streaming from the camera. Image controls kept working
  throughout, which is what the error message claims.
- **Still unexercised:** the mechanical pan/tilt nudges, recentre and the eight stored positions,
  and the QuickCam focus motor. Their payloads are verified against `cameractrls`' own constants by
  a test, which is a transcription guarantee and not a hardware one. They need a PTZ Pro, Group,
  MeetUp or Rally.
