"""Dell dock module: read-only, and one row per physical dock.

No hardware and no fwupd needed — the sysfs shapes are synthesised from a real WD22TB4.
"""

from __future__ import annotations

from hardware_ui.core import Kind
from hardware_ui.modules.dell_docks import capabilities as C


def build(**kw):
    return C.build(**kw)


def keys(page) -> list[str]:
    return [c.key for c in page]


def test_every_row_is_read_only():
    """The module is informational by design: firmware belongs to fwupd, and the settings people
    want are system-firmware attributes rather than dock state."""
    page = build(identity=["model", "serial"], link=["connection"], firmware=["WD22TB4"])
    assert all(c.kind is Kind.READOUT for c in page if c.kind is not Kind.ACTION)
    assert not any(c.writable for c in page if c.kind is Kind.READOUT)


def test_the_only_action_is_a_re_read():
    page = build(identity=["model"])
    actions = [c for c in page if c.kind is Kind.ACTION]
    assert [c.key for c in actions] == [C.REFRESH_KEY]


def test_the_first_row_explains_why_nothing_can_be_changed():
    note = build(identity=["model", "serial"]).by_key("identity.model").note
    assert "information only" in note
    assert "fwupd" in note
    assert "system firmware" in note


def test_a_usb_dock_gets_no_thunderbolt_section():
    page = build(identity=["model"], link=["connection"])
    assert "link.connection" in keys(page)
    assert not any(k.startswith("link.tb_") for k in keys(page))


def test_a_thunderbolt_dock_gets_its_own_rows():
    page = build(identity=["model"],
                 link=["connection", "tb_generation", "tb_authorised", "tb_security"])
    assert "link.tb_generation" in keys(page)
    assert page.by_key("link.tb_authorised").label == "Authorised"


def test_no_fwupd_means_no_firmware_section():
    """fwupd is optional. A row that never has a value is noise, so the section is simply absent."""
    page = build(identity=["model"], link=["connection"], firmware=[])
    assert not any(k.startswith("firmware.") for k in keys(page))


def test_firmware_rows_are_named_after_the_component():
    page = build(identity=["model"], firmware=["RTS5413 in Dell dock", "VMM5331 in Dell dock"])
    assert page.by_key("firmware.rts5413_in_dell_dock").label == "RTS5413 in Dell dock"
    assert "fwupd" in page.by_key("firmware.rts5413_in_dell_dock").note


def test_security_levels_are_explained_not_just_named():
    """"user" alone means nothing to a reader; what it does is the useful part."""
    assert "approve each device" in C.SECURITY_LEVELS["user"]
    assert "cryptographically" in C.SECURITY_LEVELS["secure"]
    assert set(C.SECURITY_LEVELS) >= {"none", "user", "secure", "dponly"}


def test_a_dock_that_writes_is_refused_at_the_module_level():
    import asyncio

    from hardware_ui.core import DeviceInfo, NotSupported, Transport
    from hardware_ui.modules.dell_docks.device import DellDock

    dock = DellDock(DeviceInfo(uid="hid:x", name="Dell dock", transport=Transport.HID))
    try:
        asyncio.run(dock.set("identity.model", "x"))
    except NotSupported as exc:
        assert "information only" in str(exc)
    else:
        raise AssertionError("a write should be refused")
