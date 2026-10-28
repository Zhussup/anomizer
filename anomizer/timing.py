"""Timecode parsing and interval helpers for timed effects."""

from __future__ import annotations

import math
from dataclasses import dataclass

Interval = tuple[float, float]
_EPSILON = 1e-6


@dataclass(frozen=True)
class EffectRegion:
    """A source-time range where one or both effects are enabled.

    Ranges are absolute positions in the source video. Overlapping ranges are
    allowed and their effects are combined.
    """

    start: float
    end: float
    blur: bool = True
    voice: bool = True


def parse_timecode(value: str | float) -> float:
    """Parse ``SS``, ``MM:SS`` or ``HH:MM:SS`` (fractional seconds allowed)."""
    if isinstance(value, (int, float)):
        seconds = float(value)
    else:
        text = str(value).strip().replace(",", ".")
        if not text:
            raise ValueError("Пустое значение времени.")
        if text.startswith("-"):
            raise ValueError("Время не может быть отрицательным.")
        parts = text.split(":")
        if len(parts) > 3:
            raise ValueError(f"Не удалось разобрать время: {value!r}")
        seconds = 0.0
        for index, part in enumerate(parts):
            try:
                number = float(part.strip())
            except ValueError as exc:
                raise ValueError(f"Не удалось разобрать время: {value!r}") from exc
            if not math.isfinite(number) or number < 0:
                raise ValueError(f"Не удалось разобрать время: {value!r}")
            if index < len(parts) - 1 and not number.is_integer():
                raise ValueError(f"Не удалось разобрать время: {value!r}")
            seconds = seconds * 60.0 + number

    if not math.isfinite(seconds) or seconds < 0:
        raise ValueError(f"Не удалось разобрать время: {value!r}")
    return seconds


def format_timecode(seconds: float) -> str:
    """Format seconds as ``MM:SS.ss`` or ``H:MM:SS.ss`` for display."""
    seconds = max(0.0, float(seconds))
    hours, rem = divmod(seconds, 3600.0)
    minutes, sec = divmod(rem, 60.0)
    if hours >= 1:
        return f"{int(hours):d}:{int(minutes):02d}:{sec:05.2f}"
    return f"{int(minutes):02d}:{sec:05.2f}"


def normalize_intervals(
    intervals: list[Interval] | tuple[Interval, ...],
    lower: float = 0.0,
    upper: float = math.inf,
) -> list[Interval]:
    """Clamp intervals to ``[lower, upper]``, drop empties and merge overlaps."""
    clean: list[Interval] = []
    for start, end in intervals:
        a = max(float(lower), float(start))
        b = min(float(upper), float(end))
        if b - a > _EPSILON:
            clean.append((a, b))
    if not clean:
        return []

    clean.sort(key=lambda interval: interval[0])
    merged: list[Interval] = [clean[0]]
    for start, end in clean[1:]:
        last_start, last_end = merged[-1]
        if start <= last_end + _EPSILON:
            merged[-1] = (last_start, max(last_end, end))
        else:
            merged.append((start, end))
    return merged


def is_active(intervals: list[Interval], seconds: float) -> bool:
    """Return True when ``seconds`` lies inside any interval."""
    for start, end in intervals:
        if start - _EPSILON <= seconds < end:
            return True
    return False


def regions_to_intervals(
    regions: list[EffectRegion], effect: str, upper: float = math.inf
) -> list[Interval]:
    """Collect and merge ranges where ``blur`` or ``voice`` is enabled."""
    if effect not in {"blur", "voice"}:
        raise ValueError(f"Неизвестный эффект: {effect}")
    selected = [
        (region.start, region.end)
        for region in regions
        if getattr(region, effect) and region.end > region.start
    ]
    return normalize_intervals(selected, 0.0, upper)


def effect_intervals(
    intervals: list[Interval] | None,
    start: float,
    end: float,
) -> list[Interval]:
    """Resolve explicit intervals, where ``None`` means the whole segment."""
    if intervals is None:
        return [(start, end)] if end > start else []
    return normalize_intervals(intervals, start, end)


def audio_segments(
    voice_intervals: list[Interval] | None,
    start: float,
    duration: float,
) -> list[tuple[float, float, bool]]:
    """Split ``[0, duration)`` into contiguous relative segments.

    Each tuple is ``(relative_start, relative_end, pitch_enabled)``. The list
    always covers the whole trimmed duration exactly once.
    """
    if duration <= _EPSILON:
        return []
    duration = float(duration)
    intervals = effect_intervals(voice_intervals, start, start + duration)
    relative = normalize_intervals(
        [(a - start, b - start) for a, b in intervals], 0.0, duration
    )

    segments: list[tuple[float, float, bool]] = []
    cursor = 0.0
    for a, b in relative:
        if a > cursor + _EPSILON:
            segments.append((cursor, a, False))
        segments.append((a, b, True))
        cursor = b
    if cursor < duration - _EPSILON:
        segments.append((cursor, duration, False))

    # Merge adjacent segments with the same state.
    merged: list[tuple[float, float, bool]] = []
    for a, b, enabled in segments:
        if merged and merged[-1][2] == enabled:
            prev_a, _prev_b, _ = merged[-1]
            merged[-1] = (prev_a, b, enabled)
        else:
            merged.append((a, b, enabled))
    return merged


def segments_to_sample_ranges(
    segments: list[tuple[float, float, bool]],
    sample_rate: int,
    total_samples: int,
) -> list[tuple[int, int, bool]]:
    """Convert relative segments to shared, rounded sample boundaries."""
    sample_rate = max(1, int(sample_rate))
    total_samples = max(0, int(total_samples))
    ranges: list[tuple[int, int, bool]] = []
    for start, end, enabled in segments:
        first = max(0, min(total_samples, round(float(start) * sample_rate)))
        last = max(0, min(total_samples, round(float(end) * sample_rate)))
        if last > first:
            ranges.append((first, last, bool(enabled)))
    return ranges
