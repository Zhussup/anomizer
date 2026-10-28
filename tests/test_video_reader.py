"""Decoding checks for interlaced sources and the FFmpeg fallback reader."""

from __future__ import annotations

import math
import subprocess
import wave
from pathlib import Path

import cv2
import numpy as np
import pytest

from anomizer import processor
from anomizer.ffmpeg_utils import (
    MediaInfo,
    find_ffmpeg,
    probe_frame_field_order,
    probe_media,
)
from anomizer.processor import ProcessConfig, probe_video, process_video
from anomizer.video_reader import FFmpegFrameReader, open_video_source


def _run_ffmpeg(args: list[str]) -> None:
    result = subprocess.run(
        [find_ffmpeg(), "-hide_banner", "-loglevel", "error", "-y", *args],
        check=False,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stderr


def _make_interlaced_fixture(
    path: Path, *, with_audio: bool = False, bottom_first: bool = False
) -> None:
    """Create a small 10 fps, 1 second interlaced H.264 file.

    ``tinterlace`` halves the frame rate, so the source is generated at 20 fps
    to end up with ten frames.  The x264 flags and ``tff``/``bff`` make FFmpeg
    report ``yuv420p(top first)`` or ``yuv420p(bottom first)`` in its banner.
    """
    if bottom_first:
        interlace, params = "interleave_bottom,setfield=bff", "bff=1"
    else:
        interlace, params = "interleave_top,setfield=tff", "tff=1"
    args = [
        "-f",
        "lavfi",
        "-i",
        "testsrc2=size=160x120:rate=20:duration=1",
    ]
    if with_audio:
        args += ["-f", "lavfi", "-i", "sine=frequency=220:duration=1"]
    args += [
        "-vf",
        f"tinterlace={interlace}",
        "-flags",
        "+ildct+ilme",
        "-c:v",
        "libx264",
        "-preset",
        "ultrafast",
        "-pix_fmt",
        "yuv420p",
        "-x264-params",
        params,
    ]
    if with_audio:
        args += ["-c:a", "aac", "-shortest"]
    args.append(str(path))
    _run_ffmpeg(args)


def _make_progressive_fixture(path: Path) -> None:
    _run_ffmpeg(
        [
            "-f",
            "lavfi",
            "-i",
            "testsrc2=size=160x120:rate=10:duration=1",
            "-c:v",
            "libx264",
            "-preset",
            "ultrafast",
            "-pix_fmt",
            "yuv420p",
            str(path),
        ]
    )


def _audio_duration(path: Path, tmp_path: Path) -> float:
    wav = tmp_path / "decode-audio.wav"
    _run_ffmpeg(["-i", str(path), "-vn", "-ac", "1", str(wav)])
    with wave.open(str(wav), "rb") as audio:
        return audio.getnframes() / audio.getframerate()


def _reference_frames(
    path: Path, *, width: int, height: int, fps: float
) -> np.ndarray:
    """Decode the whole file with yadif, to compare seeked output against."""
    result = subprocess.run(
        [
            find_ffmpeg(),
            "-hide_banner",
            "-loglevel",
            "error",
            "-i",
            str(path),
            "-map",
            "0:v:0",
            "-an",
            "-vf",
            "yadif=deint=interlaced",
            "-pix_fmt",
            "bgr24",
            "-f",
            "rawvideo",
            "-s",
            f"{width}x{height}",
            "-r",
            f"{fps:.6f}",
            "pipe:1",
        ],
        check=False,
        capture_output=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stderr.decode(errors="replace")
    frame_size = width * height * 3
    assert len(result.stdout) % frame_size == 0
    return np.frombuffer(result.stdout, dtype=np.uint8).reshape(
        -1, height, width, 3
    )


def _open_source(path: Path):
    info = probe_video(path)
    cap = open_video_source(
        path,
        width=info.width,
        height=info.height,
        fps=info.fps,
        duration=info.duration,
        decode_with_ffmpeg=info.decode_with_ffmpeg,
    )
    return info, cap


def test_interlaced_source_is_detected_and_uses_ffmpeg(tmp_path: Path) -> None:
    source = tmp_path / "interlaced.mp4"
    _make_interlaced_fixture(source)

    info = probe_video(source)
    assert info.decode_with_ffmpeg is True
    assert info.frame_count == 10
    assert info.duration == pytest.approx(1.0, abs=0.02)
    assert probe_media(str(source)).interlaced is True


def test_bottom_first_source_is_detected(tmp_path: Path) -> None:
    source = tmp_path / "bottom-first.mp4"
    _make_interlaced_fixture(source, bottom_first=True)

    info = probe_video(source)
    assert info.decode_with_ffmpeg is True
    assert probe_media(str(source)).interlaced is True
    assert probe_frame_field_order(str(source), 1.0) is True


def test_ffmpeg_reader_decodes_color_frames_and_seeks(tmp_path: Path) -> None:
    source = tmp_path / "interlaced.mp4"
    _make_interlaced_fixture(source)

    info, cap = _open_source(source)
    assert isinstance(cap, FFmpegFrameReader)
    try:
        assert cap.isOpened() is True
        assert cap.get(cv2.CAP_PROP_POS_MSEC) == pytest.approx(0.0)

        ok, first = cap.read()
        assert ok is True
        assert first is not None
        assert float(first.mean()) > 20.0
        assert float(first.std()) > 10.0
        assert cap.get(cv2.CAP_PROP_POS_MSEC) == pytest.approx(
            1000.0 / info.fps, abs=0.1
        )

        assert cap.set(cv2.CAP_PROP_POS_MSEC, 500.0) is True
        assert cap.get(cv2.CAP_PROP_POS_MSEC) == pytest.approx(500.0)
        ok, second = cap.read()
        assert ok is True
        assert second is not None
        assert float(second.mean()) > 20.0
        assert not np.array_equal(first, second)
        assert cap.get(cv2.CAP_PROP_POS_MSEC) == pytest.approx(
            500.0 + 1000.0 / info.fps, abs=0.1
        )

        assert cap.grab() is True
        ok, grabbed = cap.retrieve()
        assert ok is True
        assert grabbed is not None
        assert cap.get(cv2.CAP_PROP_POS_MSEC) == pytest.approx(
            500.0 + 2000.0 / info.fps, abs=0.1
        )
    finally:
        cap.release()
    assert cap.isOpened() is False


@pytest.mark.parametrize("start", [0.0, 0.1, 0.3, 0.5, 0.7])
def test_seek_warmup_matches_full_decode(tmp_path: Path, start: float) -> None:
    """The first frame after a seek must be a complete yadif output frame.

    FFmpeg starts decoding a couple of frames before the requested timestamp so
    yadif has its neighbours, and the reader must discard exactly those
    warm-up frames.  A direct seek leaves a boundary artefact; with an audio
    track it can also emit the same frame twice.
    """
    source = tmp_path / "interlaced-av.mp4"
    _make_interlaced_fixture(source, with_audio=True)

    info = probe_video(source)
    reference = _reference_frames(
        source, width=info.width, height=info.height, fps=info.fps
    )
    target = math.ceil(start * info.fps - 1e-6)
    assert target + 1 < len(reference)

    cap = FFmpegFrameReader(
        source,
        width=info.width,
        height=info.height,
        fps=info.fps,
        duration=info.duration,
        start=start,
    )
    try:
        ok, first = cap.read()
        assert ok is True
        assert first is not None
        np.testing.assert_array_equal(first, reference[target])

        ok, second = cap.read()
        assert ok is True
        assert second is not None
        np.testing.assert_array_equal(second, reference[target + 1])
    finally:
        cap.release()


def test_export_from_interlaced_source_fractional_start(tmp_path: Path) -> None:
    """A trim that starts between frames must keep frame count and A/V length."""
    source = tmp_path / "interlaced.mp4"
    output = tmp_path / "output.mp4"
    _make_interlaced_fixture(source, with_audio=True)

    process_video(
        ProcessConfig(
            input_path=source,
            output_path=output,
            start=0.35,
            end=0.85,
            blur_faces=False,
            pitch_semitones=0.0,
            preset="ultrafast",
            crf=28,
        )
    )

    info = probe_video(output)
    assert info.frame_count == 5
    assert info.duration == pytest.approx(0.5, abs=0.02)
    assert _audio_duration(output, tmp_path) == pytest.approx(0.5, abs=0.05)

    cap = cv2.VideoCapture(str(output))
    try:
        ok, first = cap.read()
        assert ok is True
        assert first is not None
        assert float(first.mean()) > 20.0

        ok, second = cap.read()
        assert ok is True
        assert second is not None
        assert not np.array_equal(first, second)
    finally:
        cap.release()


def test_export_from_interlaced_source_has_exact_frames_and_audio(
    tmp_path: Path,
) -> None:
    source = tmp_path / "interlaced.mp4"
    output = tmp_path / "output.mp4"
    _make_interlaced_fixture(source, with_audio=True)

    process_video(
        ProcessConfig(
            input_path=source,
            output_path=output,
            start=0.0,
            end=0.5,
            blur_faces=False,
            pitch_semitones=0.0,
            preset="ultrafast",
            crf=28,
        )
    )

    info = probe_video(output)
    assert info.frame_count == 5
    assert info.duration == pytest.approx(0.5, abs=0.02)
    assert info.has_audio is True
    assert info.decode_with_ffmpeg is False
    assert _audio_duration(output, tmp_path) == pytest.approx(0.5, abs=0.05)

    cap = cv2.VideoCapture(str(output))
    try:
        ok, frame = cap.read()
        assert ok is True
        assert frame is not None
        assert float(frame.mean()) > 20.0
    finally:
        cap.release()


def test_progressive_source_keeps_using_opencv(tmp_path: Path) -> None:
    source = tmp_path / "progressive.mp4"
    _make_progressive_fixture(source)

    info, cap = _open_source(source)
    assert info.decode_with_ffmpeg is False
    assert probe_media(str(source)).interlaced is False
    assert isinstance(cap, cv2.VideoCapture)
    try:
        ok, frame = cap.read()
        assert ok is True
        assert frame is not None
        assert float(frame.mean()) > 20.0
    finally:
        cap.release()


def test_showinfo_reports_field_coded_frames(tmp_path: Path) -> None:
    interlaced = tmp_path / "interlaced.mp4"
    progressive = tmp_path / "progressive.mp4"
    _make_interlaced_fixture(interlaced)
    _make_progressive_fixture(progressive)

    assert probe_frame_field_order(str(interlaced), 1.0) is True
    assert probe_frame_field_order(str(progressive), 1.0) is False


def test_broken_opencv_decode_triggers_fallback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "interlaced.mp4"
    _make_interlaced_fixture(source)

    # Pretend the FFmpeg banner did not reveal the field order.  OpenCV's
    # output for this file is nondeterministic: sometimes black, sometimes
    # sparse garbage, and in a reused process even a bright stale buffer.
    # The decoded frame flag from showinfo must decide before OpenCV's
    # unreliable frame content is consulted.
    monkeypatch.setattr(
        processor,
        "probe_media",
        lambda _path: MediaInfo(
            duration=1.0, has_audio=False, sample_rate=48000, interlaced=False
        ),
    )

    info = probe_video(source)
    assert info.decode_with_ffmpeg is True
