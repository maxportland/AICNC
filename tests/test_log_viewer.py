"""Log Viewer: level detection, incremental tailing, AI log file, and the widget's behavior"""

import os

import pytest

import log_sources
from log_sources import (DEBUG, ERROR, INFO, WARNING, LogSource, TailReader, append_ai_log, classify_line,
                         parse_lines, strip_ansi)


@pytest.mark.parametrize("line,level", [
    ("2026-09-27 17:05:02,939 - QTvcp.QTVCP.QT_ACTION - INFO - Homing Joint: -1", INFO),
    ("2026-09-27 17:05:02,939 - QTvcp.QTVCP.QT_ACTION - DEBUG - CALL_MDI Command: M8", DEBUG),
    ("2026-09-27 17:05:02,939 - QTvcp - ERROR - Something broke", ERROR),
    ("[QTvcp.QTVCP.QT_ISTAT][WARNING]  INI Parsing Error, No LOG_FILE_NAME Entry", WARNING),
    ("[QTvcp][DEBUG]  Loading the handler file. (qtvcp:260)", DEBUG),
    ("[QTvcp][CRITICAL]  Missing slot name", ERROR),
    ("2026-09-27 17:40:01 [USER] Move X ten", INFO),
    ("2026-09-27 17:40:01 [ERROR] OpenAI API failed", ERROR),
    ("2026-09-27 17:40:01 [TOOLS] T59 is listed 2 times", WARNING),
    ("2026-09-27 17:40:01 [VAD] Initial speech detected", DEBUG),
    ("Sun27 17:26: Hard limits tripped", ERROR),
    ("Sun27 17:26: Probe tripped during homing motion.", ERROR),
    ("Sun27 17:26: Machine ON", INFO),
    ("Sun27 17:26: MB Errors Changed : 0", INFO),
    ("Sun27 17:26: lbl_mb_errors: Setting BLACK background (error_count=0)", INFO),
    ("Traceback (most recent call last):", ERROR),
    ("ValueError: bad value", ERROR),
    ("RUN_IN_PLACE=no", INFO),
])
def test_classify_line(line, level):
    assert classify_line(line) == level


def test_continuation_lines_follow_their_record():
    entries = parse_lines(["Traceback (most recent call last):", '  File "x.py", line 1, in <module>',
                           "ValueError: boom", "Sun27 17:26: Machine ON"])
    assert [level for level, _ in entries] == [ERROR, ERROR, ERROR, INFO]


def test_ansi_codes_are_stripped_before_classifying():
    raw = "[QTvcp.QTVCP.QT_ISTAT][\x1b[33mWARNING\x1b[0m]  INI Parsing Error\n"
    assert strip_ansi(raw).startswith("[QTvcp.QTVCP.QT_ISTAT][WARNING]")
    assert parse_lines([raw]) == [(WARNING, "[QTvcp.QTVCP.QT_ISTAT][WARNING]  INI Parsing Error")]


def test_tail_reader_appends_and_buffers_partial_lines(tmp_path):
    path = tmp_path / "a.log"
    path.write_text("one\ntwo\n")
    reader = TailReader(str(path))
    assert reader.read_new() == (["one", "two"], False)
    with open(path, "a") as f:
        f.write("thr")
    assert reader.read_new() == ([], False)
    with open(path, "a") as f:
        f.write("ee\nfour\n")
    assert reader.read_new() == (["three", "four"], False)


def test_tail_reader_starts_near_the_end_of_big_files(tmp_path):
    path = tmp_path / "big.log"
    path.write_text("".join(f"line {i}\n" for i in range(1000)))
    lines, _ = TailReader(str(path), initial_bytes=100).read_new()
    assert lines[-1] == "line 999"
    assert all(line.startswith("line ") for line in lines)  # no half line at the start
    assert len(lines) < 20


def test_tail_reader_restarts_when_file_is_truncated_or_replaced(tmp_path):
    path = tmp_path / "a.log"
    path.write_text("old 1\nold 2\n")
    reader = TailReader(str(path))
    reader.read_new()
    path.write_text("new\n")  # truncated and rewritten, like machine_log.dat each run
    assert reader.read_new() == (["new"], True)
    replacement = tmp_path / "b.log"
    replacement.write_text("fresh file with more text than before\n")
    os.replace(replacement, path)
    assert reader.read_new() == (["fresh file with more text than before"], True)


def test_tail_reader_missing_file(tmp_path):
    assert TailReader(str(tmp_path / "nope.log")).read_new() == ([], False)
    assert TailReader(None).read_new() == ([], False)


def test_append_ai_log_timestamps_and_rotates(tmp_path, monkeypatch):
    path = tmp_path / "ai.log"
    append_ai_log("[USER] Move X ten", str(path))
    append_ai_log("[ERROR] line one\nline two", str(path))
    lines = path.read_text().splitlines()
    assert lines[0].endswith(" [USER] Move X ten") and lines[0][:4].isdigit()
    assert lines[1].endswith(" [ERROR] line one") and lines[2] == "    line two"
    assert [lv for lv, _ in parse_lines(lines)] == [INFO, ERROR, ERROR]
    monkeypatch.setattr(log_sources, "AI_LOG_MAX_BYTES", 10)
    append_ai_log("[MILO] after rotation", str(path))
    assert (tmp_path / "ai.log.1").exists()
    assert path.read_text().splitlines()[0].endswith("[MILO] after rotation")


# --- widget ---------------------------------------------------------------

LOG = """2026-09-27 17:00:00,000 - QTvcp - INFO - Screen started
2026-09-27 17:00:01,000 - QTvcp - DEBUG - Loading handler
2026-09-27 17:00:02,000 - QTvcp - WARNING - Spindle speed low
2026-09-27 17:00:03,000 - QTvcp - ERROR - Probe failed
2026-09-27 17:00:04,000 - QTvcp - INFO - Probe retry
"""


@pytest.fixture
def viewer(qapp, tmp_path):
    from log_viewer import LogViewer
    main, other = tmp_path / "main.log", tmp_path / "other.log"
    main.write_text(LOG)
    other.write_text("Sun27 17:26: Machine ON\n")
    sources = [LogSource("main", "Main", "", lambda: str(main)), LogSource("other", "Other", "", lambda: str(other))]
    widget = LogViewer(sources=sources, export_dir=str(tmp_path / "exports"))
    widget.resize(900, 400)
    widget.main_path = main
    return widget


def lines(viewer):
    return viewer.view.toPlainText().splitlines()


def test_loads_and_counts_levels(viewer):
    assert len(lines(viewer)) == 5
    assert viewer.counts == {ERROR: 1, WARNING: 1, INFO: 2, DEBUG: 1}
    assert viewer.level_buttons[ERROR].text() == "Errors  1"


def test_level_filter(viewer):
    viewer.level_buttons[DEBUG].setChecked(False)
    viewer.level_buttons[INFO].setChecked(False)
    assert [l.split(" - ")[2] for l in lines(viewer)] == ["WARNING", "ERROR"]


def test_search_highlights_and_navigates(viewer):
    viewer.search.setText("probe")
    assert len(viewer.matches) == 2
    assert viewer.match_label.text() == "1 of 2"
    viewer.next_match()
    assert viewer.match_label.text() == "2 of 2"
    viewer.next_match()
    assert viewer.match_label.text() == "1 of 2"  # wraps around
    assert not viewer.follow_button.isChecked()  # jumping to a match stops following


def test_matches_only(viewer):
    viewer.search.setText("probe")
    viewer.matches_only.setChecked(True)
    assert len(lines(viewer)) == 2


def test_new_lines_are_appended_live(viewer):
    with open(viewer.main_path, "a") as f:
        f.write("2026-09-27 17:00:05,000 - QTvcp - ERROR - Limit switch\n")
    viewer.poll()
    assert lines(viewer)[-1].endswith("Limit switch")
    assert viewer.counts[ERROR] == 2


def test_clear_and_show_all(viewer):
    viewer.clear()
    assert viewer.view.toPlainText() == ""
    with open(viewer.main_path, "a") as f:
        f.write("2026-09-27 17:00:06,000 - QTvcp - INFO - After clear\n")
    viewer.poll()
    assert lines(viewer) == ["2026-09-27 17:00:06,000 - QTvcp - INFO - After clear"]
    viewer.show_all()
    assert len(lines(viewer)) == 6


def test_save_writes_what_is_shown(viewer):
    viewer.level_buttons[DEBUG].setChecked(False)
    path = viewer.save()
    text = open(path).read()
    assert "Loading handler" not in text and "Probe failed" in text
    assert text.startswith("# Main log from ")


def test_switching_sources(viewer):
    viewer.select_source("other")
    assert lines(viewer) == ["Sun27 17:26: Machine ON"]
    assert viewer.source_buttons["other"].isChecked()


def test_nul_padding_is_removed():
    assert parse_lines(["\x00\x00\x002026-09-27 17:00:00,000 - QTvcp - INFO - after gap"]) == [
        (INFO, "2026-09-27 17:00:00,000 - QTvcp - INFO - after gap")]
