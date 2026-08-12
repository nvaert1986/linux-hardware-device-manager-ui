"""hardware-ui core: device model, capability schema, discovery and vendor assets.

Deliberately free of any Qt import. The CLI, the tests and any future frontend use this package
headless; only :mod:`hardware_ui.shell` knows about Qt.
"""

from __future__ import annotations

from . import photos
from .assets import (
    AcquireUI,
    AssetError,
    AssetSource,
    AssetStatus,
    ExtractInstaller,
    Progress,
    Provenance,
    RegistryFetch,
    ToolMissing,
    safe_extract,
)
from .capability import (
    Advisory,
    Capability,
    CapabilitySet,
    CapabilityValue,
    Choice,
    Kind,
    PromptField,
    Tier,
)
from .connection import ConnectionLabel
from .device import (
    Category,
    DependencyMissing,
    Device,
    DeviceError,
    DeviceInfo,
    NotSupported,
    State,
    Support,
    Transport,
    Unreachable,
)
from .interaction import SILENT, Interaction, Silent
from .modules import Enablement, MatchRule, ModuleManifest, ModuleRegistry, VendorAssets

__all__ = [
    "Advisory",
    "AcquireUI",
    "AssetError",
    "AssetSource",
    "AssetStatus",
    "Capability",
    "CapabilitySet",
    "CapabilityValue",
    "Category",
    "Choice",
    "SILENT",
    "ConnectionLabel",
    "Interaction",
    "Silent",
    "PromptField",
    "Device",
    "DependencyMissing",
    "DeviceError",
    "DeviceInfo",
    "Enablement",
    "ExtractInstaller",
    "Kind",
    "photos",
    "MatchRule",
    "ModuleManifest",
    "ModuleRegistry",
    "NotSupported",
    "Progress",
    "Provenance",
    "RegistryFetch",
    "State",
    "Support",
    "Tier",
    "ToolMissing",
    "Transport",
    "Unreachable",
    "VendorAssets",
    "safe_extract",
]

__version__ = "0.10"
