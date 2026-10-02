"""
Toolpath: the toolpath preview with the whole content area to itself.
"""

from milo_ui.shell import Page


class ToolpathPage(Page):
    """Empty on purpose: the shell's stage (the one toolpath view) fills this page"""

    key = "toolpath"
    title = "Toolpath"
    icon = "path"
    wants_stage = True
    full_stage = True

    def __init__(self, shell, parent=None):
        super().__init__(parent)
        self.shell = shell
