# UVC cameras — behavioural specification

What this module does and why. Unlike every other module here it is built on a **class
specification** rather than a vendor protocol, and almost everything below follows from that.

**Verified against a Logitech BRIO** (`046d:085e`) — standard controls read and written, both vendor
controls read and written, the inactive-flag interlock observed live. Other cameras get the standard
controls, which are as reliable as the kernel's UVC driver; other vendors' extensions are carried
from `cameractrls` unverified and marked experimental.

## 0. What makes this module different

**It claims a whole transport.** Every other module names a vendor; this one takes any V4L2 capture
device. That is correct rather than lazy: UVC is a specification, the driver reports which controls a
camera has along with their ranges, defaults and menu items, and so an unknown webcam produces a
*correct* page rather than a guessed one. There is no protocol to be wrong about. A test records the
exception so nobody has to wonder whether it was deliberate.

**It does not take the camera.** Opening `/dev/videoN` read-write to change a control does not claim
the stream, so settings can be adjusted mid-call. Every other module here interrupts the device it
configures — the 8BitDo controller stops being a gamepad for a second, the Creative card must be
unlocked first. This one has no advisory to write, because there is nothing to warn about.

**Nothing is saved to the camera.** Standard V4L2 controls are volatile and reset at power-off. That
is UVC, not a limitation here. Two exceptions exist in the table and both belong to the hardware: a
Logitech conference camera's stored pan/tilt positions, and the Razer Kiyo Pro's save command.

## 1. Two discovery mechanisms, no model tables

**Standard controls** are walked with `VIDIOC_QUERYCTRL` and `V4L2_CTRL_FLAG_NEXT_CTRL`. The driver
returns the next control it *has*, so the list is the device's own. Each arrives with type, minimum,
maximum, step, default, menu entries and flags. Capability keys are derived from the driver's control
name — `"White Balance, Automatic"` → `white_balance_automatic` — which is what `v4l2-ctl` prints, so
a key here is a name the user can look up. Derived rather than mapped from a table of ids, because
the walk finds controls this module has never heard of and they need keys too.

One quirk carried deliberately: a control that reports `0..1` with step 1 is rendered as a **toggle**,
not a slider. Every UVC driver produces several, and a two-position slider is nobody's intent.

Another: `VIDIOC_QUERYCTRL` returning `EIO` for one control is **not** the end of the list. Some
cameras do that and answer for everything after it, so the walk skips and carries on.

**Vendor controls** live in UVC extension units, and the unit id differs per model. It is found by
searching the camera's own raw USB descriptors — `/sys/class/video4linux/videoN/../../../descriptors`
— for the unit's 16-byte GUID and taking **the byte immediately before it**. On the Brio that
returned 10 and 11, and nothing in this module had to know that in advance. The technique is
`cameractrls`'s; see [README](../README.md).

Note the GUID byte order. UVC writes the first three fields little-endian and the rest big-endian, so
a descriptor never contains the GUID in the order it is printed. `_guid()` does that conversion, so
the table can be read against `lsusb -v` output directly, and a test pins the result against bytes
read from real hardware. Getting it wrong finds no units at all — silently, because "no such unit" is
a normal answer.

## 2. Three gates on a vendor control

A table entry is a **claim to check**, never an assumption. In cost order:

| gate | cost | what it rules out |
|---|---|---|
| product id, where the entry names one | a dictionary lookup | one vendor's payloads reaching another's camera |
| the unit's GUID is in the descriptors | one sysfs read | a camera without that unit |
| the selector answers `GET_LEN` | one ioctl | a model that has the unit but not the control |

The third is what makes the table safe to be generous with. The Logitech peripheral unit lists
mechanical pan and tilt; a Brio *has* that unit and does not answer for them — its pan and tilt are
digital and therefore standard V4L2 controls — so those rows quietly do not appear.

The first is not belt-and-braces. GUID `23e49ed0` is used by the Razer Kiyo Pro **and** the Dell
UltraSharp, and a Brio answers on 14 of its selectors. The payloads are model-specific, so without
the product-id gate this would write one vendor's commands to another vendor's control surface.

## 3. Two ways to write, and they are not interchangeable

**Byte at an offset**, read-modify-written. Logitech's status light is a five-byte control carrying
*two* settings — mode at offset 1, blink frequency at offset 3 — so writing the whole buffer to
change one would silently reset the other.

**Whole blob**, sent as-is. Razer, Dell and AnkerWork address features by sending an opaque byte
string to one selector. These are commands rather than fields and there is no reliable read, so they
are declared `readable=False` and the page does not claim to know where they are set.

Byte writes are **read back and verified**. A UVC extension unit does not report failure the way a
V4L2 control does: `SET_CUR` can succeed and the device keep its old value, which then reads as a
control that does nothing. Same discipline the Creative module needed for routing.

## 4. The inactive flag is a gate we get for free

`V4L2_CTRL_FLAG_INACTIVE` is the device saying a control is meaningless right now — `focus_absolute`
while continuous autofocus is on. Those rows are locked through an `Advisory`, and the flags are
re-read after **every** write, because turning autofocus off is what makes focus live.

Observed on the Brio, and worth recording because it is the whole argument for asking the device
rather than encoding the rule:

```
autofocus on  -> focus locked = True
autofocus off -> focus locked = False      the camera said so; we did not
```

Three controls arrive locked on a Brio at rest — focus, exposure time and white balance temperature —
each because its automatic counterpart is on.

## 5. One camera, one row

A camera with an infrared sensor — anything supporting Windows Hello — presents **two capture
nodes**, and they are indistinguishable by USB ids, by name, and even by extension unit: both report
the same units, and writing to either changes the one camera.

What separates them is what they can stream:

| node | formats | resolutions |
|---|---|---|
| `/dev/video4` | YUYV, MJPG, NV12 | 19, up to 1920×1080 |
| `/dev/video6` | GREY | 340×340 only |

So the node offering the most pixel formats wins, and the test is a **count**. Naming the formats
instead would be guessing: `GREY` is perfectly ordinary for a monochrome industrial camera that has
no second node to be confused with. Both nodes are kept in the row's `nodes` property, so a module
wanting the infrared one can find it.

`cameractrls` lists both as separate cameras. That is the wart this exists to avoid.

The row's uid is keyed on the **USB path**, not the node number, so a camera keeps its identity
across a reboot that hands out `/dev/video*` numbers in a different order.

## 5a. And one row, not a gamepad

A webcam is usually a composite USB device, and a Logitech BRIO exposes a HID interface alongside its
video nodes. Enumeration therefore found it twice, and the hidraw row was the problem: `046d` over
hidraw is exactly what `logitech_peripherals` claims, so the mouse-and-keyboard module claimed a
webcam, and — because a module that claims a device overrides a category enumeration could only guess
at — the BRIO appeared in the sidebar under INPUT with a **gamepad icon**.

That HID interface is worth looking at, because it settles what it is:

```
05 0c 09 01 a1 01  05 0c 09 01 a1 01  09 ff 09 fe
15 00 25 01 75 01 95 02  81 42  95 01 75 06 81 01  c0 c0
```

Thirty-four bytes: Consumer page, two one-bit buttons, six bits of padding. No report ids, no vendor
page. It is the volume keys. There is nothing on it to configure and it can never speak HID++.

Two things changed, and they do different jobs.

**A camera row displaces the HID row for the same USB device.** This reverses the rule that holds
everywhere else in `_one_row_per_device`, where hidraw wins because its row is the one that can be
opened. For a camera it is the other way round: the video row carries every setting and the hidraw
row carries none. The displaced row's nodes are kept on the survivor as `hid_nodes`, so nothing
becomes unreachable — the same bargain the other direction already makes. This is what fixes the
BRIO, and it cannot affect a receiver or a mouse, because those have no video node to displace them.

**Discovery now publishes `hid_hidpp`.** Solaar's own device filter is vendored in this tree, and it
identifies HID++ from the report descriptor: an input report on id `0x10` of six payload bytes, or on
`0x11` of nineteen. Notably it does *not* also require usage page `0xFF00` — that check is present in
the vendored source and commented out as too strict. The same test is applied here, with two
departures worth stating:

- It asks **every** node of the device, not the one the row is represented by. Measured on a Logi
  Bolt receiver, those are not the same node: the group is represented by `/dev/hidraw1`, which
  declares no report ids at all, while HID++ answers on `/dev/hidraw3`.
- The answer is three-valued: `"yes"`, `"no"`, or `""` for a descriptor nobody could read. Folding
  the third into `"no"` is how a working receiver would silently disappear from the sidebar.

The descriptor walker was checked against the vendored `hid_parser` on all fourteen HID descriptors
present on the development machine and agrees with it on every report id and every size, and both
read a HID++ descriptor as short=6, long=19. Three of those fourteen make `hid_parser` raise where
the walker still answers; being the more tolerant of the two is safe when the only question is
whether two specific ids exist at two specific sizes.

**The Logitech match rule is not yet narrowed to `hid_hidpp = "yes"`.** The property exists so it
can be, and it would be a two-line manifest change, but no HID++ hardware is attached to test the
positive case against — and the cost of getting it wrong is a receiver that vanishes rather than one
that reports a clear error. It waits for a Bolt receiver.

## 5b. Why not a separate `logitech_cameras` module

The FIDO2 and YubiKey split is the right comparison to draw, and it is the reason the answer here is
different. Those two are separate modules because they are separate *things*: CTAP over one library,
YubiKey's own manager over another, different protocols reaching different devices, either usable
without the other.

Logitech's camera extras are not that. They are `UVCIOC_CTRL_QUERY` calls on the **same file
descriptor**, opened on the **same `/dev/video*` node**, gated by the **same three checks** as every
other vendor's extras, and rendered as rows in the same groups as the standard controls — field of
view sits beside zoom because that is where a user looks for it. Splitting them would mean two
modules claiming one device, which the registry cannot express: `claim` picks exactly one manifest
per row. The alternative — a second sidebar entry for the same camera — is the `cameractrls`
two-rows-per-camera wart that §5 exists to avoid.

What would justify a separate module is a Logitech camera that needs something this one cannot do:
its own transport, a vendor library, or a firmware channel that is not UVC. If a Rally or a MeetUp
turns out to work that way, that is when to split it. Vendor *tables* are not a reason; those are
data, and `extensions.py` is where they live.

## 6. Changing the streaming mode

**Pixel format, resolution and frame rate are writable.** They were not at first, and the reasoning
that withheld them is worth recording because two thirds of it was wrong.

The first claim was that setting a format needs the descriptor reopened and exclusive use, so it
belonged to whatever was capturing. Measured on both cameras here, 2026-08-18:

| test | result |
|---|---|
| `VIDIOC_S_FMT` to 1920×1080 MJPG on an idle node | succeeds |
| the same, re-read after closing and reopening the descriptor | **persists** |
| the same while something is streaming | `EBUSY` |
| `VIDIOC_S_PARM` asking 15 fps at 640×480 YUYV | returns 30, and stays 30 |

Only the third row supports withholding anything, and the fourth turned out to be the driver
clamping to an *enumerated* rate rather than a general unreliability — 15 fps is not on that
camera's list at that size. Offer only the camera's own enumerated values and the substitution
stops being a hazard. So all three are now controls, ported from `cameractrls`' `V4L2FmtCtrls`,
which exposes the same three.

What remains true is worth saying on the page rather than using as an excuse:

- **It fails while the camera is in use**, and on a PipeWire desktop that means whenever anything
  has the camera open — a browser tab is enough. The error says so and names what to close.
  Verified by streaming from the BRIO and attempting a change: the message appears, and the image
  controls keep working throughout, which is the other half of what the message claims.
- **Whatever streams next negotiates its own format,** and this is the limit that matters. A user
  changed the resolution here and nothing happened in Kamoso, before or after restarting it. That is
  not a bug in either program. Measured: this module set a BRIO to 1280×720 MJPG at 30 fps, then a
  GStreamer `v4l2src` opened the camera and renegotiated it to **640×480 at 120 fps** — it asked for
  its own caps and got them. `ffmpeg` does the same, flipping NV12 to YUYV.

  Kamoso is a GStreamer application, and its own settings offer the captures directory, a flash
  toggle and sliders for brightness, hue, contrast, saturation and gamma — **no resolution**, per its
  handbook. So there is no setting anywhere, here or in Kamoso, that raises the resolution Kamoso
  captures at. An application that asks for a format gets it, and one that offers no choice gives its
  user none.

  To be precise about the path, since it was worth checking: Kamoso does not open the node directly.
  Its config holds `deviceObjectId=138`, and PipeWire object 138 is the node `v4l2:/dev/video5`. The
  chain is Kamoso → `pipewiresrc` → the PipeWire daemon → `uvcvideo`. That matters only because
  PipeWire, unlike GStreamer, *is* a daemon and therefore the one place in the chain with any
  external leverage — PipeWire carries `default.video.width` / `height` / `rate` (640 / 480 / 25,
  and 640×480 is exactly what was observed). Those are read at daemon start, not from the runtime
  `settings` metadata, which carries only clock and log keys. Whether they would override this path
  is untested: they are defaults for clients that do not ask, and PipeWire camera access goes
  through the desktop portal, so `pipewiresrc` would not preroll from a terminal to check.

  **There is nothing to talk to on the GStreamer side.** GStreamer is a library linked into the
  application's own process, not a service — no socket, no bus name, no daemon. Its caps are
  negotiated inside Kamoso's address space when the pipeline starts, and `v4l2src`'s properties
  belong to the element instance the application created. No settings application can reach that,
  and this module does not try: its job ends at the V4L2 node.

  This is also why `cameractrls` appears to succeed where this seems not to: its resolution control
  drives *its own preview*, which is the capturing application. Change the resolution there and the
  picture changes, because that picture is the one being renegotiated. Its effect on any other
  application is identical to this one's — none.

  So the control sets the camera's current mode; it does not reserve it. The page says so on the
  controls themselves, rather than leaving it to be discovered.

  **Which applications honour it: measured, none of them.** Camera set to 1280×720 MJPG at 30 fps,
  then each application run against it with no resolution requested:

  | application | left the camera at | verdict |
  |---|---|---|
  | `ffmpeg` | 1280×720 **YUYV** 30 | overrode |
  | VLC | **1920×1080** YUYV 30 | overrode |
  | GStreamer `v4l2src` | **640×480** MJPG **120** | overrode |
  | a capture that never calls `S_FMT` | 1280×720 MJPG 30 | honoured |

  Three for three, each substituting something different. The last row is a program written for this
  test that deliberately omits `VIDIOC_S_FMT`; it received a real 1280×720 JPEG, and it is what
  proves the setting is genuine device state rather than decoration. Note that `V4L2_CAP_READWRITE`
  is false on both cameras here, so that test had to be a full `REQBUFS`/`QBUF`/`STREAMON` capture
  rather than a `read()`.

  But no application anybody actually uses behaves that way. So the accurate answer to "does this
  work" is: it changes the camera, and it changes nothing about what a capture application displays.
  Whether three writable dropdowns are worth having on that basis is a fair question, and the
  reporting below — which formats exist and what each can do — is the part that carries real value.
- **It does not survive a replug.** Node state, not camera firmware — §0 already says nothing here
  is kept by the hardware.

### Three dependent dropdowns

The lists are built for what is currently selected, and changing one rebuilds the ones below it:

| selected | offers | measured on the BRIO |
|---|---|---|
| pixel format | every format the camera reports | YUYV, MJPG, NV12 |
| resolution | sizes **for that format** | NV12 → 4 sizes, MJPG → 20 |
| frame rate | rates **at that format and size** | 640×480 MJPG → 10 rates |

A flat list of every combination would offer modes the camera does not have. This is also why a
mode write is the one write in this module that rebuilds the page: the choices themselves changed,
so the two dropdowns below the one that moved would otherwise be showing modes that no longer exist.

Every write is a read-modify-write of the whole format — never a fresh structure, so fields nobody
is changing keep the driver's values — and every write is **verified**, because V4L2 permits a
driver to accept the ioctl and substitute a different value. What came back is compared with what
was asked and any difference is reported. The frame rate is sent as `10 / (fps × 10)` rather than
`1 / fps`, which is `cameractrls`' convention and not cosmetic: 7.5 fps is a real UVC rate and
`1/7.5` is not expressible in the integer pair the kernel takes, where `10/75` is exact. The BRIO
accepted 7.5 and reported 7.5 back.

Reading the mode needs no exclusive access, so **current mode** is shown even while something is
streaming — and it is the authoritative read-back, since a substituted write shows the substituted
mode rather than the request.

### What each format can actually do, reported per format

Because it differs, and one row for the whole camera hid that:

| camera | format | largest | rates there |
|---|---|---|---|
| Logitech BRIO | MJPG | 4096×2160 | 30 / 24 / 20 / 15 / 10 / 7.5 / 5 |
| Logitech BRIO | YUYV | 1920×1080 | 30 / 24 / 20 / 15 / 10 / 7.5 / 5 |
| Integrated_Webcam_FHD | MJPG | 1920×1080 | 30 |
| Integrated_Webcam_FHD | YUYV | 1920×1080 | 5 |

That last pair is the whole answer to "why is my webcam so slow at 1080p". The row this replaced
reported **one** largest resolution taken from whichever format the camera listed first, which for
the BRIO meant reporting a 4K camera as 1080p.

## 6a. What is still read-only

**Anything the driver flags read-only**, such as camera orientation. That is a fact about how the
sensor is mounted, not a setting.

## 7. Not carried

**No preview window.** `cameractrls` ships one; a settings application does not need to be a video
player, and it would pull in SDL and libturbojpeg. Use anything that opens a camera to see the
picture while adjusting — `ffplay /dev/video4`, `qv4l2`, `vlc v4l2:///dev/video4`, or `cameractrls`
itself, all four of which were present on the development machine already. Every control here takes
effect live on whatever is already streaming, so a player left open beside this window shows each
change as it is made; that is a property of V4L2, not something this module arranges.

**No background daemon.** Camera settings being volatile is real, and `cameractrls` answers it with a
systemd user unit plus a desktop-portal autostart request that reapplies a saved preset when a camera
appears. That is a reasonable answer and a much bigger commitment than a settings page — it means
this project running when it is not open. Deferred rather than rejected: if it is built, it should be
a shell feature for every module, not a camera one.

**No PTZ input bridges.** That project can drive pan and tilt from a SpaceNavigator, a joystick or a
MIDI controller. Genuinely useful for a conference camera, and nothing to do with configuring one.

## 8. Status

**Verified on a Logitech BRIO** (`046d:085e`). Field of view written through extension unit 10 across
all three values and read back each time; the status light written and read back on unit 11;
brightness written, including a deliberately out-of-range value to confirm the camera's clamp is
reported rather than the request; the autofocus interlock exercised in both directions.

Streaming mode exercised in full, 2026-08-18: every pixel format selected in turn with the size and
rate lists rebuilt each time (NV12 → 4 sizes, YUYV → 19, MJPG → 20), 4096×2160 MJPG set and read
back, 7.5 fps set and read back, and the `EBUSY` path provoked by streaming from the camera — the
error appeared with its message, and brightness still wrote (128 → 133) while the stream ran, which
is what that message claims. The camera was returned to its original 640×480 NV12 at 30 fps
afterwards, as it was after every test here.

**Also verified on a Realtek `Integrated_Webcam_FHD`** (`0bda:5570`), the built-in camera of a Dell
laptop, 2026-08-18. It has no extension units at all, which is why it is worth having: it is the
plain-UVC case, and the BRIO alone could not show whether the standard half stands up without vendor
extras. It does — every control on its page comes from what the driver reported.

Every other camera is `status = "family"`: the standard controls are as good as the kernel's driver,
and no one has driven that model through this page.

**Not exercised on hardware:** the mechanical pan and tilt controls — the nudges, recentre and the
eight stored positions. No camera here has a motor, and the BRIO's peripheral unit correctly declines
selectors `0x01` and `0x02`, so those rows do not appear on it. Their payloads are transcribed from
`cameractrls` and checked against its own constants by a test, which is a transcription guarantee and
not a hardware one. They need a PTZ Pro, a Group or a MeetUp.
