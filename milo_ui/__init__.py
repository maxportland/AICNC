"""
Milo UI: an AI-first, touch-first operator screen for LinuxCNC.

The screen is built in Python rather than Designer so the layout, the widgets and the
styling are one coherent system. Nothing in here talks to LinuxCNC directly: pages read
and command the machine through a MachineModel (milo_ui.machine), which has a real
qtvcp-backed implementation and a simulated one for previews and tests.
"""
