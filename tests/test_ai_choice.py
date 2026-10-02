"""Tests for choosing Milo's OpenAI model and its speed vs. quality level"""

import pytest

import ai_config


@pytest.fixture(autouse=True)
def reset_choice():
    yield
    ai_config.choose("", "balanced")
    ai_config.choose_transcription("")


def test_defaults_are_the_configured_models_and_efforts():
    ai_config.choose("", "balanced")
    assert ai_config.job_model("router") == ai_config.ROUTER_MODEL
    assert ai_config.job_options("cam", 0.2) == {"reasoning_effort": ai_config.CAM_EFFORT}


def test_one_model_for_every_job_and_quality_levels():
    ai_config.choose("gpt-5.5", "best")
    assert {ai_config.job_model(job) for job in ("router", "cam", "review")} == {"gpt-5.5"}
    assert ai_config.job_options("cam", 0.2) == {"reasoning_effort": "high"}
    ai_config.choose(quality="fast")
    assert ai_config.job_options("router", 0) == {"reasoning_effort": "low"}
    assert ai_config.job_model("cam") == "gpt-5.5"  # quality alone leaves the model


def test_older_models_get_a_temperature_instead():
    ai_config.choose("gpt-4o", "best")
    assert ai_config.job_options("cam", 0.2) == {"temperature": 0.2}
    assert not ai_config.is_reasoning("gpt-4o") and ai_config.is_reasoning("o4-mini")


def test_model_list_keeps_suitable_models_newest_first():
    models = [("gpt-4o", 1), ("gpt-5.6-sol", 9), ("gpt-5.5-pro", 8), ("gpt-5-codex", 7), ("gpt-4o-mini-tts", 6),
              ("gpt-5-2025-08-07", 5), ("gpt-5.1-chat-latest", 4), ("o3-mini", 3), ("o3", 2), ("whisper-1", 1)]
    assert ai_config.chat_models(models) == ["gpt-5.6-sol", "o3", "gpt-4o"]


def test_engine_saves_and_applies_the_choice(tmp_path, qapp):
    from milo_engine import MiloEngine
    path = tmp_path / "cfg.json"
    engine = MiloEngine(settings_path=str(path), config_dir=str(tmp_path))
    engine.save_settings(ai_model="gpt-5.4-mini", ai_quality="fast")
    assert ai_config.job_model("router") == "gpt-5.4-mini"
    ai_config.choose("", "balanced")
    again = MiloEngine(settings_path=str(path), config_dir=str(tmp_path))
    again.load_settings()
    assert ai_config.job_model("cam") == "gpt-5.4-mini"
    assert ai_config.job_options("cam", 0.2) == {"reasoning_effort": "low"}


def test_transcription_model_choice_and_list():
    assert ai_config.transcription_model() == ai_config.TRANSCRIPTION_MODEL
    ai_config.choose_transcription("whisper-1")
    assert ai_config.transcription_model() == "whisper-1"
    models = [("whisper-1", 1), ("gpt-transcribe", 9), ("gpt-live-transcribe", 9), ("gpt-4o-transcribe-diarize", 5),
              ("gpt-4o-mini-transcribe-2025-12-15", 6), ("gpt-4o-mini-transcribe", 4), ("gpt-5.5", 8)]
    assert ai_config.transcription_models(models) == ["gpt-transcribe", "gpt-4o-mini-transcribe", "whisper-1"]


def test_engine_applies_the_transcription_choice(tmp_path, qapp):
    from milo_engine import MiloEngine
    engine = MiloEngine(settings_path=str(tmp_path / "cfg.json"), config_dir=str(tmp_path))
    engine.save_settings(transcription_model="gpt-4o-mini-transcribe")
    assert ai_config.transcription_model() == "gpt-4o-mini-transcribe"
