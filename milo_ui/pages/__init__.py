"""The screen's pages, in rail order"""


def build_pages(shell, qtvcp_widgets=None):
    """Create every page and add it to the shell. qtvcp_widgets holds the real LinuxCNC
    widgets (tool table, offsets, G-code view, probe) when running under qtvcp."""
    from milo_ui.pages.home import HomePage

    widgets = qtvcp_widgets or {}
    from milo_ui.pages.jog import JogPage
    from milo_ui.pages.program import ProgramPage
    from milo_ui.pages.tools import ToolsPage
    from milo_ui.pages.offsets import OffsetsPage
    from milo_ui.pages.probe import ProbePage
    from milo_ui.pages.activity import ActivityPage
    from milo_ui.pages.settings import SettingsPage

    shell.add_page(HomePage(shell))
    shell.add_page(JogPage(shell))
    shell.add_page(ProgramPage(shell, widgets.get("gcode_view")))
    shell.add_page(ToolsPage(shell, widgets.get("tool_table")))
    shell.add_page(OffsetsPage(shell, widgets.get("offset_table")))
    shell.add_page(ProbePage(shell, widgets.get("probe")))
    shell.add_page(ActivityPage(shell), bottom=True)
    shell.add_page(SettingsPage(shell, widgets.get("pendant")))
