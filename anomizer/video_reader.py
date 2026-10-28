"""Video frame sources used by the preview and the export pipeline.

OpenCV is used for ordinary progressive files because it is fast and already a
dependency.  Some interlaced H.264/AVCHD files cannot be converted from
``yuv420p`` to ``bgr24`` by OpenCV's bundled FFmpeg/swscale.  In that case
OpenCV still reports frames, but they are black or contain only sparse garbage.
The external FFmpeg binary handles those files correctly when ``yadif``
deinterlaces them first, so those files go through a small raw-video reader with
the same interface as ``cv2.VideoCapture``.
"""

from __future__ import annotations

import collections
import math
import subprocess
import threading
from pathlib import Path

import cv2
import numpy as np

from . import resources
from .ffmpeg_utils import find_ffmpeg


class FFmpegFrameReader:
    """Read BGR frames from FFmpeg's rawvideo output.

    Only the subset of ``cv2.VideoCapture`` that this project uses is
    implemented: ``read``, ``grab``, ``retrieve``, ``get``/``set`` for the
    current position, ``isOpened`` and ``release``.  Seeking restarts FFmpeg,
    which keeps the implementation small and avoids keeping a large decoded
    frame queue in memory.
    """

    def __init__(
        self,
        path: str | Path,
        *,
        width: int,
        height: int,
        fps: float,
        duration: float = 0.0,
        start: float = 0.0,
        deinterlace: bool = True,
    ) -> None:
        if width <= 0 or height <= 0:
            raise ValueError("Размеры видео должны быть положительными.")
        self.path = Path(path)
        self.width = int(width)
        self.height = int(height)
        self.fps = float(fps) if 1.0 <= float(fps) <= 240.0 else 25.0
        self.duration = max(0.0, float(duration))
        self._deinterlace = bool(deinterlace)
        self._frame_size = self.width * self.height * 3
        self._start = max(0.0, float(start))
        self._next_time = self._start
        self._frame_index = 0
        self._next_index = 0
        self._last_frame: np.ndarray | None = None
        self._process: subprocess.Popen[bytes] | None = None
        self._stdout = None
        self._closed = False
        self._stderr_thread: threading.Thread | None = None
        self._stderr_lines: collections.deque[str] = collections.deque(maxlen=40)
        self._spawn()

    # ------------------------------------------------------------- lifecycle
    def _spawn(self) -> None:
        self._stop_process()
        self._stderr_lines.clear()
        self._next_time = self._start
        self._frame_index = 0
        self._last_frame = None

        # Yadif needs previous and next frames.  Seeking directly to the
        # requested position leaves its first output frame with a visible
        # boundary artifact, and for some audio+video remuxes FFmpeg can even
        # emit that frame twice.  Start a couple of frames earlier and discard
        # them after deinterlacing, so the first delivered frame is complete.
        warmup = 2.0 / self.fps if self._start > 0 else 0.0
        seek_time = max(0.0, self._start - warmup)
        first_index = math.ceil(seek_time * self.fps - 1e-6)
        target_index = math.ceil(self._start * self.fps - 1e-6)
        self._skip_frames = max(0, target_index - first_index)
        self._next_index = target_index

        cmd = [
            find_ffmpeg(),
            "-hide_banner",
            "-loglevel",
            "error",
            "-nostdin",
            # Keep source timestamps; without this, remuxed files that also
            # contain audio can shift timestamps and duplicate the seek frame.
            "-copyts",
            "-ss",
            f"{seek_time:.6f}",
            "-i",
            str(self.path),
            "-map",
            "0:v:0",
            "-an",
            "-sn",
            "-dn",
        ]
        if self._deinterlace:
            cmd += ["-vf", "yadif=deint=interlaced"]
        cmd += [
            "-pix_fmt",
            "bgr24",
            "-f",
            "rawvideo",
            "-s",
            f"{self.width}x{self.height}",
            "-r",
            f"{self.fps:.6f}",
            "pipe:1",
        ]
        self._process = subprocess.Popen(
            cmd,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            bufsize=self._frame_size,
            **resources.no_console_kwargs(),
        )
        self._stdout = self._process.stdout
        assert self._process.stderr is not None
        self._stderr_thread = threading.Thread(
            target=self._drain_stderr,
            args=(self._process.stderr,),
            daemon=True,
            name="anomizer-ffmpeg-stderr",
        )
        self._stderr_thread.start()

    def _drain_stderr(self, stream) -> None:
        """Drain FFmpeg diagnostics so the process never blocks on a full pipe."""
        try:
            for raw_line in stream:
                line = raw_line.decode("utf-8", errors="replace").strip()
                if line:
                    self._stderr_lines.append(line)
        except (OSError, ValueError):
            pass
        finally:
            try:
                stream.close()
            except (OSError, ValueError):
                pass

    def _stop_process(self) -> None:
        process = self._process
        self._process = None
        self._stdout = None

        if process is not None:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=1.0)
                except subprocess.TimeoutExpired:
                    process.kill()
                    try:
                        process.wait(timeout=1.0)
                    except subprocess.TimeoutExpired:
                        pass
            if process.stdout is not None:
                try:
                    process.stdout.close()
                except (OSError, ValueError):
                    pass
            if process.stderr is not None:
                try:
                    process.stderr.close()
                except (OSError, ValueError):
                    pass

        thread = self._stderr_thread
        self._stderr_thread = None
        if thread is not None and thread.is_alive():
            thread.join(timeout=1.0)

    def release(self) -> None:
        self._closed = True
        self._stop_process()
        self._last_frame = None

    def isOpened(self) -> bool:
        return self._process is not None and not self._closed

    # ------------------------------------------------------------ frame read
    def _read_exact(self, size: int) -> bytes | None:
        stream = self._stdout
        if stream is None:
            return None
        chunks: list[bytes] = []
        remaining = size
        while remaining > 0:
            chunk = stream.read(remaining)
            if not chunk:
                if remaining != size:
                    self._stderr_lines.append(
                        "Поток FFmpeg оборвался на неполном кадре: "
                        f"получено {size - remaining} из {size} байт."
                    )
                return None
            chunks.append(chunk)
            remaining -= len(chunk)
        return chunks[0] if len(chunks) == 1 else b"".join(chunks)

    def _wait_for_stream_end(self) -> None:
        """Reap FFmpeg after EOF so diagnostics and exit codes are available."""
        process = self._process
        if process is not None and process.poll() is None:
            try:
                process.wait(timeout=1.0)
            except subprocess.TimeoutExpired:
                return
        thread = self._stderr_thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=1.0)

    def grab(self) -> bool:
        stream = self._stdout
        if stream is None or self._closed:
            return False
        try:
            while self._skip_frames > 0:
                if self._read_exact(self._frame_size) is None:
                    self._last_frame = None
                    return False
                self._skip_frames -= 1
            data = self._read_exact(self._frame_size)
        except (OSError, ValueError):
            self._last_frame = None
            return False
        if data is None:
            self._last_frame = None
            self._wait_for_stream_end()
            return False

        frame = np.frombuffer(data, dtype=np.uint8).reshape(
            (self.height, self.width, 3)
        )
        self._last_frame = frame.copy()
        self._next_time = self._start + (self._frame_index + 1) / self.fps
        self._frame_index += 1
        self._next_index += 1
        return True

    def retrieve(self) -> tuple[bool, np.ndarray | None]:
        if self._last_frame is None:
            return False, None
        return True, self._last_frame

    def read(self) -> tuple[bool, np.ndarray | None]:
        if not self.grab():
            return False, None
        return self.retrieve()

    # ---------------------------------------------------------------- seeking
    def get(self, prop: int) -> float:
        if prop == cv2.CAP_PROP_POS_MSEC:
            return self._next_time * 1000.0
        if prop == cv2.CAP_PROP_POS_FRAMES:
            return float(self._next_index)
        if prop == cv2.CAP_PROP_FPS:
            return self.fps
        if prop == cv2.CAP_PROP_FRAME_WIDTH:
            return float(self.width)
        if prop == cv2.CAP_PROP_FRAME_HEIGHT:
            return float(self.height)
        if prop == cv2.CAP_PROP_FRAME_COUNT:
            return round(self.duration * self.fps) if self.duration > 0 else 0.0
        return 0.0

    def set(self, prop: int, value: float) -> bool:
        if self._closed:
            return False
        if prop == cv2.CAP_PROP_POS_MSEC:
            seconds = float(value) / 1000.0
        elif prop == cv2.CAP_PROP_POS_FRAMES:
            seconds = float(value) / self.fps
        else:
            return False
        if not math.isfinite(seconds):
            return False

        seconds = max(0.0, seconds)
        if self.duration > 0:
            seconds = min(seconds, self.duration)
        self._start = seconds
        self._spawn()
        return True

    # -------------------------------------------------------------- diagnostics
    def error_text(self, limit: int = 8) -> str:
        """Return the tail of FFmpeg's diagnostics for error messages."""
        lines = list(self._stderr_lines)[-max(1, int(limit)) :]
        return "\n".join(lines)


VideoSource = cv2.VideoCapture | FFmpegFrameReader


def open_video_source(
    path: str | Path,
    *,
    width: int,
    height: int,
    fps: float,
    duration: float = 0.0,
    decode_with_ffmpeg: bool = False,
    start: float = 0.0,
) -> VideoSource:
    """Open a frame source for preview or export.

    Progressive files keep using OpenCV.  Interlaced files (or files whose
    OpenCV decode is visibly broken) use the external FFmpeg reader.
    """
    if decode_with_ffmpeg:
        return FFmpegFrameReader(
            path,
            width=width,
            height=height,
            fps=fps,
            duration=duration,
            start=start,
        )

    cap = cv2.VideoCapture(str(path))
    if not cap.isOpened():
        cap.release()
        raise RuntimeError("OpenCV не смог открыть видео.")
    if start > 0:
        cap.set(cv2.CAP_PROP_POS_MSEC, max(0.0, float(start)) * 1000.0)
    return cap
