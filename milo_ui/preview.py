"""
Run the Milo screen against a simulated machine, with no LinuxCNC.

    venv/bin/python -m milo_ui.preview                    # interactive window (1920x1080)
    venv/bin/python -m milo_ui.preview --shot out.png --page home --scenario program

Screenshots render offscreen, so this works over SSH. Scenarios set up the pretend machine
and a scripted conversation, so every state of the design can be checked by eye.
"""

import argparse
import os
import sys

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if HERE not in sys.path:
    sys.path.insert(0, HERE)


class ScriptedEngine:
    """Stands in for MiloEngine in previews: remembers what it was asked, never calls the network"""

    def __init__(self, shell):
        self.shell = shell
        self.settings = {"api_key": "sk-preview", "show_details": False, "recording_timeout": 20,
                         "silence_timeout": 2.0, "wake_word": True}
        self.wake_word_ready = True
        self.confirmation = None
        self.asked = []

    def api_key(self):
        return self.settings["api_key"]

    @property
    def show_details(self):
        return self.settings["show_details"]

    def save_settings(self, **changes):
        self.settings.update(changes)

    def submit(self, text, from_voice=False):
        self.asked.append(text)
        self.shell.bridge.on_message(f"[USER] {text}")
        self.shell.bridge.on_busy(True, "Thinking…")

    def toggle_voice(self):
        self.shell.bridge.on_voice_state("listening")

    def cancel_listening(self):
        self.shell.bridge.on_voice_state("idle")

    def confirm(self):
        self.shell.bridge.on_proposal(None)

    def cancel(self):
        self.shell.bridge.on_proposal(None)

    def propose_run(self):
        pass

    def reset_message_history(self):
        pass

    def list_sessions(self):
        return []


def build(page="home"):
    from PyQt5 import QtWidgets
    from milo_ui import theme
    from milo_ui.machine import SimMachine
    from milo_ui.shell import MiloShell, Prefs
    from milo_ui.pages import build_pages

    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication(sys.argv)
    theme.apply(app)
    machine = SimMachine()
    prefs = Prefs(path=os.path.join(os.environ.get("TMPDIR", "/tmp"), "milo_preview_prefs.json"))
    shell = MiloShell(machine, prefs=prefs)
    shell.make_bridge()
    build_pages(shell)
    engine = ScriptedEngine(shell)
    shell.attach_engine(engine)
    shell.resize(1920, 1080)
    shell.navigate(page)
    return app, shell, machine


def scenario(name, shell, machine):
    from PyQt5 import QtCore
    b = shell.bridge
    here = os.path.join(HERE, "milo_ui", "preview_part.ngc")
    if name in ("off",):
        machine.estop = False
        machine.on = False
    if name == "estop":
        machine.estop = True
    if name in ("ready", "chat", "confirm", "program", "running", "listening", "jog", "files"):
        machine.estop, machine.on = False, True
        machine.homed = {a: True for a in machine.axes}
        machine.tool, machine.tool_comment, machine.tool_diameter = 8, "1/4\" 4-flute endmill", 6.35
    if name in ("program", "running", "files"):
        machine.open_program(here if os.path.exists(here) else "/home/cnc/linuxcnc/nc_files/ai/circle_76mm.ngc")
    if name == "running":
        machine.run(0)
        machine.line = 212
        machine.run_elapsed = 318
        machine.spindle_dir, machine.spindle_requested, machine.spindle_actual = 1, 2400, 2396
        machine.feed_rate = 842
        machine.feed_override = 110
    machine._emit("state", "homing", "program", "tool", "spindle", "overrides", "offsets")
    machine.position_changed.emit()

    if name in ("chat", "confirm", "program", "running"):
        b.on_message("[USER] Move X ten millimeters")
        b.on_message("[CONFIRM] Rapid X +10 mm (incremental)  [G91 G0 X10.0000] - say 'yes' or press Confirm, 'no' or Cancel to abort.")
        b.on_message("[CONFIRM] Confirmed: Rapid X +10 mm (incremental)")
        b.on_message("[MDI] Executed: G91 G0 X10.0000")
        b.on_message("[USER] What tool is loaded?")
        b.on_message("[MILO] Tool 8 is in the spindle: a 1/4\" 4-flute endmill, 6.35 mm diameter, with its length "
                     "offset applied. It's a good fit for the 76 mm circle you mentioned.")
    if name in ("program", "running"):
        b.on_message("[USER] Cut a 3 inch circle, 5 mm deep, centered at 50 50")
        b.on_message("[CAM] Received a program with 1 operation(s). Generating G-code...")
        b.on_program_ready({"name": "circle_76mm.ngc", "path": "", "stock": "100 × 100 × 10 mm",
                            "ops": [{"name": "Profile", "tool": 8}],
                            "tools": [{"number": 8, "diameter": 6.35, "description": "1/4\" 4-flute endmill"}]})
    if name == "confirm":
        b.on_message("[USER] Home the machine and then go to X0 Y0")
        shell.engine.confirmation = type("Gate", (), {"remaining_seconds": lambda self: 21.4})()
        b.on_message("[CONFIRM] Move to X0 Y0 in G54 (machine X 94.675 Y 84.350)  [G90 G0 X0 Y0] - say 'yes'")
        b.on_proposal({"kind": "mdi", "command": "G90 G0 X0 Y0",
                       "summary": "Move to X0 Y0 in G54 (machine X 94.675 Y 84.350)"})
    if name == "listening":
        b.on_voice_state("listening")
        b.on_audio_level(0.6)
    if name == "chat":
        b.on_busy(True, "Thinking…")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--shot", help="save a screenshot to this PNG and exit")
    parser.add_argument("--page", default="home")
    parser.add_argument("--scenario", default="ready")
    parser.add_argument("--keyboard", action="store_true", help="show the on-screen keyboard")
    parser.add_argument("--popover", default="", help="open a popover: axis, numpad, menu")
    args = parser.parse_args()
    if args.shot:
        os.environ["QT_QPA_PLATFORM"] = "offscreen"
    app, shell, machine = build(args.page)
    scenario(args.scenario, shell, machine)
    shell.navigate(args.page)
    shell.show()
    from PyQt5 import QtCore, QtWidgets
    if args.keyboard:
        shell.composer.input.setFocus()
        shell.keyboard.show_for(shell.composer.input)
    if args.popover == "axis":
        dro = shell.pages["home"].dro
        dro._axis_menu("X")
    elif args.popover in ("library", "import"):
        # Preview against copies of the Desktop libraries, never the real library folder
        import glob, shutil, tempfile
        import fusion_tools
        fusion_tools.LIBRARY_DIR = tempfile.mkdtemp(prefix="milo-lib-")
        if args.popover == "library":
            for path in glob.glob(os.path.expanduser("~/Desktop/*.json"))[:3]:
                shutil.copy(path, fusion_tools.LIBRARY_DIR)
        shell.navigate("tools")
        library = shell.pages["tools"].open_library()
        if args.popover == "import":
            library._import()
        else:
            library.search.setText("W04007")
    elif args.popover == "numpad":
        from milo_ui import kit
        kit.NumPad(shell, "Spindle speed", lambda v: None, initial=2400, units="rpm",
                   hint="100 to 3000 rpm", presets=[("1000", 1000), ("2000", 2000), ("3000", 3000)]).show_centered()
    if args.shot:
        for _ in range(12):
            app.processEvents()
            QtCore.QThread.msleep(30)
        shell.grab().save(args.shot)
        print(args.shot)
        return 0
    return app.exec_()


if __name__ == "__main__":
    sys.exit(main())
