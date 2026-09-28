"""
qtvcp handler for the Milo screen.

The .ui file is an empty main window. Right after qtvcp loads it (and before qtvcp creates
HAL pins for its widgets) we build the whole Milo interface in Python, including the few
qtvcp widgets worth keeping (toolpath graphics, G-code display, tool and offset tables,
probing, ScreenOptions' dialogs), so qtvcp HAL-initializes them as usual.
"""

import importlib.util
import os
import sys

from PyQt5 import QtCore, QtGui, QtWidgets, uic

from qtvcp.core import Status, Action, Info, Path, Qhal
from qtvcp import logger

LOG = logger.getLogger(__name__)
STATUS = Status()
ACTION = Action()
INFO = Info()
PATH = Path()
QHAL = Qhal()

CONFIG_DIR = PATH.CONFIGPATH
if CONFIG_DIR not in sys.path:
    sys.path.insert(0, CONFIG_DIR)

from milo_ui import theme  # noqa: E402
from milo_ui.theme import C  # noqa: E402
from milo_ui.machine import QtvcpMachine  # noqa: E402
from milo_ui.shell import MiloShell, Prefs  # noqa: E402
from milo_ui.pages import build_pages  # noqa: E402


class HandlerClass:
    def __init__(self, halcomp, widgets, paths):
        self.h = halcomp
        self.w = widgets
        self.paths = paths
        self.shell = None
        self.machine = None
        self.engine = None
        self.pendant_widget = None
        self.pendant_handler = None

    # --- qtvcp hooks ---------------------------------------------------------------------

    def class_patch__(self):
        """Build the interface as soon as qtvcp has loaded the (empty) .ui file"""
        original_instance = self.w.instance

        def instance(filename=None):
            original_instance(filename)
            self._build()
        self.w.instance = instance

    def initialized__(self):
        """HAL pins exist now; start Milo"""
        self.machine.make_pins(QHAL)
        self._init_pendant_handler()
        self._start_engine()
        STATUS.connect("gcode-line-selected", lambda w, line: self._line_selected(line))
        self.w.setWindowFlags(QtCore.Qt.FramelessWindowHint)
        self.shell.navigate("home")
        STATUS.emit("update-machine-log", "--- Milo screen started ---", "TIME")

    def closing_cleanup__(self):
        if self.engine is not None:
            self.engine.shutdown()

    def processed_key_event__(self, receiver, event, is_pressed, key, code, shift, cntrl):
        """Keys go to text fields and dialogs. Elsewhere only Esc (abort) does anything:
        a stray key press on the shop keyboard should never move the machine."""
        # Esc always aborts, even while typing
        if is_pressed and code == QtCore.Qt.Key_Escape and not event.isAutoRepeat():
            ACTION.ABORT()
            return True
        widget = receiver
        while widget is not None:
            if isinstance(widget, (QtWidgets.QLineEdit, QtWidgets.QPlainTextEdit, QtWidgets.QTextEdit,
                                   QtWidgets.QDialog, QtWidgets.QAbstractItemView,
                                   QtWidgets.QAbstractScrollArea)):
                return False
            widget = widget.parent() if isinstance(widget, QtCore.QObject) else None
        return False

    def __getitem__(self, item):
        return getattr(self, item)

    def __setitem__(self, item, value):
        return setattr(self, item, value)

    # --- construction --------------------------------------------------------------------

    def _build(self):
        app = QtWidgets.QApplication.instance()
        theme.apply(app)

        widgets = {
            "screen_options": self._screen_options(),
            "graphics": self._graphics(),
            "gcode_view": self._gcode_view(),
            "tool_table": self._named("qtvcp.widgets.tool_offsetview", "ToolOffsetView", "tooloffsetview"),
            "offset_table": self._named("qtvcp.widgets.origin_offsetview", "OriginOffsetView", "offset_table"),
            "probe": self._probe(),
            "pendant": self._pendant_widget(),
        }
        for name in ("tool_table", "offset_table"):
            view = widgets[name]
            if view is not None:
                view.setShowGrid(False)
                view.verticalHeader().setDefaultSectionSize(60)
                QtWidgets.QScroller.grabGesture(view.viewport(), QtWidgets.QScroller.LeftMouseButtonGesture)

        self.machine = QtvcpMachine(self.h)
        prefs = Prefs()
        self.shell = MiloShell(self.machine, graphics=widgets["graphics"], prefs=prefs)
        self.shell.make_bridge()
        build_pages(self.shell, widgets)

        central = QtWidgets.QWidget()
        layout = QtWidgets.QVBoxLayout(central)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self.shell)
        if widgets["screen_options"] is not None:
            widgets["screen_options"].setParent(central)
            widgets["screen_options"].hide()
        self.w.setCentralWidget(central)
        # qtvcp and old handlers look widgets up by name on the window
        for name, widget in widgets.items():
            if widget is not None and widget.objectName():
                self.w[widget.objectName()] = widget

    def _screen_options(self):
        try:
            from qtvcp.widgets.screen_options import ScreenOptions
        except ImportError as e:
            LOG.error(f"ScreenOptions unavailable: {e}")
            return None
        options = ScreenOptions()
        options.setObjectName("screen_options")
        settings = {
            "halCompBaseName": "milo",
            "notify_option": False,          # Milo shows errors itself (toasts + Activity)
            "catch_errors_option": True,     # ...but ScreenOptions still polls the error channel
            "catch_close_option": True,
            "play_sounds_option": False,
            "use_pref_file_option": True,
            "messageDialog_option": True,
            "closeDialog_option": True,
            "entryDialog_option": True,
            "entryDialogSoftkey_option": True,
            "toolDialog_option": True,       # manual tool change prompts
            "fileDialog_option": True,
            "runFromLineDialog_option": True,
            "calculatorDialog_option": True,
            "keyboardDialog_option": False,
        }
        for key, value in settings.items():
            options.setProperty(key, value)
        return options

    def _graphics(self):
        try:
            from qtvcp.widgets.gcode_graphics import GCodeGraphics
        except ImportError as e:
            LOG.error(f"Toolpath graphics unavailable: {e}")
            return None
        graphics = GCodeGraphics()
        graphics.setObjectName("gcodegraphics")
        graphics.setProperty("_dro", False)
        graphics.setProperty("_dtg", False)
        graphics.setProperty("_overlay", False)
        graphics.setProperty("_offsets", False)
        graphics.setProperty("_use_gradient_background", True)
        graphics.setBackgroundColor(QtGui.QColor("#121823"))
        graphics.gradient_color2 = (0.043, 0.059, 0.086)
        graphics.setProperty("jog_color", QtGui.QColor(C.amber))
        graphics.setProperty("Feed_color", QtGui.QColor(C.accent_hi))
        graphics.setProperty("Rapid_color", QtGui.QColor(C.accent_2))
        graphics.setProperty("InhibitControls", False)
        return graphics

    def _gcode_view(self):
        try:
            from qtvcp.widgets.gcode_editor import GcodeDisplay
        except ImportError as e:
            LOG.error(f"G-code display unavailable: {e}")
            return None
        view = GcodeDisplay()
        view.setObjectName("gcode_display")
        # QScintilla sizes fonts in points; a pixel-sized QFont reads as "no size" and it falls
        # back to a tiny default. 14 pt is about 19 px on this 96 dpi screen.
        mono = QtGui.QFont(theme.MONO_FONT, 14)
        margin = QtGui.QFont(theme.MONO_FONT, 11)
        try:
            view.setColorBackground(QtGui.QColor(C.bg))
            view.setColorMarginsBackground(QtGui.QColor(C.card))
            view.setColorMarginsForeground(QtGui.QColor(C.text_3))
            view.setColorMarkerBackground(QtGui.QColor(C.accent_soft))
            view.setColorSelectionBackground(QtGui.QColor(C.accent))
            view.setColor0(QtGui.QColor(C.text))       # default
            view.setColor1(QtGui.QColor(C.text_3))     # comments
            view.setColor2(QtGui.QColor(C.accent_hi))  # G codes
            view.setColor3(QtGui.QColor(C.amber))      # M codes
            view.setColor4(QtGui.QColor(C.accent_2))   # axes
            view.setColor5(QtGui.QColor(C.green))      # other words
            view.setDefaultFont(mono)
            for index in range(8):
                getattr(view, f"setFont{index}")(mono)
            view.setFontMargins(margin)
            view.set_margin_metric(4)  # room for 4 digits in the margin font
            # Taller lines are easier to tap when picking a line to run from
            view.setExtraAscent(5)
            view.setExtraDescent(5)
        except Exception as e:
            LOG.warning(f"Could not style the G-code display: {e}")
        return view

    def _named(self, module, cls, name):
        try:
            widget = getattr(__import__(module, fromlist=[cls]), cls)()
        except Exception as e:
            LOG.error(f"{cls} unavailable: {e}")
            return None
        widget.setObjectName(name)
        return widget

    def _probe(self):
        kind = (INFO.get_error_safe_setting("PROBE", "USE_PROBE", "none") or "none").lower()
        try:
            if kind == "basicprobe":
                from qtvcp.widgets.basic_probe import BasicProbe
                probe = BasicProbe()
            elif kind == "versaprobe":
                from qtvcp.widgets.versa_probe import VersaProbe
                probe = VersaProbe()
            else:
                return None
        except Exception as e:
            LOG.error(f"Probe widget unavailable: {e}")
            return None
        probe.setObjectName(kind)
        return probe

    def _pendant_widget(self):
        ui_path = os.path.join(CONFIG_DIR, "qtvcp", "panels", "pendant_config", "pendant_config.ui")
        if not os.path.exists(ui_path):
            return None
        try:
            self.pendant_widget = uic.loadUi(ui_path)
            return self.pendant_widget
        except Exception as e:
            LOG.error(f"Pendant panel unavailable: {e}")
            return None

    def _init_pendant_handler(self):
        if self.pendant_widget is None:
            return
        path = os.path.join(CONFIG_DIR, "qtvcp", "panels", "pendant_config", "pendant_config_handler.py")
        try:
            spec = importlib.util.spec_from_file_location("pendant_config_handler_embedded", path)
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            self.pendant_handler = module.HandlerClass(self.h, self.pendant_widget, PATH)
            if hasattr(self.pendant_handler, "initialized__"):
                self.pendant_handler.initialized__()
        except Exception as e:
            LOG.error(f"Pendant panel handler failed: {e}")

    # --- Milo ------------------------------------------------------------------------------

    def _start_engine(self):
        try:
            from milo_engine import MiloEngine
        except Exception as e:
            LOG.error(f"Milo is unavailable: {e}")
            self.shell.toaster.show(f"Milo couldn't start: {e}", "error", seconds=20)
            return
        self.engine = MiloEngine(
            listener=self.shell.bridge,
            stat_getter=lambda: STATUS.stat,
            action_api=ACTION,
            load_program=self.machine.open_program,
            config_dir=CONFIG_DIR,
        )
        self.engine.start()
        self.shell.attach_engine(self.engine)
        app = QtWidgets.QApplication.instance()
        app.aboutToQuit.connect(self.engine.shutdown)

    def _line_selected(self, line):
        page = self.shell.pages.get("program")
        if page is not None:
            page.set_selected_line(line)


def get_handlers(halcomp, widgets, paths):
    return [HandlerClass(halcomp, widgets, paths)]
