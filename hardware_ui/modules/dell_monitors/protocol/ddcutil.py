"""Thin wrapper around the ``ddcutil`` CLI.

All monitor I/O goes through here. Functions are plain/synchronous and raise
:class:`DDCError` on failure; the GUI runs them off the main thread (see
``workers.py``). Monitors are always addressed by **I2C bus number** (``--bus``)
because bus numbers are stable across runs, whereas ddcutil "display numbers"
can be reassigned.
"""

from __future__ import annotations

import re
import shutil
import subprocess
import time
from dataclasses import dataclass, field


class DDCError(Exception):
    """Raised when a ddcutil invocation fails or its output can't be parsed."""


# I2C/DDC reads are occasionally flaky; these substrings mark a retryable glitch.
_TRANSIENT = (
    "No monitor detected",
    "DDCRC_NULL_RESPONSE",
    "DDCRC_DDC_DATA",
    "DDCRC_READ_ALL_ZERO",
    "Error reading",
    "retry",
)


# --- monitor connector filtering -------------------------------------------
# We only support directly-attached DisplayPort / HDMI / USB-C DP-Alt panels.
# USB-C DP-Alt monitors enumerate as a normal "DP-n" DRM connector, so matching
# DP + HDMI covers them. Everything else (eDP laptop panels, DSI, LVDS) is out.
_ACCEPT_CONNECTOR = re.compile(r"-(DP|HDMI)-", re.IGNORECASE)
_EXCLUDE_CONNECTOR = re.compile(r"eDP|LVDS|DSI", re.IGNORECASE)


@dataclass
class Monitor:
    display_num: int
    bus: int
    connector: str
    mfg: str
    model: str
    serial: str

    @property
    def label(self) -> str:
        name = self.model or f"Display {self.display_num}"
        return f"{name}" + (f"  ({self.serial})" if self.serial else "")

    @property
    def short_connector(self) -> str:
        """e.g. 'card1-DP-3' -> 'DP-3'."""
        return re.sub(r"^card\d+-", "", self.connector)

    @property
    def is_dell(self) -> bool:
        """True if the EDID says this is a Dell panel ('DEL' PNP mfg id)."""
        return self.mfg.strip().upper() == "DEL" or self.model.strip().upper().startswith("DELL")

    @property
    def vendor(self) -> str:
        """Best-effort human vendor name for messages."""
        return self.mfg.strip() or "Unknown"

    @property
    def tab_label(self) -> str:
        name = self.model or f"Display {self.display_num}"
        return f"{name} · {self.short_connector}"


@dataclass
class VcpReading:
    """Result of a single ``getvcp``."""

    code: int
    kind: str  # 'continuous' | 'simple' | 'complex'
    value: int = 0          # current value (for continuous; sl byte for enums)
    maximum: int = 0        # only meaningful when kind == 'continuous'
    raw_bytes: list = field(default_factory=list)


def _run(args: list[str], timeout: int = 25, retries: int = 1) -> str:
    if shutil.which("ddcutil") is None:
        raise DDCError("ddcutil is not installed or not in PATH")
    try:
        proc = subprocess.run(
            ["ddcutil", *args],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        raise DDCError(f"ddcutil timed out: {' '.join(args)}")
    if proc.returncode != 0:
        msg = (proc.stderr or proc.stdout or "").strip()
        if retries > 0 and any(t in msg for t in _TRANSIENT):
            time.sleep(0.25)
            return _run(args, timeout=timeout, retries=retries - 1)
        raise DDCError(msg or f"ddcutil exited with code {proc.returncode}")
    return proc.stdout


# --- detection --------------------------------------------------------------
def detect_monitors() -> list[Monitor]:
    """Return the list of supported, directly-attached monitors.

    Skips "Invalid display" blocks (which is what ddcutil emits for laptop eDP
    panels and many MST/dock situations) and filters by connector type.
    """
    out = _run(["detect", "--terse"])
    monitors: list[Monitor] = []

    for block in re.split(r"\n\s*\n", out):
        block = block.strip("\n")
        if not block:
            continue
        header = block.splitlines()[0].strip()
        if not header.lower().startswith("display"):
            continue  # skip "Invalid display" etc.

        m_num = re.search(r"display\s+(\d+)", header, re.IGNORECASE)
        m_bus = re.search(r"/dev/i2c-(\d+)", block)
        m_con = re.search(r"DRM connector:\s*(\S+)", block)
        m_mon = re.search(r"Monitor:\s*([^\n]*)", block)

        if not (m_num and m_bus and m_con):
            continue

        connector = m_con.group(1)
        if _EXCLUDE_CONNECTOR.search(connector) or not _ACCEPT_CONNECTOR.search(connector):
            continue

        mfg = model = serial = ""
        if m_mon:
            parts = [p.strip() for p in m_mon.group(1).split(":")]
            parts += [""] * (3 - len(parts))
            mfg, model, serial = parts[0], parts[1], parts[2]

        monitors.append(
            Monitor(
                display_num=int(m_num.group(1)),
                bus=int(m_bus.group(1)),
                connector=connector,
                mfg=mfg,
                model=model,
                serial=serial,
            )
        )
    return monitors


def run_detect() -> str:
    """Raw full (non-terse) ``ddcutil detect`` output. Captured once and reused for
    every monitor's Information tab, so we don't re-run this slow call per monitor."""
    try:
        return _run(["detect"])
    except DDCError:
        return ""


# --- capabilities -----------------------------------------------------------
def get_capabilities(bus: int) -> dict[int, list[int] | None]:
    """Parse the monitor's raw VESA capability string.

    Returns ``{vcp_code: [supported_values] | None}`` where ``None`` means the
    feature advertised no value list (continuous or opaque), and ``[]`` means it
    advertised an empty list (e.g. ``63()`` — feature present but no options).
    """
    out = _run(["--bus", str(bus), "capabilities", "--terse"])
    m = re.search(r"Unparsed capabilities string:\s*(.*)", out)
    raw = m.group(1) if m else out

    vcp_body = _extract_paren_group(raw, "vcp")
    if vcp_body is None:
        raise DDCError("could not find vcp(...) section in capability string")

    features: dict[int, list[int] | None] = {}
    for code_hex, vals in re.findall(r"([0-9A-Fa-f]{2})(?:\(([^)]*)\))?", vcp_body):
        code = int(code_hex, 16)
        if vals is None:
            features[code] = None
        else:
            vals = vals.strip()
            features[code] = [int(v, 16) for v in vals.split()] if vals else []
    return features


def _extract_paren_group(text: str, key: str) -> str | None:
    """Return the balanced contents of ``key(...)`` within ``text``."""
    idx = text.find(key + "(")
    if idx < 0:
        return None
    start = idx + len(key) + 1
    depth = 1
    i = start
    while i < len(text) and depth > 0:
        if text[i] == "(":
            depth += 1
        elif text[i] == ")":
            depth -= 1
        i += 1
    return text[start : i - 1]


# --- read / write a single feature -----------------------------------------
def _parse_vcp_reading(parts: list[str], code: int) -> VcpReading:
    """Parse one whitespace-split 'VCP NN ...' terse line into a VcpReading.
    Formats:
      VCP 10 C 75 100                 (continuous: current, max)
      VCP 14 SNC x05                  (simple non-continuous: single value)
      VCP E2 CNC x00 xff x00 x00      (complex: mh ml sh sl -> we use sl)
    """
    kind_tok = parts[2]
    if kind_tok == "C":
        return VcpReading(code=code, kind="continuous",
                          value=int(parts[3]), maximum=int(parts[4]))
    hexvals = [int(p.lstrip("xX"), 16) for p in parts[3:] if p.lower().startswith("x")]
    if kind_tok in ("SNC", "NC"):
        return VcpReading(code=code, kind="simple", value=hexvals[0] if hexvals else 0,
                          raw_bytes=hexvals)
    # CNC / anything else: the low byte (sl, last) carries the selection.
    return VcpReading(code=code, kind="complex", value=hexvals[-1] if hexvals else 0,
                      raw_bytes=hexvals)


def get_vcp(bus: int, code: int) -> VcpReading:
    out = _run(["--bus", str(bus), "getvcp", f"{code:02X}", "--terse"]).strip()
    parts = out.split()
    if len(parts) < 3 or parts[0] != "VCP":
        raise DDCError(f"unexpected getvcp output: {out!r}")
    return _parse_vcp_reading(parts, code)


def get_vcp_many(bus: int, codes, timeout: int = 40) -> dict[int, VcpReading]:
    """Read several VCP codes in ONE ddcutil invocation (amortises ddcutil's
    per-call init — measured ~4-5x faster than one getvcp per code on a direct
    monitor, and far fewer bus transactions, so gentler on flaky DDC controllers).

    Codes that error or don't return a parseable line are simply omitted from the
    result, so callers can just do ``values.get(code)`` exactly as before. A whole-
    batch failure/timeout returns ``{}`` (the monitor is then treated as unreadable
    and skipped, same as a per-code failure would have been)."""
    codes = list(codes)
    if not codes:
        return {}
    args = ["--bus", str(bus), "getvcp", *[f"{c:02X}" for c in codes], "--terse"]
    try:
        out = _run(args, timeout=timeout)
    except DDCError:
        return {}
    result: dict[int, VcpReading] = {}
    for line in out.splitlines():
        parts = line.split()
        if len(parts) < 3 or parts[0] != "VCP" or parts[2] == "ERR":
            continue
        try:
            code = int(parts[1], 16)
            result[code] = _parse_vcp_reading(parts, code)
        except (ValueError, IndexError):
            continue
    return result


def set_vcp(bus: int, code: int, value: int) -> None:
    # --noverify: we read the value back ourselves (see gui._set_and_verify) so
    # we control the pass/fail UX. ddcutil's own verify is too strict for panels
    # that quantise continuous values or reflect complex/manufacturer codes.
    _run(["--bus", str(bus), "setvcp", "--noverify", f"{code:02X}", str(value)])


def set_vcp_word(bus: int, code: int, word: int) -> None:
    """Write a 16-bit value (high byte = sub-code, low byte = value) to a VCP
    code. Used for Dell two-level features like 0xEA USB-C Prioritization
    (e.g. 0xF801 = sub-code 0xF8, value 0x01). ddcutil splits the number into
    SH:SL. --noverify because two-level codes don't read back meaningfully."""
    _run(["--bus", str(bus), "setvcp", "--noverify", f"{code:02X}", f"0x{word:04X}"])


def _full_word(reading: "VcpReading") -> int:
    """Reconstruct the full 16-bit SH:SL value of a complex reading. ``get_vcp``
    reports only the low byte (sl) in ``.value``; bitmask registers like a
    "new-spec" 0xEF keep state in the high byte too (support bits 12-15), so a
    read-modify-write must operate on the whole word."""
    rb = reading.raw_bytes
    if rb and len(rb) >= 2:
        return (rb[-2] << 8) | rb[-1]  # sh:sl
    return reading.value


def get_vcp_word(bus: int, code: int) -> int:
    """Read a VCP code and return its full 16-bit SH:SL value (not just the low
    byte). Used for two-level word codes that read back meaningfully, e.g. the
    USB-KVM upstream association 0xE7 (reads back 0xFF0N)."""
    return _full_word(get_vcp(bus, code))


def set_vcp_bit(bus: int, code: int, bit: int, on: bool) -> int:
    """Read-modify-write a single bit of a VCP value, preserving the others.
    Used for Dell bitmask registers like 0xEF (MST = bit 4). Operates on the full
    16-bit SH:SL word (matching DDPM's SetMST model, which preserves the high-byte
    support bits) and writes it back as a word. Returns the full word read back
    afterwards so the caller can verify the bit took.

    NB: only reached for "new-spec" 0xEF monitors (see features.has_ddc_mst_control);
    old-spec monitors are OSD-only and never call this. The new-spec path is not
    hardware-verified (no such monitor available to test)."""
    cur = _full_word(get_vcp(bus, code))
    new = (cur | (1 << bit)) if on else (cur & ~(1 << bit))
    set_vcp_word(bus, code, new)
    return _full_word(get_vcp(bus, code))


_readonly_cache: dict[int, bool] = {}


def is_read_only(code: int) -> bool:
    """True if the MCCS spec marks this feature read-only (e.g. 0xAA orientation).

    Uses static ``ddcutil vcpinfo`` metadata (no monitor I/O). Manufacturer codes
    with no metadata are treated as *not* read-only — their writability can only
    be judged by attempting a write and reading it back.
    """
    if code in _readonly_cache:
        return _readonly_cache[code]
    try:
        out = _run(["vcpinfo", f"{code:02X}"], timeout=10)
    except DDCError:
        out = ""
    attr_lines = [ln for ln in out.splitlines() if "Attributes" in ln]
    has_write = any("Write" in ln for ln in attr_lines)
    has_ro = any("Read Only" in ln for ln in attr_lines)
    result = bool(attr_lines) and has_ro and not has_write
    _readonly_cache[code] = result
    return result


# --- read-only monitor identity / status (for the Information tab) ----------
_MFG_NAMES = {"DEL": "Dell"}
_TECH_TYPE = {  # VCP 0xB6 Display technology type (sl byte)
    0x01: "CRT (shadow mask)", 0x02: "CRT (aperture grille)",
    0x03: "LCD (active matrix)", 0x04: "LCoS", 0x05: "Plasma",
    0x06: "OLED", 0x07: "EL", 0x08: "MEM",
}


def get_monitor_info(mon: "Monitor", detect_text: str = "") -> list[tuple[str, str]]:
    """Return ordered (label, value) rows describing a monitor: EDID identity
    plus read-only DDC status. All reads, never writes. Missing fields are
    silently skipped so the Information tab only shows what the panel reports.

    Pass ``detect_text`` (from ``run_detect()``) to reuse one shared ``ddcutil
    detect`` for every monitor instead of re-running it per monitor."""
    info: list[tuple[str, str]] = []

    # --- EDID identity, parsed from a full (non-terse) detect block ----------
    edid: dict[str, str] = {}
    out = detect_text or run_detect()
    for block in re.split(r"\n\s*\n", out):
        if f"/dev/i2c-{mon.bus}" not in block:
            continue
        for key, pat in (
            ("mfg", r"Mfg id:\s*(.+)"),
            ("model", r"Model:\s*(.+)"),
            ("product", r"Product code:\s*(.+)"),
            ("serial", r"Serial number:\s*(.+)"),
            ("mfg_date", r"Manufacture year:\s*(.+)"),
            ("mccs", r"VCP version:\s*(.+)"),
        ):
            m = re.search(pat, block)
            if m:
                edid[key] = m.group(1).strip()
        break

    brand = _MFG_NAMES.get(mon.mfg.strip().upper())
    if not brand and edid.get("mfg") and " - " in edid["mfg"]:
        brand = edid["mfg"].split(" - ", 1)[1].strip()
    info.append(("Brand", brand or mon.mfg or "Unknown"))

    # Model name: strip the "DELL " prefix from the EDID model (avoids a second
    # slow `capabilities` call just to read model(...) — same result).
    model_code = (mon.model or edid.get("model", "")).strip()
    for token in ("DELL ", "Dell ", "dell "):
        if model_code.startswith(token):
            model_code = model_code[len(token):].strip()
    info.append(("Model", model_code or "—"))
    if edid.get("product"):
        info.append(("Product code", edid["product"]))
    if mon.serial or edid.get("serial"):
        info.append(("Serial number", mon.serial or edid.get("serial", "")))
    if edid.get("mfg_date"):
        info.append(("Manufactured", edid["mfg_date"]))
    info.append(("Connection", mon.short_connector))
    info.append(("I2C bus", f"/dev/i2c-{mon.bus}"))
    if edid.get("mccs"):
        info.append(("DDC/CI (MCCS) version", edid["mccs"]))

    # --- read-only DDC status codes ------------------------------------------
    # One non-terse getvcp for all four (keeps ddcutil's decoded controller/
    # firmware strings), then split the output into per-code chunks so the same
    # parsing as before applies to each.
    chunks: dict[str, str] = {}
    try:
        combined = _run(["--bus", str(mon.bus), "getvcp", "B6", "C8", "C9", "C0"])
        cur = None
        for line in combined.splitlines():
            mc = re.match(r"\s*VCP code 0x([0-9a-fA-F]{2})", line)
            if mc:
                cur = mc.group(1).upper()
                chunks[cur] = line
            elif cur is not None:
                chunks[cur] += "\n" + line
    except DDCError:
        pass

    def _read(code: str) -> str:
        return chunks.get(code.upper(), "").strip()

    b6 = _read("B6")  # panel technology
    mt = re.search(r"sl=0x([0-9a-fA-F]{2})", b6) or re.search(r"x([0-9a-fA-F]{2})\b", b6)
    if mt:
        info.append(("Panel technology",
                     _TECH_TYPE.get(int(mt.group(1), 16), f"type 0x{mt.group(1)}")))

    c8 = _read("C8")  # display controller — keep just the manufacturer
    mc = re.search(r"Mfg:\s*([A-Za-z0-9 ]+?)(?:\s*\(| )", c8)
    if mc and mc.group(1).strip().lower() not in ("unknown", ""):
        info.append(("Controller", mc.group(1).strip()))

    c9 = _read("C9")  # firmware level (ddcutil already decodes e.g. "65.1")
    fm = re.search(r":\s*([0-9.]+)\s*$", c9.splitlines()[-1]) if c9 else None
    if fm:
        info.append(("Firmware level", fm.group(1)))

    c0 = _read("C0")  # display usage / power-on time
    pm = re.search(r"=\s*(\d+)", c0)
    if pm:
        info.append(("Power-on time", f"{int(pm.group(1)):,} hours"))

    return info
