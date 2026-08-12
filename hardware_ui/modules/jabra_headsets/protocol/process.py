"""Interpreter for the property catalogue's process pipelines.

Each property in `data/properties.json` is a small program: a list of *operations* over named
variables, where the operations that touch the device (`gnpRead`, `gnpWrite`, `gnpEvent`) carry
a *byte converter*, and pure value transforms carry *json converters*.

Implementing this interpreter once yields all 423 properties, instead of hand-coding a getter
and setter per setting. The type names and their fields come from `properties-schema.json`, so
this is a reader of a published format rather than a guess.

Variables are a flat namespace with dotted-path reads (`eventData.subcommands`). `return` holds
the property value by convention; `nowhere` is the catalogue's idiom for "no input".
"""
from __future__ import annotations

import logging
import re
import time
from typing import Any

log = logging.getLogger(__name__)

RETURN = "return"
NOWHERE = "nowhere"

#: Operations that exchange bytes with the device.
DEVICE_STEPS = frozenset({"gnpRead", "gnpWrite", "gnpEvent", "gnpSendEvent"})
#: The BLE transport. Present in the catalogue, irrelevant to USB HID.
GATT_STEPS = frozenset({"gattRead", "gattWrite", "gattEvent", "gattSubscribe"})

_NET_FORMAT = re.compile(r"\{0:([A-Za-z])(\d*)\}")


class ProcessError(RuntimeError):
    """The pipeline could not be executed."""


class UnsupportedStep(ProcessError):
    """A step type this interpreter does not implement."""


# ---------------------------------------------------------------------------------------------
# byte converters
# ---------------------------------------------------------------------------------------------

def decode_bytes(convert: dict[str, Any] | None, raw: bytes) -> Any:
    """bytes -> value, per a byteConverter."""
    if convert is None:
        return raw
    kind = convert.get("typeName", "number")

    if kind == "number":
        size = convert["sizeByte"]
        order = "little" if convert["isLittleEndian"] else "big"
        if len(raw) < size:
            raise ProcessError(f"need {size} bytes for a number, got {len(raw)}")
        return int.from_bytes(raw[:size], order, signed=convert["isSigned"])

    if kind == "string":
        body = raw
        if convert.get("isLengthPrefixed"):
            if not raw:
                return ""
            length = raw[0]
            body = raw[1:1 + length] if length <= len(raw) - 1 else raw[1:]
        text = body.decode(convert.get("encoding", "utf-8"), errors="replace")
        if convert.get("isNullTerminated"):
            text = text.split("\x00", 1)[0]
        return text.rstrip("\x00")

    if kind == "list":
        inner = convert["convert"]
        body = raw
        count = None
        if convert.get("isLengthPrefixed"):
            if not raw:
                return []
            count, body = raw[0], raw[1:]
        item_size = inner.get("sizeByte", 1)
        items = [
            decode_bytes(inner, body[i:i + item_size])
            for i in range(0, len(body) - item_size + 1, item_size)
        ]
        return items[:count] if count is not None else items

    if kind == "jsonObject":
        result: dict[str, Any] = {}
        offset = 0
        for field in convert["fields"]:
            inner = field["convert"]
            inner_kind = inner.get("typeName")
            if inner_kind == "skip":
                offset += inner.get("sizeByte", 1)
                continue
            if inner_kind == "match":
                expected = inner["value"]
                if offset >= len(raw):
                    raise ProcessError(f"{field['fieldName']}: ran out of bytes for match")
                if raw[offset] != expected:
                    raise ProcessError(
                        f"{field['fieldName']}: expected 0x{expected:02X}, "
                        f"got 0x{raw[offset]:02X}"
                    )
                result[field["fieldName"]] = expected
                offset += 1
                continue
            if inner_kind == "list":
                # A list consumes the remainder, so it must be the last field.
                result[field["fieldName"]] = decode_bytes(inner, raw[offset:])
                offset = len(raw)
                continue
            size = inner.get("sizeByte")
            chunk = raw[offset:offset + size] if size else raw[offset:]
            result[field["fieldName"]] = decode_bytes(inner, chunk)
            offset += size if size else len(chunk)
        return result

    raise UnsupportedStep(f"byte converter {kind!r}")


def encode_bytes(convert: dict[str, Any] | None, value: Any) -> bytes:
    """value -> bytes, per a byteConverter."""
    if convert is None:
        return value if isinstance(value, bytes) else bytes([int(value)])
    kind = convert.get("typeName", "number")

    if kind == "number":
        order = "little" if convert["isLittleEndian"] else "big"
        return int(value).to_bytes(convert["sizeByte"], order,
                                   signed=convert["isSigned"])

    if kind == "string":
        body = str(value).encode(convert.get("encoding", "utf-8"))
        if convert.get("isNullTerminated"):
            body += b"\x00"
        if convert.get("isLengthPrefixed"):
            body = bytes([len(body)]) + body
        return body

    if kind == "list":
        inner = convert["convert"]
        body = b"".join(encode_bytes(inner, item) for item in value)
        return (bytes([len(value)]) + body) if convert.get("isLengthPrefixed") else body

    if kind == "jsonObject":
        # Fields are written in declaration order. `match` carries a fixed value the device
        # expects (e.g. a sub-sub-command selector), and `skip` pads — neither comes from the
        # caller. Needed by properties such as autoMuteCallAudio, whose payload is
        # {subSubCmd, param} rather than a bare number.
        if not isinstance(value, dict):
            raise ProcessError(
                f"jsonObject expects a dict of fields, got {type(value).__name__}"
            )
        out = bytearray()
        for field in convert["fields"]:
            name = field["fieldName"]
            inner = field["convert"]
            inner_kind = inner.get("typeName")
            if inner_kind == "match":
                out.append(inner["value"] & 0xFF)
                continue
            if inner_kind == "skip":
                out.extend(b"\x00" * inner.get("sizeByte", 1))
                continue
            if name not in value:
                raise ProcessError(f"jsonObject is missing field {name!r}")
            out.extend(encode_bytes(inner, value[name]))
        return bytes(out)

    raise UnsupportedStep(f"byte converter {kind!r} (encode)")


# ---------------------------------------------------------------------------------------------
# json converters
# ---------------------------------------------------------------------------------------------

def _net_format(spec: str, value: Any) -> str:
    """Translate a .NET format string such as "{0:X2}" to Python's."""
    def repl(match: re.Match) -> str:
        letter, width = match.group(1), match.group(2)
        if letter in "Xx":
            return f"{{0:0{width or ''}{letter}}}"
        if letter in "Dd":
            return f"{{0:0{width or ''}d}}"
        return "{0}"
    return _NET_FORMAT.sub(repl, spec).format(value)


def apply_converters(converters: Any, value: Any) -> Any:
    """Apply a jsonConverter, or a list of them in order."""
    if converters is None:
        return value
    if isinstance(converters, dict):
        converters = [converters]
    for convert in converters:
        value = _apply_converter(convert, value)
    return value


def _apply_converter(convert: dict[str, Any], value: Any) -> Any:
    kind = convert.get("typeName")

    if kind == "constant":
        return convert["value"]

    if kind == "translate":
        mapping = convert["values"]           # {name: int}
        if convert["direction"] == "toValue":
            for name, number in mapping.items():
                if number == value:
                    return _as_bool(name)
            log.debug("translate: no name for %r in %s", value, mapping)
            return value
        # toInt
        key = _as_key(value)
        if key in mapping:
            return mapping[key]
        raise ProcessError(f"translate: {value!r} is not one of {sorted(mapping)}")

    if kind == "scale":
        return value * convert["by"]

    if kind == "bitmaskExtract":
        return (int(value) >> convert["offset"]) & ((1 << convert["bitCount"]) - 1)

    if kind == "join":
        separator = convert.get("separator", "")
        fmt = convert.get("format")
        parts = [(_net_format(fmt, v) if fmt else str(v)) for v in value]
        return separator.join(parts)

    if kind == "map":
        return [apply_converters(convert["convert"], item) for item in value]

    raise UnsupportedStep(f"json converter {kind!r}")


def _as_bool(name: str) -> Any:
    """Catalogue boolean mappings use the strings "true"/"false" as keys."""
    if name == "true":
        return True
    if name == "false":
        return False
    return name


def _as_key(value: Any) -> str:
    if value is True:
        return "true"
    if value is False:
        return "false"
    return str(value)


# ---------------------------------------------------------------------------------------------
# the interpreter
# ---------------------------------------------------------------------------------------------

class Context:
    """Named variables, with dotted-path reads."""

    def __init__(self, **initial: Any):
        self._vars: dict[str, Any] = dict(initial)

    def get(self, path: str) -> Any:
        if path == NOWHERE:
            return None
        head, _, rest = path.partition(".")
        if head not in self._vars:
            raise ProcessError(f"variable {head!r} is not set")
        value = self._vars[head]
        for part in filter(None, rest.split(".")):
            if not isinstance(value, dict) or part not in value:
                raise ProcessError(f"{path!r}: no field {part!r}")
            value = value[part]
        return value

    def set(self, path: str, value: Any) -> None:
        """Assign, creating intermediate dicts for a dotted path.

        Write pipelines build their payload field by field — `autoMuteCallAudio` assigns a
        constant `{subSubCmd: 0}` to `input`, then the translated value to `input.param`. So a
        plain top-level assignment is not enough.
        """
        head, _, rest = path.partition(".")
        if not rest:
            self._vars[head] = value
            return
        container = self._vars.get(head)
        if not isinstance(container, dict):
            container = {}
            self._vars[head] = container
        parts = rest.split(".")
        for part in parts[:-1]:
            nested = container.get(part)
            if not isinstance(nested, dict):
                nested = {}
                container[part] = nested
            container = nested
        container[parts[-1]] = value

    def has(self, name: str) -> bool:
        return name.partition(".")[0] in self._vars

    @property
    def result(self) -> Any:
        return self._vars.get(RETURN)

    def __repr__(self) -> str:
        return f"Context({self._vars!r})"


class Interpreter:
    """Runs catalogue pipelines against a GNP session.

    `session` needs `read(command, subcommand, address=...)` and
    `write(command, subcommand, payload, address=...)`. Pass None to run pipelines that never
    touch the device (useful for decoding a captured event payload).
    """

    def __init__(self, session=None, *, address: int | None = None,
                 timeout: float | None = None):
        self.session = session
        self.address = address
        #: Default per-request timeout; None means the session's own. Capability probing passes a
        #: short one so absent properties that never answer do not dominate the connect.
        self.timeout = timeout

    # -- entry points ----------------------------------------------------------------------

    def read(self, prop, *, timeout: float | None = None) -> Any:
        if not prop.read:
            raise ProcessError(f"{prop.name} is not readable")
        previous, self.timeout = self.timeout, (timeout if timeout is not None
                                                else self.timeout)
        try:
            context = Context()
            self._run(prop.read, context)
            return context.result
        finally:
            self.timeout = previous

    def write(self, prop, value: Any) -> None:
        if not prop.write:
            raise ProcessError(f"{prop.name} is not writable")
        context = Context(**{RETURN: value})
        self._run(prop.write, context)

    def decode_event(self, prop, payload: bytes) -> Any:
        """Decode an already-received event payload (subcommand byte removed)."""
        if not prop.event:
            raise ProcessError(f"{prop.name} has no event")
        context = Context()
        self._run(prop.event, context, event_payload=payload)
        return context.result

    def subscribe(self, prop) -> None:
        """Arm a property's event. Read-modify-write — see bitmaskInsert."""
        if not prop.subscribe:
            return
        self._run(prop.subscribe, Context())

    # -- execution -------------------------------------------------------------------------

    def _run(self, steps, context: Context, event_payload: bytes | None = None) -> None:
        for step in steps:
            self._step(step, context, event_payload)

    def _step(self, step: dict[str, Any], context: Context,
              event_payload: bytes | None) -> None:
        kind = step.get("typeName")

        if kind in GATT_STEPS:
            raise UnsupportedStep(
                f"{kind}: this property is only reachable over Bluetooth LE"
            )

        if kind == "gnpRead":
            raw = self._device_read(step, context)
            context.set(step.get("variableName", RETURN),
                        decode_bytes(step.get("convert"), raw))
            return

        if kind == "gnpWrite":
            name = step.get("variableName", RETURN)
            payload = encode_bytes(step.get("convert"), context.get(name))
            self._device_write(step, payload)
            return

        if kind == "gnpEvent":
            if event_payload is None:
                raise ProcessError("gnpEvent needs an event payload")
            context.set(step.get("variableName", RETURN),
                        decode_bytes(step.get("convert"), event_payload))
            return

        if kind == "hidInputReport":
            raise UnsupportedStep(
                "hidInputReport: this property arrives as a plain HID report on a "
                "telephony/vendor usage page, not through GNP"
            )

        if kind == "assign":
            source = step.get("from", RETURN)
            value = None if source == NOWHERE else context.get(source)
            context.set(step.get("to", RETURN),
                        apply_converters(step.get("convert"), value))
            return

        if kind == "assignIf":
            branch = step["then"] if context.get(step["condition"]) else step["else"]
            context.set(step["output"], context.get(branch))
            return

        if kind == "bitmaskInsert":
            width = step["bitCount"]
            offset = step["offset"]
            mask = ((1 << width) - 1) << offset
            target = int(context.get(step["target"])) if context.has(step["target"]) else 0
            inserted = (int(context.get(step["value"])) & ((1 << width) - 1)) << offset
            context.set(step["into"], (target & ~mask) | inserted)
            return

        if kind == "concat":
            first, second = context.get(step["first"]), context.get(step["second"])
            if isinstance(first, (bytes, bytearray)):
                context.set(step["output"], bytes(first) + bytes(second))
            elif isinstance(first, list):
                context.set(step["output"], list(first) + list(second))
            else:
                context.set(step["output"], f"{first}{second}")
            return

        if kind in ("equals", "notEquals"):
            same = context.get(step["operand1"]) == context.get(step["operand2"])
            context.set(step["output"], same if kind == "equals" else not same)
            return

        if kind == "intToString":
            context.set(step.get("output", RETURN), str(context.get(step.get("from", RETURN))))
            return

        if kind == "stringToInt":
            context.set(step.get("output", RETURN), int(context.get(step.get("from", RETURN))))
            return

        if kind == "wait":
            time.sleep(step["timeMs"] / 1000.0)
            return

        if kind == "gnpSendEvent":
            payload = encode_bytes(step.get("convert"),
                                   context.get(step.get("variableName", RETURN)))
            self._device_write(step, payload)
            return

        raise UnsupportedStep(f"operation {kind!r}")

    # -- device access ---------------------------------------------------------------------

    def _require_session(self, step: dict[str, Any]):
        if self.session is None:
            raise ProcessError(f"{step['typeName']} needs a live session")
        return self.session

    def _address_for(self, step: dict[str, Any]) -> int | None:
        """An explicit catalogue address wins; otherwise this interpreter's endpoint."""
        return step.get("address", self.address)

    def _device_read(self, step: dict[str, Any], context: Context) -> bytes:
        session = self._require_session(step)
        payload = b""
        request = step.get("requestConvert")
        if request:
            # A parameterised read — the request carries an argument (e.g. an index).
            payload = encode_bytes(request.get("convert"),
                                   context.get(request.get("inputName", RETURN)))
        reply = session.request(
            step["command"], step["subcommand"], payload,
            address=self._address_for(step), timeout=self.timeout,
        )
        data = reply.data
        return data[1:] if data and data[0] == step["subcommand"] else data

    def _device_write(self, step: dict[str, Any], payload: bytes) -> None:
        session = self._require_session(step)
        session.write(step["command"], step["subcommand"], payload,
                      address=self._address_for(step), timeout=self.timeout)
