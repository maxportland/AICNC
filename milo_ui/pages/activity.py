"""
Activity: every log the machine keeps (machine, screen, Milo, LinuxCNC) in one live,
searchable view.
"""

from PyQt5 import QtWidgets

from milo_ui import kit
from milo_ui.shell import Page


class ActivityPage(Page):
    key = "activity"
    title = "Activity"
    icon = "list-bullets"
    wants_stage = False

    def __init__(self, shell, parent=None):
        super().__init__(parent)
        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        card = kit.Card(title="Activity")
        try:
            from log_viewer import LogViewer
            self.viewer = LogViewer()
            card.add(self.viewer, 1)
        except Exception as e:  # the log viewer needs the config directory on the path
            self.viewer = None
            card.add(kit.label(f"Logs unavailable: {e}", "muted", wrap=True))
        layout.addWidget(card)
