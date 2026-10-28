"""Server-side state, preview streams and export jobs for the web UI.

The desktop application and the web server intentionally have separate frame
readers and face trackers.  A still-frame request, an MJPEG stream and an export
must never share a decoder or tracker: seeking one would corrupt the others.
"""

from __future__ import annotations

import asyncio
import collections
import concurrent.futures
import functools
import math
import shutil
import subprocess
import tempfile
import threading
import time
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from pathlib import Path

import cv2
import numpy as np

from .. import resources
from ..faces import FaceAnonymizer, default_model_path
from ..ffmpeg_utils import build_audio_filter, clear_probe_cache, find_ffmpeg
from ..processor import (
    ProcessConfig,
    ProcessingCancelled,
    VideoInfo,
    probe_video,
    process_video,
)
from ..timing import is_active, regions_to_intervals
from ..video_reader import FFmpegFrameReader, VideoSource, open_video_source
from .models import RegionModel, SettingsModel

PREVIEW_DETECT_WIDTH = 640
PREVIEW_JPEG_WIDTH = 1600
STREAM_JPEG_WIDTH = 1280
STREAM_MAX_FPS = 30.0
JPEG_QUALITY = 85
JPEG_STREAM_QUALITY = 80
MJPEG_BOUNDARY = "frame"
MAX_UPLOAD_BYTES_DEFAULT = 64 * 1024**3


class StudioError(RuntimeError):
    """An error that should be shown to the user with a 4xx status."""

    status_code = 400


class BusyError(StudioError):
    status_code = 409


class ConflictError(StudioError):
    status_code = 409


class NoAudioError(StudioError):
    status_code = 404


class TooLargeError(StudioError):
    status_code = 413


@dataclass(frozen=True)
class LoadedSource:
    path: Path
    info: VideoInfo
    name: str
    temporary: bool = False


@dataclass
class ExportJob:
    id: str
    source_name: str
    output_path: Path
    download_name: str
    status: str = "running"
    progress: float = 0.0
    message: str = "Подготовка…"
    error: str | None = None
    size_bytes: int = 0
    created_at: float = field(default_factory=time.time)
    finished_at: float | None = None
    cancel_event: threading.Event = field(default_factory=threading.Event)
    done_event: threading.Event = field(default_factory=threading.Event)


@dataclass(frozen=True)
class StreamSpec:
    source: LoadedSource
    settings: SettingsModel
    blur_intervals: list[tuple[float, float]]
    voice_intervals: list[tuple[float, float]]
    start: float
    end: float
    version: int


@dataclass(frozen=True)
class AudioSpec:
    spec: StreamSpec
    command: list[str]
    media_type: str


def _safe_filename(name: str) -> str:
    """Return a display/download filename without path or control characters."""
    base = Path(str(name or "video")).name
    cleaned = "".join(
        char
        for char in base
        if char.isprintable() and char not in '\\/:*?"<>|'
    ).strip(" .")
    if not cleaned:
        cleaned = "video"
    if len(cleaned) > 180:
        stem = Path(cleaned).stem[:160]
        suffix = Path(cleaned).suffix[:16]
        cleaned = stem + suffix
    return cleaned


def _encode_jpeg(frame: np.ndarray, *, max_width: int, quality: int) -> bytes:
    height, width = frame.shape[:2]
    if width > max_width:
        scale = max_width / float(width)
        frame = cv2.resize(
            frame,
            (max_width, max(1, round(height * scale))),
            interpolation=cv2.INTER_AREA,
        )
    ok, encoded = cv2.imencode(
        ".jpg", frame, [int(cv2.IMWRITE_JPEG_QUALITY), int(quality)]
    )
    if not ok:
        raise StudioError("Не удалось закодировать кадр в JPEG.")
    return encoded.tobytes()


def _part_header(length: int) -> bytes:
    return (
        f"--{MJPEG_BOUNDARY}\r\n"
        "Content-Type: image/jpeg\r\n"
        f"Content-Length: {length}\r\n\r\n"
    ).encode("ascii")


@functools.lru_cache(maxsize=1)
def _audio_encoder() -> tuple[list[str], str]:
    """Pick a streaming-friendly audio encoder that the bundled FFmpeg has."""
    try:
        result = subprocess.run(
            [find_ffmpeg(), "-hide_banner", "-encoders"],
            capture_output=True,
            timeout=30,
            check=False,
            **resources.ffmpeg_text_kwargs(),
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise StudioError(f"Не удалось запустить FFmpeg: {exc}") from exc
    listing = (result.stdout or "") + (result.stderr or "")
    if "libmp3lame" in listing:
        return (
            ["-c:a", "libmp3lame", "-b:a", "192k", "-f", "mp3"],
            "audio/mpeg",
        )
    if " aac " in listing or "AAC" in listing:
        # ADTS is streamable and understood by Chromium/Firefox.
        return (["-c:a", "aac", "-b:a", "192k", "-f", "adts"], "audio/aac")
    raise StudioError("В этой сборке FFmpeg нет MP3/AAC-кодировщика для звука.")


class Studio:
    """All mutable state of one local Anomizer server process."""

    def __init__(
        self,
        *,
        temp_dir: Path | None = None,
        max_upload_bytes: int = MAX_UPLOAD_BYTES_DEFAULT,
    ) -> None:
        if temp_dir is None:
            root = Path(tempfile.mkdtemp(prefix="anomizer-web-"))
        else:
            root = Path(temp_dir)
            root.mkdir(parents=True, exist_ok=True)
        self.temp_root = root.resolve()
        try:
            self.temp_root.chmod(0o700)
        except OSError:
            pass
        self.uploads_dir = self.temp_root / "uploads"
        self.jobs_dir = self.temp_root / "jobs"
        for directory in (self.uploads_dir, self.jobs_dir):
            directory.mkdir(parents=True, exist_ok=True)
        self.max_upload_bytes = max(0, int(max_upload_bytes))

        self._model_path = default_model_path()
        self._lock = threading.RLock()
        self._op_lock = threading.RLock()
        self._source: LoadedSource | None = None
        self._settings: SettingsModel | None = None
        self._blur_intervals: list[tuple[float, float]] = []
        self._voice_intervals: list[tuple[float, float]] = []
        self._version = 0
        self._detector_backend = ""
        self._detector_warning: str | None = None

        self._preview_cap: VideoSource | None = None
        self._preview_anonymizer: FaceAnonymizer | None = None
        self._preview_key: tuple[object, ...] | None = None

        self._jobs: dict[str, ExportJob] = {}
        self._executor = concurrent.futures.ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="anomizer-export"
        )

        self._streams: dict[int, threading.Event] = {}
        self._stream_ids = 0
        self._closed = False

    # --------------------------------------------------------------- lifecycle
    def _check_open_locked(self) -> LoadedSource:
        if self._closed:
            raise StudioError("Сервер уже остановлен.")
        if self._source is None:
            raise StudioError("Видео не открыто.")
        return self._source

    def _running_job_locked(self) -> ExportJob | None:
        for job in self._jobs.values():
            if job.status == "running":
                return job
        return None

    def _close_preview_locked(self) -> None:
        if self._preview_cap is not None:
            self._preview_cap.release()
            self._preview_cap = None
        self._preview_anonymizer = None
        self._preview_key = None

    def _make_anonymizer(self, settings: SettingsModel) -> FaceAnonymizer:
        return FaceAnonymizer(
            model_path=self._model_path,
            method=settings.blur_method,
            strength=settings.blur_strength,
            margin=settings.blur_margin,
            detect_width=PREVIEW_DETECT_WIDTH,
            allow_download=False,
        )

    def _remember_detector_locked(self, anonymizer: FaceAnonymizer) -> None:
        with self._lock:
            self._detector_backend = anonymizer.backend_name
            self._detector_warning = anonymizer.warning

    def _ensure_preview_anonymizer_locked(
        self, settings: SettingsModel
    ) -> FaceAnonymizer:
        key = (
            settings.blur_method,
            settings.blur_strength,
            settings.blur_margin,
        )
        if self._preview_anonymizer is None or self._preview_key != key:
            try:
                anonymizer = self._make_anonymizer(settings)
            except Exception as exc:
                raise StudioError(f"Детектор лиц недоступен: {exc}") from exc
            self._preview_anonymizer = anonymizer
            self._preview_key = key
            self._remember_detector_locked(anonymizer)
        return self._preview_anonymizer

    def _clear_jobs_locked(self) -> None:
        for job in self._jobs.values():
            if job.status == "running":
                raise BusyError("Дождитесь завершения экспорта или отмените его.")
        self._jobs.clear()
        self._remove_dir(self.jobs_dir)
        self.jobs_dir.mkdir(parents=True, exist_ok=True)

    def _clear_audio_cache(self) -> None:
        # Audio is streamed, not cached.  Kept as one place to remove any
        # temporary files a future implementation might leave behind.
        pass

    @staticmethod
    def _remove_dir(path: Path) -> None:
        shutil.rmtree(path, ignore_errors=True)

    def _stop_streams(self, timeout: float = 3.0) -> None:
        with self._lock:
            events = list(self._streams.values())
        for event in events:
            event.set()
        deadline = time.monotonic() + max(0.0, timeout)
        while time.monotonic() < deadline:
            with self._lock:
                if not self._streams:
                    return
            time.sleep(0.02)

    def _detach_source_locked(self) -> LoadedSource | None:
        old = self._source
        self._close_preview_locked()
        self._source = None
        self._settings = None
        self._blur_intervals = []
        self._voice_intervals = []
        self._detector_backend = ""
        self._detector_warning = None
        self._version += 1
        return old

    def _delete_source_file(self, source: LoadedSource | None) -> None:
        if source is None or not source.temporary:
            return
        self._remove_dir(source.path.parent)

    # ---------------------------------------------------------------- opening
    def open_path(
        self,
        path: str | Path,
        *,
        display_name: str | None = None,
        temporary: bool = False,
    ) -> dict:
        """Probe and select a video.  The source itself is opened read-only."""
        with self._op_lock:
            with self._lock:
                running = self._running_job_locked()
            if running is not None:
                raise BusyError("Дождитесь завершения экспорта или отмените его.")

            candidate = Path(path).expanduser()
            try:
                candidate = candidate.resolve(strict=True)
            except OSError as exc:
                raise StudioError("Файл не найден.") from exc
            if not candidate.is_file():
                raise StudioError("Это не обычный файл.")
            try:
                if candidate.stat().st_size <= 0:
                    raise StudioError("Файл пустой.")
            except OSError as exc:
                raise StudioError("Не удалось прочитать файл.") from exc

            self._stop_streams()
            clear_probe_cache()
            try:
                info = probe_video(candidate)
            except Exception as exc:
                raise StudioError(f"Не удалось открыть видео: {exc}") from exc

            default_settings = SettingsModel(
                end=info.duration,
                regions=[
                    RegionModel(
                        start=0.0,
                        end=info.duration,
                        blur=True,
                        voice=True,
                    )
                ],
            )
            try:
                anonymizer = self._make_anonymizer(default_settings)
            except Exception as exc:
                raise StudioError(f"Детектор лиц недоступен: {exc}") from exc

            name = _safe_filename(display_name or candidate.name)
            with self._lock:
                self._clear_jobs_locked()
                self._clear_audio_cache()
                old = self._detach_source_locked()
                self._source = LoadedSource(
                    path=candidate,
                    info=info,
                    name=name,
                    temporary=temporary,
                )
                self._settings = default_settings
                self._blur_intervals = regions_to_intervals(
                    default_settings.regions, "blur", info.duration
                )
                self._voice_intervals = regions_to_intervals(
                    default_settings.regions, "voice", info.duration
                )
                self._preview_anonymizer = anonymizer
                self._preview_key = (
                    default_settings.blur_method,
                    default_settings.blur_strength,
                    default_settings.blur_margin,
                )
                self._remember_detector_locked(anonymizer)
                payload = self._state_locked()
            self._delete_source_file(old)
            return payload

    def ensure_can_replace_source(self) -> None:
        """Raise 409 before an upload starts if an export is still running."""
        with self._lock:
            if self._running_job_locked() is not None:
                raise BusyError("Дождитесь завершения экспорта или отмените его.")

    def close_source(self) -> dict:
        with self._op_lock:
            with self._lock:
                if self._running_job_locked() is not None:
                    raise BusyError(
                        "Дождитесь завершения экспорта или отмените его."
                    )
            self._stop_streams()
            with self._lock:
                self._clear_jobs_locked()
                self._clear_audio_cache()
                old = self._detach_source_locked()
                payload = self.state_payload()
            self._delete_source_file(old)
            return payload

    def shutdown(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            jobs = list(self._jobs.values())
        for job in jobs:
            job.cancel_event.set()
        self._stop_streams(timeout=2.0)
        with self._lock:
            self._detach_source_locked()
            self._closed = True
        # Give a running export a bounded chance to notice cancellation.
        deadline = time.monotonic() + 5.0
        for job in jobs:
            remaining = max(0.0, deadline - time.monotonic())
            job.done_event.wait(timeout=remaining)
        self._executor.shutdown(wait=True, cancel_futures=True)
        self._remove_dir(self.temp_root)

    # --------------------------------------------------------------- settings
    def _normalize_settings_locked(
        self, payload: SettingsModel, source: LoadedSource
    ) -> SettingsModel:
        duration = float(source.info.duration)
        start = min(max(0.0, float(payload.start)), duration)
        end = duration if payload.end is None else float(payload.end)
        end = min(max(0.0, end), duration)
        if end - start < 0.02:
            raise StudioError("Выбранный фрагмент пустой.")

        regions: list[RegionModel] = []
        for index, region in enumerate(payload.regions, start=1):
            first = min(max(0.0, float(region.start)), duration)
            last = min(max(0.0, float(region.end)), duration)
            if last - first < 0.001:
                raise StudioError(
                    f"Диапазон {index}: конец должен быть больше начала "
                    "и находиться внутри видео."
                )
            regions.append(
                RegionModel(
                    start=first,
                    end=last,
                    blur=bool(region.blur),
                    voice=bool(region.voice),
                )
            )

        data = payload.model_dump()
        data.update(
            start=start,
            end=end,
            regions=[region.model_dump() for region in regions],
        )
        return SettingsModel.model_validate(data)

    def update_settings(
        self, payload: SettingsModel, *, expected_version: int | None = None
    ) -> dict:
        with self._lock:
            source = self._check_open_locked()
            if expected_version is not None and expected_version != self._version:
                raise ConflictError("Настройки уже изменились. Обновите страницу.")
            settings = self._normalize_settings_locked(payload, source)
            self._settings = settings
            self._blur_intervals = regions_to_intervals(
                settings.regions, "blur", source.info.duration
            )
            self._voice_intervals = regions_to_intervals(
                settings.regions, "voice", source.info.duration
            )
            self._version += 1
            return self._state_locked()

    def _state_locked(self) -> dict:
        source = self._check_open_locked()
        assert self._settings is not None
        return {
            "loaded": True,
            "version": self._version,
            "info": self._info_dict(source),
            "settings": self._settings.model_dump(),
            "blur_intervals": [list(interval) for interval in self._blur_intervals],
            "voice_intervals": [list(interval) for interval in self._voice_intervals],
            "detector": {
                "backend": self._detector_backend,
                "warning": self._detector_warning,
            },
        }

    @staticmethod
    def _info_dict(source: LoadedSource) -> dict:
        info = source.info
        return {
            "name": source.name,
            "uploaded": source.temporary,
            "width": info.width,
            "height": info.height,
            "fps": info.fps,
            "frame_count": info.frame_count,
            "duration": info.duration,
            "has_audio": info.has_audio,
            "sample_rate": info.sample_rate,
            "decode_with_ffmpeg": info.decode_with_ffmpeg,
        }

    def state_payload(self) -> dict:
        with self._lock:
            if self._source is None:
                return {
                    "loaded": False,
                    "version": self._version,
                    "offline": True,
                }
            return self._state_locked()

    def settings_snapshot(
        self, expected_version: int | None = None
    ) -> tuple[LoadedSource, SettingsModel, int]:
        with self._lock:
            source = self._check_open_locked()
            if expected_version is not None and expected_version != self._version:
                raise ConflictError("Настройки уже изменились. Обновите страницу.")
            if self._settings is None:
                raise StudioError("Видео не открыто.")
            return source, self._settings, self._version

    # ---------------------------------------------------------------- preview
    def _ensure_preview_cap_locked(self, source: LoadedSource) -> VideoSource:
        if self._preview_cap is not None and self._preview_cap.isOpened():
            return self._preview_cap
        if self._preview_cap is not None:
            self._preview_cap.release()
            self._preview_cap = None
        info = source.info
        try:
            cap = open_video_source(
                source.path,
                width=info.width,
                height=info.height,
                fps=info.fps,
                duration=info.duration,
                decode_with_ffmpeg=info.decode_with_ffmpeg,
                start=0.0,
            )
        except Exception as exc:
            raise StudioError(f"Не удалось открыть видео: {exc}") from exc
        if not cap.isOpened():
            cap.release()
            raise StudioError("Не удалось открыть видео.")
        self._preview_cap = cap
        return cap

    def _read_preview_frame_locked(
        self, source: LoadedSource, seconds: float
    ) -> tuple[np.ndarray, float]:
        info = source.info
        last = max(0.0, info.duration - 1.0 / max(info.fps, 1.0))
        frame_time = min(max(0.0, float(seconds)), last)
        cap = self._ensure_preview_cap_locked(source)
        cap.set(cv2.CAP_PROP_POS_MSEC, frame_time * 1000.0)
        ok, frame = cap.read()
        if (not ok or frame is None) and isinstance(cap, FFmpegFrameReader):
            detail = cap.error_text()
            cap.release()
            self._preview_cap = None
            raise StudioError(
                "Не удалось прочитать кадр." + (f"\n\n{detail}" if detail else "")
            )
        if not ok or frame is None:
            cap.release()
            self._preview_cap = None
            raise StudioError("Не удалось прочитать кадр.")
        return frame, frame_time

    def frame_jpeg(
        self, seconds: float, *, expected_version: int | None = None
    ) -> tuple[bytes, float, int]:
        with self._lock:
            source = self._check_open_locked()
            if expected_version is not None and expected_version != self._version:
                raise ConflictError("Кадр устарел. Обновите страницу.")
            assert self._settings is not None
            settings = self._settings
            blur_intervals = list(self._blur_intervals)
            frame, frame_time = self._read_preview_frame_locked(source, seconds)
            blur_active = settings.blur_enabled and is_active(
                blur_intervals, frame_time
            )
            show_boxes = settings.show_boxes
            if blur_active or show_boxes:
                anonymizer = self._ensure_preview_anonymizer_locked(settings)
                anonymizer.reset()
                frame, _ = anonymizer.process(
                    frame, draw_boxes=show_boxes, blur=blur_active
                )
            jpeg = _encode_jpeg(
                frame,
                max_width=PREVIEW_JPEG_WIDTH,
                quality=JPEG_QUALITY,
            )
            return jpeg, frame_time, self._version

    def prepare_stream(
        self, *, from_time: float | None = None, expected_version: int | None = None
    ) -> StreamSpec:
        with self._lock:
            source = self._check_open_locked()
            if expected_version is not None and expected_version != self._version:
                raise ConflictError("Настройки уже изменились. Обновите страницу.")
            if self._settings is None:
                raise StudioError("Видео не открыто.")
            settings = self._settings.model_copy(deep=True)
            start = settings.start if from_time is None else float(from_time)
            end = float(settings.end or source.info.duration)
            start = min(max(settings.start, start), max(settings.start, end))
            if end - start < 0.02:
                raise StudioError("Выбранный фрагмент пустой.")
            return StreamSpec(
                source=source,
                settings=settings,
                blur_intervals=list(self._blur_intervals),
                voice_intervals=list(self._voice_intervals),
                start=start,
                end=end,
                version=self._version,
            )

    def _register_stream(self) -> tuple[int, threading.Event]:
        with self._lock:
            self._stream_ids += 1
            stream_id = self._stream_ids
            event = threading.Event()
            self._streams[stream_id] = event
            return stream_id, event

    def _finish_stream(self, stream_id: int) -> None:
        with self._lock:
            self._streams.pop(stream_id, None)

    def _version_current(self, version: int) -> bool:
        with self._lock:
            return not self._closed and version == self._version

    async def mjpeg_stream(
        self, spec: StreamSpec
    ) -> AsyncIterator[bytes]:
        """Yield an MJPEG multipart stream, paced against a monotonic clock."""
        stream_id, cancel = self._register_stream()
        cap: VideoSource | None = None
        anonymizer: FaceAnonymizer | None = None
        try:
            info = spec.source.info
            cap = await asyncio.to_thread(
                open_video_source,
                spec.source.path,
                width=info.width,
                height=info.height,
                fps=info.fps,
                duration=info.duration,
                decode_with_ffmpeg=info.decode_with_ffmpeg,
                start=spec.start,
            )
            if not cap.isOpened():
                return
            need_anonymizer = spec.settings.show_boxes or (
                spec.settings.blur_enabled and bool(spec.blur_intervals)
            )
            if need_anonymizer:
                try:
                    anonymizer = await asyncio.to_thread(
                        self._make_anonymizer, spec.settings
                    )
                except Exception:  # noqa: BLE001 - preview falls back to no boxes
                    return
                self._remember_detector_locked(anonymizer)

            fps = max(1.0, float(info.fps))
            step = max(1, math.ceil(fps / STREAM_MAX_FPS))
            out_fps = fps / step
            duration = max(0.0, spec.end - spec.start)
            total = max(1, round(duration * out_fps))
            started = time.monotonic()
            previous_blur = False
            output_index = 0
            source_index = 0
            ok, frame = await asyncio.to_thread(cap.read)
            while ok and frame is not None:
                if cancel.is_set() or not self._version_current(spec.version):
                    break
                now = time.monotonic() - started
                desired = min(total - 1, int(now * out_fps))
                if desired > output_index:
                    # Drop stale frames instead of letting video drift behind
                    # the audio.  At most two seconds are skipped per pass so a
                    # short scheduling hiccup does not turn into a seek storm.
                    max_drop = max(1, round(out_fps * 2.0))
                    desired = min(desired, output_index + max_drop)
                    target_source = desired * step
                    skip = target_source - source_index - 1
                    for _ in range(max(0, skip)):
                        if not await asyncio.to_thread(cap.grab):
                            ok = False
                            break
                    if not ok:
                        break
                    ok, frame = await asyncio.to_thread(cap.read)
                    if not ok or frame is None:
                        break
                    output_index = desired
                    source_index = target_source

                frame_time = spec.start + output_index * step / fps
                if frame_time >= spec.end - 1e-9:
                    break
                blur_active = spec.settings.blur_enabled and is_active(
                    spec.blur_intervals, frame_time
                )
                if anonymizer is not None:
                    tracking = blur_active or spec.settings.show_boxes
                    if tracking:
                        if (
                            blur_active
                            and not previous_blur
                            and not spec.settings.show_boxes
                        ):
                            anonymizer.reset()
                        frame, _ = await asyncio.to_thread(
                            anonymizer.process,
                            frame,
                            spec.settings.show_boxes,
                            blur_active,
                        )
                    elif previous_blur:
                        anonymizer.reset()
                    previous_blur = blur_active

                jpeg = await asyncio.to_thread(
                    _encode_jpeg,
                    frame,
                    max_width=STREAM_JPEG_WIDTH,
                    quality=JPEG_STREAM_QUALITY,
                )
                yield _part_header(len(jpeg)) + jpeg + b"\r\n"

                output_index += 1
                if output_index >= total:
                    break
                deadline = started + output_index / out_fps
                delay = deadline - time.monotonic()
                if delay > 0:
                    await asyncio.sleep(min(delay, 0.2))
                if cancel.is_set() or not self._version_current(spec.version):
                    break

                # Advance to the next output frame, dropping the frames between
                # source indices when the source is faster than 30 fps.
                for _ in range(step - 1):
                    if not await asyncio.to_thread(cap.grab):
                        ok = False
                        break
                if not ok:
                    break
                ok, frame = await asyncio.to_thread(cap.read)
                source_index += step
        finally:
            if cap is not None:
                cap.release()
            self._finish_stream(stream_id)

    # ------------------------------------------------------------------ audio
    def prepare_audio(
        self, *, from_time: float | None = None, expected_version: int | None = None
    ) -> AudioSpec:
        spec = self.prepare_stream(
            from_time=from_time, expected_version=expected_version
        )
        if not spec.source.info.has_audio:
            raise NoAudioError("В видео нет звуковой дорожки.")
        if spec.settings.mute:
            raise NoAudioError("Звук отключён в настройках.")
        graph = build_audio_filter(
            input_label="0:a:0",
            start=spec.start,
            duration=spec.end - spec.start,
            voice_intervals=spec.voice_intervals,
            semitones=spec.settings.voice_semitones,
            preserve_formants=spec.settings.preserve_formants,
            volume=spec.settings.volume,
            sample_rate=spec.source.info.sample_rate,
        )
        encoder, media_type = _audio_encoder()
        command = [
            find_ffmpeg(),
            "-hide_banner",
            "-loglevel",
            "error",
            "-nostdin",
            "-ss",
            f"{spec.start:.6f}",
            "-t",
            f"{spec.end - spec.start:.6f}",
            "-i",
            str(spec.source.path),
            "-vn",
            "-sn",
            "-dn",
            "-filter_complex",
            graph,
            "-map",
            "[a]",
            "-ac",
            "2",
            "-ar",
            "48000",
            *encoder,
            "pipe:1",
        ]
        return AudioSpec(spec=spec, command=command, media_type=media_type)

    async def audio_stream(self, audio: AudioSpec) -> AsyncIterator[bytes]:
        stream_id, cancel = self._register_stream()
        process: subprocess.Popen[bytes] | None = None
        stderr_lines: collections.deque[str] = collections.deque(maxlen=20)
        stderr_thread: threading.Thread | None = None
        try:
            process = await asyncio.to_thread(
                subprocess.Popen,
                audio.command,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                bufsize=0,
                **resources.no_console_kwargs(),
            )
            assert process.stdout is not None
            assert process.stderr is not None

            def drain_stderr(stream) -> None:
                try:
                    for raw in stream:
                        line = raw.decode("utf-8", errors="replace").strip()
                        if line:
                            stderr_lines.append(line)
                except (OSError, ValueError):
                    pass

            stderr_thread = threading.Thread(
                target=drain_stderr,
                args=(process.stderr,),
                daemon=True,
                name="anomizer-web-audio-stderr",
            )
            stderr_thread.start()

            while True:
                if cancel.is_set() or not self._version_current(audio.spec.version):
                    break
                chunk = await asyncio.to_thread(process.stdout.read, 65536)
                if not chunk:
                    break
                yield chunk
        finally:
            if process is not None:
                if process.poll() is None:
                    process.terminate()
                    try:
                        process.wait(timeout=2.0)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        try:
                            process.wait(timeout=1.0)
                        except subprocess.TimeoutExpired:
                            pass
                for stream in (process.stdout, process.stderr):
                    if stream is not None:
                        try:
                            stream.close()
                        except (OSError, ValueError):
                            pass
            if stderr_thread is not None and stderr_thread.is_alive():
                stderr_thread.join(timeout=1.0)
            self._finish_stream(stream_id)

    # ------------------------------------------------------------------- jobs
    def _job_dict(self, job: ExportJob) -> dict:
        return {
            "id": job.id,
            "source_name": job.source_name,
            "status": job.status,
            "progress": max(0.0, min(1.0, float(job.progress))),
            "message": job.message,
            "error": job.error,
            "download_name": job.download_name,
            "size_bytes": int(job.size_bytes),
            "download_url": (
                f"/api/jobs/{job.id}/download"
                if job.status == "done" and job.output_path.is_file()
                else None
            ),
            "created_at": job.created_at,
            "finished_at": job.finished_at,
        }

    def _prune_finished_jobs_locked(self) -> None:
        for job_id, job in list(self._jobs.items()):
            if job.status != "running":
                self._remove_dir(job.output_path.parent)
                self._jobs.pop(job_id, None)

    def start_export(self, *, expected_version: int | None = None) -> dict:
        with self._lock:
            source = self._check_open_locked()
            if expected_version is not None and expected_version != self._version:
                raise ConflictError("Настройки уже изменились. Обновите страницу.")
            if self._settings is None:
                raise StudioError("Видео не открыто.")
            if self._running_job_locked() is not None:
                raise BusyError("Экспорт уже выполняется. Дождитесь его окончания.")
            settings = self._settings.model_copy(deep=True)
            blur_intervals = list(self._blur_intervals)
            voice_intervals = list(self._voice_intervals)
            self._prune_finished_jobs_locked()

            job_id = uuid.uuid4().hex
            job_dir = self.jobs_dir / job_id
            job_dir.mkdir(parents=True, exist_ok=True)
            output = job_dir / "result.mp4"
            download_name = _safe_filename(
                f"{Path(source.name).stem}_anonymized.mp4"
            )
            job = ExportJob(
                id=job_id,
                source_name=source.name,
                output_path=output,
                download_name=download_name,
            )
            self._jobs[job_id] = job
            config = ProcessConfig(
                input_path=source.path,
                output_path=output,
                start=settings.start,
                end=settings.end,
                blur_faces=settings.blur_enabled,
                blur_method=settings.blur_method,
                blur_strength=settings.blur_strength,
                blur_margin=settings.blur_margin,
                blur_ranges=blur_intervals,
                pitch_semitones=settings.voice_semitones,
                voice_ranges=voice_intervals,
                preserve_formants=settings.preserve_formants,
                volume=settings.volume,
                mute=settings.mute,
                show_boxes=False,
                crf=settings.crf,
                preset=settings.preset,
            )
            info = source.info

        self._executor.submit(self._run_export, job, config, info)
        with self._lock:
            return self._job_dict(job)

    def _run_export(
        self, job: ExportJob, config: ProcessConfig, info: VideoInfo
    ) -> None:
        try:
            if job.cancel_event.is_set():
                raise ProcessingCancelled("Обработка отменена.")

            def progress(fraction: float, message: str) -> None:
                with self._lock:
                    if job.status != "running":
                        return
                    job.progress = max(0.0, min(1.0, float(fraction)))
                    job.message = message

            process_video(
                config,
                progress=progress,
                cancel_event=job.cancel_event,
                info=info,
            )
            with self._lock:
                if job.cancel_event.is_set() and not job.output_path.is_file():
                    job.status = "cancelled"
                    job.message = "Отменено"
                else:
                    job.status = "done"
                    job.progress = 1.0
                    job.message = "Готово"
                    job.size_bytes = (
                        job.output_path.stat().st_size
                        if job.output_path.is_file()
                        else 0
                    )
                job.finished_at = time.time()
        except ProcessingCancelled:
            with self._lock:
                job.status = "cancelled"
                job.message = "Отменено"
                job.error = None
                job.finished_at = time.time()
        except Exception as exc:  # noqa: BLE001 - reported to the local UI
            message = str(exc).strip() or exc.__class__.__name__
            with self._lock:
                job.status = "error"
                job.message = "Ошибка экспорта"
                job.error = message[:8000]
                job.finished_at = time.time()
        finally:
            job.done_event.set()

    def job_state(self, job_id: str) -> dict:
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                raise StudioError("Задача не найдена.")
            return self._job_dict(job)

    def cancel_job(self, job_id: str) -> dict:
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                raise StudioError("Задача не найдена.")
            if job.status == "running":
                job.cancel_event.set()
                job.message = "Отмена…"
            return self._job_dict(job)

    def job_download(self, job_id: str) -> tuple[Path, str]:
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                raise StudioError("Задача не найдена.")
            if job.status != "done" or not job.output_path.is_file():
                raise BusyError("Результат ещё не готов.")
            return job.output_path, job.download_name

    def delete_job(self, job_id: str) -> None:
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                raise StudioError("Задача не найдена.")
            if job.status == "running":
                raise BusyError("Сначала отмените экспорт.")
            job_dir = job.output_path.parent
            self._jobs.pop(job_id, None)
        try:
            shutil.rmtree(job_dir)
        except OSError as exc:
            raise BusyError(
                "Не удалось удалить файл: возможно, он ещё скачивается."
            ) from exc

    # -------------------------------------------------------------- uploading
    def new_upload_target(self, display_name: str) -> tuple[Path, Path]:
        """Create a private directory and a server-chosen file path."""
        directory = self.uploads_dir / uuid.uuid4().hex
        directory.mkdir(parents=True, exist_ok=False)
        name = _safe_filename(display_name)
        target = directory / name
        return directory, target

    def remove_upload_dir(self, directory: Path) -> None:
        self._remove_dir(directory)

    def open_upload(
        self, target: Path, display_name: str
    ) -> dict:
        try:
            return self.open_path(
                target, display_name=display_name, temporary=True
            )
        except Exception:
            self.remove_upload_dir(target.parent)
            raise
