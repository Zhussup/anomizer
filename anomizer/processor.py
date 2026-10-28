"""Video probing and the face/audio processing pipeline."""

from __future__ import annotations

import os
import subprocess
import tempfile
import threading
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import cv2
import numpy as np

from . import resources
from .faces import FaceAnonymizer
from .ffmpeg_utils import (
    FFmpegError,
    build_audio_filter,
    find_ffmpeg,
    probe_frame_field_order,
    probe_media,
)
from .timing import effect_intervals, is_active
from .video_reader import FFmpegFrameReader, open_video_source

ProgressCallback = Callable[[float, str], None]


class ProcessingCancelled(RuntimeError):
    pass


@dataclass(frozen=True)
class VideoInfo:
    path: Path
    width: int
    height: int
    fps: float
    frame_count: int
    duration: float
    has_audio: bool
    sample_rate: int
    decode_with_ffmpeg: bool = False


@dataclass
class ProcessConfig:
    input_path: Path
    output_path: Path
    start: float = 0.0
    end: float | None = None
    blur_faces: bool = True
    blur_method: str = "gaussian"  # gaussian | pixelate
    blur_strength: float = 3.0
    blur_margin: float = 0.22
    blur_ranges: list[tuple[float, float]] | None = None
    detection_threshold: float = 0.6
    detect_width: int = 960
    show_boxes: bool = False  # useful for debug exports, normally off
    pitch_semitones: float = -3.0
    voice_ranges: list[tuple[float, float]] | None = None
    preserve_formants: bool = False
    volume: float = 1.0
    mute: bool = False
    crf: int = 20
    preset: str = "veryfast"


def _frame_is_blank(frame) -> bool:
    """Detect the unusable frames OpenCV returns for some interlaced files.

    OpenCV's FFmpeg backend can report a successful read while the swscale
    conversion produced only black pixels.  Such a frame is not a legitimate
    dark scene in practice, and falling back to the external FFmpeg reader is
    harmless even when it is.
    """
    if frame is None or getattr(frame, "size", 0) == 0:
        return True
    height, width = frame.shape[:2]
    if height <= 0 or width <= 0:
        return True
    step_y = max(1, height // 64)
    step_x = max(1, width // 64)
    return float(frame[::step_y, ::step_x].mean()) < 1.0


def _frames_look_broken(first, second) -> bool:
    """Detect the sparse, frozen garbage left by a failed interlaced convert.

    A broken decode often returns the same mostly-black frame for every
    timestamp.  The exact output varies between processes, so checking only
    brightness is not reliable.  Requiring two identical low-mean frames keeps
    ordinary bright static scenes on the fast OpenCV path; a dark static scene
    merely uses the FFmpeg fallback, which is still correct.
    """
    if first is None or second is None or first.shape != second.shape:
        return True
    if _frame_is_blank(first) or _frame_is_blank(second):
        return True
    if float(first.mean()) >= 60.0:
        return False
    return bool(np.array_equal(first, second))


def probe_video(path: Path) -> VideoInfo:
    cap = cv2.VideoCapture(str(path))
    if not cap.isOpened():
        cap.release()
        raise RuntimeError(f"Не удалось открыть видео: {path}")

    try:
        width = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH) or 0)
        height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT) or 0)
        fps = float(cap.get(cv2.CAP_PROP_FPS) or 0.0)
        frame_count = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)

        if width <= 0 or height <= 0:
            raise RuntimeError("Не удалось определить размеры видео.")
        if not (1.0 <= fps <= 240.0):
            fps = 25.0

        media = probe_media(str(path))
        decode_with_ffmpeg = media.interlaced
        if not decode_with_ffmpeg:
            # The banner can omit or mislabel field order.  A decoded frame
            # flag is authoritative; OpenCV fails only on field-coded frames
            # and reports success even when its conversion produced garbage.
            field_order = probe_frame_field_order(str(path), media.duration)
            decode_with_ffmpeg = field_order is True
        if not decode_with_ffmpeg:
            ok, first_frame = cap.read()
            if not ok or _frame_is_blank(first_frame):
                decode_with_ffmpeg = True
            else:
                ok, second_frame = cap.read()
                if not ok or _frames_look_broken(first_frame, second_frame):
                    decode_with_ffmpeg = True
    finally:
        cap.release()

    duration = frame_count / fps if frame_count > 0 and fps > 0 else media.duration
    if duration <= 0:
        raise RuntimeError("Не удалось определить длительность видео.")
    if frame_count <= 0:
        frame_count = round(duration * fps)

    return VideoInfo(
        path=path,
        width=width,
        height=height,
        fps=fps,
        frame_count=frame_count,
        duration=duration,
        has_audio=media.has_audio,
        sample_rate=media.sample_rate,
        decode_with_ffmpeg=decode_with_ffmpeg,
    )


def process_video(
    config: ProcessConfig,
    progress: ProgressCallback | None = None,
    cancel_event: threading.Event | None = None,
    *,
    info: VideoInfo | None = None,
) -> Path:
    """Trim, blur faces and change voice, then write a new video file.

    Frames are piped to FFmpeg as raw BGR, so no huge temporary video is written
    and H.264/AAC are used regardless of OpenCV's codec support.
    """
    source = Path(config.input_path)
    output = Path(config.output_path)
    if source.resolve() == output.resolve():
        raise ValueError("Исходный и итоговый файлы не должны совпадать.")
    if not source.is_file():
        raise FileNotFoundError(f"Видео не найдено: {source}")
    if config.blur_method not in {"gaussian", "pixelate"}:
        raise ValueError("Неизвестный метод блюра.")
    if not -12.0 <= float(config.pitch_semitones) <= 12.0:
        raise ValueError("Сдвиг тона должен быть в диапазоне −12…+12 полутонов.")

    if info is None:
        info = probe_video(source)
    elif info.path.resolve() != source.resolve():
        raise ValueError("Данные probe относятся к другому файлу.")
    start = max(0.0, min(float(config.start), info.duration))
    end = info.duration if config.end is None else float(config.end)
    end = max(0.0, min(end, info.duration))
    if end - start < 0.02:
        raise ValueError("Выбранный фрагмент пустой.")

    duration = end - start
    blur_intervals = effect_intervals(config.blur_ranges, start, end)
    output.parent.mkdir(parents=True, exist_ok=True)

    cap = open_video_source(
        source,
        width=info.width,
        height=info.height,
        fps=info.fps,
        duration=info.duration,
        decode_with_ffmpeg=info.decode_with_ffmpeg,
        start=start,
    )

    ok, first_frame = cap.read()
    if not ok or first_frame is None:
        detail = (
            cap.error_text()
            if isinstance(cap, FFmpegFrameReader)
            else ""
        )
        cap.release()
        raise RuntimeError(
            "Не удалось прочитать первый кадр выбранного фрагмента."
            + (f"\n\n{detail}" if detail else "")
        )

    # libx264 + yuv420p need even dimensions.
    frame_height, frame_width = first_frame.shape[:2]
    out_width = frame_width - (frame_width % 2)
    out_height = frame_height - (frame_height % 2)
    if out_width < 2 or out_height < 2:
        cap.release()
        raise RuntimeError("Слишком маленькое видео.")

    anonymizer: FaceAnonymizer | None = None
    try:
        if config.blur_faces and blur_intervals:
            anonymizer = FaceAnonymizer(
                method=config.blur_method,
                strength=config.blur_strength,
                margin=config.blur_margin,
                score_threshold=config.detection_threshold,
                detect_width=config.detect_width,
            )
        use_audio = info.has_audio and not config.mute
        ffmpeg = find_ffmpeg()
    except BaseException:
        cap.release()
        raise
    tmp_output = output.with_name(f".{output.stem}.{os.getpid()}.part{output.suffix}")

    cmd = [
        ffmpeg,
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-f",
        "rawvideo",
        "-vcodec",
        "rawvideo",
        "-pix_fmt",
        "bgr24",
        "-s",
        f"{out_width}x{out_height}",
        "-r",
        f"{info.fps:.6f}",
        "-i",
        "pipe:0",
    ]

    if use_audio:
        cmd += [
            "-ss",
            f"{start:.6f}",
            "-t",
            f"{duration:.6f}",
            "-i",
            str(source),
        ]
        try:
            audio_graph = build_audio_filter(
                input_label="1:a:0",
                start=start,
                duration=duration,
                voice_intervals=config.voice_ranges,
                semitones=config.pitch_semitones,
                preserve_formants=config.preserve_formants,
                volume=config.volume,
                sample_rate=info.sample_rate,
            )
        except BaseException:
            cap.release()
            raise
        cmd += [
            "-filter_complex",
            audio_graph,
            "-map",
            "0:v:0",
            "-map",
            "[a]",
        ]
    else:
        cmd += ["-map", "0:v:0"]

    cmd += [
        "-c:v",
        "libx264",
        "-preset",
        config.preset,
        "-crf",
        str(int(config.crf)),
        "-pix_fmt",
        "yuv420p",
    ]
    if use_audio:
        # The audio graph now pads/trims to exactly the trimmed duration, so
        # `-shortest` is unnecessary and could cut the last audio samples.
        # Output `-t` remains as a final cap for containers.
        cmd += [
            "-c:a",
            "aac",
            "-b:a",
            "192k",
            "-t",
            f"{duration:.6f}",
        ]
    if output.suffix.lower() in {".mp4", ".m4v", ".mov"}:
        cmd += ["-movflags", "+faststart"]
    cmd += ["-map_metadata", "-1", "-map_chapters", "-1", str(tmp_output)]

    total_frames = max(1, round(duration * info.fps))
    report_every = max(1, total_frames // 200)
    cancelled = False
    try:
        log_file = tempfile.TemporaryFile(mode="w+b")  # noqa: SIM115 - closed after FFmpeg
    except BaseException:
        cap.release()
        raise

    try:
        proc = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            stderr=log_file,
            **resources.no_console_kwargs(),
        )
        assert proc.stdin is not None
        previous_blur_active = False

        try:
            for frame_index in range(total_frames):
                if cancel_event is not None and cancel_event.is_set():
                    cancelled = True
                    break

                if frame_index == 0:
                    frame = first_frame
                else:
                    ok, frame = cap.read()
                    if not ok or frame is None:
                        break

                if frame.shape[1] != out_width or frame.shape[0] != out_height:
                    frame = frame[:out_height, :out_width]

                frame_time = start + frame_index / info.fps
                blur_active = (
                    anonymizer is not None
                    and is_active(blur_intervals, frame_time)
                )
                if blur_active:
                    if not previous_blur_active:
                        anonymizer.reset()
                    frame, _ = anonymizer.process(
                        frame, draw_boxes=config.show_boxes
                    )
                elif previous_blur_active:
                    anonymizer.reset()
                previous_blur_active = blur_active

                proc.stdin.write(frame.tobytes())

                if progress is not None and (
                    frame_index % report_every == 0 or frame_index + 1 == total_frames
                ):
                    fraction = min(1.0, (frame_index + 1) / float(total_frames))
                    progress(fraction, f"Кадр {frame_index + 1} из {total_frames}")

            if cancelled:
                raise ProcessingCancelled("Обработка отменена.")

            proc.stdin.close()
            return_code = proc.wait()
        except BrokenPipeError:
            return_code = proc.wait()
        except BaseException:
            if proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait()
            raise

        if cancelled:
            raise ProcessingCancelled("Обработка отменена.")

        if return_code != 0:
            log_file.seek(0)
            log = log_file.read().decode("utf-8", errors="replace").strip()
            raise FFmpegError(
                "FFmpeg не смог создать видео."
                + (f"\n\n{log[-4000:]}" if log else "")
            )

        if not tmp_output.is_file() or tmp_output.stat().st_size == 0:
            raise FFmpegError("FFmpeg завершился без ошибки, но файл не создан.")

        os.replace(tmp_output, output)
        if progress is not None:
            progress(1.0, "Готово")
        return output
    finally:
        cap.release()
        log_file.close()
        tmp_output.unlink(missing_ok=True)
