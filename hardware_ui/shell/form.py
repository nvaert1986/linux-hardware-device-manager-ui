"""Turns a :class:`CapabilitySet` into real widgets.

This is the whole reason the app looks the same for every device: there is one renderer, one
widget per :class:`Kind`, and a module contributes capabilities rather than UI. Adding a seventh
device family means writing no widget code at all.

Replaces the QML delegates and the Qt model that fed them. `QFormLayout` gives the two-column,
right-aligned-label form that had to be hand-built in QML, Breeze's `QStyle` draws every control,
and a widget cannot silently lose a binding the way a QML property can.

Behaviour ported from the reference implementation, all of it load-bearing:

* a write disables the controls it touches until the device confirms (``_mark_pending``)
* a pending control ignores incoming state, or a refresh captured before the write lands repaints
  the old value
* an error releases the in-flight set, or the control stays dead forever
* programmatic updates must not emit change signals -- the ``_loading`` guard
* a composite write holds *every* member of its group
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtGui import QColor, QIcon
from PyQt6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QFormLayout,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QProgressBar,
    QPushButton,
    QSlider,
    QTabWidget,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from hardware_ui.core import Advisory, Capability, CapabilitySet, Diagram, Kind
from hardware_ui.core.capability import gate_satisfied

from .diagram import DiagramPanel, caption_label

log = logging.getLogger(__name__)


# No muted text anywhere. Dimming secondary text was tried three ways -- the disabled palette
# (~1.6:1, invisible), then a heavy blend, then a shallow one -- and every version was reported
# as hard to read. Section headings are distinguished by being bold and uppercase, notes by
# position; neither needs a lighter colour, and the style's normal text colour is the one the
# user has already chosen as legible.


def _paint_swatch(button: QWidget, colour: str | None) -> None:
    """Show the colour on the button itself. A swatch is the only honest label for a colour."""
    if not colour:
        button.setText("Choose…")
        button.setStyleSheet("")
        return
    shade = QColor(colour)
    # Pick readable text for the swatch rather than assuming a light or dark theme.
    ink = "#000000" if shade.lightnessF() > 0.5 else "#ffffff"
    button.setText(shade.name())
    button.setStyleSheet(f"background-color: {shade.name()}; color: {ink};")


class CapabilityRow:
    """One capability and the widgets that present it."""

    def __init__(self, cap: Capability, control: QWidget, label: QLabel) -> None:
        self.cap = cap
        self.control = control
        self.label = label
        self.note: QLabel | None = None
        self.value: Any = None

    def widgets(self) -> list[QWidget]:
        return [w for w in (self.control, self.label, self.note) if w is not None]


class CapabilityForm(QWidget):
    """A scrollable form for one capability group (one tab)."""

    #: (key) -- a value was copied to the clipboard, so the shell can say so.
    copied = pyqtSignal(str)

    #: (key, value) -- the user changed a control.
    changed = pyqtSignal(str, object)
    #: key -- the user triggered an ACTION.
    triggered = pyqtSignal(str)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._rows: dict[str, CapabilityRow] = {}
        #: The sub-tab strip holding this group's drawings, created on the first one.
        self._views: QTabWidget | None = None
        self._values: dict[str, Any] = {}
        self._pending: set[str] = set()
        self._failed: set[str] = set()
        self._advisories: dict[str, Advisory] = {}
        #: Guards against a programmatic repaint echoing back as a user edit. The reference
        #: implementation's `_loading` flag; without it, populating a combo emits activated().
        self._loading = False

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)

        # Drawings sit above the form and take the tab's spare height, which is the whole point of
        # giving them their own strip: a drawing that only ever gets its size hint is a thumbnail
        # in the top corner of an empty tab. The trailing stretch below is switched off whenever
        # this is in use, so the two are not competing for the same slack.
        self._views_holder = QWidget()
        self._views_layout = QVBoxLayout(self._views_holder)
        self._views_layout.setContentsMargins(0, 0, 0, 0)
        self._views_holder.setVisible(False)
        outer.addWidget(self._views_holder, 1)

        self._form = QFormLayout()
        self._form.setLabelAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        # Anchor the form to the top-left. Without this QFormLayout centres its rows, so each tab
        # drifted to a different horizontal position depending on its widest label.
        self._form.setFormAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop)
        self._form.setContentsMargins(16, 12, 16, 12)
        # Fields must be allowed to grow: a wrapped description cannot work out its own height
        # at a fixed size hint, so the rows collapsed and the text overlapped the row below.
        self._form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.ExpandingFieldsGrow)
        self._form.setVerticalSpacing(8)
        self._form.setHorizontalSpacing(12)
        outer.addLayout(self._form)

        # One note per tab, at the bottom -- the reference implementation's panel-level note
        # (EqualizerPanel._note). Per-row wrapped labels do not report their height correctly
        # inside a QFormLayout field and overlapped the row below; this also matches how the
        # original presents "why is this disabled" information.
        self._note = QLabel()
        self._note.setWordWrap(True)
        self._note.setVisible(False)
        # Same left margin as the form, so the note lines up with the labels above it rather
        # than starting at the panel edge.
        self._note.setContentsMargins(16, 12, 16, 0)
        outer.addWidget(self._note)
        self._tail = outer.count()
        outer.addStretch(1)
        self._outer = outer

    # ------------------------------------------------------------------ building

    def build(self, caps: CapabilitySet, diagrams: dict[str, Diagram] | None = None) -> None:
        """Replace the form's contents.

        *diagrams* maps a :attr:`Capability.section` to a drawing of the hardware. A section that
        has one is laid out around the picture with a leader line from each control to the part it
        changes; every other section, and every control the drawing cannot point at, falls back to
        an ordinary two-column row. That fallback is not a corner case: a desktop without Qt's SVG
        module gets the whole page as a plain form and loses nothing but the picture.
        """
        self._loading = True
        try:
            while self._form.rowCount():
                self._form.removeRow(0)
            self._rows.clear()

            section = ""
            panel: DiagramPanel | None = None
            self._clear_views()
            for cap in caps:
                if cap.section != section:
                    section = cap.section
                    panel = self._panel_for(section, diagrams)
                    if panel is None and section:
                        self._form.addRow(self._section_header(section))
                self._add(cap, panel)
        finally:
            self._loading = False
        self._update_note()
        self._restyle()

    def _panel_for(self, section: str, diagrams: dict[str, Diagram] | None) -> DiagramPanel | None:
        """Start a drawing for this section, or None if it has none this shell can render.

        **Every drawing goes in its own sub-tab**, rather than stacking down the page. Three
        drawings plus their controls is well over two screens, so stacking them meant the tab
        opened on a scrollbar the size of a thumbnail and the user had to scroll to discover that
        the shoulder buttons existed at all. Only one side of a controller is being looked at
        anyway. The source configurator splits the same way, and so does every vendor tool for a
        device with more than one face.

        A single drawing still gets a tab, deliberately: a lone tab bar is a small cost against
        having two layouts to keep working, and it labels what is being shown.
        """
        diagram = (diagrams or {}).get(section)
        if diagram is None:
            return None
        panel = DiagramPanel(diagram, self)
        if not panel.usable:
            # The module offered a drawing and it could not be loaded. The controls are what
            # matter, so the section renders as ordinary rows -- `_renderer` has said why already.
            panel.deleteLater()
            return None

        if self._views is None:
            self._views = QTabWidget()
            self._views.setDocumentMode(True)
            self._views_layout.addWidget(self._views)
            self._views_holder.setVisible(True)
            # The drawings now own the spare height, so the form must stop claiming it.
            self._outer.setStretch(self._tail, 0)
        self._views.addTab(_view_page(panel, diagram), section)
        return panel

    def _clear_views(self) -> None:
        """Drop any drawings from a previous build, and give the slack back to the form."""
        if self._views is not None:
            self._views.setParent(None)
            self._views.deleteLater()
            self._views = None
        self._views_holder.setVisible(False)
        self._outer.setStretch(self._tail, 1)

    def _section_header(self, text: str) -> QWidget:
        holder = QWidget()
        box = QVBoxLayout(holder)
        box.setContentsMargins(0, 12, 0, 2)
        # '&&' is the Qt Widgets escape for a literal ampersand; a section header is a plain
        # QLabel with no mnemonic handling, so it wants the single character.
        label = QLabel(text.replace("&&", "&").upper())
        font = label.font()
        font.setBold(True)
        font.setPointSizeF(max(7.0, font.pointSizeF() - 1.5))
        label.setFont(font)
        box.addWidget(label)
        line = QFrame()
        line.setFrameShape(QFrame.Shape.HLine)
        line.setFrameShadow(QFrame.Shadow.Sunken)
        box.addWidget(line)
        return holder

    def _add(self, cap: Capability, panel: DiagramPanel | None = None) -> None:
        control = self._control_for(cap)
        # No colon inside a diagram: the label sits in a bordered card next to its control rather
        # than in a right-aligned label column, and a trailing colon there reads as a truncation.
        on_diagram = panel is not None and cap.key in panel.diagram.anchors
        label = QLabel((cap.short_label or cap.label) if on_diagram else f"{cap.label}:")
        label.setBuddy(control)
        row = CapabilityRow(cap, control, label)

        # Descriptions are tooltips, not visible labels: static help does not need to occupy
        # the form, and a wrapped label here breaks the row height. Anything the user must see
        # is an Advisory, which appears in the tab's note.
        if cap.description:
            control.setToolTip(cap.description)
            label.setToolTip(cap.description)

        # The panel takes the same widgets rather than making its own, so every value, pending
        # flag and gate in this class keeps working on a control that moved into a picture.
        if not (panel is not None and panel.add(cap.key, label, control)):
            self._form.addRow(label, control)
        self._rows[cap.key] = row

    def _control_for(self, cap: Capability) -> QWidget:
        match cap.kind:
            case Kind.TOGGLE:
                w = QCheckBox()
                w.toggled.connect(lambda on, k=cap.key: self._emit(k, on))
                return w
            case Kind.CHOICE:
                w = QComboBox()
                for choice in cap.choices:
                    w.addItem(choice.label, choice.value)
                # activated fires only on user interaction, unlike currentIndexChanged --
                # exactly the distinction the `_loading` guard exists to enforce elsewhere.
                w.activated.connect(lambda i, k=cap.key, c=w: self._emit(k, c.itemData(i)))
                return w
            case Kind.RANGE:
                holder = QWidget()
                box = QHBoxLayout(holder)
                box.setContentsMargins(0, 0, 0, 0)
                slider = QSlider(Qt.Orientation.Horizontal)
                slider.setRange(int(cap.minimum), int(cap.maximum))
                slider.setSingleStep(int(cap.step) or 1)
                slider.setPageStep(int(cap.step) or 1)
                slider.setFixedWidth(180)
                readout = QLabel("—")
                readout.setMinimumWidth(52)
                box.addSpacing(0)
                box.addWidget(slider)
                box.addSpacing(10)
                box.addWidget(readout)
                box.addStretch(1)
                slider.valueChanged.connect(
                    lambda v, r=readout, u=cap.unit: r.setText(f"{v}{' ' + u if u else ''}")
                )
                # Commit on release: these are serialised protocols, and a drag would otherwise
                # queue one write per pixel.
                slider.sliderReleased.connect(lambda k=cap.key, s=slider: self._emit(k, s.value()))
                holder.slider = slider  # type: ignore[attr-defined]
                holder.readout = readout  # type: ignore[attr-defined]
                return holder
            case Kind.COLOR:
                w = QPushButton()
                w.setFixedWidth(96)
                w.clicked.connect(lambda _, k=cap.key, b=w: self._pick_colour(k, b))
                _paint_swatch(w, None)
                return w
            case Kind.TEXT:
                # Text needs an explicit Save. Committing on focus-loss is invisible -- you cannot
                # tell whether a rename took, and clicking elsewhere writing to the device is a
                # surprise. The button is enabled only while the text differs from what the device
                # last reported, so it doubles as an unsaved-changes indicator.
                holder = QWidget()
                box = QHBoxLayout(holder)
                box.setContentsMargins(0, 0, 0, 0)
                line = QLineEdit()
                line.setMaximumWidth(240)
                if cap.max_length:
                    line.setMaxLength(cap.max_length)
                if cap.secret:
                    line.setEchoMode(QLineEdit.EchoMode.Password)
                    line.setPlaceholderText("PIN")
                save = QPushButton("Save")
                save.setEnabled(False)
                save.setToolTip("Write this value to the device")
                box.addWidget(line, 1)
                box.addWidget(save)
                holder.line = line  # type: ignore[attr-defined]
                holder.save = save  # type: ignore[attr-defined]
                holder.committed = ""  # type: ignore[attr-defined]

                def _commit(k: str = cap.key, h: QWidget = holder) -> None:
                    text = h.line.text()
                    if text == h.committed:
                        return
                    h.save.setEnabled(False)
                    self._emit(k, text)

                def _edited(text: str, h: QWidget = holder) -> None:
                    h.save.setEnabled(text != h.committed)

                line.textEdited.connect(_edited)
                line.returnPressed.connect(_commit)
                save.clicked.connect(_commit)
                return holder
            case Kind.ACTION:
                holder = QWidget()
                box = QHBoxLayout(holder)
                box.setContentsMargins(0, 0, 0, 0)
                button = QPushButton(cap.action_label or "Run")
                button.clicked.connect(lambda _, k=cap.key: self.triggered.emit(k))
                # A button that does something invisible needs to say it worked. Without this a
                # successful self-test on a security key looked identical to nothing happening.
                result = QLabel()
                result.setMinimumWidth(18)
                box.addWidget(button)
                box.addSpacing(6)
                box.addWidget(result)
                box.addStretch(1)
                holder.button = button  # type: ignore[attr-defined]
                holder.result = result  # type: ignore[attr-defined]
                return holder
            case Kind.METER:
                holder = QWidget()
                box = QHBoxLayout(holder)
                box.setContentsMargins(0, 0, 0, 0)
                bar = QProgressBar()
                bar.setRange(int(cap.minimum), int(cap.maximum))
                bar.setTextVisible(False)
                bar.setFixedSize(120, 10)
                readout = QLabel("—")
                readout.setMinimumWidth(52)
                box.addWidget(bar)
                box.addSpacing(10)
                box.addWidget(readout)
                box.addStretch(1)
                holder.bar = bar  # type: ignore[attr-defined]
                holder.readout = readout  # type: ignore[attr-defined]
                return holder
            case _:
                label = QLabel("—")
                label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
                if not cap.copyable:
                    return label
                holder = QWidget()
                box = QHBoxLayout(holder)
                box.setContentsMargins(0, 0, 0, 0)
                box.addWidget(label)
                if cap.suffix_total:
                    bar = QProgressBar()
                    bar.setRange(0, int(cap.suffix_total))
                    bar.setTextVisible(False)
                    bar.setFixedSize(34, 6)
                    box.addSpacing(8)
                    box.addWidget(bar)
                    holder.bar = bar  # type: ignore[attr-defined]
                button = QToolButton()
                # An icon, not the word: this sits at the end of every code on the page, and a
                # button wearing a five-letter label is wider than the value it copies.
                icon = QIcon.fromTheme("edit-copy")
                if icon.isNull():
                    button.setText("⧉")
                else:
                    button.setIcon(icon)
                button.setAutoRaise(True)
                button.setToolTip("Copy to the clipboard")
                button.clicked.connect(lambda _, k=cap.key: self._copy(k))
                box.addSpacing(6)
                box.addWidget(button)
                box.addStretch(1)
                holder.readout = label  # type: ignore[attr-defined]
                return holder

    def _copy(self, key: str) -> None:
        """Copy the raw value, not what is on screen.

        The displayed string may carry a unit; what someone wants on the clipboard is the value.
        """
        from PyQt6.QtWidgets import QApplication

        value = self._values.get(key)
        if value is None:
            return
        clipboard = QApplication.clipboard()
        if clipboard is not None:
            clipboard.setText(str(value))
            self.copied.emit(key)

    def _pick_colour(self, key: str, button: QWidget) -> None:
        """Open the platform colour dialog, seeded with the current value."""
        from PyQt6.QtWidgets import QColorDialog

        current = QColor(str(self._values.get(key) or "#000000"))
        chosen = QColorDialog.getColor(
            current, self, "Choose a colour", QColorDialog.ColorDialogOption.DontUseNativeDialog
        )
        if not chosen.isValid():
            return
        _paint_swatch(button, chosen.name())
        self._emit(key, chosen.name())

    def _emit(self, key: str, value: Any) -> None:
        if self._loading:
            return
        self.changed.emit(key, value)

    # ------------------------------------------------------------------ state

    def set_value(self, key: str, value: Any, *, confirmed: bool = False) -> None:
        """Apply a value from the device.

        Dropped while a write is in flight unless it is that write's own confirmation: a refresh
        captured before our write landed would otherwise repaint the pre-write value.
        """
        row = self._rows.get(key)
        if row is None:
            # No row here, but the value is still this tab's business for two reasons, and the
            # second one is why the Creative equaliser was dead on arrival.
            #
            # It may be something another row shows *after* its own value -- a countdown beside a
            # one-time code.
            #
            # It may be what one of this tab's rows is **gated on**. A `requires` naming a
            # capability in another group resolved against this form's own value map, which never
            # held it, so `gate_satisfied` compared `None` and every dependent row stayed disabled
            # for ever. That is not a rare shape: Direct Mode lives on the Sound tab and bypasses
            # the whole Equalizer tab, which is exactly the interlock `requires` exists for.
            #
            # So the value is always recorded. Storing a key this form does not draw costs one
            # dictionary entry and cannot paint anything, because painting is driven by rows.
            self._values[key] = value
            for dependant in [r for r in self._rows.values() if r.cap.suffix_from == key]:
                self._paint(dependant)
            self._restyle()
            return
        if key in self._pending and not confirmed:
            return
        self._values[key] = value
        self._failed.discard(key)
        self._pending.discard(key)
        self._paint(row)
        self._restyle()

    def value_of(self, key: str) -> Any:
        return self._values.get(key)

    def set_pending(self, keys: list[str], key: str, value: Any) -> None:
        """Mark a composite group in flight and show the optimistic value."""
        self._values[key] = value
        self._pending.update(keys)
        row = self._rows.get(key)
        if row is not None:
            self._paint(row)
        self._restyle()

    def clear_pending(self, keys: list[str]) -> None:
        self._pending.difference_update(keys)
        self._restyle()

    def mark_failed(self, key: str) -> None:
        self._failed.add(key)
        self._pending.discard(key)
        self._restyle()

    def set_result(self, key: str, ok: bool, message: str = "") -> None:
        """Mark an action as having succeeded or failed.

        A tick beside the button, with the detail on hover. Actions are the one kind whose effect
        is often invisible -- a self-test, a reset, a sync -- so "nothing appeared to happen" and
        "it worked" must not look the same.
        """
        row = self._rows.get(key)
        if row is None or row.cap.kind is not Kind.ACTION:
            return
        label = getattr(row.control, "result", None)
        if label is None:
            return
        label.setText("✓" if ok else "✗")
        label.setStyleSheet(f"color: {'#27ae60' if ok else '#da4453'}; font-weight: bold;")
        label.setToolTip(message or ("Done" if ok else "Failed"))

    def clear_result(self, key: str) -> None:
        row = self._rows.get(key)
        label = getattr(row.control, "result", None) if row is not None else None
        if label is not None:
            label.setText("")
            label.setToolTip("")

    def set_advisories(self, advisories: dict[str, Advisory]) -> None:
        if advisories == self._advisories:
            return
        self._advisories = dict(advisories)
        self._update_note()
        self._restyle()

    def _update_note(self) -> None:
        """Show the most relevant message for this tab.

        An advisory wins over a static note: it is the state-dependent one, and it is the message
        that tells the user what to do ("set Connection preference to Priority on Stable
        Connection to use the equalizer").
        """
        for key in self._rows:
            advisory = self._advisories.get(key)
            if advisory is not None and advisory.message:
                self._note.setText(advisory.message)
                self._note.setVisible(True)
                return
        static = next((r.cap.note for r in self._rows.values() if r.cap.note), "")
        self._note.setText(static)
        self._note.setVisible(bool(static))

    def keys(self) -> list[str]:
        return list(self._rows)

    # ------------------------------------------------------------------ painting

    def _paint(self, row: CapabilityRow) -> None:
        """Push a value into its widget without emitting a change."""
        value = self._values.get(row.cap.key)
        self._loading = True
        try:
            control, kind = row.control, row.cap.kind
            if kind is Kind.TOGGLE:
                control.setChecked(bool(value))
            elif kind is Kind.CHOICE:
                index = control.findData(value)
                if index >= 0:
                    control.setCurrentIndex(index)
            elif kind is Kind.RANGE:
                unit = f" {row.cap.unit}" if row.cap.unit else ""
                if value is None:
                    control.readout.setText("—")
                else:
                    control.slider.setValue(int(value))
                    # Set the readout directly: setValue emits nothing when the value is
                    # unchanged, which left it showing "-" for a value the device had reported.
                    control.readout.setText(f"{int(value)}{unit}")
            elif kind is Kind.COLOR:
                _paint_swatch(control, None if value is None else str(value))
            elif kind is Kind.TEXT:
                # A secret is never painted back: repainting would put a PIN on screen after a
                # refresh, and the device never reports one anyway.
                if not row.cap.secret:
                    text = "" if value is None else str(value)
                    control.line.setText(text)
                    # This is now what the device holds, so there is nothing unsaved.
                    control.committed = text
                    control.save.setEnabled(False)
            elif kind is Kind.METER:
                control.bar.setValue(int(value or 0))
                unit = f" {row.cap.unit}" if row.cap.unit else ""
                control.readout.setText("—" if value is None else f"{int(value)}{unit}")
            elif kind is not Kind.ACTION:
                unit = f" {row.cap.unit}" if row.cap.unit else ""
                text = "—" if value is None else f"{value}{unit}"
                if row.cap.suffix_from and value is not None:
                    suffix = self._values.get(row.cap.suffix_from)
                    if suffix is not None and suffix != "":
                        if row.cap.suffix_total:
                            left = int(suffix)
                            text = f"{text}     {left} s"
                            bar = getattr(control, "bar", None)
                            if bar is not None:
                                bar.setValue(left)
                        else:
                            text = f"{text}     {suffix}"
                target = getattr(control, "readout", control)
                target.setText(text)
        finally:
            self._loading = False

    def _restyle(self) -> None:
        """Recompute every control's enabled state.

        Cheap enough to do wholesale: a device has tens of rows, not thousands, and deriving
        each one independently is what stops a gate and its dependants drifting apart.
        """
        for key, row in self._rows.items():
            cap = row.cap
            if not cap.writable:
                # Two different things wear the same flag, and they need opposite treatment.
                #
                # A readout or a meter was never interactive, and the style's disabled palette
                # makes it unreadable -- grey text on the Info tab, a grey battery bar. Those stay
                # enabled.
                #
                # An *interactive* kind marked unwritable is a control the module can read but
                # must not change -- Logitech's key diversion, which this application can report
                # but cannot honour. Leaving that enabled offered a live dropdown whose values
                # would silently disable a physical button.
                interactive = cap.kind not in (Kind.READOUT, Kind.METER)
                row.control.setEnabled(not interactive)
                row.label.setEnabled(not interactive)
                continue
            advisory = self._advisories.get(key)
            enabled = (
                key not in self._pending
                and key not in self._failed
                and not (advisory is not None and advisory.locked)
                and gate_satisfied(cap, self._values.get)
            )
            row.control.setEnabled(enabled)
            row.label.setEnabled(enabled)


def _view_page(panel: DiagramPanel, diagram: Diagram) -> QWidget:
    """One sub-tab: the drawing, filling the tab, with its caption underneath."""
    page = QWidget()
    box = QVBoxLayout(page)
    box.setContentsMargins(0, 0, 0, 0)
    box.setSpacing(0)
    box.addWidget(panel, 1)
    if diagram.caption:
        box.addWidget(caption_label(diagram))
    return page


def build_forms(
    caps: CapabilitySet,
    on_changed: Callable[[str, Any], None],
    on_triggered: Callable[[str], None],
    on_copied: Callable[[str], None] | None = None,
    diagrams: dict[str, Diagram] | None = None,
) -> dict[str, CapabilityForm]:
    """One form per group, in the module's declared order -- i.e. one per tab."""
    forms: dict[str, CapabilityForm] = {}
    for group, members in caps.groups().items():
        form = CapabilityForm()
        form.build(CapabilitySet(list(members)), diagrams)
        form.changed.connect(on_changed)
        form.triggered.connect(on_triggered)
        if on_copied is not None:
            form.copied.connect(on_copied)
        forms[group] = form
    return forms
