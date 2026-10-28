"""Command-line range parsing."""

from __future__ import annotations

import pytest

from anomizer.cli import _parse_ranges, build_parser


def test_parse_ranges_converts_timecodes() -> None:
    assert _parse_ranges(
        [["0", "1.5"], ["1:02", "1:03.5"]], "Блюр"
    ) == [(0.0, 1.5), (62.0, 63.5)]


def test_parse_ranges_absent_means_none() -> None:
    assert _parse_ranges(None, "Голос") is None


def test_parse_ranges_rejects_empty_and_reversed_intervals() -> None:
    with pytest.raises(ValueError, match="конец диапазона"):
        _parse_ranges([["2", "2"]], "Голос")
    with pytest.raises(ValueError, match="конец диапазона"):
        _parse_ranges([["3", "2"]], "Голос")


def test_parse_ranges_reports_timecode_error() -> None:
    with pytest.raises(ValueError, match="Блюр"):
        _parse_ranges([["nope", "2"]], "Блюр")


def test_parser_accepts_repeatable_effect_ranges() -> None:
    args = build_parser().parse_args(
        [
            "input.mp4",
            "-o",
            "output.mp4",
            "--blur-range",
            "0",
            "5",
            "--blur-range",
            "10",
            "12",
            "--voice-range",
            "5",
            "10",
        ]
    )
    assert args.blur_range == [["0", "5"], ["10", "12"]]
    assert args.voice_range == [["5", "10"]]
