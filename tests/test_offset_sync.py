"""Work offsets on the real screen before LinuxCNC's status reports them (g5x_index 0 after startup)"""

import pytest

import machine_safety
from conftest import FakeStat
from milo_ui.machine import MachineModel, QtvcpMachine
from test_machine_safety import VAR_FILE
from test_milo_ui import shell  # noqa: F401  (fixture)


class FakeAction:
    def __init__(self):
        self.systems = []

    def SET_USER_SYSTEM(self, name):
        self.systems.append(name)


class FakeStatus:
    def __init__(self, stat):
        self.stat = stat


@pytest.fixture
def unsynced(tmp_path):
    (tmp_path / "linuxcnc.var").write_text(VAR_FILE)
    ini = tmp_path / "machine.ini"
    ini.write_text("[RS274NGC]\nPARAMETER_FILE = linuxcnc.var\n")
    machine_safety._ini_cache.clear()
    return FakeStat(g5x_index=0, g5x_offset=[0.0] * 9, ini_filename=str(ini), inpos=True)


@pytest.fixture
def machine(qapp, unsynced):
    """A QtvcpMachine without qtvcp: fake STATUS/ACTION, powered on and idle"""
    m = QtvcpMachine.__new__(QtvcpMachine)
    MachineModel.__init__(m)
    m.STATUS, m.ACTION = FakeStatus(unsynced), FakeAction()
    m._offset_sync_tried = False
    m.estop, m.on = False, True
    m.homed = {a: True for a in m.axes}
    return m


def test_dro_shows_work_position_from_file_offsets(machine):
    """The reported bug: after 'go to X250' the Work DRO read 480 (machine), not 250"""
    absolute = [480.0, 8.4858, -147.907333] + [0.0] * 6
    machine._on_position(None, absolute, absolute, [0.0] * 9, [0.0] * 9)  # qtvcp's relative = machine
    assert machine.pos_abs == pytest.approx([480.0, 8.4858, -147.907333])
    assert machine.pos_rel == pytest.approx([250.0, 0.0, 0.0])
    assert machine.work_offset_known


def test_dro_trusts_qtvcp_once_synced(machine):
    machine.STATUS.stat.g5x_index = 1
    machine._on_position(None, [480.0, 0, 0] + [0.0] * 6, [250.0, 1, 2] + [0.0] * 6, [0.0] * 9, [0.0] * 9)
    assert machine.pos_rel == [250.0, 1, 2]


def test_dro_flags_unknown_offset(machine):
    machine.STATUS.stat.ini_filename = ""  # no parameter file to fall back on
    topics = []
    machine.changed.connect(topics.append)
    machine._on_position(None, [480.0, 0, 0] + [0.0] * 6, [480.0, 0, 0] + [0.0] * 6, [0.0] * 9, [0.0] * 9)
    assert not machine.work_offset_known and "offsets" in topics


def test_offsets_synced_once_after_power_on(machine, qapp):
    s = machine.STATUS.stat
    machine._sync_offsets_if_needed(s)
    machine._sync_offsets_if_needed(s)  # a second poll before it ran doesn't queue another
    qapp.processEvents()
    assert machine.ACTION.systems == ["G54"]
    machine._sync_offsets_if_needed(s)
    qapp.processEvents()
    assert machine.ACTION.systems == ["G54"]  # once per power-on
    machine.on = False
    machine._sync_offsets_if_needed(s)
    machine.on = True
    machine._sync_offsets_if_needed(s)
    qapp.processEvents()
    assert machine.ACTION.systems == ["G54", "G54"]


def test_sync_selects_the_active_system(machine, qapp):
    machine.STATUS.stat.gcodes = (0, 900, 210, 560)
    machine._sync_offsets_if_needed(machine.STATUS.stat)
    qapp.processEvents()
    assert machine.ACTION.systems == ["G56"]


@pytest.mark.parametrize("state", [
    {"on": False}, {"estop": True}, {"interp": "running"}, {"mode": "auto"}, {"homing": True},
    {"homed": {"X": True, "Y": True, "Z": False}},  # LinuxCNC refuses MDI before homing
])
def test_no_sync_while_not_ready(machine, qapp, state):
    for name, value in state.items():
        setattr(machine, name, value)
    machine._sync_offsets_if_needed(machine.STATUS.stat)
    qapp.processEvents()
    assert machine.ACTION.systems == []


def test_no_sync_while_moving_or_synced(machine, qapp):
    s = machine.STATUS.stat
    s.inpos = False
    machine._sync_offsets_if_needed(s)
    s.inpos, s.g5x_index = True, 1
    machine._sync_offsets_if_needed(s)
    qapp.processEvents()
    assert machine.ACTION.systems == []


def test_fixture_saves_g54_from_the_file_before_sync(machine, unsynced):
    """Saving a fixture at startup must not store X0 Y0 Z0 (the unsynced status offset): the
    machine reads G54 from linuxcnc.var until LinuxCNC reports it, and says None if it can't"""
    assert machine.current_offset("G54")[0] == pytest.approx([230.0, 8.4858, -147.907333])
    unsynced.ini_filename = ""  # no way to read G54
    assert machine.current_offset("G54") is None


def test_offsets_page_wont_save_an_unknown_origin(shell, monkeypatch):
    page = shell.pages["offsets"]
    monkeypatch.setattr(page.machine, "current_offset", lambda wcs: None)
    page.refresh()
    asked = []
    monkeypatch.setattr(shell, "ask_text", lambda *a, **k: asked.append(a))
    page._save_fixture()
    assert asked == []


def test_sync_waits_for_homing_only_when_required(machine, qapp):
    machine.homed = {a: False for a in machine.axes}
    machine.home_required = False  # [TRAJ] NO_FORCE_HOMING = 1
    machine._sync_offsets_if_needed(machine.STATUS.stat)
    qapp.processEvents()
    assert machine.ACTION.systems == ["G54"]
