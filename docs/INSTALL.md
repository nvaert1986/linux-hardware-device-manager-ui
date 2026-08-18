# Installing and running

## Dependencies

Everything comes from Portage. There is **no virtualenv, no pip and no uv** — a pip-installed
module is invisible to Portage, never gets rebuilt on a Python upgrade, and would stop this
shipping as an ebuild.

### Required

| Package | Why |
|---|---|
| `dev-python/pyqt6` | The UI. Needs `gui`, `widgets`, `network`; **QtDBus** comes with it and carries Bluetooth hotplug — no extra package for that. `QtBluetooth` is *not* used: it exposes a strict subset of the same BlueZ signals — no pairing or removal, and no property filtering. (It was also unimportable before `pyqt6-6.11.0-r1`, which fixed a `QBluetoothUuid::toUInt128` signature mismatch.) |
| `dev-qt/qtbase` | Pulled in by pyqt6. Breeze's QStyle is what makes the app look native. |
| `kde-frameworks/breeze-icons` | Device and action icons. |

### Optional

Per-module, and each one only gates its own family. The [README](../README.md#requirements) has
the same table with what happens when each is missing.

| Package | Enables |
|---|---|
| `app-misc/ddcutil` | **required for monitors.** All DDC/CI I/O. 2.x; tested against 2.2.6 |
| `sys-apps/openrazer-daemon` | **required for Razer devices.** Kernel drivers plus the daemon. Add yourself to `plugdev`. |
| *(none)* | **Logitech needs no Solaar install.** The library is vendored under `hardware_ui/third_party`, so `app-misc/solaar` is *not* a dependency and its GTK stack is never pulled in. It needs `pyudev` and `pyyaml`, both above. See `docs/LOGITECH_UI_BEHAVIOUR.md` §1. |
| `dev-python/fido2` | **required for FIDO2 security keys.** Yubico's pure-Python CTAP2 library; no compiler, no `libfido2` bindings. |
| `app-crypt/yubikey-manager` | **required for everything a YubiKey does beyond the FIDO2 standard.** Enabling and disabling the USB and NFC **interfaces**, the application list, the **OTP slots**, **OATH accounts**, the model name, firmware version and serial number all come from it — none of that is expressible in CTAP, so none of it is reachable without this package. Absent, the key falls back to what the standard reports (passkeys, PIN, reset) and the page says so on a single row instead of showing dead controls. So: optional in that a YubiKey still *works*, required in that the YubiKey-specific half of the page is otherwise empty. Needs nothing else — the management application is read over the FIDO interface already open, so no `pcscd` and no smartcard stack. |
| `dev-python/pytest` | the test suite |
| `dev-python/pyudev` | **hotplug for USB, HID and DRM, *and* required for Logitech devices.** Two unrelated uses of one package: hotplug installs a kernel-side filter so unrelated events never reach the application, and the vendored Logitech library enumerates receivers through it. Absent, hotplug degrades to Rescan and nothing else changes — **but Logitech devices cannot be opened at all**. Bluetooth hotplug is BlueZ over D-Bus and does not come from here. |
| `dev-python/pyyaml` | **required for Logitech devices.** How per-device settings are persisted and re-applied: most HID++ settings do not survive a reconnect in hardware, so the host writes them back. Used by no other module. |
| `dev-python/pybluez` | **optional, Sony only.** One of several ways to resolve the headset's RFCOMM channel; without it the lookup falls back to `sdptool` and then to a cached or default channel. Imported inside the function that uses it, so its absence costs nothing. |
| `dev-python/dbus-python` | **optional.** Sony reads battery level and active codec from BlueZ through it; without it those two show as unknown and everything else works. OpenRazer pulls it in as its own dependency, so a Razer install already has it. |
| `app-arch/msitools` | unpacking vendor MSIs for `ExtractInstaller`; `app-arch/7zip` is the fallback |
| `dev-python/hatchling` | building a wheel |

```
emerge -av dev-python/pyqt6 kde-frameworks/breeze-icons
emerge -av app-misc/ddcutil                        # monitors
emerge -av sys-apps/openrazer-daemon               # Razer keyboards and mice
emerge -av dev-python/fido2                        # FIDO2 / U2F security keys
emerge -av app-crypt/yubikey-manager               # YubiKey interfaces, OTP, OATH, model info
emerge -av dev-python/pytest app-arch/msitools     # optional
```

Notably **not** needed, and deliberately designed around:

- **qasync** — no ebuild in ::gentoo, ::guru or ::pentoo. The asyncio loop runs on its own thread
  instead, which is a better structure anyway (see [ARCHITECTURE](ARCHITECTURE.md#threading)).
- **dbus-fast / dbus-next** — `PyQt6.QtDBus` ships with pyqt6 and works before the event loop
  starts.
- **`PyQt6.QtQuickControls2`** — not built by Gentoo's pyqt6, and no longer relevant since the UI
  is QWidgets.

## Running from source

```
./run.sh            # the GUI
./run.sh -v         # with debug logging
python3 -m hardware_ui.cli --all    # headless: what is detected, what is unclaimed
```

`run.sh` sets nothing but the working directory — modules are found by scanning
`hardware_ui/modules/`, so a checkout works without installation.

## Desktop entry

```
install -Dm644 packaging/hardware-ui.desktop ~/.local/share/applications/hardware-ui.desktop
kbuildsycoca6
```

Two things that are easy to get wrong, both of which cost a debugging session:

**On Wayland a client cannot set its own window icon.** `setWindowIcon()` is a no-op; the
compositor resolves the icon from the `.desktop` file matching the app_id, which Qt takes from
`setDesktopFileName()`. Changing icons in code has no effect.

**KWin reads `.desktop` entries through ksycoca**, which `update-desktop-database` does not
touch — run `kbuildsycoca6` after installing or editing the file.

The entry uses an **absolute path** for `Icon=`, pointing at `hardware_ui/shell/icon/`. A custom
theme icon under `~/.local/share/icons` would need plasmashell's cache refreshed; an absolute path
has nothing to cache. Adjust `Exec=` to match your checkout.

### Per-module dependencies are optional

**None of these is required to run the application.** A module's dependency is imported only when
one of its devices is opened, so an installation without `openrazer` or `fido2` starts normally,
lists everything else, and simply cannot open that one family. Pressing **Connect** on such a
device shows what to install, verbatim and with the command — not a traceback, and not the
"switch it on and Rescan" advice that belongs to unreachable hardware.

## Device permissions

The app runs as your user and needs no root. Access comes from udev, and the rules file ships with
the source so a package can install it rather than asking you to retype it:

```
cp packaging/70-hardware-ui.rules /etc/udev/rules.d/
udevadm control --reload-rules && udevadm trigger
```

which is these four lines:

```
SUBSYSTEM=="hidraw", MODE="0660", TAG+="uaccess"
SUBSYSTEM=="usb", ATTR{idVendor}=="041e", MODE="0660", TAG+="uaccess"
SUBSYSTEM=="usb", ATTR{idVendor}=="2dc8", MODE="0660", TAG+="uaccess"
SUBSYSTEM=="i2c-dev", MODE="0660", TAG+="uaccess"
```

`uaccess` grants the device to whoever is logged in at the seat, which is the right semantics and
avoids a setuid binary or a root daemon.

**That one file is all most modules need.** Written per-vendor instead, this would be a rule per
product id and a new rule for every device bought; matched on the *node type* it covers every
vendor at once, including ones with no module yet.

**The two middle lines are the exceptions, and they name vendors on purpose.** Creative devices are
not opened as a node: their control protocol rides on a CDC-ACM function that has to be *claimed*
with libusb, and there is no node type to match on. Matching `SUBSYSTEM=="usb"` without narrowing
it would hand every USB device on the machine to the logged-in session, so the rule is scoped to
Creative Technology (`041e`). Nothing is detached: on kernels without `cdc_acm` those interfaces
have no driver bound, and ALSA owns only the audio interfaces either way.

8BitDo (`2dc8`) is the same situation for the same reason — its Xbox controllers speak GIP on a
vendor-specific interface and expose no hidraw at all. One difference: claiming that interface
*does* detach `xpad`, which is put back afterwards, so the controller stops acting as a gamepad for
about a second during a read or a save.

### One module costs you time rather than a package

With a **Logitech receiver** attached, the first device sweep of a session takes about **2–3 seconds**
longer. That is not the application starting slowly — every transport combined enumerates in 0.02 s
— it is the receiver being asked about all six of its pairing slots, four of which are empty and
time out.

It happens once per session and is cached afterwards (about 0.04 s), and your previous session's
devices appear immediately from the discovery cache while it runs. If you have no Logitech hardware
the module never opens anything and costs nothing. Switching it off on the **Modules** page removes
the delay outright.

### What each module actually needs

| Module | Reaches the device by | Beyond the rule above |
|---|---|---|
| Sony headphones | Bluetooth RFCOMM | nothing — pair it in your desktop's Bluetooth settings |
| Poly headsets | `hidraw` (adapter or cable) and Bluetooth | nothing |
| Jabra headsets | `hidraw` | nothing |
| Logitech peripherals | `hidraw` | nothing |
| FIDO2 / YubiKey | `hidraw` | nothing |
| Dell docks | `hidraw` | nothing |
| Razer peripherals | **the OpenRazer daemon**, not the device | `sys-apps/openrazer-daemon`, and your user in **`plugdev`** |
| Dell monitors | `i2c-dev` (DDC/CI) | the `i2c-dev` module loaded and your user in `i2c` — see below |

Two of those are worth spelling out.

**Razer is the exception to everything on this page.** This application never opens a Razer device:
it asks OpenRazer's daemon, which owns the hardware through its own kernel drivers and ships its
own udev rules. So the rule above is irrelevant to Razer, and what matters instead is that the
daemon is installed and running and that you are in `plugdev`. A Razer device that never appears is
almost always that group membership, and it needs a fresh login to take effect.

**Jabra and Logitech get in for free.** Jabra's control protocol is a vendor HID report (usage page
`0xFF00`) and Logitech's is HID++; both arrive on ordinary `hidraw` nodes, so no vendor rule is
needed. In particular this application does **not** need Solaar's
`42-logitech-unify-permissions.rules` — the `uaccess` rule above is broader than it, and grants the
receiver to the seat rather than to a group.

FIDO2 security keys need read/write on their `hidraw` node; the rule covers a key plugged in at
your seat, and without it opening one fails with a permission error rather than a missing device.

**Nothing here wants root.** If a step seems to need it, that is a bug worth reporting — the one
legitimate use of root on this page is writing the rules file itself.

For DDC/CI the `i2c-dev` module must be loaded, and `ddcutil` must work **without root**:

```
modprobe i2c-dev
echo i2c-dev > /etc/modules-load.d/i2c-dev.conf
gpasswd -a <user> i2c        # then log out and back in
ddcutil detect               # must list your monitors, as your own user
```

If a monitor is missing from that list, DDC/CI is switched off in its own menu — no software can
reach it until you turn it on. The app never runs `ddcutil` as root and never asks to.

## Development

```
python3 -m pytest              # 64 tests: schema, matching, write path, nine Dell models
ruff check .
python3 tools/audit_port.py    # mechanical diff of a port against its source project
```

`pyproject.toml` sets `pythonpath = ["."]`, so pytest runs from the repo root with no install and
no editable shim.

Ported protocol and transport code is excluded from ruff (`extend-exclude`) so it stays
byte-identical to its origin and can be diffed and re-synced. Adapter code is linted normally.

## Where things are written

| Path | Contents | Safe to delete? |
|---|---|---|
| `$XDG_CONFIG_HOME/hardware-ui/modules.toml` | module enable/disable | loses your choices |
| `$XDG_DATA_HOME/hardware-ui/vendor/<module>/` | imported vendor assets | needs re-importing |
| `$XDG_DATA_HOME/hardware-ui/modules/` | user-supplied modules | yes |
| `$XDG_CACHE_HOME/hardware-ui/devices.json` | last known device list | yes |
| `$XDG_CACHE_HOME/hardware-ui/<module>/*.json` | per-device discovery cache | yes, costs a slower connect |
| `$XDG_CACHE_HOME/hardware-ui/dell_monitors/calibration.json` | probed slider ranges, by serial | yes, costs re-running the probe |
| `$XDG_CACHE_HOME/hardware-ui/dell_monitors/input_names.json` | your labels for a monitor's inputs | loses the labels |
| `$XDG_CACHE_HOME/hardware-ui/photos/` | device photos | yes |

**Per-module user data.** A few modules keep small files of their own — they are settings *about*
a device rather than values *on* it, so they live in config and not cache:

| Path | Contents |
|---|---|
| `$XDG_CONFIG_HOME/hardware-ui/razer_peripherals/macros.json` | saved Razer macros, keyed by serial, plus the restore-on-connect flag |
| `$XDG_CONFIG_HOME/hardware-ui/razer_peripherals/dpi_stages.json` | saved DPI stages, keyed by serial |

Razer macros are worth explaining: **OpenRazer's daemon keeps them in memory only** and loses them
when it stops, so this application saves a copy and can put them back. They can also be exported
to and imported from a file of your choosing.

**No setting value is ever written to disk.** Values are read from the device on every connect,
because the vendor's phone app can change them while the app is closed.

## Packaging

The project is a single distribution with a flat layout, so a Gentoo ebuild is unremarkable:
`distutils-r1`, `PYTHON_COMPAT` for 3.13+, `RDEPEND` on the packages above. Per-module subsetting
is a USE flag, not separate packages — nothing vendor-owned ships in the tree, so there is nothing
to isolate.

Flatpak is possible but fights the hardware access: it cannot install udev rules, and would need
`--device=all` plus BlueZ system-bus access, which undercuts the sandbox. Distro packages are the
better fit for a hardware tool.
