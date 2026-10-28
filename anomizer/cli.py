"""Command-line interface for batch/local processing."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from . import resources
from .processor import ProcessConfig, probe_video, process_video
from .timing import parse_timecode


def _parse_ranges(pairs: list[list[str]] | None, label: str) -> list[tuple[float, float]] | None:
    if not pairs:
        return None
    ranges: list[tuple[float, float]] = []
    for start_text, end_text in pairs:
        try:
            start = parse_timecode(start_text)
            end = parse_timecode(end_text)
        except ValueError as exc:
            raise ValueError(f"{label}: {exc}") from exc
        if end <= start:
            raise ValueError(
                f"{label}: конец диапазона должен быть больше начала "
                f"({start_text} → {end_text})."
            )
        ranges.append((start, end))
    return ranges


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m anomizer.cli",
        description=(
            "Автоматически блюрит лица, меняет голос и обрезает видео. "
            "Никакой разметки и обучения не требуется."
        ),
    )
    parser.add_argument("input", type=Path, help="исходное видео")
    parser.add_argument("-o", "--output", type=Path, required=True, help="итоговый .mp4")
    parser.add_argument("--start", type=float, default=0.0, help="начало фрагмента, сек")
    parser.add_argument("--end", type=float, default=None, help="конец фрагмента, сек")
    parser.add_argument(
        "--no-blur", action="store_true", help="не блюрить лица"
    )
    parser.add_argument(
        "--method",
        choices=("gaussian", "pixelate"),
        default="gaussian",
        help="способ скрытия лиц",
    )
    parser.add_argument(
        "--strength", type=float, default=3.0, help="сила блюра от 1 до 5"
    )
    parser.add_argument(
        "--margin",
        type=float,
        default=0.22,
        help="запас вокруг лица, доля от размера (0.22 = 22%%)",
    )
    parser.add_argument(
        "--blur-range",
        nargs=2,
        action="append",
        metavar=("START", "END"),
        help=(
            "блюрить только в этом диапазоне; можно повторять. "
            "Время: секунды, MM:SS или HH:MM:SS"
        ),
    )
    parser.add_argument(
        "--voice",
        type=float,
        default=-3.0,
        metavar="SEMITONES",
        help="сдвиг тона в полутонах, -12..12 (0 = не менять)",
    )
    parser.add_argument(
        "--voice-range",
        nargs=2,
        action="append",
        metavar=("START", "END"),
        help="менять голос только в этом диапазоне; можно повторять",
    )
    parser.add_argument(
        "--preserve-formants",
        action="store_true",
        help="сохранить тембр (менее анонимно)",
    )
    parser.add_argument("--volume", type=float, default=1.0, help="громкость, 1.0 = 100%%")
    parser.add_argument("--mute", action="store_true", help="убрать звук")
    parser.add_argument("--crf", type=int, default=20, help="качество H.264, меньше = лучше")
    parser.add_argument(
        "--preset", default="veryfast", help="preset libx264, например veryfast"
    )
    parser.add_argument(
        "--detect-width",
        type=int,
        default=960,
        help="ширина кадра для детектора (больше = точнее и медленнее)",
    )
    return parser


def _describe_ranges(ranges: list[tuple[float, float]] | None) -> str:
    if ranges is None:
        return "везде"
    if not ranges:
        return "выкл"
    return ", ".join(f"{start:.2f}–{end:.2f}" for start, end in ranges)


def main(argv: list[str] | None = None) -> int:
    resources.configure_console()
    args = build_parser().parse_args(argv)
    source = args.input.expanduser()
    output = args.output.expanduser()

    try:
        blur_ranges = _parse_ranges(args.blur_range, "Блюр")
        voice_ranges = _parse_ranges(args.voice_range, "Голос")
    except ValueError as exc:
        print(f"Ошибка: {exc}", file=sys.stderr)
        return 2

    try:
        info = probe_video(source)
    except Exception as exc:  # noqa: BLE001 - CLI reports any setup failure
        print(f"Ошибка: {exc}", file=sys.stderr)
        return 2

    end = info.duration if args.end is None else args.end
    print(f"Вход:  {source}")
    print(
        f"       {info.width}x{info.height}, {info.fps:.2f} fps, "
        f"{info.duration:.2f} c, звук: {'да' if info.has_audio else 'нет'}"
    )
    print(f"Выход: {output}")
    print(
        f"Лица:  {'выкл' if args.no_blur else f'{args.method}, сила {args.strength:.1f}'}; "
        f"голос: {args.voice:+.1f} пт"
    )
    print(f"Фрагмент: {args.start:.2f}–{min(end, info.duration):.2f} c")
    print(f"Блюр:  {_describe_ranges(blur_ranges)}")
    print(f"Голос: {_describe_ranges(voice_ranges)}")

    config = ProcessConfig(
        input_path=source,
        output_path=output,
        start=args.start,
        end=args.end,
        blur_faces=not args.no_blur,
        blur_method=args.method,
        blur_strength=args.strength,
        blur_margin=args.margin,
        blur_ranges=blur_ranges,
        pitch_semitones=args.voice,
        voice_ranges=voice_ranges,
        preserve_formants=args.preserve_formants,
        volume=args.volume,
        mute=args.mute,
        crf=args.crf,
        preset=args.preset,
        detect_width=args.detect_width,
    )

    last_bucket = -1

    def report(fraction: float, message: str) -> None:
        nonlocal last_bucket
        bucket = int(fraction * 20)  # at most 20 progress lines
        if bucket != last_bucket or fraction >= 1.0:
            last_bucket = bucket
            print(f"\r[{fraction * 100:6.2f}%] {message}", end="", flush=True)

    try:
        result = process_video(config, progress=report)
    except KeyboardInterrupt:
        print("\nОтменено.", file=sys.stderr)
        return 130
    except Exception as exc:  # noqa: BLE001 - CLI reports any processing failure
        print(f"\nОшибка: {exc}", file=sys.stderr)
        return 1

    print(f"\nГотово: {result}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
