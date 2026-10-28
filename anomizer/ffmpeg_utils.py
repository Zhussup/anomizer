"""Small helpers around the FFmpeg binary used for audio and muxing."""

from __future__ import annotations

import functools
import os
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from . import resources
from .timing import audio_segments, segments_to_sample_ranges


class FFmpegError(RuntimeError):
    pass


@functools.lru_cache(maxsize=1)
def find_ffmpeg() -> str:
    """Return an FFmpeg executable.

    Priority: explicit env var, the wheel shipped by imageio-ffmpeg, system PATH.
    The wheel keeps the application self-contained on machines without FFmpeg.
    """
    env_path = os.environ.get("ANOMIZER_FFMPEG")
    if env_path and Path(env_path).is_file():
        return env_path

    bundled = resources.bundled_ffmpeg_path()
    if bundled is not None:
        return str(bundled)

    try:
        import imageio_ffmpeg

        exe = imageio_ffmpeg.get_ffmpeg_exe()
        if exe and Path(exe).is_file():
            return exe
    except Exception:  # noqa: BLE001, S110 - optional dependency; try system FFmpeg next
        pass

    exe = shutil.which("ffmpeg")
    if exe:
        return exe

    raise FFmpegError(
        "FFmpeg не найден. Установите imageio-ffmpeg (pip install imageio-ffmpeg) "
        "или системный ffmpeg."
    )


@functools.lru_cache(maxsize=1)
def _available_filters() -> set[str]:
    proc = subprocess.run(
        [find_ffmpeg(), "-hide_banner", "-filters"],
        capture_output=True,
        timeout=30,
        check=False,
        **resources.ffmpeg_text_kwargs(),
    )
    names: set[str] = set()
    for line in proc.stdout.splitlines():
        parts = line.split()
        if len(parts) >= 2 and not line.startswith("Filters:"):
            names.add(parts[1])
    return names


def has_filter(name: str) -> bool:
    return name in _available_filters()


@functools.lru_cache(maxsize=1)
def _available_output_devices() -> set[str]:
    proc = subprocess.run(
        [find_ffmpeg(), "-hide_banner", "-devices"],
        capture_output=True,
        timeout=30,
        check=False,
        **resources.ffmpeg_text_kwargs(),
    )
    names: set[str] = set()
    for line in proc.stdout.splitlines():
        parts = line.split()
        if len(parts) < 2 or "=" in parts:
            continue
        flags = parts[0]
        if len(flags) <= 2 and "E" in flags:
            names.add(parts[1])
    return names


def has_output_device(name: str) -> bool:
    return name in _available_output_devices()


def preview_audio_output_args() -> list[str] | None:
    """Return FFmpeg output arguments for live preview sound, if supported."""
    if not sys.platform.startswith("linux"):
        return None
    if has_output_device("alsa"):
        return ["-f", "alsa", "default"]
    if has_output_device("pulse"):
        return ["-f", "pulse", "default"]
    return None


@dataclass(frozen=True)
class MediaInfo:
    duration: float
    has_audio: bool
    sample_rate: int
    interlaced: bool = False


_DURATION_RE = re.compile(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)")
_VIDEO_LINE_RE = re.compile(r"Stream #\d+:\d+.*?:\s*Video:\s*(.*)")
_AUDIO_LINE_RE = re.compile(r"Stream #\d+:\d+.*?:\s*Audio:\s*(.*)")
_SAMPLE_RATE_RE = re.compile(r"(\d+)\s*Hz")
_FIELD_ORDER_RE = re.compile(
    r"\b(progressive|top first|bottom first|top coded first|bottom coded first|"
    r"interlaced)\b",
    re.IGNORECASE,
)
_SHOWINFO_FIELD_RE = re.compile(r"\bi:([PBT?])\b", re.IGNORECASE)


@functools.lru_cache(maxsize=64)
def probe_media(path: str) -> MediaInfo:
    """Read duration/audio info from FFmpeg's banner.

    FFmpeg exits with code 1 when only ``-i`` is given, which is expected here.
    """
    proc = subprocess.run(
        [find_ffmpeg(), "-hide_banner", "-i", path],
        capture_output=True,
        timeout=60,
        check=False,
        **resources.ffmpeg_text_kwargs(),
    )
    text = proc.stderr
    if proc.returncode not in (0, 1) or not text.strip():
        raise FFmpegError(
            f"FFmpeg не смог прочитать контейнер {Path(path).name} "
            f"(код {proc.returncode}). Для MTS/AVCHD сконвертируйте файл в MP4 "
            "через HandBrake или системный ffmpeg."
        )

    duration = 0.0
    match = _DURATION_RE.search(text)
    if match:
        hours, minutes, seconds = match.groups()
        duration = int(hours) * 3600 + int(minutes) * 60 + float(seconds)

    audio_match = _AUDIO_LINE_RE.search(text)
    has_audio = audio_match is not None
    sample_rate = 48000
    if audio_match:
        sr_match = _SAMPLE_RATE_RE.search(audio_match.group(1))
        if sr_match:
            sample_rate = int(sr_match.group(1))

    video_match = _VIDEO_LINE_RE.search(text)
    interlaced = False
    if video_match:
        field_match = _FIELD_ORDER_RE.search(video_match.group(1))
        interlaced = bool(
            field_match and field_match.group(1).lower() != "progressive"
        )

    return MediaInfo(
        duration=duration,
        has_audio=has_audio,
        sample_rate=sample_rate,
        interlaced=interlaced,
    )


@functools.lru_cache(maxsize=256)
def _probe_frame_field_order_at(path: str, position: float) -> bool | None:
    """Ask FFmpeg's ``showinfo`` whether the frame at ``position`` is coded.

    ``i:T`` / ``i:B`` mean a top/bottom field, ``i:P`` means progressive.
    Returns ``None`` when FFmpeg cannot report a field flag.
    """
    try:
        proc = subprocess.run(
            [
                find_ffmpeg(),
                "-hide_banner",
                "-loglevel",
                "info",
                "-nostdin",
                "-ss",
                f"{max(0.0, position):.6f}",
                "-i",
                path,
                "-map",
                "0:v:0",
                "-an",
                "-sn",
                "-dn",
                "-vf",
                "showinfo",
                "-frames:v",
                "1",
                "-f",
                "null",
                "-",
            ],
            capture_output=True,
            timeout=60,
            check=False,
            **resources.ffmpeg_text_kwargs(),
        )
    except (OSError, subprocess.SubprocessError):
        return None
    match = _SHOWINFO_FIELD_RE.search(proc.stderr)
    if match is None:
        return None
    return match.group(1).upper() in {"T", "B"}


def clear_probe_cache() -> None:
    """Forget cached probes, e.g. when a file at the same path was replaced."""
    probe_media.cache_clear()
    _probe_frame_field_order_at.cache_clear()


def probe_frame_field_order(path: str, duration: float = 0.0) -> bool | None:
    """Detect field-coded frames from FFmpeg's decoded frame flags.

    The container banner is usually enough, but its field order can be missing
    or wrong.  ``showinfo`` reports the flag of an actual decoded frame, which
    is what OpenCV's converter fails on.  Sampling the start and the middle
    also catches streams that begin with progressive frames.  ``None`` means
    the frames could not be inspected.
    """
    positions = [0.0]
    if duration > 2.0:
        positions.append(duration * 0.6)
    results = [
        _probe_frame_field_order_at(path, round(position, 3))
        for position in positions
    ]
    if any(result is True for result in results):
        return True
    if all(result is False for result in results):
        return False
    return None


def pitch_filter(
    semitones: float,
    preserve_formants: bool = False,
    sample_rate: int = 48000,
) -> str:
    """Return only the pitch-shifting part of an audio filter chain."""
    factor = 2.0 ** (float(semitones) / 12.0)
    if abs(semitones) <= 0.01:
        return ""

    if has_filter("rubberband"):
        formant = "preserved" if preserve_formants else "shifted"
        return f"rubberband=pitch={factor:.6f}:formant={formant}:pitchq=quality"

    sr = int(sample_rate or 48000)
    stretched = round(sr * factor)
    # atempo accepts 0.5..2.0; ±12 semitones stay exactly in range.
    return f"asetrate={stretched},aresample={sr},atempo={sr / stretched:.6f}"


def volume_filter(volume: float) -> str:
    if abs(float(volume) - 1.0) <= 0.001:
        return ""
    return f"volume={max(0.0, float(volume)):.4f}"


def voice_filter(
    semitones: float,
    preserve_formants: bool = False,
    volume: float = 1.0,
    sample_rate: int = 48000,
) -> str:
    """Build an audio filter chain for pitch and volume changes."""
    parts = [
        part
        for part in (
            pitch_filter(semitones, preserve_formants, sample_rate),
            volume_filter(volume),
        )
        if part
    ]
    return ",".join(parts)


def _bounded_audio_chain(
    input_label: str,
    *,
    duration: float,
    sample_rate: int,
    total_samples: int,
) -> str:
    """Pad or trim one branch to exactly the trimmed duration."""
    return (
        f"[{input_label}]asetpts=PTS-STARTPTS,"
        f"aresample={sample_rate},"
        f"apad=whole_dur={duration + 0.05:.6f},"
        f"atrim=end_sample={total_samples}"
    )


def build_audio_filter(
    *,
    input_label: str,
    start: float,
    duration: float,
    voice_intervals: list[tuple[float, float]] | None,
    semitones: float,
    preserve_formants: bool,
    volume: float,
    sample_rate: int,
) -> str:
    """Build a filter_complex graph that pitch-shifts only selected intervals.

    ``voice_intervals`` are absolute source-time ranges. ``start`` and
    ``duration`` describe the already trimmed input. ``None`` means the whole
    segment. Outside the intervals the original audio is passed through
    untouched, so total duration and A/V sync are preserved.

    The graph is bounded to exactly ``round(duration * sample_rate)`` samples.
    This makes the output muxable without ``-shortest`` and avoids the
    unbounded ``apad`` behavior that can add stray audio.
    """
    duration = float(duration)
    if duration <= 0:
        raise ValueError("Длительность аудиофрагмента должна быть больше нуля.")

    sr = int(sample_rate or 48000)
    total_samples = round(duration * sr)
    if total_samples <= 0:
        raise ValueError("Аудиофрагмент слишком короткий.")
    pitch = pitch_filter(semitones, preserve_formants, sr)
    segments = audio_segments(voice_intervals, float(start), duration)
    sample_ranges = segments_to_sample_ranges(segments, sr, total_samples)
    volume_part = volume_filter(volume)
    volume_suffix = f",{volume_part}" if volume_part else ""

    if not pitch or not any(enabled for _a, _b, enabled in sample_ranges):
        chain = _bounded_audio_chain(
            input_label,
            duration=duration,
            sample_rate=sr,
            total_samples=total_samples,
        )
        return chain + volume_suffix + "[a]"

    if all(enabled for _a, _b, enabled in sample_ranges):
        chain = _bounded_audio_chain(
            input_label,
            duration=duration,
            sample_rate=sr,
            total_samples=total_samples,
        )
        chain += f",{pitch},aresample={sr},asetpts=PTS-STARTPTS"
        chain += (
            f",apad=whole_dur={duration + 0.05:.6f},"
            f"atrim=end_sample={total_samples}"
        )
        return chain + volume_suffix + "[a]"

    count = len(sample_ranges)
    dry_count = sum(1 for _a, _b, enabled in sample_ranges if not enabled)
    wet_count = count - dry_count

    chains = [
        _bounded_audio_chain(
            input_label,
            duration=duration,
            sample_rate=sr,
            total_samples=total_samples,
        )
        + ",asplit=2[dryin][wetin]"
    ]
    wet_chain = (
        f"[wetin]{pitch},aresample={sr},asetpts=PTS-STARTPTS,"
        f"apad=whole_dur={duration + 0.05:.6f},"
        f"atrim=end_sample={total_samples}"
    )
    if wet_count > 1:
        wet_labels = [f"wet{index}" for index in range(wet_count)]
        chains.append(
            wet_chain
            + f",asplit={wet_count}"
            + "".join(f"[{label}]" for label in wet_labels)
        )
    else:
        wet_labels = ["wet"]
        chains.append(wet_chain + "[wet]")

    if dry_count > 1:
        dry_labels = [f"dry{index}" for index in range(dry_count)]
        chains.append(
            "[dryin]asplit="
            + str(dry_count)
            + "".join(f"[{label}]" for label in dry_labels)
        )
    else:
        dry_labels = ["dryin"]

    dry_index = 0
    wet_index = 0
    segment_labels: list[str] = []
    for index, (first, last, enabled) in enumerate(sample_ranges):
        if enabled:
            source = wet_labels[wet_index]
            wet_index += 1
        else:
            source = dry_labels[dry_index]
            dry_index += 1
        segment_labels.append(f"seg{index}")
        chains.append(
            f"[{source}]atrim=start_sample={first}:end_sample={last},"
            f"asetpts=PTS-STARTPTS[seg{index}]"
        )

    chains.append(
        "".join(f"[{label}]" for label in segment_labels)
        + f"concat=n={count}:v=0:a=1"
        + volume_suffix
        + "[a]"
    )
    return ";".join(chains)


def _preview_input_args(
    source: str | Path,
    start: float,
    duration: float,
) -> list[str]:
    return [
        "-nostdin",
        "-ss",
        f"{start:.6f}",
        "-t",
        f"{duration:.6f}",
        "-i",
        str(source),
    ]


def build_preview_audio_command(
    *,
    source: str | Path,
    start: float,
    duration: float,
    voice_intervals: list[tuple[float, float]] | None,
    semitones: float,
    preserve_formants: bool,
    volume: float,
    sample_rate: int,
) -> list[str] | None:
    """Build an FFmpeg command that writes the processed preview to ALSA/Pulse.

    This is the fallback used when no external PCM player is installed. The
    bundled static FFmpeg may not have a working ALSA plugin for PipeWire, so
    ``build_preview_pcm_command`` plus ``pw-play``/``aplay`` is preferred.
    """
    output_args = preview_audio_output_args()
    if output_args is None or duration <= 0:
        return None

    graph = build_audio_filter(
        input_label="0:a:0",
        start=start,
        duration=duration,
        voice_intervals=voice_intervals,
        semitones=semitones,
        preserve_formants=preserve_formants,
        volume=volume,
        sample_rate=sample_rate,
    )
    return [
        find_ffmpeg(),
        "-hide_banner",
        "-loglevel",
        "error",
        *_preview_input_args(source, start, duration),
        "-filter_complex",
        graph,
        "-map",
        "[a]",
        "-ac",
        "2",
        "-ar",
        "48000",
        *output_args,
    ]


def build_preview_pcm_command(
    *,
    source: str | Path,
    start: float,
    duration: float,
    voice_intervals: list[tuple[float, float]] | None,
    semitones: float,
    preserve_formants: bool,
    volume: float,
    sample_rate: int,
) -> list[str]:
    """Build an FFmpeg command that writes raw stereo PCM to stdout.

    The PCM stream is 48 kHz, signed 16-bit little-endian. ``PreviewAudio``
    pipes it into a small system player (``pw-play`` or ``aplay``), which is
    much more reliable than asking the bundled static FFmpeg to open ALSA
    directly on PipeWire desktops.
    """
    if duration <= 0:
        raise ValueError("Длительность аудиофрагмента должна быть больше нуля.")

    graph = build_audio_filter(
        input_label="0:a:0",
        start=start,
        duration=duration,
        voice_intervals=voice_intervals,
        semitones=semitones,
        preserve_formants=preserve_formants,
        volume=volume,
        sample_rate=sample_rate,
    )
    return [
        find_ffmpeg(),
        "-hide_banner",
        "-loglevel",
        "error",
        *_preview_input_args(source, start, duration),
        "-filter_complex",
        graph,
        "-map",
        "[a]",
        "-ac",
        "2",
        "-ar",
        "48000",
        "-f",
        "s16le",
        "pipe:1",
    ]
