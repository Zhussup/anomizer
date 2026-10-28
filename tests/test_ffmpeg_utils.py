"""Audio graph construction and the preview PCM pipeline."""

from __future__ import annotations

import subprocess
from pathlib import Path

import numpy as np
import pytest

from anomizer import ffmpeg_utils
from anomizer.ffmpeg_utils import (
    build_audio_filter,
    build_preview_pcm_command,
    find_ffmpeg,
    pitch_filter,
)
from anomizer.preview_audio import preview_player_command


def test_audio_graph_without_voice_ranges_has_no_pitch_filter() -> None:
    graph = build_audio_filter(
        input_label="1:a:0",
        start=0.0,
        duration=5.0,
        voice_intervals=[],
        semitones=-3.0,
        preserve_formants=False,
        volume=1.0,
        sample_rate=48000,
    )
    assert graph == (
        "[1:a:0]asetpts=PTS-STARTPTS,aresample=48000,"
        "apad=whole_dur=5.050000,atrim=end_sample=240000[a]"
    )
    assert "rubberband" not in graph
    assert "asetrate" not in graph


def test_audio_graph_full_coverage_uses_single_pitch_chain() -> None:
    graph = build_audio_filter(
        input_label="1:a:0",
        start=0.0,
        duration=5.0,
        voice_intervals=None,
        semitones=-3.0,
        preserve_formants=False,
        volume=1.0,
        sample_rate=48000,
    )
    assert graph.count("rubberband") + graph.count("asetrate") == 1
    assert "asplit" not in graph
    assert graph.endswith("atrim=end_sample=240000[a]")
    assert "apad=whole_dur=5.050000" in graph


def test_audio_graph_partial_coverage_splits_and_concatenates() -> None:
    graph = build_audio_filter(
        input_label="1:a:0",
        start=10.0,
        duration=5.0,
        voice_intervals=[(11.0, 12.0), (13.0, 14.0)],
        semitones=-3.0,
        preserve_formants=False,
        volume=1.0,
        sample_rate=48000,
    )
    # Pitch is applied once to a shared wet branch, not once per voice range.
    assert graph.count("rubberband") + graph.count("asetrate") == 1
    assert "[wetin]" in graph
    assert "asplit=2[dryin][wetin]" in graph
    assert graph.count("atrim=") == 7  # pre-chain, wet branch, five segments
    assert "concat=n=5:v=0:a=1" in graph
    assert "apad=whole_dur=5.050000" in graph
    assert graph.count("atrim=end_sample=240000") == 2  # pre-chain and wet branch
    assert "atrim=start_sample=48000:end_sample=96000" in graph
    assert "atrim=start_sample=144000:end_sample=192000" in graph
    assert "atrim=start_sample=96000:end_sample=144000" in graph
    assert "atrim=start_sample=192000:end_sample=240000" in graph


def test_pitch_fallback_uses_actual_rounded_rate(monkeypatch) -> None:
    monkeypatch.setattr("anomizer.ffmpeg_utils.has_filter", lambda _name: False)
    factor = 2 ** (-3 / 12)
    stretched = round(48000 * factor)
    assert pitch_filter(-3.0, False, 48000) == (
        f"asetrate={stretched},aresample=48000,"
        f"atempo={48000 / stretched:.6f}"
    )


def test_preview_pcm_command_writes_raw_audio_to_stdout() -> None:
    command = build_preview_pcm_command(
        source=Path("input.mp4"),
        start=1.5,
        duration=2.5,
        voice_intervals=[(2.0, 3.0)],
        semitones=-3.0,
        preserve_formants=False,
        volume=1.0,
        sample_rate=48000,
    )
    assert command[-3:] == ["-f", "s16le", "pipe:1"]
    assert "-ss" in command and "1.500000" in command
    assert "-t" in command and "2.500000" in command


def test_custom_audio_player_override(monkeypatch) -> None:
    monkeypatch.setenv(
        "ANOMIZER_AUDIO_PLAYER", "pw-play -a --rate 48000 --channels 2 -"
    )
    assert preview_player_command() == [
        "pw-play",
        "-a",
        "--rate",
        "48000",
        "--channels",
        "2",
        "-",
    ]


def _peak_frequency(samples: np.ndarray, sample_rate: int) -> float:
    window = samples * np.hanning(len(samples))
    spectrum = np.abs(np.fft.rfft(window))
    frequencies = np.fft.rfftfreq(len(window), 1.0 / sample_rate)
    return float(frequencies[int(np.argmax(spectrum))])


def _make_tone(path: Path, duration: float = 4.0, rate: int = 48000) -> None:
    result = subprocess.run(
        [
            find_ffmpeg(),
            "-hide_banner",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            f"sine=frequency=220:duration={duration:g}",
            "-ac",
            "1",
            "-ar",
            str(rate),
            str(path),
        ],
        capture_output=True,
        check=False,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr.decode(errors="replace")


def test_preview_pcm_pipeline_respects_voice_ranges(tmp_path: Path) -> None:
    source = tmp_path / "tone.wav"
    _make_tone(source)

    command = build_preview_pcm_command(
        source=source,
        start=0.0,
        duration=4.0,
        voice_intervals=[(1.0, 2.0)],
        semitones=-3.0,
        preserve_formants=False,
        volume=1.0,
        sample_rate=48000,
    )
    result = subprocess.run(command, capture_output=True, check=False, timeout=60)
    assert result.returncode == 0, result.stderr.decode(errors="replace")

    samples = np.frombuffer(result.stdout, dtype="<i2").reshape(-1, 2).mean(axis=1)
    sample_rate = 48000
    assert len(samples) == 4 * sample_rate

    before = _peak_frequency(samples[int(0.2 * sample_rate) : int(0.8 * sample_rate)], sample_rate)
    inside = _peak_frequency(samples[int(1.2 * sample_rate) : int(1.8 * sample_rate)], sample_rate)
    after = _peak_frequency(samples[int(2.2 * sample_rate) : int(2.8 * sample_rate)], sample_rate)
    assert before == pytest.approx(220.0, abs=5.0)
    assert inside == pytest.approx(220.0 * 2 ** (-3 / 12), abs=5.0)
    assert after == pytest.approx(220.0, abs=5.0)


def test_preview_pcm_multiple_absolute_ranges(tmp_path: Path) -> None:
    source = tmp_path / "tone.wav"
    _make_tone(source)

    command = build_preview_pcm_command(
        source=source,
        start=1.0,
        duration=2.0,
        voice_intervals=[(1.5, 2.0), (2.5, 3.0)],
        semitones=-3.0,
        preserve_formants=False,
        volume=1.0,
        sample_rate=48000,
    )
    result = subprocess.run(command, capture_output=True, check=False, timeout=60)
    assert result.returncode == 0, result.stderr.decode(errors="replace")

    sample_rate = 48000
    samples = np.frombuffer(result.stdout, dtype="<i2").reshape(-1, 2).mean(axis=1)
    assert len(samples) == 2 * sample_rate

    lowered = 220.0 * 2 ** (-3 / 12)
    peaks = [
        _peak_frequency(samples[int(start * sample_rate) : int(end * sample_rate)], sample_rate)
        for start, end in ((0.1, 0.4), (0.6, 0.9), (1.1, 1.4), (1.6, 1.9))
    ]
    assert peaks[0] == pytest.approx(220.0, abs=5.0)
    assert peaks[1] == pytest.approx(lowered, abs=5.0)
    assert peaks[2] == pytest.approx(220.0, abs=5.0)
    assert peaks[3] == pytest.approx(lowered, abs=5.0)


def test_preview_pcm_fallback_without_rubberband(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setattr("anomizer.ffmpeg_utils.has_filter", lambda _name: False)
    source = tmp_path / "tone.wav"
    _make_tone(source, duration=3.0)

    command = build_preview_pcm_command(
        source=source,
        start=0.0,
        duration=3.0,
        voice_intervals=[(1.0, 2.0)],
        semitones=-3.0,
        preserve_formants=False,
        volume=1.0,
        sample_rate=48000,
    )
    result = subprocess.run(command, capture_output=True, check=False, timeout=60)
    assert result.returncode == 0, result.stderr.decode(errors="replace")

    sample_rate = 48000
    samples = np.frombuffer(result.stdout, dtype="<i2").reshape(-1, 2).mean(axis=1)
    assert len(samples) == 3 * sample_rate
    lowered = 220.0 * 2 ** (-3 / 12)
    before = _peak_frequency(
        samples[int(0.2 * sample_rate) : int(0.8 * sample_rate)], sample_rate
    )
    inside = _peak_frequency(
        samples[int(1.2 * sample_rate) : int(1.8 * sample_rate)], sample_rate
    )
    after = _peak_frequency(
        samples[int(2.2 * sample_rate) : int(2.8 * sample_rate)], sample_rate
    )
    assert before == pytest.approx(220.0, abs=5.0)
    assert inside == pytest.approx(lowered, abs=6.0)
    assert after == pytest.approx(220.0, abs=5.0)


@pytest.mark.parametrize(
    ("field_order", "expected"),
    [
        ("progressive", False),
        ("top coded first (swapped)", True),
        ("bottom coded first (swapped)", True),
        ("interlaced", True),
    ],
)
def test_probe_media_field_order_variants(
    monkeypatch: pytest.MonkeyPatch, field_order: str, expected: bool
) -> None:
    banner = (
        "Input #0, mov,mp4,m4a,3gp,3g2,mj2, from 'input.mp4':\n"
        "  Duration: 00:00:10.00, start: 0.000000, bitrate: 1000 kb/s\n"
        "  Stream #0:0[0x1](und): Video: h264 (High) (avc1 / 0x31637661), "
        f"yuv420p({field_order}), 1920x1080, 25 fps\n"
    )

    def fake_run(*_args, **_kwargs) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(
            args=[], returncode=1, stdout="", stderr=banner
        )

    monkeypatch.setattr(ffmpeg_utils.subprocess, "run", fake_run)
    ffmpeg_utils.probe_media.cache_clear()
    try:
        info = ffmpeg_utils.probe_media("input.mp4")
    finally:
        ffmpeg_utils.probe_media.cache_clear()
    assert info.interlaced is expected
