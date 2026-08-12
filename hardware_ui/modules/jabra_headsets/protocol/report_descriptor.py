"""Minimal HID report-descriptor parser.

Only enough to answer one question: which report id carries a given usage page/usage, and how
many bytes does it hold? Jabra puts the GNP tunnel on vendor usage page 0xFF00 usage 0x01, but
the report id and size differ per product (Link 390 dongle: report 0x05, 63 bytes; Evolve2 85
deskstand: report 0x05, 32 bytes), so neither may be hardcoded.

Report descriptor item format: HID 1.11 §6.2.2.
"""
from __future__ import annotations

from dataclasses import dataclass

TAG_USAGE_PAGE = 0x04
TAG_USAGE = 0x08
TAG_REPORT_ID = 0x84
TAG_REPORT_SIZE = 0x74
TAG_REPORT_COUNT = 0x94
TAG_INPUT = 0x80
TAG_OUTPUT = 0x90
TAG_FEATURE = 0xB0

_SIZE_BY_PREFIX = {0: 0, 1: 1, 2: 2, 3: 4}


@dataclass(frozen=True)
class ReportField:
    usage_page: int
    usage: int
    report_id: int
    direction: str        # "input" | "output" | "feature"
    size_bytes: int


def _items(desc: bytes):
    i = 0
    while i < len(desc):
        prefix = desc[i]
        if prefix == 0xFE:                      # long item — Jabra does not use these
            size = desc[i + 1]
            yield desc[i + 2], desc[i + 3:i + 3 + size]
            i += 3 + size
            continue
        size = _SIZE_BY_PREFIX[prefix & 0x03]
        yield prefix & 0xFC, desc[i + 1:i + 1 + size]
        i += 1 + size


def parse(desc: bytes) -> list[ReportField]:
    """All data fields in the descriptor, one entry per (page, usage, report, direction)."""
    page = report_id = 0
    bit_size = count = 0
    usages: list[int] = []
    fields: list[ReportField] = []

    for tag, data in _items(desc):
        value = int.from_bytes(data, "little") if data else 0
        if tag == TAG_USAGE_PAGE:
            page = value
        elif tag == TAG_REPORT_ID:
            report_id = value
        elif tag == TAG_REPORT_SIZE:
            bit_size = value
        elif tag == TAG_REPORT_COUNT:
            count = value
        elif tag == TAG_USAGE:
            usages.append(value)
        elif tag in (TAG_INPUT, TAG_OUTPUT, TAG_FEATURE):
            direction = {TAG_INPUT: "input", TAG_OUTPUT: "output",
                         TAG_FEATURE: "feature"}[tag]
            total_bytes = (bit_size * count) // 8
            for usage in usages or [0]:
                fields.append(
                    ReportField(page, usage, report_id, direction, total_bytes)
                )
            usages = []
        elif tag in (0xA0, 0xC0):               # Collection / EndCollection
            usages = []

    return fields


def find_report(
    desc: bytes, usage_page: int, usage: int, direction: str = "output"
) -> ReportField | None:
    """The report carrying `usage_page`/`usage`, or None if the device lacks it."""
    for field in parse(desc):
        if (
            field.usage_page == usage_page
            and field.usage == usage
            and field.direction == direction
            and field.size_bytes > 0
        ):
            return field
    return None
