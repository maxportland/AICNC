"""
Home: the cockpit. Milo's conversation, position and overrides beside it, and suggestions
that follow the machine's state. The toolpath has its own page.
"""

import os

from PyQt5 import QtCore, QtWidgets
from PyQt5.QtCore import Qt

from milo_ui import theme, kit
from milo_ui.theme import C, T
from milo_ui.shell import Page
from milo_ui.widgets import DRO, OverridesCard
from milo_ui.assistant.conversation import Conversation


def suggestions(machine):
    """What Milo can do next, given the machine state: [(icon, text), ...]"""
    m = machine
    if m.estop:
        return [("question", "Why can't the machine move?")]
    if not m.on:
        return [("power", "Turn the machine on"), ("question", "What should I check before starting?")]
    if not m.all_homed:
        return [("house-line", "Home all axes"), ("question", "Why do I need to home?")]
    if m.is_running:
        return [("clock", "How long until this program finishes?"), ("question", "What is the current operation?")]
    items = []
    if m.file:
        items.append(("play", f"Run {os.path.basename(m.file)}"))
        items.append(("list-bullets", "Which tools does this program use?"))
    items += [("arrow-right", "Go to X0 Y0"), ("square", "Face the stock 100 by 50, 1 mm deep"),
              ("dots-nine", "Drill 4 holes on a 60 mm bolt circle")]
    return items[:3]


def welcome_context(machine):
    m = machine
    if m.estop:
        return "The E-stop is active. Release it at the machine and I can turn it on for you."
    if not m.on:
        return "The machine is off. Say “Hey Milo, turn the machine on”, or tap below."
    if not m.all_homed:
        return "The machine is on but not homed yet. I can home it for you."
    if m.file:
        return f"{os.path.basename(m.file)} is loaded. Ask me to run it, change it, or make something new."
    return "Ask me to move the machine, make a program, or answer a question. Just say “Hey Milo”."


class SuggestionBar(QtWidgets.QWidget):
    picked = QtCore.pyqtSignal(str)

    def __init__(self, machine, parent=None):
        super().__init__(parent)
        self.machine = machine
        self._layout = kit.FlowLayout(self, spacing=10)
        self.setContentsMargins(48, 0, 0, 0)
        self._last = None
        machine.changed.connect(lambda topic: topic in ("state", "homing", "program") and self.refresh())
        self.refresh()

    def refresh(self):
        items = suggestions(self.machine)
        if items == self._last:
            return
        self._last = items
        kit.clear_layout(self._layout)
        for icon, text in items:
            chip = kit.Chip(text, icon=icon, tone="accent")
            chip.clicked.connect(lambda _=False, t=text: self.picked.emit(t))
            self._layout.addWidget(chip)
        self.updateGeometry()


class HomePage(Page):
    key = "home"
    title = "Milo"
    icon = "sparkle"

    def __init__(self, shell, parent=None):
        super().__init__(parent)
        self.shell = shell
        machine = shell.machine
        row = QtWidgets.QHBoxLayout(self)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(20)

        left = QtWidgets.QVBoxLayout()
        left.setSpacing(16)
        self.dro = DRO(machine, big=46)
        left.addWidget(self.dro)
        self.overrides = OverridesCard(machine)
        left.addWidget(self.overrides)
        left.addStretch(1)
        holder = QtWidgets.QWidget()
        holder.setLayout(left)
        holder.setFixedWidth(420)
        row.addWidget(holder)

        center = QtWidgets.QVBoxLayout()
        center.setSpacing(12)
        header = QtWidgets.QHBoxLayout()
        header.setContentsMargins(8, 0, 0, 0)
        header.setSpacing(10)
        self.title_label = kit.label("Milo", "page_title")
        header.addWidget(self.title_label)
        self.listening_hint = kit.label("", "muted")
        header.addWidget(self.listening_hint)
        header.addStretch(1)
        self.menu_button = kit.RoundButton("dots-three-outline", 52, variant="ghost", icon_size=22,
                                           tip="Conversation options", on_click=self._menu)
        header.addWidget(self.menu_button)
        center.addLayout(header)

        self.conversation = Conversation()
        self.conversation.example_clicked.connect(shell.composer.set_text)
        self.conversation.orb_clicked.connect(shell._voice)
        self.conversation.view_program.connect(self._view_program)
        self.conversation.run_program.connect(lambda: shell.engine and shell.engine.propose_run())
        shell.conversation = self.conversation
        center.addWidget(self.conversation, 1)
        self.suggestions = SuggestionBar(machine)
        self.suggestions.picked.connect(shell.ask)
        center.addWidget(self.suggestions)
        # The compact transcript goes without the suggestion chips
        self.conversation.style_changed.connect(lambda style: self.suggestions.setVisible(style != "compact"))
        self.conversation.set_style(shell.prefs.get("conversation_style", "bubbles"))
        row.addLayout(center, 1)

        machine.changed.connect(lambda topic: topic in ("state", "homing", "program") and self._context())
        self._context()

    def _context(self):
        self.conversation.set_context(welcome_context(self.shell.machine))
        engine = self.shell.engine
        ready = engine is not None and getattr(engine, "wake_word_ready", False)
        self.listening_hint.setText("· listening for “Hey Milo”" if ready else "")

    def on_show(self):
        self._context()

    def _view_program(self):
        self.shell.navigate("toolpath")

    def _menu(self):
        engine = self.shell.engine
        if engine is None:
            return
        details = engine.show_details

        def toggle_details():
            engine.save_settings(show_details=not details)
            self.conversation.set_show_details(not details)

        def new_conversation():
            engine.new_conversation()

        def save():
            self.shell.ask_text("Save conversation", engine.save_session, placeholder="Name, e.g. bracket v2")

        def load():
            sessions = engine.list_sessions()
            if not sessions:
                return self.shell.toaster.show("No saved conversations yet.", "info")
            actions = [("chat-teardrop-dots", name[:-5], lambda n=name: engine.load_session(n)) for name in sessions[:8]]
            kit.ActionSheet(self, "Load conversation", actions,
                            subtitle="Restores the program context Milo builds on.").show_at(self.menu_button, "below")

        kit.ActionSheet(self, "Conversation", [
            ("plus-circle", "New conversation", new_conversation),
            ("floppy-disk", "Save conversation…", save),
            ("folder-open", "Load conversation…", load),
            ("eye", "Hide technical details" if details else "Show technical details", toggle_details),
        ]).show_at(self.menu_button, "below")
        self._context()
