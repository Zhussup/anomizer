"""Playback of the processed soundtrack through a small system audio player."""

from __future__ import annotations

import os
import shlex
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

from . import resources
from .ffmpeg_utils import (
    build_preview_audio_command,
    build_preview_pcm_command,
)


def _pipewire_socket_available() -> bool:
    runtime_dir = (
        os.environ.get("PIPEWIRE_RUNTIME_DIR")
        or os.environ.get("XDG_RUNTIME_DIR")
        or f"/run/user/{os.getuid()}"
    )
    return (Path(runtime_dir) / "pipewire-0").exists()


def _ffplay_command() -> list[str] | None:
    """PCM player fallback for systems that have FFmpeg's ``ffplay``.

    ``-nodisp`` is important: preview sound must never create a video window.
    """
    if not shutil.which("ffplay"):
        return None
    return [
        "ffplay",
        "-hide_banner",
        "-loglevel",
        "error",
        "-nodisp",
        "-autoexit",
        "-f",
        "s16le",
        "-ar",
        "48000",
        "-ac",
        "2",
        "-i",
        "pipe:0",
    ]


def preview_player_command() -> list[str] | None:
    """Return a command that accepts raw s16le/48 kHz/stereo PCM on stdin.

    ``ANOMIZER_AUDIO_PLAYER`` may contain a complete custom command. Otherwise
    the app prefers PipeWire's ``pw-play`` and falls back to ALSA's ``aplay``.
    ``ffplay`` is the cross-platform fallback (for example on Windows, where
    the bundled FFmpeg has no waveOut/Pulse output device).
    """
    override = os.environ.get("ANOMIZER_AUDIO_PLAYER")
    if override:
        command = shlex.split(override)
        if command:
            return command

    if not sys.platform.startswith("linux"):
        return _ffplay_command()

    if shutil.which("pw-play") and _pipewire_socket_available():
        return [
            "pw-play",
            "-a",
            "--rate",
            "48000",
            "--channels",
            "2",
            "--format",
            "s16",
            "--latency",
            "60ms",
            "-",
        ]
    if shutil.which("aplay"):
        return [
            "aplay",
            "-q",
            "--buffer-time=120000",
            "--period-time=30000",
            "-f",
            "S16_LE",
            "-c",
            "2",
            "-r",
            "48000",
            "-",
        ]
    return _ffplay_command()


class PreviewAudio:
    """A short-lived FFmpeg/player pipeline that plays one preview segment.

    The GUI owns the video clock; this class only starts/stops the audio
    pipeline and reports failures. Minimal environments may have no usable
    output at all, so every error is returned to the caller instead of being
    raised.
    """

    def __init__(self) -> None:
        self._producer: subprocess.Popen[bytes] | None = None
        self._player: subprocess.Popen[bytes] | None = None
        self._reader_threads: list[threading.Thread] = []
        self._errors: list[str] = []
        self._lock = threading.Lock()

    @property
    def running(self) -> bool:
        with self._lock:
            processes = [
                proc
                for proc in (self._producer, self._player)
                if proc is not None
            ]
        return any(proc.poll() is None for proc in processes)

    def start(
        self,
        *,
        source: Path,
        start: float,
        duration: float,
        voice_intervals: list[tuple[float, float]] | None,
        semitones: float,
        preserve_formants: bool,
        volume: float,
        sample_rate: int,
    ) -> str | None:
        """Start audio; return an error message when sound cannot be played."""
        self.stop()
        with self._lock:
            self._errors.clear()

        player = preview_player_command()
        try:
            if player is not None:
                command = build_preview_pcm_command(
                    source=source,
                    start=start,
                    duration=duration,
                    voice_intervals=voice_intervals,
                    semitones=semitones,
                    preserve_formants=preserve_formants,
                    volume=volume,
                    sample_rate=sample_rate,
                )
                self._start_pipe(command, player)
                return None

            command = build_preview_audio_command(
                source=source,
                start=start,
                duration=duration,
                voice_intervals=voice_intervals,
                semitones=semitones,
                preserve_formants=preserve_formants,
                volume=volume,
                sample_rate=sample_rate,
            )
        except (OSError, ValueError, subprocess.SubprocessError) as exc:
            self.stop()
            return str(exc)

        if command is None:
            if sys.platform == "win32":
                return (
                    "не найден аудиовыход для предпросмотра: установите ffplay "
                    "(входит в полную сборку FFmpeg) или используйте "
                    "веб-интерфейс, где звук проигрывает браузер"
                )
            return (
                "не найден аудиовыход: установите alsa-utils (aplay) "
                "или PipeWire (pw-play), либо системный FFmpeg с ALSA/Pulse"
            )

        try:
            proc = subprocess.Popen(
                command,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                **resources.detached_process_kwargs(),
            )
        except (OSError, subprocess.SubprocessError) as exc:
            self.stop()
            return str(exc)

        with self._lock:
            self._player = proc
        self._start_reader(proc, "Аудиовыход")
        return None

    def _start_pipe(
        self, producer_command: list[str], player_command: list[str]
    ) -> None:
        producer = subprocess.Popen(
            producer_command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            **resources.detached_process_kwargs(),
        )
        assert producer.stdout is not None
        try:
            player = subprocess.Popen(
                player_command,
                stdin=producer.stdout,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                **resources.detached_process_kwargs(),
            )
        except (OSError, subprocess.SubprocessError):
            self._terminate(producer)
            raise

        # The player owns the read end of the pipe now. Keeping a second copy
        # in the GUI process would prevent the player from seeing EOF.
        producer.stdout.close()
        with self._lock:
            self._producer = producer
            self._player = player
        self._start_reader(producer, "FFmpeg")
        self._start_reader(player, "Аудиовыход")

    def _start_reader(
        self, proc: subprocess.Popen[bytes], label: str
    ) -> None:
        reader = threading.Thread(
            target=self._read_stderr,
            args=(proc, label),
            name="anomizer-preview-audio",
            daemon=True,
        )
        self._reader_threads.append(reader)
        reader.start()

    def poll_error(self) -> str | None:
        """Return an error once one of the audio processes exited badly."""
        with self._lock:
            processes = [
                (label, proc)
                for label, proc in (
                    ("FFmpeg", self._producer),
                    ("Аудиовыход", self._player),
                )
                if proc is not None
            ]

        for label, proc in processes:
            return_code = proc.poll()
            if return_code is None or return_code == 0:
                continue
            try:
                proc.wait(timeout=0.2)
            except subprocess.TimeoutExpired:
                pass
            with self._lock:
                lines = list(self._errors[-6:])
            details = "\n".join(lines).strip()
            message = f"{label} завершил вывод звука с кодом {return_code}."
            return message + (f"\n{details}" if details else "")
        return None

    def stop(self) -> None:
        with self._lock:
            processes = [
                proc
                for proc in (self._producer, self._player)
                if proc is not None
            ]
            self._producer = None
            self._player = None
        if not processes:
            self._reader_threads.clear()
            return

        for proc in processes:
            if proc.poll() is None:
                try:
                    proc.terminate()
                except OSError:
                    pass
        deadline = time.monotonic() + 0.6
        for proc in processes:
            if proc.poll() is not None:
                continue
            remaining = max(0.05, deadline - time.monotonic())
            try:
                proc.wait(timeout=remaining)
            except subprocess.TimeoutExpired:
                pass
        for proc in processes:
            if proc.poll() is None:
                try:
                    proc.kill()
                except OSError:
                    pass
        for proc in processes:
            if proc.poll() is not None:
                continue
            try:
                proc.wait(timeout=0.3)
            except subprocess.TimeoutExpired:
                pass
        for proc in processes:
            for stream in (proc.stdout, proc.stderr):
                if stream is None:
                    continue
                try:
                    stream.close()
                except OSError:
                    pass
        self._reader_threads.clear()

    @staticmethod
    def _terminate(proc: subprocess.Popen[bytes]) -> None:
        if proc.poll() is None:
            try:
                proc.terminate()
            except OSError:
                pass
        try:
            proc.wait(timeout=1.0)
        except subprocess.TimeoutExpired:
            try:
                proc.kill()
            except OSError:
                pass
            try:
                proc.wait(timeout=1.0)
            except subprocess.TimeoutExpired:
                pass
        for stream in (proc.stdout, proc.stderr):
            if stream is None:
                continue
            try:
                stream.close()
            except OSError:
                pass

    def _read_stderr(
        self, proc: subprocess.Popen[bytes], label: str
    ) -> None:
        if proc.stderr is None:
            return
        try:
            for raw_line in proc.stderr:
                line = raw_line.decode("utf-8", errors="replace").strip()
                if not line:
                    continue
                with self._lock:
                    self._errors.append(f"{label}: {line}")
                    if len(self._errors) > 20:
                        del self._errors[0]
        except (OSError, ValueError):
            return
