"""Shared fixtures for the AI Assistant tests. Run from the config directory: venv/bin/python -m pytest tests"""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import linuxcnc  # noqa: E402


class FakeStat:
    """Stand-in for linuxcnc.stat: a powered, homed, idle 3-axis mm machine"""

    def __init__(self, **overrides):
        self.task_state = linuxcnc.STATE_ON
        self.interp_state = linuxcnc.INTERP_IDLE
        self.joints = 3
        self.homed = (1, 1, 1)
        self.joint = [{"homing": 0}] * 3
        self.axis_mask = 0b111
        self.program_units = 2  # mm
        self.linear_units = 1.0
        self.gcodes = (0, 900, 210, 540)
        self.rotation_xy = 0.0
        self.position = [100.0, 50.0, -10.0] + [0.0] * 6
        self.g5x_offset = [10.0, 10.0, -100.0] + [0.0] * 6
        self.g92_offset = [0.0] * 9
        self.tool_offset = [0.0] * 9
        self.axis = [
            {"min_position_limit": 0.0, "max_position_limit": 500.0},
            {"min_position_limit": 0.0, "max_position_limit": 175.0},
            {"min_position_limit": -253.0, "max_position_limit": 0.0},
        ]
        self.g5x_index = 1  # G54, as reported once a work-offset command has run
        self.spindle = [{"enabled": 0, "speed": 0}]
        self.tool_in_spindle = 8
        self.file = ""
        self.ini_filename = ""
        self.__dict__.update(overrides)

    def poll(self):
        pass


@pytest.fixture(autouse=True)
def isolated_ai_log(tmp_path, monkeypatch):
    """Keep tests from writing to the real ai_assistant.log"""
    import log_sources
    path = tmp_path / "ai_assistant.log"
    monkeypatch.setattr(log_sources, "AI_LOG_FILE", str(path))
    return path


@pytest.fixture(autouse=True)
def no_real_heightmap(monkeypatch):
    """Tests never see a height map from a real camera scan of this machine"""
    import action_controller
    monkeypatch.setattr(action_controller, "_heightmap", lambda: None)


@pytest.fixture
def stat():
    return FakeStat()


@pytest.fixture
def ini_with_spindle_max(tmp_path):
    path = tmp_path / "machine.ini"
    path.write_text("[SPINDLE_0]\nMAX_OUTPUT = 3000\n")
    return str(path)


@pytest.fixture(scope="session")
def qapp():
    # A full QApplication (offscreen) so tests can create widgets
    from PyQt5.QtWidgets import QApplication
    return QApplication.instance() or QApplication([])


@pytest.fixture(autouse=True)
def isolated_fixtures(tmp_path, monkeypatch):
    """Keep tests from touching the real fixtures.json (saved work origins)"""
    from milo_ui.pages import offsets
    monkeypatch.setattr(offsets, "FIXTURE_FILE", str(tmp_path / "fixtures.json"))
