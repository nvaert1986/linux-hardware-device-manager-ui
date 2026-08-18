# Changelog

What changed between releases, from a user's point of view. **Why** a thing was done the way it was
lives in [`PROJECT_STATE.md`](PROJECT_STATE.md) and the `docs/*_UI_BEHAVIOUR.md` specs; this file is
for deciding whether a release affects you.

Entries say plainly whether a module has been exercised against real hardware. That distinction
matters more here than a version number does: a device family this application has never opened is a
different proposition from one that has been read and written, and the status table in
[`README.md`](README.md) is the authority on which is which.

> **Why `0.10.1` and not `0.1.1`.** Worth writing down, because it is an easy and expensive slip.
> PEP 440 compares versions component by component, so `0.10` parses as `(0, 10)` and sorts
> *above* `0.1.1` — a release numbered `0.1.1` would look **older** than the `0.10` it followed,
> and `pip` would refuse to install it as an upgrade. `0.10.1` is the patch that follows `0.10`.

## 0.10.1

Three new device families, two new transports, a system tray icon, controller artwork that is
finally on screen, and a round of fixes — most of them reported from real use, and all three families
exercised against real hardware. 1027 tests, `ruff` clean.

### Added

- **A system tray icon.** Right-click for Open and Quit; a plain click toggles the window.
  Closing the window leaves the application running in the tray rather than exiting, and says so
  once — but only where the desktop actually has a tray. Without one nothing changes and the window
  quits on close as before, because swallowing the close button where no tray can be shown would
  make the application impossible to quit.

  It uses the application's own artwork, loaded from file rather than looked up by theme name.
  Breeze's own candidates are all unsuitable at panel size: its peripherals icon is a pale window
  drawn for a 32 px settings header and reads as a blank document at 22 px, and every monochrome
  one is dark ink that vanishes on a dark panel.
- **Creative Sound Blaster module.** Sound cards and headphone amplifiers over their CDC-ACM
  control channel, behind Creative's AES-256-GCM unlock handshake: feature toggles, output routing,
  Super X-Fi, the ten-band equaliser with presets, the card's four stored profiles, and readouts for
  volume, mute and what the firmware reports it can do. **Exercised against a Sound Blaster X4** —
  it unlocks, reads and takes writes. Eleven equaliser presets ship with it, recovered from a USB
  capture and named for kinds of music; Creative's own preset files and artwork are neither shipped
  nor importable, since the importer was not carried across from the source project.
- **8BitDo Xbox wired controllers module.** Button remapping including both back paddles, stick and
  trigger settings, and vibration, across three stored profiles. Works over USB (the Xbox Game Input
  Protocol) *and* over the controller's hidden Bluetooth config radio, which exists because the
  vendor's configurator is an Android app. **Verified over USB against an Ultimate Wired Controller
  for Xbox**: read, remap, save and read-back. Bluetooth sends the same save session but has not
  been run through this application.

  Changes are edited here and written by a **Sync to controller** button, which appears on every
  tab. The controller stores its whole configuration as one 532-byte block with one checksum, so
  there is no such thing as writing a single setting — saving per change meant a full session, a
  kernel-driver detach and a one-second gap in gamepad input for every dropdown touched. While a
  sync runs the whole page is disabled, because the record is assembled from what is on screen.
- **Camera support, for every camera.** A new `uvc_cameras` module covering any V4L2 capture device:
  zoom, pan and tilt, focus, exposure, white balance and picture controls, discovered from the camera
  rather than from a table of models — so a webcam nobody has seen produces a correct page. On top of
  that, the vendor features a camera publishes in a UVC extension unit, which no general-purpose
  Linux tool reaches: on a Logitech BRIO that is **field of view** (65°/78°/90°) and the **status
  light**. Every Logitech extension control `cameractrls` implements is covered — both status light
  variants, relative pan and tilt, recentre, all eight stored pan/tilt positions, and the focus motor
  on the old QuickCams — each appearing only when the camera's own extension unit answers for it, so a
  BRIO shows its two and none of the conference-camera rows. Entries for Razer, Dell and AnkerWork
  cameras are carried from `cameractrls` and marked experimental, since no such camera has been
  attached here.

  Cameras get their own sidebar heading, **CAMERAS**. There is no preview window: adjust with
  `ffplay`, `qv4l2` or any other viewer open beside it and every change shows up live, which is how
  V4L2 works rather than something arranged here.

  **Resolutions and frame rates, per pixel format.** What a camera can reach depends on the format,
  and one row for the whole camera hid that — a BRIO that does 4096×2160 in MJPG was reported as
  1920×1080, because YUYV came first in its list. There is now a row per format with its largest size
  and the rates available there, plus what the node is set to at this moment. The practical case:
  the integrated webcam here does 1080p at 30 fps in MJPG and 5 fps in YUYV, which is the whole
  answer to why a webcam feels slow.

  **And they can be changed**, as three dependent dropdowns: pick a pixel format and the resolution
  list rebuilds for it, pick a resolution and the frame-rate list follows. Only the camera's own
  enumerated modes are ever offered, and every write is verified against what the driver actually
  took — V4L2 lets a driver accept a format and substitute another, so a substitution is reported
  rather than swallowed.

  Two honest limits, both stated on the controls rather than hidden. Changing the mode fails while
  the camera is in use — on a PipeWire desktop a single browser tab is enough — and the error names
  what to close instead of showing an errno; image controls keep working either way.

  And it **will not change what a capture application shows.** Applications ask for their own format
  when they open a camera and get it. Measured, from a camera set to 1280×720 MJPG at 30 fps:
  `ffmpeg` left it at 1280×720 YUYV, VLC at 1920×1080 YUYV, GStreamer at 640×480 at 120 fps — three
  for three, each substituting something different. Kamoso does the same whether or not you restart
  it, and its own settings offer no resolution at all. A capture written to omit `VIDIOC_S_FMT`
  received a real 1280×720 frame, which is what shows the setting is genuine device state and not
  decoration — but no application anybody uses behaves that way.

  So the control changes the camera and changes nothing about what an application displays, and the
  page says exactly that on the control, because the obvious expectation of a resolution dropdown is
  the wrong one. **Every other setting is unaffected**: the image controls, field of view and the
  status light all take effect live, including while something is streaming.

  This reverses an earlier decision to keep them read-only, which rested on a claim that turned out
  to be wrong: setting a format needs no reopened descriptor and does persist. Ported from
  `cameractrls`, which exposes the same three controls.

  One module rather than one per brand, because every camera speaks the same protocol and the vendor
  part is a small annex reached through the same ioctl. Nothing is saved to the camera — UVC controls
  are volatile — and the page says so rather than implying otherwise.

- **A fifth transport: V4L2 cameras.** One row per physical camera, which takes some doing: a webcam
  with an infrared sensor for face unlock presents two capture nodes that are identical in USB ids,
  name and even extension units. They differ in what they can stream, so the node offering more
  pixel formats wins and the other is remembered rather than shown. `cameractrls` lists both as
  separate cameras; this does not.

- **A fourth transport: raw USB discovery.** hidraw, Bluetooth and DRM all hand over a device node to
  open; a vendor protocol tunnelled through USB has to have its interface *claimed* instead. Devices
  qualify by matching an explicit list of interface signatures — CDC-ACM for Creative, GIP for
  8BitDo — rather than by a broad class test, so the sidebar does not fill up with hubs, webcams and
  card readers.
- **Two udev rules**, for Creative (`041e`) and 8BitDo (`2dc8`). These are the only rules in the file
  that name a vendor: claiming a USB interface needs access to the USB device itself, and there is no
  node type to match on. See [`docs/INSTALL.md`](docs/INSTALL.md).
- **Diagrams: a device drawn, with its controls arranged around it.** A module supplies a drawing
  and a table of positions — data, not widgets — and the shell lays the controls out around it with
  a line from each to the part it changes. For settings that name a *place on the hardware*, a
  column of dropdowns is a list of names; every vendor configurator for a controller, a mouse or a
  keyboard draws the device instead. A section with no drawing, or a desktop without Qt's SVG
  support, renders as an ordinary form and loses nothing but the picture.
- **Original controller artwork** for the 8BitDo pages — three views, front, top edge and back,
  drawn for this project because the vendor's own renders cannot be redistributed. A controller has
  more than one side: the bumpers are a sliver from the face, the triggers are hidden behind them,
  and the rear paddles sit directly behind the D-pad and the right stick, so each view carries only
  what it can honestly show, in its own sub-tab. Positions are measured, and the anchors are read
  out of the drawings themselves, so picture and coordinates cannot drift apart.

### Changed

- **One row per physical device, for USB too.** A device exposing both a HID interface and a USB
  control channel is found by both enumerators; the hidraw row wins, because it carries an openable
  node, a device kind and an icon, and the control channel is still reachable from the USB path they
  share.
- **`DeviceInfo.ready` is now one rule for every transport.** It used to special-case Bluetooth, and
  that case only existed to paper over the state confusion fixed below.
- **The publish tooling refuses to clobber a repository.** Publishing mirrors the tree with
  deletions enabled, which would have removed the `.git` directory of the published copy — taking
  its history and remote with it. It now protects `.git`, and it refuses outright to publish
  anything carrying a home directory path, an email address or vendor data that is not ours to
  redistribute.

### Fixed

- **A webcam no longer appears as a gamepad.** A Logitech BRIO was showing up twice — once correctly
  as a camera, and once as an entry filed under INPUT with a gamepad icon, claimed by the module that
  configures mice and keyboards. Both rows were the same camera: a webcam is a composite USB device,
  and the BRIO's HID interface is its two volume-key bits and nothing else. A camera's row now
  displaces the HID row for the same device, keeping that row's nodes so nothing becomes unreachable.

  Discovery also publishes whether a device actually speaks HID++ now, read from its report
  descriptor by the same test Solaar applies — an input report on id `0x10` of six bytes or `0x11` of
  nineteen — asked of every node of the device rather than the one the sidebar shows, because on a
  Logi Bolt receiver those are different nodes. The Logitech module's match rule is not yet narrowed
  to require it: that wants a receiver attached to test, and the cost of getting it wrong is a
  receiver that disappears instead of one that reports a clear error.

- **Logitech devices paired over Bluetooth showed one working row and one dead one.** A mouse
  paired directly appears twice — as a hidraw node, which is the row that opens and carries HID++,
  and as a BlueZ device, which nothing claimed and which read "no module". One mouse, two entries,
  no way to tell which was which. The duplicate is now dropped whenever the working row exists.
  This is the likeliest explanation for a report that Logitech configuration "does not work over
  Bluetooth" on a machine where it does.
- **A Logitech device switched off vanished instead of moving to Disconnected devices.** Switching
  it off removes the hidraw node, leaving only the BlueZ row — and with no Bluetooth match rule that
  row was unclaimed, and unclaimed rows are never rendered. The module now matches Bluetooth as
  well, scoped to Logitech's own vendor GATT service rather than to a name. Sony and Poly never had
  this problem because they matched Bluetooth in the first place.
- **A Bluetooth mouse or keyboard showed a generic icon and the wrong category.** Devices were
  classified from the USB interface's protocol byte, and a Bluetooth device has no USB interface —
  so an MX Master got no classification at all, and its category fell back to whether its name
  happened to contain the word "mouse". "Logitech Wireless Mouse MX Master 2S" did and landed under
  INPUT; "Logitech MX Master 3S" did not and landed under OTHER with the generic peripherals icon.
  Now read from the device's own HID report descriptor, with BlueZ's icon hint used for its rows.
- **Poly headsets: the Hardware row rendered as Chinese characters** on a Voyager 4320, while the
  serial and firmware beside it read correctly. String fields were assigned an encoding by sniffing
  the first two bytes for a NUL, which mis-fires two ways: ASCII text with one stray leading NUL
  decodes as CJK, and a field that is not text at all decodes as CJK without complaint. Both
  encodings are now tried and the more legible result wins; a field that is not text in either falls
  back to hex, which is at least true and makes the field diagnosable. Not a distribution issue — the
  same bytes decode identically everywhere; the variable was the model.
- **Bluetooth devices showed the green "connected" dot before Connect had been pressed.** The state
  meaning "this application has an open session" was also being used by enumeration to mean "the
  operating system has a link to this device". They are different things, and the dot reports the
  first. A switched-on headset is now simply *available*: no dot until you connect to it, which is
  what every other transport already did and what other applications do.
- **The README understated the test suite** — it claimed 721 tests against an actual 974.

#### 8BitDo controllers, all reported from a controller in hand

- **A saved mapping was forgotten the moment the controller was unplugged.** Sending the record's
  chunks is not a save: the controller accepts all 532 bytes and reads them back correctly for as
  long as it stays powered. What commits is a `0x0006` finalize packet that was never sent, because
  it looked like session bookkeeping. The control packet that opens a save was also mis-split — the
  request header is seventeen bytes, so its `34 34 00 00` constant belongs in the *offset* field and
  the `aa` that means "save" is the payload — so save mode was never entered either. Both errors
  came from the source project's USB backend, whose own header warns it was never validated on
  hardware; its Bluetooth path replays the full session, which is why that one always worked.
- **The configuration checksum was chained from the controller instead of from a fixed seed.**
  Reading the device's own header and continuing its chain is the obvious design and it is wrong:
  three consecutive reads of a real controller, with no write between them, returned three different
  values. They are not a checksum of anything, so the records built from them could not validate.
- **Writing over Bluetooth demanded a USB cable first.** That restriction existed only to obtain the
  checksum described above. The seed is a constant, so a controller can now be configured over
  Bluetooth having never been plugged in.
- **The three controller drawings were shipped and never displayed.** The artwork and its anchors
  were in the module from the start with nothing in the shell to render them, so the Buttons tab was
  nineteen dropdowns named after parts of a controller nobody could see.
- **Rows on a drawing showed raw names**, `PADDLE_L` rather than "Left paddle", and longer labels
  were clipped. There is now a short label for use beside a picture, and a test that no row can show
  an enum name again.
- **A Bluetooth row could be shown the wrong controller's drawings and offsets.** In config mode
  every 8BitDo controller advertises as `82CE`, and nothing in the advertisement says which model it
  is. The drawings are now withheld for anything not positively identified, and the page says so and
  points at the USB cable, which does identify it. The artwork is also named for the model it
  depicts, so a second controller has somewhere to go.
- **An empty profile looked like a broken page.** Every control greyed out with the explanation on a
  tab the user was not looking at, under a button offering to *reset* a profile that did not exist.
  The advisory is on every tab now and the button reads "Create profile" when the slot is empty.

#### Creative Sound Blaster, all reported from an X4 in hand

- **The graphic equaliser was a checkbox and is now a list of modes.** Ticking it played whichever
  of the card's four stored curves was already live, so "turn the equaliser on" meant "turn on Movie
  mode" and there was no way to see that, let alone choose. One row now carries Off, on with the
  curve below, and the four stored modes **by name** — Music, Movie, Footstep Enhancer and EQ for
  Super X-Fi on a Sound Blaster X4, which is what the button on the front cycles through, a colour
  each. They were numbered "Profile 1" to "Profile 4", which named them without saying what they
  are; they are fixed modes, not slots anyone addresses by number. There is no bare "On" entry
  either — the card has no such state.

  The modes are split between the two Super X-Fi states and the card enforces the split: with Super
  X-Fi on it takes only its Super X-Fi curve, with it off only the other three, and it refuses the
  wrong one outright. The page says which are available before you pick, and a refusal arrives as a
  sentence rather than as "command 26 rejected, status=128 (1a 80 00 …)". Selecting a mode the card
  will not take also no longer leaves the equaliser switched on into whatever mode was live before.
- **Super X-Fi, its mode and the live equaliser mode never showed what the card was doing.** The
  switch sprang back to off on every click and the mode selector could only report what this
  application had itself chosen. The card replies to a change with a *different* operation code than
  the one that made it — a Super X-Fi write goes out as op 7 and comes back as op 8, a mode write
  goes out as `0x0c` and comes back as `0x0d` — and both this application and the project it was
  ported from matched the code they had sent, so the reply was never recognised. All three are read
  now, and the mode selector follows the button on the front of the card as well as driving it. They
  are still not read at connect, so a freshly connected card shows Super X-Fi as unknown until
  something touches it.
- **Choosing Speakers put the box straight back on Headphones**, while the card really had
  switched. The card acknowledges a write and commits it a while later — measured at anywhere from
  200 ms to 1.4 s — so reading the value back after the acknowledge returns the one from before the
  write. Routing is now confirmed by reading it back until the card agrees. Left alone, the card
  never reverts: three times out of three in both directions.
- **The output list offered a "Powered Speakers" the card does not have.** The bit exists in the
  protocol's enum and there is no query for which outputs a unit really has, so a third entry was
  this application inventing one — and selecting it is silence, not a cosmetic slip. Two entries
  now, as the vendor application offers.
- **The equaliser was greyed out whichever way Direct Mode was set.** Two causes. Its rows were also
  gated on the Graphic Equalizer toggle, which greys the whole tab for anyone whose card has the
  equaliser switched off — the factory state, and the one state from which a curve can then never be
  built. And a `requires` naming a capability in *another tab* could never resolve at all, because
  each tab kept its own value map: the Equalizer tab never learned Direct Mode's value and compared
  against nothing for ever. The second was a shell bug affecting every module.
- **Super X-Fi Mode could never be selected.** It was gated on the Super X-Fi toggle, which the
  initial sync does not read — the vendor's own does not either — so it sat at "unknown", which is
  falsy, and the gate never opened. Both rows are now gated on Direct Mode, like the rest of the DSP.
- **Direct Mode greyed out only the equaliser.** It is a DSP bypass, so Super X-Fi and headphone
  virtualisation stop applying too and were offering switches that do nothing.
- **The sliders did not move when a preset was applied.** A preset writes eleven values in one
  action; the module knew immediately and the page did not, because the shell repaints the control
  that was written and nothing else. A capability can now declare which rows a write disturbs.
- **`fetch_photo` crashed on any card.** A dropped `import os` during the port left `image_path()`
  raising `NameError`.

### Verified on hardware

- **Sound Blaster X4** (`041e:3278`, firmware `1.7.250324.0910`) — unlock, read, and writes across
  the page. Marked verified on **both** its transports: the card exposes a HID interface as well as
  its control channel, and the row the sidebar shows is the hidraw one, so marking only the USB rule
  left it reading "untested model". It reports a DSP mask of graphic-equaliser-only, which is why no Crystalizer or
  Surround appears: those run on the host as Windows audio processing objects.
- **8BitDo Ultimate Wired Controller for Xbox** (`2dc8:2002`) over USB — read, remap, save, and
  read-back after the save.
- **Logitech BRIO** (`046d:085e`) — the standard controls plus both of its Logitech extension
  controls: field of view on extension unit 10 across all three values, and the status light on
  unit 11, each written and read back.
- **Realtek Integrated_Webcam_FHD** (`0bda:5570`) — a built-in laptop camera with no extension units
  at all. Worth having precisely for that: it is the plain-UVC case, and the BRIO alone could not
  show whether the standard half stands up without vendor extras.

These modules match more devices than these, and everything else they match is still marked as an
untested model. That is what the "untested model" line under a device in the sidebar means.

### Known limits

- **Super X-Fi and the live equaliser mode are not read at connect.** The card volunteers them after
  anything changes them, and the initial sync does not ask; read forms for them are untested, and
  probing unlisted operations on a sound card is how you find out which of them writes. So a freshly
  connected card shows Super X-Fi as unknown until something touches it.

### Known issues

- **`State.ABSENT` is defined and never produced.** Nothing marks a device that was present a moment
  ago and is now gone; each sweep replaces the list outright. It does not matter for Bluetooth, where
  BlueZ remembers pairings and supplies the row, but unplugging a USB device still makes it disappear
  rather than move to Disconnected devices. Fixing it properly needs a retention policy, which is a
  design question rather than a patch.
- **The 8BitDo Bluetooth path has not been run through this application.** It sends the same save
  session as USB and its L2CAP handling comes from a project that was hardware-validated on it, but
  none of that is the same as having opened a controller with it.
- **Both new modules are verified on one model each** — a Sound Blaster X4 and an Ultimate Wired
  Controller for Xbox. The Creative module matches on vendor id alone, so every other Creative
  device it claims is untested by definition; that is what `status = "family"` on a match rule
  means.
- **Equaliser sliders move in whole decibels.** The protocol takes fractions and the vendor
  application works in tenths; the shell's slider is integral. Not wrong, just coarser.

## 0.10

The first published release — see [the GitHub repository](https://github.com/nvaert1986/linux-hardware-device-manager-ui)
for the tree as published. Ten device modules, of which Sony, Dell monitors, Dell docks, Poly,
Razer, FIDO2, YubiKey, Jabra and Logitech had been exercised against real hardware.
