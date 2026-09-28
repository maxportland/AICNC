"""
Design tokens, fonts, icons and the application stylesheet.

Every color, radius and type size in the UI comes from here, so the whole screen can be
retuned in one place. Widgets refer to tokens by name (theme.C.text) and style themselves
through objectName / dynamic properties matched by the stylesheet below.
"""

import os
from types import SimpleNamespace

from PyQt5 import QtCore, QtGui, QtWidgets

try:
    import qtawesome as qta
except ImportError:  # the UI still works, with text in place of icons
    qta = None

FONT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fonts")

# --- color -----------------------------------------------------------------------------

C = SimpleNamespace(
    bg="#0A0D12",          # app background
    surface="#0F131A",     # rail, top bar, dock
    card="#151A22",        # cards
    card_hi="#1B212B",     # raised / pressed card, inputs
    card_top="#222936",    # hover-free "pressed" state on touch
    line="#222935",        # hairlines
    line_hi="#2F3846",
    text="#EAEEF4",
    text_2="#A4AEBC",
    text_3="#6B7686",
    text_4="#4A5362",
    accent="#8B7CFF",      # Milo
    accent_hi="#A89DFF",
    accent_2="#3DD6F5",    # Milo gradient end
    accent_soft="#1E1A3A",
    green="#34D399",
    green_soft="#0F2A22",
    amber="#FBBF24",
    amber_soft="#2B2210",
    red="#F43F5E",
    red_soft="#2E1119",
    blue="#60A5FA",
    x="#FF7A85",
    y="#5EE6A0",
    z="#6AA8FF",
    a="#FBBF24",
)

AXIS_COLORS = {"X": C.x, "Y": C.y, "Z": C.z, "A": C.a}

# --- shape & type ----------------------------------------------------------------------

R = SimpleNamespace(sm=10, md=14, lg=20, xl=28, pill=999)
S = SimpleNamespace(xs=4, sm=8, md=12, lg=16, xl=24, xxl=32)

UI_FONT = "Inter"
MONO_FONT = "JetBrains Mono"

T = SimpleNamespace(
    caption=13,
    label=15,
    body=17,
    body_lg=19,
    title=22,
    h2=28,
    h1=40,
    display=64,
)

# Minimum touch target (px). The screen is 1920x1080 at ~0.26 mm/px, so 64 px is ~17 mm.
TOUCH = 64
TOUCH_SM = 52


_fonts_loaded = False


def load_fonts():
    """Register the bundled Inter and JetBrains Mono fonts (safe to call more than once)"""
    global _fonts_loaded, UI_FONT, MONO_FONT
    if _fonts_loaded:
        return
    _fonts_loaded = True
    families = set()
    if os.path.isdir(FONT_DIR):
        for name in sorted(os.listdir(FONT_DIR)):
            if name.endswith(".ttf"):
                font_id = QtGui.QFontDatabase.addApplicationFont(os.path.join(FONT_DIR, name))
                families.update(QtGui.QFontDatabase.applicationFontFamilies(font_id))
    if "Inter" not in families:
        UI_FONT = "Lato" if "Lato" in QtGui.QFontDatabase().families() else "DejaVu Sans"
    if "JetBrains Mono" not in families:
        MONO_FONT = "DejaVu Sans Mono"


def font(size=T.body, weight=QtGui.QFont.Normal, mono=False):
    f = QtGui.QFont(MONO_FONT if mono else UI_FONT)
    f.setPixelSize(size)
    f.setWeight(weight)
    f.setHintingPreference(QtGui.QFont.PreferNoHinting)
    return f


# Qt 5 QFont weights for the Inter cuts we bundle
LIGHT, REGULAR, MEDIUM, SEMIBOLD, BOLD = (QtGui.QFont.Light, QtGui.QFont.Normal, QtGui.QFont.Medium,
                                          QtGui.QFont.DemiBold, QtGui.QFont.Bold)


def icon(name, color=None, size=None, **kwargs):
    """A Phosphor icon from qtawesome ('house' -> ph.house); empty icon if unavailable"""
    if qta is None:
        return QtGui.QIcon()
    full = name if "." in name else f"ph.{name}"
    try:
        return qta.icon(full, color=color or C.text_2, **kwargs)
    except Exception:
        return QtGui.QIcon()


def pixmap(name, color=None, size=24):
    return icon(name, color).pixmap(size, size)


def mix(color_a, color_b, t):
    """Blend two colors: t=0 gives a, t=1 gives b"""
    a, b = QtGui.QColor(color_a), QtGui.QColor(color_b)
    return QtGui.QColor(
        int(a.red() + (b.red() - a.red()) * t),
        int(a.green() + (b.green() - a.green()) * t),
        int(a.blue() + (b.blue() - a.blue()) * t),
    )


def alpha(color, a):
    c = QtGui.QColor(color)
    c.setAlphaF(a)
    return c


def repolish(widget):
    """Re-apply the stylesheet after changing a dynamic property"""
    widget.style().unpolish(widget)
    widget.style().polish(widget)
    widget.update()


def set_prop(widget, name, value):
    if widget.property(name) != value:
        widget.setProperty(name, value)
        repolish(widget)


# --- stylesheet --------------------------------------------------------------------------

def stylesheet():
    t = dict(vars(C))
    t.update(ui=UI_FONT, mono=MONO_FONT, caption=T.caption, label=T.label, body=T.body,
             body_lg=T.body_lg, title=T.title, h2=T.h2)
    return QSS.format(**t)


QSS = """
* {{
    font-family: "{ui}";
    color: {text};
    outline: none;
}}
QMainWindow, #milo_root, QDialog {{
    background: {bg};
}}
QToolTip {{
    background: {card_hi}; color: {text}; border: 1px solid {line_hi};
    border-radius: 8px; padding: 6px 10px; font-size: {label}px;
}}
QLabel {{ background: transparent; }}

/* --- scroll bars: slim, touch-draggable ------------------------------------------ */
QScrollBar:vertical {{
    background: transparent; width: 14px; margin: 6px 3px 6px 3px;
}}
QScrollBar::handle:vertical {{
    background: {line_hi}; border-radius: 4px; min-height: 48px;
}}
QScrollBar::handle:vertical:pressed {{ background: {text_3}; }}
QScrollBar:horizontal {{
    background: transparent; height: 14px; margin: 3px 6px 3px 6px;
}}
QScrollBar::handle:horizontal {{
    background: {line_hi}; border-radius: 4px; min-width: 48px;
}}
QScrollBar::add-line, QScrollBar::sub-line {{ width: 0; height: 0; }}
QScrollBar::add-page, QScrollBar::sub-page {{ background: transparent; }}
QScrollArea {{ background: transparent; border: none; }}
QAbstractScrollArea::corner {{ background: transparent; border: none; }}
QScrollArea > QWidget > QWidget {{ background: transparent; }}

/* --- shell ------------------------------------------------------------------------ */
#rail {{
    background: {surface};
    border-right: 1px solid {line};
}}
#rail_button {{
    background: transparent; border: none; border-radius: 16px;
    color: {text_3}; font-size: 13px; font-weight: 600;
    padding: 0px;
}}
#rail_button:checked {{ background: {card_hi}; color: {text}; }}
#rail_button[attention="true"] {{ color: {amber}; }}
#topbar {{
    background: {surface};
    border-bottom: 1px solid {line};
}}
#dock {{
    background: {surface};
    border-top: 1px solid {line};
}}
#page_title {{ font-size: {title}px; font-weight: 600; color: {text}; }}
#page_subtitle {{ font-size: {label}px; color: {text_3}; }}

/* --- cards -------------------------------------------------------------------------- */
#card, #stage {{
    background: {card};
    border: 1px solid {line};
    border-radius: 20px;
}}
#card[tone="accent"] {{ border-color: #3A3170; }}
#card[tone="amber"] {{ border-color: #5A4515; background: #17150F; }}
#card[tone="red"] {{ border-color: #5C1C2A; background: #1A1014; }}
#card_title {{ font-size: {label}px; font-weight: 600; color: {text_2}; letter-spacing: 0.3px; }}
#eyebrow {{ font-size: 12px; font-weight: 700; color: {text_3}; letter-spacing: 1.2px; }}
#muted {{ color: {text_3}; font-size: {label}px; }}
#body {{ color: {text_2}; font-size: {body}px; }}
#value {{ color: {text}; font-size: {body_lg}px; font-weight: 600; }}
#mono {{ font-family: "{mono}"; color: {text}; }}
#divider {{ background: {line}; max-height: 1px; min-height: 1px; border: none; }}
#vdivider {{ background: {line}; max-width: 1px; min-width: 1px; border: none; }}

/* --- buttons ------------------------------------------------------------------------ */
QPushButton, QToolButton {{
    background: {card_hi};
    border: 1px solid {line_hi};
    border-radius: 14px;
    color: {text};
    font-size: {body}px; font-weight: 600;
    padding: 0px 20px;
    min-height: 52px;
}}
QPushButton:pressed, QToolButton:pressed {{ background: {card_top}; border-color: {text_4}; }}
QPushButton:checked, QToolButton:checked {{ background: {accent_soft}; border-color: {accent}; color: {text}; }}
QPushButton:disabled, QToolButton:disabled {{ color: {text_4}; background: {card}; border-color: {line}; }}

QPushButton[variant="primary"] {{ background: {accent}; border-color: {accent}; color: #0B0A1A; }}
QPushButton[variant="primary"]:pressed {{ background: {accent_hi}; }}
QPushButton[variant="go"] {{ background: {green}; border-color: {green}; color: #04160F; }}
QPushButton[variant="go"]:pressed {{ background: #6EE7B7; }}
QPushButton[variant="warn"] {{ background: {amber}; border-color: {amber}; color: #1C1403; }}
QPushButton[variant="warn"]:pressed {{ background: #FCD34D; }}
QPushButton[variant="danger"] {{ background: {red}; border-color: {red}; color: #FFFFFF; }}
QPushButton[variant="danger"]:pressed {{ background: #FB7185; }}
QPushButton[variant="ghost"] {{ background: transparent; border-color: transparent; color: {text_2}; }}
QPushButton[variant="ghost"]:pressed {{ background: {card_hi}; }}
QPushButton[variant="outline"] {{ background: transparent; border-color: {line_hi}; color: {text}; }}
QPushButton[variant="primary"]:disabled, QPushButton[variant="go"]:disabled,
QPushButton[variant="warn"]:disabled, QPushButton[variant="danger"]:disabled {{
    background: {card_hi}; border-color: {line}; color: {text_4};
}}
QPushButton[variant="ghost"]:disabled, QPushButton[variant="outline"]:disabled {{
    color: {text_4}; border-color: {line};
}}
QPushButton[size="lg"] {{ min-height: 72px; border-radius: 18px; font-size: {body_lg}px; }}
QPushButton[size="sm"] {{ min-height: 44px; border-radius: 12px; font-size: {label}px; padding: 0 14px; }}
QPushButton[shape="round"] {{ padding: 0px; }}

#chip {{
    background: {card_hi}; border: 1px solid {line_hi}; border-radius: 26px;
    font-size: {label}px; font-weight: 500; color: {text}; padding: 0 20px; min-height: 52px;
}}
#chip:pressed {{ background: {card_top}; }}
#chip[tone="accent"] {{ background: {accent_soft}; border-color: #3E3480; color: {accent_hi}; }}

/* segmented control */
#segmented {{ background: {bg}; border: 1px solid {line}; border-radius: 16px; }}
#segment {{
    background: transparent; border: none; border-radius: 12px;
    color: {text_2}; font-size: {label}px; font-weight: 600; min-height: 48px; padding: 0 10px;
}}
#segment:checked {{ background: {card_top}; color: {text}; }}

/* --- inputs -------------------------------------------------------------------------- */
QLineEdit, QPlainTextEdit, QTextEdit, QSpinBox, QDoubleSpinBox {{
    background: {bg};
    border: 1px solid {line_hi};
    border-radius: 14px;
    padding: 0 16px;
    min-height: 52px;
    font-size: {body}px;
    color: {text};
    selection-background-color: {accent};
    selection-color: #0B0A1A;
}}
QLineEdit:focus, QPlainTextEdit:focus, QTextEdit:focus {{ border-color: {accent}; }}
QLineEdit:read-only {{ color: {text_2}; background: {card}; }}
QPlainTextEdit, QTextEdit {{ padding: 10px 12px; }}

QComboBox {{
    background: {card_hi}; border: 1px solid {line_hi}; border-radius: 14px;
    padding: 0 16px; min-height: 52px; font-size: {body}px;
}}
QComboBox::drop-down {{ border: none; width: 36px; }}
QComboBox QAbstractItemView {{
    background: {card_hi}; border: 1px solid {line_hi}; selection-background-color: {accent_soft};
    font-size: {body}px; padding: 6px; outline: none;
}}
QComboBox QAbstractItemView::item {{ min-height: 52px; padding: 0 12px; }}

QCheckBox {{ font-size: {body}px; spacing: 14px; min-height: 48px; }}
QCheckBox::indicator {{ width: 28px; height: 28px; border-radius: 8px; border: 2px solid {line_hi}; background: {bg}; }}
QCheckBox::indicator:checked {{ background: {accent}; border-color: {accent}; }}

/* --- tables & lists (tool table, offsets, file manager) ---------------------------- */
QTableView, QTableWidget, QTreeView, QListView, QListWidget {{
    background: {card};
    alternate-background-color: #171D26;
    border: none;
    gridline-color: {line};
    font-size: {body}px;
    selection-background-color: {accent_soft};
    selection-color: {text};
}}
QTableView::item, QListView::item, QTreeView::item {{ padding: 4px 10px; border: none; }}
QTableView::item:selected, QListView::item:selected, QTreeView::item:selected {{
    background: {accent_soft}; color: {text};
}}
QHeaderView {{ background: {card}; border: none; }}
QHeaderView::section {{
    background: {card}; color: {text_3}; border: none; border-bottom: 1px solid {line};
    font-size: 13px; font-weight: 700; padding: 10px; text-transform: uppercase;
}}
QTableCornerButton::section {{ background: {card}; border: none; }}

/* --- menus & dialogs ------------------------------------------------------------------ */
QMenu {{
    background: {card_hi}; border: 1px solid {line_hi}; border-radius: 16px; padding: 8px;
}}
QMenu::item {{ padding: 14px 24px; border-radius: 10px; font-size: {body}px; min-width: 220px; }}
QMenu::item:selected {{ background: {card_top}; }}
QMenu::separator {{ height: 1px; background: {line}; margin: 6px 10px; }}
QMessageBox {{ background: {card}; }}
QMessageBox QLabel {{ font-size: {body}px; }}
QDialog QPushButton {{ min-width: 120px; }}
QProgressBar {{
    background: {card_hi}; border: none; border-radius: 4px; height: 8px; max-height: 8px;
    text-align: center; color: transparent;
}}
QProgressBar::chunk {{ background: {accent}; border-radius: 4px; }}

/* --- conversation ----------------------------------------------------------------- */
#conversation, #conversation_body {{ background: transparent; }}
#msg_user {{
    background: {card_hi}; border: 1px solid {line_hi};
    border-radius: 22px; border-bottom-right-radius: 8px;
}}
#msg_user_text {{ font-size: {body_lg}px; color: {text}; }}
#msg_milo_text {{ font-size: {body_lg}px; color: {text}; }}
#msg_note {{ font-size: {label}px; color: {text_3}; }}
#msg_note[kind="detail"] {{ font-family: "{mono}"; font-size: 13px; color: {text_4}; }}
#msg_card {{ background: {card}; border: 1px solid {line}; border-radius: 20px; }}
#msg_card[kind="success"] {{ border-color: #1D4C3B; background: #0F1A17; }}
#msg_card[kind="error"] {{ border-color: #5C1C2A; background: #1A1014; }}
#msg_card[kind="warning"] {{ border-color: #5A4515; background: #17150F; }}
#msg_card[kind="confirm"] {{ border-color: #5A4515; background: #17150F; }}
#msg_card[kind="program"] {{ border-color: #3A3170; background: #13122A; }}
#msg_card_title {{ font-size: {body}px; font-weight: 600; }}
#msg_card_text {{ font-size: {body}px; color: {text_2}; }}
#code_chip {{
    font-family: "{mono}"; font-size: {label}px; color: {accent_hi};
    background: {bg}; border: 1px solid {line}; border-radius: 10px; padding: 8px 12px;
}}
#welcome_title {{ font-size: 44px; font-weight: 600; color: {text}; letter-spacing: -0.6px; }}
#welcome_sub {{ font-size: {body_lg}px; color: {text_2}; }}
#example_tile {{
    background: {card}; border: 1px solid {line}; border-radius: 20px;
    text-align: left; padding: 0px; min-height: 104px;
}}
#example_tile:pressed {{ background: {card_hi}; border-color: {line_hi}; }}
#example_title {{ font-size: {body}px; font-weight: 600; }}
#example_text {{ font-size: {label}px; color: {text_3}; }}

/* composer */
#composer {{
    background: {bg}; border: 1px solid {line_hi}; border-radius: 36px;
}}
#composer[focused="true"] {{ border-color: {accent}; }}
#composer_input {{
    background: transparent; border: none; font-size: {body_lg}px; padding: 0px; min-height: 40px;
}}
#composer_status {{ font-size: {label}px; color: {accent_hi}; font-weight: 600; }}
#send_button {{
    background: {accent}; border: none; border-radius: 26px; padding: 0; min-height: 52px;
}}
#send_button:disabled {{ background: {card_hi}; }}

/* confirmation sheet */
#confirm_sheet {{
    background: #17150F; border: 1px solid #6B5217; border-radius: 24px;
}}
#confirm_eyebrow {{ font-size: 12px; font-weight: 800; color: {amber}; letter-spacing: 1.6px; }}
#confirm_summary {{ font-size: 24px; font-weight: 600; color: {text}; }}
#confirm_hint {{ font-size: {label}px; color: {text_3}; }}

/* toasts */
#toast {{ background: {card_hi}; border: 1px solid {line_hi}; border-radius: 18px; }}
#toast[level="error"] {{ background: #22121A; border-color: #6A2334; }}
#toast[level="warning"] {{ background: #211B0E; border-color: #6B5217; }}
#toast[level="success"] {{ background: #0F1F1A; border-color: #1D4C3B; }}
#toast_text {{ font-size: {body}px; }}

/* status bar pills */
#pill {{
    background: {card_hi}; border: 1px solid {line}; border-radius: 22px;
    font-size: {label}px; font-weight: 600; color: {text_2}; padding: 0 16px; min-height: 44px;
}}
#state_pill_text {{ font-size: {body}px; font-weight: 700; letter-spacing: 0.6px; }}
#state_pill_sub {{ font-size: 13px; color: {text_3}; font-weight: 500; }}
#clock {{ font-size: {body}px; color: {text_2}; font-weight: 500; }}

/* touch keyboard */
#keyboard {{ background: {surface}; border-top: 1px solid {line_hi}; }}
#key {{
    background: {card_hi}; border: 1px solid {line}; border-radius: 12px;
    font-size: 22px; font-weight: 500; min-height: 64px; padding: 0;
}}
#key:pressed {{ background: {line_hi}; }}
#key[kind="mod"] {{ background: {card}; color: {text_2}; font-size: {label}px; font-weight: 600; }}
#key[kind="enter"] {{ background: {accent}; border-color: {accent}; color: #0B0A1A; font-size: {body}px; font-weight: 700; }}
#numpad {{ background: {card_hi}; border: 1px solid {line_hi}; border-radius: 24px; }}
#numpad_display {{
    font-family: "{mono}"; font-size: 40px; font-weight: 500; background: {bg};
    border: 1px solid {line}; border-radius: 16px; padding: 8px 18px; min-height: 72px;
}}
#numpad_title {{ font-size: {body}px; color: {text_2}; font-weight: 600; }}

/* log viewer (Activity page) */
#log_viewer {{ background: transparent; }}
#log_view {{
    background: {card}; border: 1px solid {line}; border-radius: 16px;
    font-family: "{mono}"; font-size: 14px; padding: 8px;
}}
#log_tool_button {{ min-height: 48px; font-size: {label}px; padding: 0 14px; }}
#log_search {{ min-height: 48px; }}
#log_footer, #log_match_label {{ color: {text_3}; font-size: 13px; }}
#log_message {{ color: {accent_hi}; font-size: 13px; }}
#log_link {{ background: transparent; border: none; color: {accent_hi}; min-height: 24px; padding: 0; }}
"""


def apply(app):
    """Load fonts and install the stylesheet and default font on the application"""
    load_fonts()
    app.setFont(font(T.body))
    app.setStyleSheet(stylesheet())
    # Touch: no hover styles are relied on; make scrolling by drag possible everywhere
    app.setAttribute(QtCore.Qt.AA_SynthesizeMouseForUnhandledTouchEvents, True)
