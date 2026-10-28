"""End-to-end export checks that do not need a face model."""

from __future__ import annotations

import subprocess
import wave
from pathlib import Path

import pytest

from anomizer.ffmpeg_utils import find_ffmpeg
from anomizer.processor import ProcessConfig, probe_video, process_video


def _make_av_fixture(path: Path) -> None:
    result = subprocess.run(
        [
            find_ffmpeg(),
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "testsrc2=size=160x120:rate=10:duration=1",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=220:duration=1",
            "-c:v",
            "libx264",
            "-preset",
            "ultrafast",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            "-shortest",
            str(path),
        ],
        check=False,
        capture_output=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr.decode(errors="replace")


def _audio_duration(path: Path, tmp_path: Path) -> float:
    wav = tmp_path / "audio.wav"
    result = subprocess.run(
        [
            find_ffmpeg(),
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-i",
            str(path),
            "-vn",
            "-ac",
            "1",
            str(wav),
        ],
        check=False,
        capture_output=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr.decode(errors="replace")
    with wave.open(str(wav), "rb") as audio:
        return audio.getnframes() / audio.getframerate()


def test_export_without_pitch_keeps_container_duration(tmp_path: Path) -> None:
    source = tmp_path / "input.mp4"
    output = tmp_path / "output.mp4"
    _make_av_fixture(source)

    process_video(
        ProcessConfig(
            input_path=source,
            output_path=output,
            blur_faces=False,
            pitch_semitones=0.0,
            volume=0.5,
            preset="ultrafast",
            crf=28,
        )
    )

    info = probe_video(output)
    assert info.frame_count == 10
    assert info.duration == pytest.approx(1.0, abs=0.02)
    assert _audio_duration(output, tmp_path) == pytest.approx(1.0, abs=0.05)
