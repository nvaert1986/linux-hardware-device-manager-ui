"""The application window.

Replaces Main.qml, Sidebar.qml and DevicePage.qml. Everything here is a stock widget, so Breeze
draws it: tabs, buttons, the device list, menus and dialogs all match System Settings without a
line of styling. That was the whole reason for moving off QML.

Structure mirrors the reference implementation's: a device list on the left, and on the right a
connection bar above a tab per capability group.
"""

from __future__ import annotations

import dataclasses
import logging
import secrets
from collections import Counter
from collections.abc import Sequence
from typing import Any

from PyQt6.QtCore import QPointF, Qt, pyqtSignal
from PyQt6.QtGui import QColor, QIcon, QPainter, QPen
from PyQt6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMenu,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSplitter,
    QStackedWidget,
    QStatusBar,
    QStyledItemDelegate,
    QTabWidget,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from hardware_ui.core import (
    CapabilitySet,
    DeviceInfo,
    Diagram,
    Kind,
    PromptField,
    State,
    Support,
    Transport,
    connection,
)

from .form import CapabilityForm, build_forms

log = logging.getLogger(__name__)


class _DeviceDelegate(QStyledItemDelegate):
    """Draws the connection dot at the right of a device row.

    A delegate rather than a second icon: QListWidget has one decoration slot, already used for
    the device icon, and the dot needs to sit opposite it. Colour carries the meaning -- green
    reachable, red failed, hollow for paired-but-off -- which is the indicator the QML sidebar
    had and this one lost.
    """

    DOT = 8

    def paint(self, painter, option, index) -> None:  # noqa: ANN001 - Qt signature
        super().paint(painter, option, index)
        colour = index.data(Qt.ItemDataRole.UserRole + 1)
        if colour is None:
            return
        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        radius = self.DOT / 2
        centre = QPointF(
            option.rect.right() - self.DOT - 6,
            option.rect.center().y() + 1,
        )
        painter.setPen(QPen(QColor(colour), 1.4))
        filled = bool(index.data(Qt.ItemDataRole.UserRole + 2))
        painter.setBrush(QColor(colour) if filled else Qt.BrushStyle.NoBrush)
        painter.drawEllipse(centre, radius, radius)
        painter.restore()


DISCONNECTED = "DISCONNECTED DEVICES"


def _section(device: DeviceInfo) -> str:
    """Which heading a device sits under: its category, or the disconnected bucket.

    A switched-off headset is not "an audio device you can configure" -- BlueZ simply still
    remembers it. Grouping by reachability first keeps every category heading unique.
    """
    # UNKNOWN means "not scanned yet": every device for the first ~30 ms, while the cached list is
    # on screen and the live sweep runs. Filing those under DISCONNECTED made a normal startup look
    # like every device had gone missing, which is the opposite of what load_cache() promises.
    #
    # **But only where absence is unlikely.** A cached hidraw or DRM device is almost certainly
    # still plugged in. A cached *Bluetooth* device usually is not: BlueZ remembers every headset
    # ever paired, and most of them are switched off. Treating those as settled put two powered-off
    # WH-1000X headsets under AUDIO as though they were ready to configure -- correcting one wrong
    # heading by inventing another.
    unscanned = device.state is State.UNKNOWN and device.transport not in (
        Transport.BLUETOOTH, Transport.BLE
    )
    settled = device.ready or unscanned
    # The enum value doubles as the heading, so a two-word category is spelt with an underscore
    # and reads as two words here: "security_keys" -> "SECURITY KEYS".
    return device.category.value.replace("_", " ").upper() if settled else DISCONNECTED


def _connector_label(device: DeviceInfo, *, disambiguate: bool = False) -> str:
    """The ``Connection:`` line for one row -- see :mod:`hardware_ui.core.connection`.

    *disambiguate* adds the device's identifier, and is set only when another visible row carries
    the same name. A serial number is what tells two identical devices apart and clutter on a row
    that is already unique, so it is shown exactly when it earns its place: the sidebar is the only
    thing that can see the whole list, so the decision belongs here rather than in a module.
    """
    label = device.connection or connection.from_connector(
        str(device.properties.get("connector", ""))
    )
    if not disambiguate:
        label = dataclasses.replace(label, identifier="")
    return label.display()


class Sidebar(QWidget):
    """Devices, grouped by category. Unreachable ones are dimmed and sorted last."""

    selected = pyqtSignal(str)
    rescanRequested = pyqtSignal()
    modulesRequested = pyqtSignal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        box = QVBoxLayout(self)
        box.setContentsMargins(0, 0, 0, 0)
        box.setSpacing(0)

        self._list = QListWidget()
        self._list.setFrameShape(QListWidget.Shape.NoFrame)
        self._list.setIconSize(
            self._list.iconSize().scaled(24, 24, Qt.AspectRatioMode.KeepAspectRatio)
        )
        self._list.setItemDelegate(_DeviceDelegate(self._list))
        self._list.itemSelectionChanged.connect(self._on_selection)
        box.addWidget(self._list, 1)

        footer = QWidget()
        row = QHBoxLayout(footer)
        row.setContentsMargins(8, 4, 8, 4)
        self._status = QLabel("")
        modules = QPushButton("Modules…")
        modules.setFlat(True)
        modules.setToolTip("Which device families this application looks for")
        modules.clicked.connect(self.modulesRequested)
        rescan = QPushButton("Rescan")
        rescan.setFlat(True)
        rescan.clicked.connect(self.rescanRequested)
        row.addWidget(self._status, 1)
        row.addWidget(modules)
        row.addWidget(rescan)
        box.addWidget(footer)

    def set_status(self, text: str) -> None:
        self._status.setText(text)

    def reconcile(self, devices: list[DeviceInfo]) -> None:
        """Rebuild the list, preserving the current selection where possible."""
        current = self.current_uid()
        self._list.blockSignals(True)
        self._list.clear()

        # Unreachable devices get their own heading rather than sinking to the bottom of the
        # list. Sorting them last while still heading them by category emitted AUDIO twice --
        # once for the live headset, once for the switched-off one -- which reads as a bug.
        ordered = sorted(
            devices,
            key=lambda d: (
                not d.supported, not d.ready, _section(d), d.name.casefold(), d.uid
            ),
        )
        # A name that appears twice is the reason the second line exists at all -- two P2425Ds,
        # two BT700 adapters. Everything else stays short.
        seen = Counter(d.name for d in ordered)
        section = ""
        for device in ordered:
            if _section(device) != section:
                section = _section(device)
                header = QListWidgetItem(section)
                header.setFlags(Qt.ItemFlag.NoItemFlags)
                self._list.addItem(header)
            self._list.addItem(self._item(device, seen[device.name] > 1))

        self._list.blockSignals(False)
        if current:
            self.select(current)

    def _item(self, device: DeviceInfo, disambiguate: bool = False) -> QListWidgetItem:
        subtitle = (
            "no module" if not device.supported
            else "connecting…" if device.state is State.CONNECTING
            else "unavailable" if device.state is State.FAILED
            else "connected" if device.state is State.CONNECTED
            # Paired but switched off: already under the DISCONNECTED heading, so the row itself
            # only needs to say something the heading does not.
            else "" if not device.ready
            else "untested model" if device.support is Support.FAMILY
            else ""
        )
        # Two identical monitors are the normal case, and their EDID names are identical too --
        # the connector is the only thing that says which physical panel a row is. The reference
        # implementation labels its tabs "model · DP-3" for exactly this reason.
        where = _connector_label(device, disambiguate=disambiguate)
        if where:
            subtitle = f"{where} · {subtitle}" if subtitle else where
        item = QListWidgetItem(QIcon.fromTheme(device.icon), f"{device.name}\n{subtitle}".strip())
        item.setData(Qt.ItemDataRole.UserRole, device.uid)

        # Dot colour and whether it is filled. Hollow grey means "paired but switched off":
        # present in the list, but nothing to talk to.
        if device.state is State.FAILED:
            item.setData(Qt.ItemDataRole.UserRole + 1, "#da4453")
            item.setData(Qt.ItemDataRole.UserRole + 2, True)
        elif device.state is State.CONNECTED:
            item.setData(Qt.ItemDataRole.UserRole + 1, "#27ae60")
            item.setData(Qt.ItemDataRole.UserRole + 2, True)
        elif not device.ready:
            item.setData(Qt.ItemDataRole.UserRole + 1, "#9aa0a6")
            item.setData(Qt.ItemDataRole.UserRole + 2, False)

        if not device.ready or not device.supported:
            item.setForeground(self.palette().placeholderText())
        return item

    def current_uid(self) -> str:
        items = self._list.selectedItems()
        return items[0].data(Qt.ItemDataRole.UserRole) or "" if items else ""

    def select(self, uid: str) -> None:
        for i in range(self._list.count()):
            item = self._list.item(i)
            if item.data(Qt.ItemDataRole.UserRole) == uid:
                self._list.setCurrentItem(item)
                return

    def _on_selection(self) -> None:
        uid = self.current_uid()
        if uid:
            self.selected.emit(uid)


class DevicePage(QWidget):
    """Connection bar plus one tab per capability group."""

    connectRequested = pyqtSignal()
    disconnectRequested = pyqtSignal()
    changed = pyqtSignal(str, object)
    triggered = pyqtSignal(str)
    copied = pyqtSignal(str)
    photoRequested = pyqtSignal()
    photoFetchRequested = pyqtSignal()
    photoCleared = pyqtSignal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._forms: dict[str, CapabilityForm] = {}

        box = QVBoxLayout(self)
        box.setContentsMargins(0, 0, 0, 0)

        bar = QWidget()
        row = QHBoxLayout(bar)
        row.setContentsMargins(12, 8, 12, 8)
        self._photo = QLabel()
        self._photo.setVisible(False)
        self._title = QLabel()
        font = self._title.font()
        font.setBold(True)
        font.setPointSizeF(font.pointSizeF() + 2)
        self._title.setFont(font)
        self._state = QLabel()

        self._photo_button = QPushButton("Add photo")
        self._photo_button.setFlat(True)
        self._photo_menu = QMenu(self)
        self._act_choose = self._photo_menu.addAction("Choose image…")
        self._act_fetch = self._photo_menu.addAction("Download from vendor")
        self._act_remove = self._photo_menu.addAction("Remove")
        self._act_choose.triggered.connect(self.photoRequested)
        self._act_fetch.triggered.connect(self.photoFetchRequested)
        self._act_remove.triggered.connect(self.photoCleared)
        self._photo_button.setMenu(self._photo_menu)

        self._connect_button = QPushButton("Connect")
        self._connect_button.clicked.connect(self._on_connect)

        row.addWidget(self._photo)
        row.addWidget(self._title)
        row.addSpacing(12)
        row.addWidget(self._state, 1)
        row.addWidget(self._photo_button)
        row.addWidget(self._connect_button)
        box.addWidget(bar)

        self._stack = QStackedWidget()
        self._placeholder = QLabel("Press Connect to open this device and read its settings.")
        self._placeholder.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._tabs = QTabWidget()
        self._tabs.setDocumentMode(True)
        self._stack.addWidget(self._placeholder)
        self._stack.addWidget(self._tabs)
        box.addWidget(self._stack, 1)

        self._connected = False
        self._device_name = ""

    # ------------------------------------------------------------------ state

    def set_device(self, name: str, untested: bool) -> None:
        # Kept so the connection copy can name the device. The shell serves headsets, monitors
        # and controllers from one page, so hardcoding "the headphones" here was wrong the moment
        # a second device family existed -- and it read as a bug, because it was one.
        self._device_name = name
        self._title.setText(name)
        self._photo_button.setEnabled(bool(name))
        if untested:
            self._state.setToolTip(
                "This model has not been verified. Some settings may be missing."
            )

    def set_connection(self, *, connected: bool, busy: bool, reconnecting: bool) -> None:
        self._connected = connected
        self._connect_button.setText("Disconnect" if connected else "Connect")
        self._connect_button.setEnabled(not busy and not reconnecting)
        subject = self._device_name or "This device"
        self._state.setText(
            "Restarting — reconnecting automatically…" if reconnecting
            else "Reading settings from the device…" if busy
            else "Connected" if connected
            else "Not connected"
        )
        if not connected:
            self._placeholder.setText(
                f"{subject} is restarting. This takes about 15 seconds."
                if reconnecting
                else f"Reading every setting from {subject}."
                if busy
                else "Press Connect to open this device and read its settings."
            )
            self._stack.setCurrentWidget(self._placeholder)

    def set_photo(self, path: str | None, fetchable: bool) -> None:
        from PyQt6.QtGui import QPixmap

        self._act_fetch.setEnabled(fetchable and self._connected)
        self._act_remove.setEnabled(bool(path))
        self._photo_button.setText("Change photo" if path else "Add photo")
        if not path:
            self._photo.setVisible(False)
            return
        pixmap = QPixmap(path)
        if pixmap.isNull():
            self._photo.setVisible(False)
            return
        self._photo.setPixmap(
            pixmap.scaledToHeight(32, Qt.TransformationMode.SmoothTransformation)
        )
        self._photo.setVisible(True)

    def show_capabilities(
        self, caps: CapabilitySet, diagrams: dict[str, Diagram] | None = None
    ) -> None:
        # tabText returns the label with its escaping intact, so compare like with like below.
        current = self._tabs.tabText(self._tabs.currentIndex()) if self._tabs.count() else ""
        self._tabs.clear()
        self._forms = build_forms(
            caps, self.changed.emit, self.triggered.emit, self.copied.emit, diagrams
        )
        for group, form in self._forms.items():
            # Two things a long page needs, both found on a Jabra Evolve2 85 whose 15 groups
            # include one with forty rows.
            #
            # The scroll area is what stops a tall tab dragging the window past the bottom of the
            # screen: without it the tab widget adopts the form's full height as its minimum, and
            # the window has no choice but to grow.
            scroll = QScrollArea()
            scroll.setWidgetResizable(True)
            scroll.setFrameShape(QFrame.Shape.NoFrame)
            scroll.setWidget(form)
            # Qt reads "&" in a tab label as a mnemonic and eats it, so "ANC & HearThrough" was
            # painted "ANC HearThrough". Doubling it prints the character.
            self._tabs.addTab(scroll, group.replace("&", "&&"))
        if not self._forms:
            self._stack.setCurrentWidget(self._placeholder)
            return
        self._stack.setCurrentWidget(self._tabs)
        for i in range(self._tabs.count()):
            if self._tabs.tabText(i) == current:
                self._tabs.setCurrentIndex(i)
                break

    def forms(self) -> dict[str, CapabilityForm]:
        return self._forms

    def publish(self, key: str, value: Any, *, confirmed: bool = False) -> None:
        """Announce a value to every tab, not only the one that draws it.

        A tab needs values it has no row for: to show one after somebody else's reading, and --
        the case this method exists for -- to resolve a ``requires`` that names a capability in
        another group. Direct Mode sits on the Sound tab and gates the whole Equalizer tab, and
        sending its new value only to the form that owns the checkbox left the equaliser greyed
        out until the next full refresh.
        """
        for form in self._forms.values():
            form.set_value(key, value, confirmed=confirmed)

    def publish_advisories(self, advisories: dict[str, Any]) -> None:
        """Give every tab the whole advisory map; each shows the one belonging to its own rows."""
        for form in self._forms.values():
            form.set_advisories(advisories)

    def all_keys(self) -> list[str]:
        """Every capability key on the page, across all tabs."""
        # noqa: SIM118 -- CapabilityForm.keys() is a method of ours, not a mapping's.
        return [key for form in self._forms.values() for key in form.keys()]  # noqa: SIM118

    def publish_pending(self, keys: list[str], key: str, value: Any) -> None:
        """Mark a write in flight on every tab, not only the one that owns the control.

        A device whose settings are written as one block has to freeze the whole page while that
        happens; a tab left live is a tab where a value can be changed after the record carrying it
        was built. Each form ignores the keys it has no row for.
        """
        for form in self._forms.values():
            form.set_pending(keys, key, value)

    def publish_clear_pending(self, keys: list[str]) -> None:
        for form in self._forms.values():
            form.clear_pending(keys)

    def publish_result(self, key: str, ok: bool, message: str = "") -> None:
        """Mark an action's outcome on every tab that offers it.

        One action can appear on several tabs -- a device whose settings are only written on a
        single "apply" wants that button wherever the user is working. Sending the tick to the
        first form that owns the key would put it on a tab they are not looking at.
        """
        for form in self._forms.values():
            form.set_result(key, ok, message)

    def clear_result(self, key: str) -> None:
        for form in self._forms.values():
            form.clear_result(key)

    def _on_connect(self) -> None:
        if self._connected:
            self.disconnectRequested.emit()
        else:
            self.connectRequested.emit()


class MainWindow(QMainWindow):
    """Top level: sidebar, device page, status bar."""

    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("Hardware")
        # Wide enough that a long device name -- "Plantronics Poly Voyager 4310
        # Series" -- fits the sidebar instead of running into its edge.
        self.resize(1280, 800)

        self.sidebar = Sidebar()
        self.page = DevicePage()

        splitter = QSplitter(Qt.Orientation.Horizontal)
        splitter.addWidget(self.sidebar)
        splitter.addWidget(self.page)
        splitter.setStretchFactor(1, 1)
        splitter.setSizes([320, 960])
        self.setCentralWidget(splitter)
        self.setStatusBar(QStatusBar())

    def notify(self, message: str, severity: str = "info") -> None:
        """Transient feedback.

        The status bar rather than a floating toast: it is where a desktop application puts this,
        it does not cover content, and it cannot pile up.
        """
        self.statusBar().showMessage(message, 8000 if severity == "error" else 5000)

    def confirm_reboot(self, feature: str, detail: str, subject: str = "") -> bool:
        """Warn before a change that restarts the device. Cancel is the default."""
        who = subject or "the device"
        body = f"Changing “{feature}” makes {who} disconnect and restart."
        if detail:
            body += f"\n\n{detail}"
        body += "\n\nThe app will reconnect automatically after a few seconds. Continue?"
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Icon.Warning)
        box.setWindowTitle("Device will reconnect")
        box.setText(body)
        box.setStandardButtons(QMessageBox.StandardButton.Ok | QMessageBox.StandardButton.Cancel)
        box.setDefaultButton(QMessageBox.StandardButton.Cancel)
        return box.exec() == QMessageBox.StandardButton.Ok

    def confirm_change(self, feature: str, detail: str) -> bool:
        """Warn before a disruptive change that does *not* restart the device. Cancel is default.

        Switching a monitor's input hands the picture to another machine; a factory reset throws
        away every setting on the panel. Both are recoverable and neither drops the connection --
        which is exactly why they get this dialog rather than the reboot one, whose promise to
        reconnect afterwards would be a lie.
        """
        body = f"Apply “{feature}”?"
        if detail:
            body += f"\n\n{detail}"
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Icon.Warning)
        box.setWindowTitle("Confirm change")
        box.setText(body)
        box.setStandardButtons(QMessageBox.StandardButton.Ok | QMessageBox.StandardButton.Cancel)
        box.setDefaultButton(QMessageBox.StandardButton.Cancel)
        return box.exec() == QMessageBox.StandardButton.Ok

    def ask_pin(
        self, title: str, detail: str = "", *, change: bool = False,
        has_pin: bool = True, minimum: int = 4, field_label: str = "PIN",
        allow_empty: bool = False,
    ) -> str | tuple[str, str] | None:
        """Ask for a secret. Returns ``None`` if cancelled.

        With *change*, asks for current (when one is set), new and confirm-new, and refuses to
        return until the two new entries match and the new one is long enough -- catching a typo
        here rather than after it has been written to a security key.

        *field_label* names what is being asked for, because not every secret is a PIN, and
        *allow_empty* lets blank through where blank is a real choice rather than a mistake.
        """
        dialog = QDialog(self)
        dialog.setWindowTitle(title)
        dialog.setMinimumWidth(380)
        outer = QVBoxLayout(dialog)
        if detail:
            note = QLabel(detail)
            note.setWordWrap(True)
            outer.addWidget(note)

        form = QFormLayout()
        current = new = confirm = None
        if change:
            if has_pin:
                current = QLineEdit()
                current.setEchoMode(QLineEdit.EchoMode.Password)
                form.addRow(f"Current {field_label}:", current)
            new = QLineEdit()
            new.setEchoMode(QLineEdit.EchoMode.Password)
            confirm = QLineEdit()
            confirm.setEchoMode(QLineEdit.EchoMode.Password)
            form.addRow(f"New {field_label}:", new)
            form.addRow(f"Confirm new {field_label}:", confirm)
        else:
            current = QLineEdit()
            current.setEchoMode(QLineEdit.EchoMode.Password)
            form.addRow(f"{field_label}:", current)
        outer.addLayout(form)

        problem = QLabel()
        problem.setWordWrap(True)
        outer.addWidget(problem)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        outer.addWidget(buttons)
        buttons.rejected.connect(dialog.reject)

        def accept() -> None:
            if change:
                if new.text() != confirm.text():
                    problem.setText(f"The new {field_label} entries do not match.")
                    return
                if len(new.text()) < minimum:
                    problem.setText(
                        f"The new {field_label} is too short — at least {minimum} characters."
                    )
                    return
            elif not (current.text() or allow_empty):
                problem.setText(f"Enter the {field_label}.")
                return
            dialog.accept()

        buttons.accepted.connect(accept)
        (new or current).setFocus()
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return None
        if change:
            return (current.text() if current is not None and has_pin else "", new.text())
        return current.text()

    def ask_fields(
        self, title: str, detail: str, fields: Sequence[PromptField]
    ) -> dict[str, Any] | None:
        """Ask for several things at once. Returns ``None`` if cancelled.

        The modifiers belong with what they modify. A "require touch" switch sitting on the page
        beside four different programming buttons says nothing about which one it changes -- and
        that is exactly what a user reported about the first version of the YubiKey slots page.
        """
        dialog = QDialog(self)
        dialog.setWindowTitle(title)
        dialog.setMinimumWidth(420)
        outer = QVBoxLayout(dialog)
        if detail:
            note = QLabel(detail)
            note.setWordWrap(True)
            outer.addWidget(note)

        form = QFormLayout()
        widgets: dict[str, QWidget] = {}
        for field in fields:
            if field.kind is Kind.TOGGLE:
                box = QCheckBox()
                box.setChecked(bool(field.default))
                widgets[field.key] = box
                form.addRow(f"{field.label}:", box)
            elif field.kind is Kind.CHOICE:
                combo = QComboBox()
                for choice in field.choices:
                    combo.addItem(choice.label, choice.value)
                index = combo.findData(field.default)
                combo.setCurrentIndex(max(index, 0))
                widgets[field.key] = combo
                form.addRow(f"{field.label}:", combo)
            else:
                form.addRow(f"{field.label}:", self._text_field(field, widgets))
            if field.description:
                hint = QLabel(field.description)
                hint.setWordWrap(True)
                form.addRow("", hint)
        outer.addLayout(form)

        problem = QLabel()
        problem.setWordWrap(True)
        outer.addWidget(problem)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        outer.addWidget(buttons)
        buttons.rejected.connect(dialog.reject)

        def accept() -> None:
            for field in fields:
                widget = widgets[field.key]
                if isinstance(widget, QLineEdit):
                    text = widget.text()
                    if not text and not field.optional:
                        problem.setText(f"{field.label} is required.")
                        return
                    if field.max_length and len(text) > field.max_length:
                        problem.setText(
                            f"{field.label} can be at most {field.max_length} characters."
                        )
                        return
            dialog.accept()

        buttons.accepted.connect(accept)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return None
        out: dict[str, Any] = {}
        for field in fields:
            widget = widgets[field.key]
            if isinstance(widget, QCheckBox):
                out[field.key] = widget.isChecked()
            elif isinstance(widget, QComboBox):
                out[field.key] = widget.currentData()
            else:
                out[field.key] = widget.text()
        return out

    def _text_field(self, field: PromptField, widgets: dict[str, QWidget]) -> QWidget:
        """A line edit, plus the affordances the vendor's own dialogs have."""
        edit = QLineEdit()
        if field.default:
            edit.setText(str(field.default))
        if field.secret:
            edit.setEchoMode(QLineEdit.EchoMode.Password)
        widgets[field.key] = edit

        row = QWidget()
        layout = QHBoxLayout(row)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(edit, 1)

        if field.max_length:
            counter = QLabel(f"0/{field.max_length}")
            edit.textChanged.connect(
                lambda text, c=counter, n=field.max_length: c.setText(f"{len(text)}/{n}")
            )
            counter.setText(f"{len(edit.text())}/{field.max_length}")
            layout.addWidget(counter)
        if field.secret:
            reveal = QToolButton()
            reveal.setText("👁")
            reveal.setCheckable(True)
            reveal.setToolTip("Show")
            reveal.toggled.connect(
                lambda shown, e=edit: e.setEchoMode(
                    QLineEdit.EchoMode.Normal if shown else QLineEdit.EchoMode.Password
                )
            )
            layout.addWidget(reveal)
        if field.generate and field.max_length:
            make = QToolButton()
            make.setText("⟳")
            make.setToolTip("Generate")
            make.clicked.connect(
                lambda _=False, e=edit, n=field.max_length: e.setText(
                    secrets.token_hex(n // 2)[:n]
                )
            )
            layout.addWidget(make)
        return row

    def choose_path(self, mode: str, title: str, filters: str, suffix: str = "") -> str:
        """Ask for a file to read or write. Returns "" if the user cancels.

        `QFileDialog` uses the platform theme's helper, which under Plasma is the real dialog.
        """
        filters = filters or "All files (*)"
        if mode == "save":
            path, _ = QFileDialog.getSaveFileName(self, title, "", filters)
            if path and suffix and not path.endswith(suffix):
                path += suffix
        else:
            path, _ = QFileDialog.getOpenFileName(self, title, "", filters)
        return path

    def choose_image(self) -> str:
        """Native file chooser.

        `QFileDialog` uses the platform theme's helper, which under Plasma is the real dialog --
        the thing a `QGuiApplication` could not get and which sent us to the XDG portal by hand.
        """
        path, _ = QFileDialog.getOpenFileName(
            self,
            "Choose a photo for this device",
            "",
            "Images (*.png *.jpg *.jpeg *.webp *.svg);;All files (*)",
        )
        return path


__all__ = ["DevicePage", "MainWindow", "Sidebar"]
