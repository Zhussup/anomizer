"""Timecode parsing and interval behavior."""

from __future__ import annotations

import math

import pytest

from anomizer.timing import (
    EffectRegion,
    audio_segments,
    effect_intervals,
    format_timecode,
    is_active,
    normalize_intervals,
    parse_timecode,
    regions_to_intervals,
    segments_to_sample_ranges,
)


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("0", 0.0),
        ("12.5", 12.5),
        ("12,5", 12.5),
        ("0:12.5", 12.5),
        ("1:02", 62.0),
        ("1:02.345", 62.345),
        ("1:02:03", 3723.0),
        ("1:02:03,5", 3723.5),
        (" 2:03 ", 123.0),
    ],
)
def test_parse_timecode(text: str, expected: float) -> None:
    assert parse_timecode(text) == pytest.approx(expected)


@pytest.mark.parametrize(
    "text",
    ["", "  ", "-1", "-0:01", "abc", "1:xx", "1:2:3:4", "1.5:00", "nan", "inf"],
)
def test_parse_timecode_rejects_bad_values(text: str) -> None:
    with pytest.raises(ValueError):
        parse_timecode(text)


def test_parse_timecode_rejects_non_finite_numbers() -> None:
    with pytest.raises(ValueError):
        parse_timecode(math.nan)
    with pytest.raises(ValueError):
        parse_timecode(math.inf)


@pytest.mark.parametrize(
    ("seconds", "expected"),
    [
        (0.0, "00:00.00"),
        (5.5, "00:05.50"),
        (62.346, "01:02.35"),
        (3723.5, "1:02:03.50"),
    ],
)
def test_format_timecode(seconds: float, expected: str) -> None:
    assert format_timecode(seconds) == expected


def test_normalize_intervals_clamps_sorts_and_merges() -> None:
    assert normalize_intervals(
        [(2.0, 5.0), (0.0, 1.5), (1.5, 2.0), (-2.0, 0.5), (8.0, 7.0)],
        lower=0.0,
        upper=4.0,
    ) == [(0.0, 4.0)]


def test_normalize_intervals_keeps_non_touching_gap() -> None:
    assert normalize_intervals([(0.0, 1.0), (1.1, 2.0)]) == [
        (0.0, 1.0),
        (1.1, 2.0),
    ]


def test_is_active_uses_half_open_intervals() -> None:
    intervals = [(1.0, 2.0)]
    assert not is_active(intervals, 0.999)
    assert is_active(intervals, 1.0)
    assert is_active(intervals, 1.999)
    assert not is_active(intervals, 2.0)


def test_regions_to_intervals_combines_flags_independently() -> None:
    regions = [
        EffectRegion(0.0, 1.0, blur=True, voice=False),
        EffectRegion(1.0, 2.0, blur=False, voice=True),
        EffectRegion(2.0, 3.0, blur=True, voice=False),
    ]
    assert regions_to_intervals(regions, "blur") == [(0.0, 1.0), (2.0, 3.0)]
    assert regions_to_intervals(regions, "voice") == [(1.0, 2.0)]


def test_effect_intervals_none_means_whole_segment_and_empty_means_off() -> None:
    assert effect_intervals(None, 10.0, 20.0) == [(10.0, 20.0)]
    assert effect_intervals([], 10.0, 20.0) == []
    assert effect_intervals([(0.0, 12.0), (18.0, 30.0)], 10.0, 20.0) == [
        (10.0, 12.0),
        (18.0, 20.0),
    ]


def test_audio_segments_covers_trimmed_duration_once() -> None:
    assert audio_segments([(1.0, 2.0), (3.0, 4.0)], 0.0, 5.0) == [
        (0.0, 1.0, False),
        (1.0, 2.0, True),
        (2.0, 3.0, False),
        (3.0, 4.0, True),
        (4.0, 5.0, False),
    ]


def test_audio_segments_clips_absolute_ranges_to_trim() -> None:
    assert audio_segments([(8.0, 12.0)], 10.0, 5.0) == [
        (0.0, 2.0, True),
        (2.0, 5.0, False),
    ]


def test_audio_segments_full_and_empty_coverage() -> None:
    assert audio_segments(None, 10.0, 2.0) == [(0.0, 2.0, True)]
    assert audio_segments([], 10.0, 2.0) == [(0.0, 2.0, False)]


def test_sample_ranges_share_rounded_boundaries() -> None:
    segments = [(0.0, 0.5, False), (0.5, 1.5, True), (1.5, 2.0, False)]
    total = round(2.0 * 48000)
    assert segments_to_sample_ranges(segments, 48000, total) == [
        (0, 24000, False),
        (24000, 72000, True),
        (72000, 96000, False),
    ]


def test_sample_ranges_clamp_and_drop_empty_segments() -> None:
    segments = [(-0.5, 0.0, False), (0.0, 1.0, True), (1.0, 3.0, False)]
    assert segments_to_sample_ranges(segments, 48000, 48000) == [
        (0, 48000, True),
    ]

