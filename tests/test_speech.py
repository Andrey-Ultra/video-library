import sys
from dataclasses import asdict
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from video_agent.app import create_app
from video_agent.app_state import AppState, parse_config
from video_agent.backends.speech import ParakeetTranscriber, WhisperTranscriber
from video_agent.config import Config, SpeechConfig, load_config, signature
from video_agent.signatures import backend_version, stage_signatures
from video_agent.speech_options import PARAKEET_V2, PARAKEET_V3


def test_legacy_pinned_speech_configs_keep_checkpoints(tmp_path):
    for backend, model, package in (
        ("parakeet", PARAKEET_V2, "parakeet-mlx"),
        ("faster-whisper", "large-v3-turbo", "faster-whisper"),
    ):
        legacy = {
            "backend": backend,
            "model": model,
            "chunk_s": 120.0,
            "device": "cuda",
            "compute_type": "float16",
        }
        config = parse_config({"speech": legacy})
        assert stage_signatures(config)["speech"] == signature("speech-v1", legacy, backend_version(package))
        path = tmp_path / "old.toml"
        path.write_text(f'[speech]\nbackend = "{backend}"\nmodel = "{model}"\n')
        assert stage_signatures(load_config(path))["speech"] == stage_signatures(config)["speech"]


@pytest.mark.parametrize(
    "backend,language,model",
    [
        ("parakeet", "ru", PARAKEET_V3),
        ("faster-whisper", "xx", "large-v3-turbo"),
        ("faster-whisper", "ru", "small.en"),
        ("faster-whisper", ["en", "ru"], "large-v3-turbo"),
        ("faster-whisper", None, "large-v3-turbo"),
    ],
)
def test_invalid_language_or_incompatible_model_rejected(backend, language, model):
    cfg = Config()
    cfg.speech = SpeechConfig(backend=backend, language=language, model=model)
    with pytest.raises(ValueError):
        cfg.validate()


def test_api_language_roundtrip_and_speech_only_fingerprint_change(tmp_path):
    folder = tmp_path / "footage"
    folder.mkdir()
    state_dir = tmp_path / "state"
    state = AppState(state_dir)
    lib = state.select(str(folder))
    client = TestClient(create_app(state_dir))
    options = client.get("/api/state").json()["speech_options"]
    assert len(options["parakeet_models"][0]["languages"]) == 25
    assert "Russian" in options["parakeet_models"][0]["languages"]
    assert {"ru", "en", "zh", "ja", "ar", "hi"} <= {x["code"] for x in options["whisper_languages"]}
    config = asdict(load_config(profile="cuda"))
    before = stage_signatures(parse_config(config))
    config["speech"]["language"] = "ru"
    response = client.put(
        f"/api/libraries/{lib['id']}/config", json=config, headers={"X-Video-Agent": "local"}
    )
    assert response.status_code == 200
    saved = AppState(state_dir).library(lib["id"])["config"]
    assert saved["speech"]["language"] == "ru"
    after = stage_signatures(parse_config(saved))
    assert {stage for stage in before if before[stage] != after[stage]} == {"speech"}
    config["speech"]["language"] = ["en", "ru"]
    assert (
        client.put(
            f"/api/libraries/{lib['id']}/config", json=config, headers={"X-Video-Agent": "local"}
        ).status_code
        == 400
    )


@pytest.mark.parametrize("model,language", [(PARAKEET_V3, None), (PARAKEET_V2, "en")])
def test_parakeet_one_pass_preserves_mixed_text_and_does_not_invent_language(monkeypatch, model, language):
    monkeypatch.setattr("video_agent.backends.speech.check_parakeet_download_space", lambda _: None)
    calls = []
    texts = ["Привет, мир.", "Hello world."]

    def transcribe(audio):
        calls.append(audio)
        return SimpleNamespace(
            sentences=[
                SimpleNamespace(
                    start=i, end=i + 1, text=text, tokens=[SimpleNamespace(start=i, end=i + 1, text=text)]
                )
                for i, text in enumerate(texts)
            ]
        )

    loaded = []

    def load(model_id):
        loaded.append(model_id)
        return SimpleNamespace(transcribe=transcribe)

    monkeypatch.setitem(sys.modules, "parakeet_mlx", SimpleNamespace(from_pretrained=load))
    backend = ParakeetTranscriber(SpeechConfig(model=model))
    result = backend.transcribe("audio.wav")
    assert calls == ["audio.wav"] and loaded == [model]
    assert [s["text"] for s in result] == texts
    assert all(s["language"] == language for s in result)
    assert result[1]["words"][0]["start_s"] == 1
    backend.transcribe("second.wav")
    assert loaded == [model]  # reuse the loaded model


@pytest.mark.parametrize("language", ["auto", "ru", "en", "zh"])
def test_whisper_passes_language_and_preserves_original_text(monkeypatch, language):
    calls = []

    def transcribe(audio, **kwargs):
        calls.append((audio, kwargs))
        segments = iter(
            [
                SimpleNamespace(
                    start=1, end=2, text=" Привет ", words=[SimpleNamespace(start=1, end=2, word=" Привет")]
                )
            ]
        )
        return segments, SimpleNamespace(language="ru")

    class FakeModel:
        def __init__(self, *args, **kwargs):
            self.model = SimpleNamespace(is_multilingual=True)
            self.transcribe = transcribe

    monkeypatch.setitem(sys.modules, "faster_whisper", SimpleNamespace(WhisperModel=FakeModel))
    result = WhisperTranscriber(
        SpeechConfig(backend="faster-whisper", model="large-v3-turbo", language=language)
    ).transcribe("audio.wav")
    assert len(calls) == 1
    assert calls[0][1] == {
        "language": None if language == "auto" else language,
        "task": "transcribe",
        "multilingual": language == "auto",
        "word_timestamps": True,
        "vad_filter": True,
    }
    assert result[0]["text"] == "Привет"
    assert result[0]["language"] == (None if language == "auto" else language)
    assert result[0]["chunk_language"] == "ru"


def test_parakeet_download_checks_free_space_but_reuses_cached_model(tmp_path, monkeypatch):
    from video_agent.backends import speech
    from video_agent.backends.common import ModelSetupError

    cached = tmp_path / "model.safetensors"
    monkeypatch.setitem(
        sys.modules, "huggingface_hub", SimpleNamespace(try_to_load_from_cache=lambda *a: None)
    )
    monkeypatch.setitem(
        sys.modules, "huggingface_hub.constants", SimpleNamespace(HF_HUB_CACHE=str(tmp_path / "hub"))
    )
    monkeypatch.setattr(speech.shutil, "disk_usage", lambda _: SimpleNamespace(free=1800 * 1024**2))
    with pytest.raises(ModelSetupError, match="3 GB"):
        speech.check_parakeet_download_space(PARAKEET_V3)
    cached.write_bytes(b"cached weights")
    monkeypatch.setitem(
        sys.modules, "huggingface_hub", SimpleNamespace(try_to_load_from_cache=lambda *a: str(cached))
    )
    speech.check_parakeet_download_space(PARAKEET_V3)
