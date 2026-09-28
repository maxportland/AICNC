"""
Settings: Milo (API key, voice, wake word), the screen, the pendant, and the machine.
"""

import os
import platform

from PyQt5 import QtCore, QtWidgets
from PyQt5.QtCore import Qt

from milo_ui import theme, kit
from milo_ui.theme import C, T
from milo_ui.shell import Page
from milo_ui.pages.probe import ParamRow


class SettingsPage(Page):
    key = "settings"
    title = "Settings"
    icon = "gear-six"
    wants_stage = False

    def __init__(self, shell, pendant_widget=None, parent=None):
        super().__init__(parent)
        self.shell = shell
        self.pendant_widget = pendant_widget
        outer = QtWidgets.QHBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(20)

        # --- Milo -------------------------------------------------------------------
        milo = kit.Card(title="Milo")
        milo.add(kit.label("OpenAI API key", "value", size=T.body, weight=theme.MEDIUM))
        milo.add(kit.label("Milo uses OpenAI to understand requests, write programs and transcribe speech. "
                           "The key is stored only on this machine (~/.linuxcnc/gpt_config.json).", "muted", wrap=True))
        key_row = QtWidgets.QHBoxLayout()
        key_row.setSpacing(10)
        self.key_field = QtWidgets.QLineEdit()
        self.key_field.setEchoMode(QtWidgets.QLineEdit.Password)
        self.key_field.setPlaceholderText("sk-…")
        key_row.addWidget(self.key_field, 1)
        self.show_key = kit.Button("", icon="eye", checkable=True)
        self.show_key.toggled.connect(lambda on: self.key_field.setEchoMode(
            QtWidgets.QLineEdit.Normal if on else QtWidgets.QLineEdit.Password))
        key_row.addWidget(self.show_key)
        key_row.addWidget(kit.Button("Save", icon="check", variant="primary", on_click=self._save_key))
        milo.add(key_row)
        milo.add(kit.hline())
        self.wake = kit.ToggleRow("Listen for “Hey Milo”", "Wake word detection runs offline on this machine")
        self.wake.toggled.connect(lambda on: self._engine_set(wake_word=on))
        milo.add(self.wake)
        self.details = kit.ToggleRow("Show technical details", "Router, CAM and voice diagnostics in the conversation")
        self.details.toggled.connect(self._details)
        milo.add(self.details)
        milo.add(kit.eyebrow("Listening"))
        milo.add(ParamRow("Stop listening after", lambda: float(self._setting("recording_timeout", 20)),
                          lambda v: self._engine_set(recording_timeout=max(3, int(v))), "s"))
        milo.add(ParamRow("End of speech after silence", lambda: float(self._setting("silence_timeout", 2.0)),
                          lambda v: self._engine_set(silence_timeout=max(0.5, float(v))), "s"))
        milo.body.addStretch(1)
        outer.addWidget(milo, 1)

        # --- Screen & machine -----------------------------------------------------------
        right = QtWidgets.QVBoxLayout()
        right.setSpacing(20)
        screen = kit.Card(title="Screen")
        self.keyboard = kit.ToggleRow("On-screen keyboard", "Slides up when you tap a text field")
        self.keyboard.setChecked(shell.prefs.get("touch_keyboard", True))
        self.keyboard.toggled.connect(lambda on: shell.prefs.set("touch_keyboard", on))
        screen.add(self.keyboard)
        if pendant_widget is not None:
            screen.add(kit.hline())
            pendant_row = QtWidgets.QHBoxLayout()
            words = kit.vbox(kit.label("Wireless pendant", "value", size=T.body, weight=theme.MEDIUM),
                             kit.label("XHC WHB04B-6 jog speeds and buttons", "muted"), spacing=2)
            pendant_row.addLayout(words, 1)
            pendant_row.addWidget(kit.Button("Configure", icon="sliders-horizontal", on_click=self._pendant))
            screen.add(pendant_row)
        right.addWidget(screen)

        machine = kit.Card(title="Machine")
        for key, value in self._machine_facts():
            line = QtWidgets.QHBoxLayout()
            line.addWidget(kit.label(key, "muted"))
            line.addStretch(1)
            line.addWidget(kit.label(value, "body"))
            machine.add(line)
        machine.add(kit.hline())
        quit_row = QtWidgets.QHBoxLayout()
        quit_row.addWidget(kit.label("Close LinuxCNC", "value", size=T.body, weight=theme.MEDIUM), 1)
        quit_row.addWidget(kit.Button("Shut down", icon="power", variant="danger", on_click=self._quit))
        machine.add(quit_row)
        right.addWidget(machine)
        right.addStretch(1)
        outer.addLayout(right, 1)

    def on_show(self):
        engine = self.shell.engine
        if engine is None:
            return
        self.key_field.setText(engine.settings.get("api_key", ""))
        self.wake.setChecked(bool(engine.settings.get("wake_word", True)))
        self.details.setChecked(bool(engine.settings.get("show_details", False)))
        for row in self.findChildren(ParamRow):
            row.refresh()

    def _setting(self, key, default):
        engine = self.shell.engine
        return engine.settings.get(key, default) if engine is not None else default

    def _engine_set(self, **changes):
        if self.shell.engine is not None:
            self.shell.engine.save_settings(**changes)

    def _save_key(self):
        key = self.key_field.text().strip()
        self._engine_set(api_key=key)
        self.shell.composer.set_ai_available(bool(key))
        self.shell.keyboard.hide_keyboard()
        self.shell.toaster.show("API key saved. Milo is ready." if key else "API key removed.",
                                "success" if key else "info")

    def _details(self, on):
        self._engine_set(show_details=on)
        if self.shell.conversation is not None:
            self.shell.conversation.set_show_details(on)

    def _machine_facts(self):
        facts = []
        try:
            from milo_ui.machine import QtvcpMachine
            if not isinstance(self.shell.machine, QtvcpMachine):
                raise ImportError("simulated")
            from qtvcp.core import Info, Status
            info = Info()
            facts.append(("Machine", str(info.MACHINE_NAME if hasattr(info, "MACHINE_NAME") else "")))
            facts.append(("LinuxCNC", str(Status().get_linuxcnc_version())))
        except Exception:
            facts.append(("Machine", "Simulated"))
        m = self.shell.machine
        facts.append(("Travel", "  ".join(f"{a} {lo:g}…{hi:g}" for a, (lo, hi) in m.limits.items())))
        facts.append(("Spindle", f"{m.spindle_min:g}–{m.spindle_max:g} rpm"))
        facts.append(("Computer", platform.node()))
        return [(k, v) for k, v in facts if v]

    def _pendant(self):
        pop = kit.Popover(self, title="Pendant", width=1100)
        scroll = QtWidgets.QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setMinimumHeight(700)
        scroll.setWidget(self.pendant_widget)
        pop.add(scroll)
        # Keep the pendant widget alive when the popover closes
        pop.closed.connect(lambda: self.pendant_widget.setParent(None))
        pop.show_centered()

    def _quit(self):
        kit.ActionSheet(self, "Shut down LinuxCNC?", [
            ("power", "Shut down", lambda: self.window().close(), "danger"),
            ("x", "Cancel", lambda: None),
        ], subtitle="The machine will stop and the screen will close.").show_centered()
