"""
Settings, in tabs: Milo (API key, wake word, listening), Screen (keyboard, conversation style,
pendant), Voice (Milo speaking) and Machine. Each tab scrolls, so it can grow.
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
        # --- Milo -------------------------------------------------------------------
        milo = kit.Card()
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
        milo.add(kit.eyebrow("AI model"))
        from ai_config import QUALITY_LEVELS, DEFAULT_MODELS
        model_row = QtWidgets.QHBoxLayout()
        model_row.setSpacing(10)
        model_row.addLayout(kit.vbox(kit.label("Model", "value", size=T.body, weight=theme.MEDIUM),
                                     kit.label("Used for requests, programs and checking results", "muted"),
                                     spacing=2), 1)
        self.model = QtWidgets.QComboBox()
        self.model.setMinimumWidth(360)
        self._recommended = f"Recommended ({DEFAULT_MODELS['cam']})"
        self.model.addItem(self._recommended, "")
        self.model.activated.connect(lambda i: self._choose_model(self.model.itemData(i)))
        model_row.addWidget(self.model)
        milo.add(model_row)
        from ai_config import TRANSCRIPTION_MODEL
        stt_row = QtWidgets.QHBoxLayout()
        stt_row.setSpacing(10)
        stt_row.addLayout(kit.vbox(kit.label("Speech to text", "value", size=T.body, weight=theme.MEDIUM),
                                   kit.label("Turns what you say into text (the “Transcribing…” step)", "muted"),
                                   spacing=2), 1)
        self.stt_model = QtWidgets.QComboBox()
        self.stt_model.setMinimumWidth(360)
        self._stt_recommended = f"Recommended ({TRANSCRIPTION_MODEL})"
        self.stt_model.addItem(self._stt_recommended, "")
        self.stt_model.activated.connect(lambda i: self._engine_set(transcription_model=self.stt_model.itemData(i)))
        stt_row.addWidget(self.stt_model)
        milo.add(stt_row)
        quality_row = QtWidgets.QHBoxLayout()
        quality_row.addLayout(kit.vbox(kit.label("Speed vs. quality", "value", size=T.body, weight=theme.MEDIUM),
                                       kit.label("How long the model thinks before answering", "muted"), spacing=2), 1)
        self._qualities = [key for key, _ in QUALITY_LEVELS]
        self.quality = kit.Segmented([name for _, name in QUALITY_LEVELS])
        self.quality.selected.connect(lambda i: self._engine_set(ai_quality=self._qualities[i]))
        quality_row.addWidget(self.quality)
        milo.add(quality_row)
        self.quality_note = kit.label("", "muted", wrap=True)
        milo.add(self.quality_note)
        # --- Screen -------------------------------------------------------------------
        screen = kit.Card()
        self.keyboard = kit.ToggleRow("On-screen keyboard", "Slides up when you tap a text field")
        self.keyboard.setChecked(shell.prefs.get("touch_keyboard", True))
        self.keyboard.toggled.connect(lambda on: shell.prefs.set("touch_keyboard", on))
        screen.add(self.keyboard)
        chat_row = QtWidgets.QHBoxLayout()
        chat_row.addLayout(kit.vbox(kit.label("Conversation style", "value", size=T.body, weight=theme.MEDIUM),
                                    kit.label("Bubbles, or a compact terminal-like transcript", "muted"), spacing=2), 1)
        from milo_ui.assistant.conversation import STYLES
        self.chat_style = kit.Segmented(["Bubbles", "Compact"])
        self.chat_style.set_index(max(0, list(STYLES).index(shell.prefs.get("conversation_style", "bubbles"))
                                      if shell.prefs.get("conversation_style") in STYLES else 0))
        self.chat_style.selected.connect(lambda i: self._chat_style(STYLES[i]))
        chat_row.addWidget(self.chat_style)
        screen.add(chat_row)
        if pendant_widget is not None:
            screen.add(kit.hline())
            pendant_row = QtWidgets.QHBoxLayout()
            words = kit.vbox(kit.label("Wireless pendant", "value", size=T.body, weight=theme.MEDIUM),
                             kit.label("XHC WHB04B-6 jog speeds and buttons", "muted"), spacing=2)
            pendant_row.addLayout(words, 1)
            pendant_row.addWidget(kit.Button("Configure", icon="sliders-horizontal", on_click=self._pendant))
            screen.add(pendant_row)
        # --- Voice (Milo speaking) ----------------------------------------------------
        voice_card = kit.Card()
        self.speak = kit.ToggleRow("Speak Milo’s replies", "Answers and questions are read aloud through the speakers")
        self.speak.toggled.connect(lambda on: self._engine_set(speak_replies=on))
        voice_card.add(self.speak)
        voice_row = QtWidgets.QHBoxLayout()
        voice_row.setSpacing(10)
        voice_row.addWidget(kit.label("Voice", "body"), 1)
        from ai_config import TTS_VOICES
        self.voice = QtWidgets.QComboBox()
        self.voice.setMinimumWidth(220)
        for name in TTS_VOICES:
            self.voice.addItem(name.capitalize(), name)
        self.voice.activated.connect(lambda i: self._engine_set(tts_voice=self.voice.itemData(i)))
        voice_row.addWidget(self.voice)
        voice_row.addWidget(kit.Button("Test", icon="speaker-high",
                                       on_click=lambda: self.shell.engine and self.shell.engine.preview_voice()))
        voice_card.add(voice_row)
        style_row = QtWidgets.QHBoxLayout()
        style_row.setSpacing(10)
        style_row.addWidget(kit.label("Speaking style", "body"), 1)
        from ai_config import TTS_STYLES
        self.style = QtWidgets.QComboBox()
        self.style.setMinimumWidth(328)
        for key, name, _ in TTS_STYLES:
            self.style.addItem(name, key)
        self.style.activated.connect(lambda i: self._engine_set(speech_style=self.style.itemData(i)))
        style_row.addWidget(self.style)
        voice_card.add(style_row)
        speed_row = QtWidgets.QHBoxLayout()
        speed_row.addWidget(kit.label("Speed", "body"), 1)
        from ai_config import TTS_SPEEDS
        self._speeds = [value for _, value in TTS_SPEEDS]
        self.speed = kit.Segmented([name for name, _ in TTS_SPEEDS])
        self.speed.selected.connect(lambda i: self._engine_set(speech_speed=self._speeds[i]))
        speed_row.addWidget(self.speed)
        voice_card.add(speed_row)
        voice_card.add(ParamRow("Volume", lambda: float(self._setting("speech_volume", 80)),
                          lambda v: self._engine_set(speech_volume=max(0, min(100, int(v)))), "%"))

        # --- Machine ------------------------------------------------------------------
        machine = kit.Card()
        for key, value in self._machine_facts():
            line = QtWidgets.QHBoxLayout()
            line.addWidget(kit.label(key, "muted"))
            line.addStretch(1)
            line.addWidget(kit.label(value, "body"))
            machine.add(line)

        tabs = [("Milo", milo), ("Screen", screen), ("Voice", voice_card)]
        if getattr(shell, "pendant", None) is not None:
            from milo_ui.pages.pendant_settings import build_pendant_tab
            self.pendant_card = build_pendant_tab(shell)
            tabs.append(("Pendant", self.pendant_card))
        tabs.append(("Machine", machine))
        self._build_tabs(tabs)

    def _build_tabs(self, tabs):
        outer = QtWidgets.QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(20)
        self.tab_names = [name for name, _ in tabs]
        header = QtWidgets.QHBoxLayout()
        header.setSpacing(20)
        # Closing LinuxCNC is always at hand, whichever tab is open
        self.shutdown = kit.Button("Shut down", icon="power", variant="danger", on_click=self._quit)
        header.addWidget(self.shutdown)
        self.tabs = kit.Segmented(self.tab_names, min_width=170)
        header.addWidget(self.tabs)
        header.addStretch(1)
        outer.addLayout(header)
        self.tab_stack = QtWidgets.QStackedWidget()
        for _, card in tabs:
            # Each tab scrolls, so settings can be added without running out of room
            scroll = QtWidgets.QScrollArea()
            scroll.setWidgetResizable(True)
            scroll.setFrameShape(QtWidgets.QFrame.NoFrame)
            scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
            scroll.setStyleSheet("QScrollArea, QScrollArea > QWidget > QWidget { background: transparent; }")
            QtWidgets.QScroller.grabGesture(scroll.viewport(), QtWidgets.QScroller.LeftMouseButtonGesture)
            body = QtWidgets.QWidget()
            column = QtWidgets.QVBoxLayout(body)
            column.setContentsMargins(0, 0, 0, 0)
            card.setMaximumWidth(1100)
            column.addWidget(card)
            column.addStretch(1)
            scroll.setWidget(body)
            self.tab_stack.addWidget(scroll)
        outer.addWidget(self.tab_stack, 1)
        self.tabs.selected.connect(self.show_tab)
        saved = self.shell.prefs.get("settings_tab", self.tab_names[0])
        self.show_tab(self.tab_names.index(saved) if saved in self.tab_names else 0)

    def show_tab(self, index):
        self.tabs.set_index(index)
        self.tab_stack.setCurrentIndex(index)
        self.shell.prefs.set("settings_tab", self.tab_names[index])

    def on_show(self):
        engine = self.shell.engine
        if engine is None:
            return
        self.key_field.setText(engine.settings.get("api_key", ""))
        self.wake.setChecked(bool(engine.settings.get("wake_word", True)))
        self.details.setChecked(bool(engine.settings.get("show_details", False)))
        self.speak.setChecked(bool(engine.settings.get("speak_replies", False)))
        index = self.voice.findData(engine.settings.get("tts_voice"))
        self.voice.setCurrentIndex(max(0, index))
        self.style.setCurrentIndex(max(0, self.style.findData(engine.settings.get("speech_style"))))
        self._show_model_choice()
        fetch = getattr(engine, "fetch_models", None)
        if fetch is not None and not getattr(self, "_models_loaded", False):
            fetch(self._fill_models)
        current = float(engine.settings.get("speech_speed", 1.0) or 1.0)
        self.speed.set_index(min(range(len(self._speeds)), key=lambda i: abs(self._speeds[i] - current)))
        for row in self.findChildren(ParamRow):
            row.refresh()

    def _fill_models(self, models):
        """The model list arrived from OpenAI (None if it couldn't be fetched)"""
        if not models:
            return
        self._models_loaded = True
        for combo, recommended, names in ((self.model, self._recommended, models.get("chat") or []),
                                          (self.stt_model, self._stt_recommended, models.get("transcription") or [])):
            combo.clear()
            combo.addItem(recommended, "")
            for name in names:
                combo.addItem(name, name)
        self._show_model_choice()

    def _choose_model(self, model):
        self._engine_set(ai_model=model)
        self._show_model_choice()

    def _show_model_choice(self):
        from ai_config import is_reasoning, DEFAULT_MODELS
        chosen = self._setting("ai_model", "") or ""
        if chosen and self.model.findData(chosen) < 0:
            self.model.addItem(chosen, chosen)  # saved choice not in the (not yet fetched) list
        self.model.setCurrentIndex(max(0, self.model.findData(chosen)))
        stt = self._setting("transcription_model", "") or ""
        if stt and self.stt_model.findData(stt) < 0:
            self.stt_model.addItem(stt, stt)
        self.stt_model.setCurrentIndex(max(0, self.stt_model.findData(stt)))
        quality = self._setting("ai_quality", "balanced")
        self.quality.set_index(self._qualities.index(quality) if quality in self._qualities else 1)
        reasoning = is_reasoning(chosen or DEFAULT_MODELS["cam"])
        self.quality.setEnabled(reasoning)
        self.quality_note.setText(
            "Fastest: quickest replies and programs, simpler plans. Best quality: most careful programs, "
            "which can take a minute; replies get a little slower." if reasoning else
            f"{chosen} doesn't have a reasoning setting, so this has no effect with it.")

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

    def _chat_style(self, style):
        self.shell.prefs.set("conversation_style", style)
        if self.shell.conversation is not None:
            self.shell.conversation.set_style(style)

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
