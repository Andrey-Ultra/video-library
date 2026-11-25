import pytest

from video_agent.backends.text_recognition import IsolatedVisionOCR, VisionOCR
from video_agent.config import OCRConfig


def test_vision_accelerator_failure_retries_same_frame_and_stays_on_cpu(monkeypatch):
    backend = VisionOCR(OCRConfig())
    calls = []
    image = object()

    def request(frame, cpu_only):
        calls.append((frame, cpu_only))
        if not cpu_only:
            raise RuntimeError("e5rtError")
        return [{"text": "FOREST"}]

    monkeypatch.setattr(backend, "_recognize", request)
    assert backend.recognize(image)[0]["text"] == "FOREST"
    assert backend.recognize(image)[0]["text"] == "FOREST"
    assert calls == [(image, False), (image, True), (image, True)]


def test_vision_cpu_failure_is_not_silently_skipped(monkeypatch):
    backend = VisionOCR(OCRConfig())

    def broken(*_):
        raise RuntimeError("OCR unavailable")

    monkeypatch.setattr(backend, "_recognize", broken)
    with pytest.raises(RuntimeError, match="OCR unavailable"):
        backend.recognize(object())


def test_isolated_vision_restarts_poisoned_worker_and_retries_same_image(monkeypatch):
    backend = IsolatedVisionOCR(OCRConfig())
    image = object()
    calls, resets = [], []

    def request(frame):
        calls.append(frame)
        if len(calls) == 1:
            raise RuntimeError("e5rt poisoned process")
        return [{"text": "FOREST"}]

    monkeypatch.setattr(backend, "_request", request)
    monkeypatch.setattr(backend, "close", lambda: resets.append(True))
    assert backend.recognize(image) == [{"text": "FOREST"}]
    assert calls == [image, image]
    assert resets == [True]


def test_isolated_vision_surfaces_persistent_failures(monkeypatch):
    backend = IsolatedVisionOCR(OCRConfig())

    def broken(_):
        raise RuntimeError("broken frame")

    monkeypatch.setattr(backend, "_request", broken)
    with pytest.raises(RuntimeError, match="broken frame"):
        backend.recognize(object())
