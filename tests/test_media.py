import shutil
import subprocess

import pytest

from video_agent.backends import IsolatedVisionOCR
from video_agent.config import OCRConfig
from video_agent.media import Window, frame_at, metadata, sample_window, sampled_frames


@pytest.fixture
def video(tmp_path):
    if not shutil.which("ffmpeg"):
        pytest.skip("ffmpeg is not installed")
    path = tmp_path / "sample.mp4"
    subprocess.run(
        [
            "ffmpeg",
            "-nostdin",
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            "testsrc2=size=160x120:rate=10:duration=2",
            "-c:v",
            "mpeg4",
            str(path),
        ],
        check=True,
    )
    return path


def test_real_decode_and_short_tail(video, tmp_path):
    info = metadata(video)
    assert info["duration_ms"] == 2000
    assert not info["has_audio"]
    assert frame_at(video, 0).size == (160, 120)
    samples = sample_window(video, Window(1900, 2000), 6, 80, tmp_path)
    assert samples and samples[0][0] == 1900
    assert samples[0][1].is_file()
    times = [t for t, _ in sampled_frames(video, 1, 80)]
    assert times == [0, 1000]


def test_ocr_checkpoint_does_not_skip_sampling_point(video):
    assert [t for t, _ in sampled_frames(video, 1, 80, 1)] == [1000]


def test_vision_ocr_recognizes_english_without_model_download():
    pytest.importorskip("Vision")
    from PIL import Image, ImageDraw, ImageFont

    image = Image.new("RGB", (800, 200), "white")
    font_path = "/System/Library/Fonts/Supplemental/Arial.ttf"
    if not __import__("pathlib").Path(font_path).exists():
        pytest.skip("Arial not available")
    font = ImageFont.truetype(font_path, 64)
    ImageDraw.Draw(image).text((25, 55), "FOX CAMERA", font=font, fill="black")
    backend = IsolatedVisionOCR(OCRConfig())
    try:
        lines = backend.recognize(image)
    finally:
        backend.close()
    assert "FOX CAMERA" in " ".join(line["text"] for line in lines)
