"""Tkinter desktop interface for the local video anonymizer."""

from __future__ import annotations

import queue
import sys
import threading
import time
import tkinter as tk
import traceback
from dataclasses import dataclass
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

import cv2
from PIL import Image, ImageTk

from .faces import FaceAnonymizer, default_model_path
from .preview_audio import PreviewAudio
from .processor import (
    ProcessConfig,
    ProcessingCancelled,
    VideoInfo,
    probe_video,
    process_video,
)
from .timing import (
    EffectRegion,
    format_timecode,
    is_active,
    parse_timecode,
    regions_to_intervals,
)
from .video_reader import FFmpegFrameReader, VideoSource, open_video_source

format_time = format_timecode

APP_BG = "#f3f5f7"
PANEL_BG = "#ffffff"
PREVIEW_BG = "#0b0d10"
ACCENT = "#2563eb"
MUTED = "#667085"
BLUR_BAND = "#3b82f6"
VOICE_BAND = "#f59e0b"
TRACK_LEFT = 30.0
TRACK_RIGHT_PAD = 12.0
PREVIEW_DETECT_WIDTH = 640


class Timeline(tk.Canvas):
    """Trim range, playhead and bands showing where effects are active."""

    def __init__(self, master: tk.Misc, on_seek) -> None:
        super().__init__(
            master,
            height=72,
            bg=PANEL_BG,
            highlightthickness=0,
            cursor="hand2",
        )
        self.on_seek = on_seek
        self.duration = 0.0
        self.trim_start = 0.0
        self.trim_end = 0.0
        self.current = 0.0
        self.blur_intervals: list[tuple[float, float]] = []
        self.voice_intervals: list[tuple[float, float]] = []
        self._playhead_ids: tuple[int, int] | None = None
        self.bind("<Button-1>", self._seek)
        self.bind("<B1-Motion>", self._seek)
        self.bind("<Configure>", lambda _event: self.redraw())

    def set_range(self, duration: float, trim_start: float, trim_end: float) -> None:
        self.duration = max(0.0, float(duration))
        self.trim_start = max(0.0, float(trim_start))
        self.trim_end = max(self.trim_start, float(trim_end))
        self.redraw()

    def set_current(self, current: float) -> None:
        self.current = max(0.0, float(current))
        if self._playhead_ids is None:
            return
        x = self._x_for(self.current)
        for item in self._playhead_ids:
            self.coords(item, x, 2, x, 62)

    def set_effects(
        self,
        blur_intervals: list[tuple[float, float]],
        voice_intervals: list[tuple[float, float]],
    ) -> None:
        self.blur_intervals = list(blur_intervals)
        self.voice_intervals = list(voice_intervals)
        self.redraw()

    def _x_for(self, value: float) -> float:
        width = max(1.0, self.winfo_width() - TRACK_LEFT - TRACK_RIGHT_PAD)
        if self.duration <= 0:
            return TRACK_LEFT
        ratio = min(1.0, max(0.0, value / self.duration))
        return TRACK_LEFT + ratio * width

    def _time_for(self, x: float) -> float:
        width = max(1.0, self.winfo_width() - TRACK_LEFT - TRACK_RIGHT_PAD)
        ratio = min(1.0, max(0.0, (x - TRACK_LEFT) / width))
        return ratio * self.duration

    def _seek(self, event: tk.Event) -> None:
        if self.duration <= 0:
            return
        self.on_seek(self._time_for(float(event.x)))

    def _draw_band(
        self,
        start: float,
        end: float,
        y: float,
        height: float,
        color: str,
    ) -> None:
        if self.duration <= 0 or end <= start:
            return
        x1 = self._x_for(start)
        x2 = self._x_for(end)
        if x2 - x1 < 1.0:
            x2 = x1 + 1.0
        self.create_rectangle(x1, y, x2, y + height, fill=color, outline="")

    def redraw(self) -> None:
        self.delete("all")
        self._playhead_ids = None
        width = self.winfo_width()
        if width < 30:
            return

        left, right = TRACK_LEFT, width - TRACK_RIGHT_PAD
        blur_y, voice_y, band_h = 5.0, 15.0, 5.0
        track_y, track_h = 35.0, 7.0

        self.create_rectangle(
            left, blur_y, right, blur_y + band_h, fill="#edf1f6", outline=""
        )
        self.create_rectangle(
            left, voice_y, right, voice_y + band_h, fill="#edf1f6", outline=""
        )
        self.create_text(
            5,
            blur_y + band_h / 2,
            text="Б",
            anchor="w",
            fill=BLUR_BAND,
            font=("TkDefaultFont", 7, "bold"),
        )
        self.create_text(
            5,
            voice_y + band_h / 2,
            text="Г",
            anchor="w",
            fill=VOICE_BAND,
            font=("TkDefaultFont", 7, "bold"),
        )

        for start, end in self.blur_intervals:
            self._draw_band(start, end, blur_y, band_h, BLUR_BAND)
        for start, end in self.voice_intervals:
            self._draw_band(start, end, voice_y, band_h, VOICE_BAND)

        self.create_rectangle(
            left, track_y, right, track_y + track_h, fill="#e3e7ec", outline=""
        )

        if self.duration > 0:
            start_x = self._x_for(self.trim_start)
            end_x = self._x_for(self.trim_end)
            self.create_rectangle(
                start_x,
                track_y,
                max(start_x + 1.0, end_x),
                track_y + track_h,
                fill=ACCENT,
                outline="",
            )
            for x in (start_x, end_x):
                self.create_line(
                    x, track_y - 7, x, track_y + track_h + 5, fill="#111827", width=2
                )
                self.create_polygon(
                    x - 4,
                    track_y - 8,
                    x + 4,
                    track_y - 8,
                    x,
                    track_y - 1,
                    fill="#111827",
                    outline="",
                )

            play_x = self._x_for(self.current)
            white_line = self.create_line(
                play_x, 2, play_x, 62, fill="#ffffff", width=2
            )
            dark_line = self.create_line(
                play_x, 2, play_x, 62, fill="#111827", width=1
            )
            self._playhead_ids = (white_line, dark_line)

        self.create_text(
            left,
            51,
            text=format_time(self.trim_start),
            anchor="nw",
            fill=MUTED,
            font=("TkDefaultFont", 8),
        )
        self.create_text(
            right,
            51,
            text=format_time(self.trim_end),
            anchor="ne",
            fill=MUTED,
            font=("TkDefaultFont", 8),
        )


@dataclass
class _RangeRow:
    frame: ttk.Frame
    start_var: tk.StringVar
    end_var: tk.StringVar
    blur_var: tk.BooleanVar
    voice_var: tk.BooleanVar
    start: float
    end: float


class EffectRangesDialog(tk.Toplevel):
    """Editable list of source-time ranges for blur and voice effects."""

    def __init__(self, app: tk.Tk) -> None:
        super().__init__(app)
        self.app = app
        self.title("Диапазоны эффектов")
        self.geometry("700x390")
        self.minsize(640, 300)
        self.configure(bg=APP_BG)
        self.transient(app)
        self.rows: list[_RangeRow] = []

        body = ttk.Frame(self, padding=12)
        body.pack(fill="both", expand=True)
        body.grid_columnconfigure(0, weight=1)
        body.grid_rowconfigure(3, weight=1)

        ttk.Label(
            body,
            text=(
                "Эффекты работают только внутри диапазонов. В промежутках "
                "остаётся оригинал. Время: секунды, MM:SS или HH:MM:SS."
            ),
            style="Hint.TLabel",
            wraplength=650,
            justify="left",
        ).grid(row=0, column=0, sticky="w")

        legend = ttk.Frame(body)
        legend.grid(row=1, column=0, sticky="w", pady=(6, 8))
        tk.Label(
            legend, text="  ", bg=BLUR_BAND, bd=0, highlightthickness=0
        ).pack(side="left")
        ttk.Label(legend, text=" блюр   ", style="Hint.TLabel").pack(side="left")
        tk.Label(
            legend, text="  ", bg=VOICE_BAND, bd=0, highlightthickness=0
        ).pack(side="left")
        ttk.Label(legend, text=" голос", style="Hint.TLabel").pack(side="left")

        toolbar = ttk.Frame(body)
        toolbar.grid(row=2, column=0, sticky="ew", pady=(0, 8))
        ttk.Button(toolbar, text="+ Добавить", command=self._add).pack(side="left")
        ttk.Button(toolbar, text="Сбросить", command=self._reset).pack(
            side="left", padx=(6, 0)
        )
        ttk.Button(toolbar, text="Готово", command=self._close).pack(side="right")
        ttk.Button(toolbar, text="Отмена", command=self._cancel).pack(
            side="right", padx=(0, 6)
        )

        table = ttk.Frame(body)
        table.grid(row=3, column=0, sticky="nsew")
        table.grid_columnconfigure(0, weight=1)
        table.grid_rowconfigure(1, weight=1)

        header = ttk.Frame(table)
        header.grid(row=0, column=0, columnspan=2, sticky="ew", padx=(0, 14))
        header.grid_columnconfigure(6, weight=1)
        for column, minsize in ((0, 105), (1, 105), (2, 50), (3, 50), (4, 86), (5, 76)):
            header.grid_columnconfigure(column, minsize=minsize)
        for column, text, width in (
            (0, "Начало", 10),
            (1, "Конец", 10),
            (2, "Блюр", 5),
            (3, "Голос", 5),
            (4, "", 2),
            (5, "", 2),
        ):
            padx = 16 if column in (2, 3) else 2
            ttk.Label(header, text=text, width=width).grid(
                row=0, column=column, sticky="w", padx=padx
            )

        self.rows_canvas = tk.Canvas(
            table, bg=APP_BG, highlightthickness=0, bd=0
        )
        self.rows_canvas.grid(row=1, column=0, sticky="nsew")
        scrollbar = ttk.Scrollbar(
            table, orient="vertical", command=self.rows_canvas.yview
        )
        scrollbar.grid(row=1, column=1, sticky="ns")
        self.rows_canvas.configure(yscrollcommand=scrollbar.set)

        self.rows_frame = ttk.Frame(self.rows_canvas)
        self.rows_frame.grid_columnconfigure(0, weight=1)
        self.rows_window = self.rows_canvas.create_window(
            (0, 0), window=self.rows_frame, anchor="nw"
        )
        self.rows_frame.bind(
            "<Configure>",
            lambda _event: self.rows_canvas.configure(
                scrollregion=self.rows_canvas.bbox("all")
            ),
        )
        self.rows_canvas.bind(
            "<Configure>",
            lambda event: self.rows_canvas.itemconfigure(
                self.rows_window, width=event.width
            ),
        )
        self.rows_canvas.bind("<MouseWheel>", self._on_mousewheel)
        self.rows_canvas.bind("<Button-4>", self._on_mousewheel)
        self.rows_canvas.bind("<Button-5>", self._on_mousewheel)

        self.hint = ttk.Label(
            body,
            text=(
                "Вне диапазонов эффект не применяется. «Разделить» режет "
                "строку по плейхеду; изменения видны сразу."
            ),
            style="Hint.TLabel",
            wraplength=650,
            justify="left",
        )
        self.hint.grid(row=4, column=0, sticky="w", pady=(8, 0))

        self._reload_rows()
        self.update_idletasks()
        width, height = 700, 390
        x = self.app.winfo_rootx() + max(0, (self.app.winfo_width() - width) // 2)
        y = self.app.winfo_rooty() + 80
        self.geometry(f"{width}x{height}+{x}+{y}")
        self.protocol("WM_DELETE_WINDOW", self._close)
        self.bind("<Escape>", lambda _event: self._cancel())
        self.lift()

    def _create_row(self, region: EffectRegion) -> None:
        frame = ttk.Frame(self.rows_frame)
        frame.grid(row=len(self.rows), column=0, sticky="ew", pady=2)
        frame.grid_columnconfigure(6, weight=1)
        for column, minsize in ((0, 105), (1, 105), (2, 50), (3, 50), (4, 86), (5, 76)):
            frame.grid_columnconfigure(column, minsize=minsize)

        start_var = tk.StringVar(value=format_time(region.start))
        end_var = tk.StringVar(value=format_time(region.end))
        blur_var = tk.BooleanVar(value=region.blur)
        voice_var = tk.BooleanVar(value=region.voice)
        row = _RangeRow(
            frame=frame,
            start_var=start_var,
            end_var=end_var,
            blur_var=blur_var,
            voice_var=voice_var,
            start=float(region.start),
            end=float(region.end),
        )

        start_entry = ttk.Entry(
            frame, textvariable=start_var, width=10, justify="center"
        )
        start_entry.grid(row=0, column=0, sticky="ew", padx=(2, 4))
        end_entry = ttk.Entry(
            frame, textvariable=end_var, width=10, justify="center"
        )
        end_entry.grid(row=0, column=1, sticky="ew", padx=(0, 8))
        for entry in (start_entry, end_entry):
            entry.bind(
                "<FocusOut>", lambda _event, current=row: self._commit_entry(current)
            )
            entry.bind(
                "<Return>", lambda _event, current=row: self._commit_entry(current)
            )

        ttk.Checkbutton(
            frame,
            variable=blur_var,
            command=self._publish,
            style="App.TCheckbutton",
        ).grid(row=0, column=2, sticky="w", padx=6)
        ttk.Checkbutton(
            frame,
            variable=voice_var,
            command=self._publish,
            style="App.TCheckbutton",
        ).grid(row=0, column=3, sticky="w", padx=6)
        ttk.Button(
            frame,
            text="Разделить",
            width=9,
            command=lambda current=row: self._split(current),
        ).grid(row=0, column=4, sticky="w", padx=(8, 2))
        ttk.Button(
            frame,
            text="Удалить",
            width=7,
            command=lambda current=row: self._delete(current),
        ).grid(row=0, column=5, sticky="w", padx=(2, 2))

        self.rows.append(row)

    def _reload_rows(self) -> None:
        for child in self.rows_frame.winfo_children():
            child.destroy()
        self.rows.clear()
        for region in self.app.effect_regions:
            self._create_row(region)
        if not self.rows:
            ttk.Label(
                self.rows_frame,
                text="Диапазонов нет — блюр и смена голоса выключены.",
                style="Hint.TLabel",
            ).grid(row=0, column=0, sticky="w", pady=8, padx=4)
        self.rows_frame.update_idletasks()
        self.rows_canvas.configure(scrollregion=self.rows_canvas.bbox("all"))

    def _commit_row(self, row: _RangeRow, show_hint: bool = True) -> bool:
        duration = self.app.info.duration if self.app.info else 0.0
        try:
            start = parse_timecode(row.start_var.get())
            end = parse_timecode(row.end_var.get())
        except ValueError as exc:
            if show_hint:
                self._set_hint(str(exc))
            return False

        start = min(max(0.0, start), duration)
        end = min(max(0.0, end), duration)
        if end - start < 0.01:
            if show_hint:
                self._set_hint("Конец диапазона должен быть больше начала.")
            return False

        row.start = start
        row.end = end
        row.start_var.set(format_time(start))
        row.end_var.set(format_time(end))
        return True

    def _commit_entry(self, _row: _RangeRow) -> None:
        """Apply an edited timecode as soon as the entry loses focus."""
        self._publish()

    def _commit_rows(self, show_hint: bool) -> bool:
        if show_hint:
            self._set_hint("")
        valid = True
        for row in list(self.rows):
            if not self._commit_row(row, show_hint=show_hint):
                valid = False
        return valid

    def _regions_from_rows(self) -> list[EffectRegion]:
        return [
            EffectRegion(
                row.start,
                row.end,
                row.blur_var.get(),
                row.voice_var.get(),
            )
            for row in self.rows
            if row.end - row.start >= 0.01
        ]

    def _publish(self) -> bool:
        """Commit all rows and publish the valid ranges to the preview."""
        valid = self._commit_rows(show_hint=True)
        self.app.effect_regions = self._regions_from_rows()
        self.app._on_effects_changed()
        return valid

    def _set_hint(self, text: str) -> None:
        if text:
            self.hint.configure(text=text)
        else:
            self.hint.configure(
                text=(
                    "Вне диапазонов эффект не применяется. «Разделить» режет "
                    "строку по плейхеду; изменения видны сразу."
                )
            )

    def _add(self) -> None:
        if not self._commit_rows(show_hint=True) or self.app.info is None:
            return
        duration = self.app.info.duration
        start = min(max(0.0, self.app.current_time), duration)
        if start >= duration - 0.01:
            start = max(0.0, duration - 5.0)
        end = min(duration, start + 5.0)
        if end - start < 0.02:
            start = max(0.0, end - 0.02)

        self.app.effect_regions = self._regions_from_rows()
        self.app.effect_regions.append(EffectRegion(start, end, True, True))
        self._reload_rows()
        self._publish()
        self.rows_frame.update_idletasks()
        self.rows_canvas.yview_moveto(1.0)

    def _row_index(self, row: _RangeRow) -> int:
        return next(index for index, current in enumerate(self.rows) if current is row)

    def _split(self, row: _RangeRow) -> None:
        if not self._commit_rows(show_hint=True):
            return
        index = self._row_index(row)
        now = self.app.current_time
        if not (row.start + 0.01 < now < row.end - 0.01):
            self._set_hint(
                "Поставьте плейхед внутрь диапазона, чтобы разделить его."
            )
            return

        regions = self._regions_from_rows()
        regions[index : index + 1] = [
            EffectRegion(
                row.start, now, row.blur_var.get(), row.voice_var.get()
            ),
            EffectRegion(
                now, row.end, row.blur_var.get(), row.voice_var.get()
            ),
        ]
        self.app.effect_regions = regions
        self._reload_rows()
        self._publish()
        self._set_hint("Диапазон разделён. Галочки можно менять независимо.")

    def _delete(self, row: _RangeRow) -> None:
        self._commit_rows(show_hint=False)
        index = self._row_index(row)
        regions = self._regions_from_rows()
        del regions[index]
        self.app.effect_regions = regions
        self._reload_rows()
        self._publish()

    def _reset(self) -> None:
        if self.app.info is None:
            return
        self.app.effect_regions = [
            EffectRegion(0.0, self.app.info.duration, True, True)
        ]
        self._reload_rows()
        self._publish()
        self._set_hint("Оба эффекта включены на всём видео.")

    def _on_mousewheel(self, event: tk.Event) -> None:
        if getattr(event, "num", None) == 4:
            step = -1
        elif getattr(event, "num", None) == 5:
            step = 1
        else:
            step = -1 if event.delta > 0 else 1
        self.rows_canvas.yview_scroll(step, "units")

    def _close(self) -> None:
        if not self.winfo_exists():
            return
        # Keep the dialog open when an edited timecode is invalid, so the user
        # sees the hint instead of losing the change silently.
        if not self._publish():
            return
        self.app._ranges_dialog = None
        self.destroy()

    def _cancel(self) -> None:
        """Close without applying the current (possibly invalid) entry text."""
        if not self.winfo_exists():
            return
        self.app._ranges_dialog = None
        self.destroy()


class AnomizerApp(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title("Anomizer — локальная обработка видео")
        self.geometry("1180x730")
        self.minsize(940, 620)
        self.configure(bg=APP_BG)

        self.info: VideoInfo | None = None
        self.cap: VideoSource | None = None
        self.anonymizer: FaceAnonymizer | None = None
        self._photo: ImageTk.PhotoImage | None = None
        self._preview_after: str | None = None
        self._play_after: str | None = None
        self._playing = False
        self.current_time = 0.0
        self._busy = False
        self._closing = False
        self._cancel_event = threading.Event()
        self._queue: queue.Queue[tuple] = queue.Queue()
        self._export_thread: threading.Thread | None = None

        self.effect_regions: list[EffectRegion] = []
        self._ranges_dialog: EffectRangesDialog | None = None
        self._preview_audio = PreviewAudio()
        self._audio_check_after: str | None = None
        self._play_start_wall = 0.0
        self._play_start_time = 0.0
        self._play_end = 0.0
        self._last_preview_blur_active = False
        self._preview_sound = tk.BooleanVar(value=True)

        self._build_styles()
        self._build_ui()
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self.after(100, self._drain_queue)
        self._show_placeholder()

    # ------------------------------------------------------------------ UI
    def _build_styles(self) -> None:
        style = ttk.Style(self)
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass
        style.configure("TFrame", background=APP_BG)
        style.configure("Panel.TFrame", background=PANEL_BG)
        style.configure("TLabel", background=APP_BG)
        style.configure("Panel.TLabel", background=PANEL_BG)
        style.configure("Hint.TLabel", background=APP_BG, foreground=MUTED)
        style.configure("PanelHint.TLabel", background=PANEL_BG, foreground=MUTED)
        style.configure("Title.TLabel", background=APP_BG, font=("TkDefaultFont", 12, "bold"))
        style.configure(
            "TLabelframe", background=PANEL_BG, bordercolor="#d9dee5", relief="solid"
        )
        style.configure(
            "TLabelframe.Label", background=PANEL_BG, foreground="#111827",
            font=("TkDefaultFont", 10, "bold")
        )
        style.configure("TButton", padding=(10, 5))
        style.configure(
            "Accent.TButton",
            background=ACCENT,
            foreground="#ffffff",
            padding=(12, 7),
        )
        style.map(
            "Accent.TButton",
            background=[("active", "#1d4ed8"), ("disabled", "#aab7cc")],
            foreground=[("disabled", "#eef2f7")],
        )
        style.configure("TCheckbutton", background=PANEL_BG)
        style.configure("App.TCheckbutton", background=APP_BG)
        style.configure("TRadiobutton", background=PANEL_BG)
        style.configure("TSpinbox", padding=3)
        style.configure("TCombobox", padding=3)

    def _build_ui(self) -> None:
        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(1, weight=1)

        header = ttk.Frame(self, padding=(14, 12, 14, 8))
        header.grid(row=0, column=0, sticky="ew")
        header.grid_columnconfigure(1, weight=1)

        self.open_btn = ttk.Button(header, text="Открыть видео…", command=self._open_video)
        self.open_btn.grid(row=0, column=0, sticky="w")
        self.file_label = ttk.Label(header, text="Файл не выбран", style="Title.TLabel")
        self.file_label.grid(row=0, column=1, sticky="w", padx=(12, 12))
        self.video_info_label = ttk.Label(header, text="", style="Hint.TLabel")
        self.video_info_label.grid(row=0, column=2, sticky="e")

        main = ttk.Frame(self, padding=(14, 4, 14, 12))
        main.grid(row=1, column=0, sticky="nsew")
        main.grid_columnconfigure(0, weight=1)
        main.grid_rowconfigure(0, weight=1)

        left = ttk.Frame(main)
        left.grid(row=0, column=0, sticky="nsew")
        left.grid_columnconfigure(0, weight=1)
        left.grid_rowconfigure(0, weight=1)

        self.preview = tk.Canvas(
            left,
            bg=PREVIEW_BG,
            highlightthickness=0,
            bd=0,
        )
        self.preview.grid(row=0, column=0, sticky="nsew")
        self.preview.bind("<Configure>", self._on_canvas_configure)

        self.timeline = Timeline(left, self._on_timeline_seek)
        self.timeline.grid(row=1, column=0, sticky="ew", pady=(8, 2))

        trim = ttk.Frame(left)
        trim.grid(row=2, column=0, sticky="ew", pady=(2, 0))
        ttk.Label(trim, text="Фрагмент:").pack(side="left")
        ttk.Label(trim, text="с").pack(side="left", padx=(8, 4))
        self.in_var = tk.StringVar(value="0.00")
        self.in_entry = ttk.Spinbox(
            trim, from_=0, to=0, increment=0.1, width=8, textvariable=self.in_var
        )
        self.in_entry.pack(side="left")
        ttk.Button(
            trim, text="= текущий", command=self._set_in_from_current
        ).pack(side="left", padx=(4, 10))
        ttk.Label(trim, text="по").pack(side="left")
        self.out_var = tk.StringVar(value="0.00")
        self.out_entry = ttk.Spinbox(
            trim, from_=0, to=0, increment=0.1, width=8, textvariable=self.out_var
        )
        self.out_entry.pack(side="left", padx=(4, 0))
        ttk.Button(
            trim, text="= текущий", command=self._set_out_from_current
        ).pack(side="left", padx=(4, 0))
        ttk.Label(trim, text="сек", style="Hint.TLabel").pack(side="left", padx=(6, 0))
        self.in_var.trace_add("write", lambda *_args: self._refresh_timeline_range())
        self.out_var.trace_add("write", lambda *_args: self._refresh_timeline_range())

        transport = ttk.Frame(left)
        transport.grid(row=3, column=0, sticky="ew", pady=(7, 0))
        self.play_btn = ttk.Button(
            transport, text="▶ Смотреть", command=self._toggle_play, state="disabled"
        )
        self.play_btn.pack(side="left")
        self.time_label = ttk.Label(transport, text="00:00.00 / 00:00.00")
        self.time_label.pack(side="left", padx=(12, 0))
        self.ranges_btn = ttk.Button(
            transport,
            text="Диапазоны эффектов…",
            command=self._open_effect_ranges,
            state="disabled",
        )
        self.ranges_btn.pack(side="right")
        self.sound_check = ttk.Checkbutton(
            transport,
            text="Звук",
            variable=self._preview_sound,
            command=self._on_sound_toggle,
            style="App.TCheckbutton",
            state="disabled",
        )
        self.sound_check.pack(side="right", padx=(0, 10))
        self.audio_status_label = ttk.Label(
            transport, text="", style="Hint.TLabel"
        )
        self.audio_status_label.pack(side="right", padx=(0, 10))

        right = ttk.Frame(main, width=348)
        right.grid(row=0, column=1, sticky="ns", padx=(14, 0))
        right.grid_propagate(False)
        right.grid_columnconfigure(0, weight=1)
        right.grid_rowconfigure(0, weight=1)

        settings_canvas = tk.Canvas(
            right, bg=APP_BG, highlightthickness=0, bd=0, width=330
        )
        settings_canvas.grid(row=0, column=0, sticky="nsew")
        settings_scroll = ttk.Scrollbar(
            right, orient="vertical", command=settings_canvas.yview
        )
        settings_scroll.grid(row=0, column=1, sticky="ns")
        settings_canvas.configure(yscrollcommand=settings_scroll.set)

        settings = ttk.Frame(settings_canvas)
        settings.grid_columnconfigure(0, weight=1)
        settings_window = settings_canvas.create_window(
            (0, 0), window=settings, anchor="nw"
        )
        settings.bind(
            "<Configure>",
            lambda _event: settings_canvas.configure(
                scrollregion=settings_canvas.bbox("all")
            ),
        )
        settings_canvas.bind(
            "<Configure>",
            lambda event: settings_canvas.itemconfigure(
                settings_window, width=event.width
            ),
        )

        def scroll_settings(event: tk.Event) -> None:
            if getattr(event, "num", None) == 4:
                step = -1
            elif getattr(event, "num", None) == 5:
                step = 1
            else:
                step = -1 if event.delta > 0 else 1
            settings_canvas.yview_scroll(step, "units")

        self._build_faces_panel(settings).grid(row=0, column=0, sticky="ew")
        self._build_voice_panel(settings).grid(row=1, column=0, sticky="ew", pady=(8, 0))
        self._build_export_panel(settings).grid(
            row=2, column=0, sticky="ew", pady=(8, 0)
        )

        def bind_wheel_tree(widget: tk.Misc) -> None:
            for sequence in ("<MouseWheel>", "<Button-4>", "<Button-5>"):
                widget.bind(sequence, scroll_settings, add="+")
            for child in widget.winfo_children():
                bind_wheel_tree(child)

        bind_wheel_tree(settings)

    def _build_faces_panel(self, parent: ttk.Frame) -> ttk.LabelFrame:
        frame = ttk.LabelFrame(parent, text="Лица", padding=8)
        frame.grid_columnconfigure(1, weight=1)

        self.blur_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(
            frame,
            text="Блюрить лица автоматически",
            variable=self.blur_var,
            command=self._on_preview_settings,
        ).grid(row=0, column=0, columnspan=2, sticky="w")

        ttk.Label(frame, text="Метод", style="Panel.TLabel").grid(
            row=1, column=0, sticky="w", pady=(8, 0)
        )
        self.method_var = tk.StringVar(value="Гаусс")
        method = ttk.Combobox(
            frame,
            textvariable=self.method_var,
            values=("Гаусс", "Пиксели"),
            state="readonly",
            width=12,
        )
        method.grid(row=1, column=1, sticky="e", pady=(8, 0))
        method.bind("<<ComboboxSelected>>", lambda _event: self._on_preview_settings())

        ttk.Label(frame, text="Сила", style="Panel.TLabel").grid(
            row=2, column=0, sticky="w", pady=(6, 0)
        )
        self.strength_label = ttk.Label(frame, text="3.0", style="Panel.TLabel")
        self.strength_label.grid(row=2, column=1, sticky="e", pady=(6, 0))
        self.strength_var = tk.DoubleVar(value=3.0)
        ttk.Scale(
            frame,
            from_=1.0,
            to=5.0,
            variable=self.strength_var,
            command=lambda _value: self._on_preview_settings(),
        ).grid(row=3, column=0, columnspan=2, sticky="ew")

        ttk.Label(frame, text="Запас вокруг лица", style="Panel.TLabel").grid(
            row=4, column=0, sticky="w", pady=(6, 0)
        )
        self.margin_label = ttk.Label(frame, text="22%", style="Panel.TLabel")
        self.margin_label.grid(row=4, column=1, sticky="e", pady=(6, 0))
        self.margin_var = tk.DoubleVar(value=22.0)
        ttk.Scale(
            frame,
            from_=0.0,
            to=45.0,
            variable=self.margin_var,
            command=lambda _value: self._on_preview_settings(),
        ).grid(row=5, column=0, columnspan=2, sticky="ew")

        self.show_boxes_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(
            frame,
            text="Показывать рамки",
            variable=self.show_boxes_var,
            command=self._on_preview_settings,
        ).grid(row=6, column=0, columnspan=2, sticky="w", pady=(7, 0))

        return frame

    def _build_voice_panel(self, parent: ttk.Frame) -> ttk.LabelFrame:
        frame = ttk.LabelFrame(parent, text="Голос", padding=8)
        frame.grid_columnconfigure(1, weight=1)

        ttk.Label(frame, text="Сдвиг тона", style="Panel.TLabel").grid(
            row=0, column=0, sticky="w"
        )
        self.voice_label = ttk.Label(frame, text="−3.0 полутона", style="Panel.TLabel")
        self.voice_label.grid(row=0, column=1, sticky="e")
        self.voice_var = tk.DoubleVar(value=-3.0)
        ttk.Scale(
            frame,
            from_=-12.0,
            to=12.0,
            variable=self.voice_var,
            command=lambda _value: self._on_voice_settings(),
        ).grid(row=1, column=0, columnspan=2, sticky="ew")

        self.formant_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(
            frame,
            text="Сохранять тембр (менее анонимно)",
            variable=self.formant_var,
            command=self._on_voice_settings,
        ).grid(row=2, column=0, columnspan=2, sticky="w", pady=(7, 0))

        ttk.Label(frame, text="Громкость", style="Panel.TLabel").grid(
            row=3, column=0, sticky="w", pady=(7, 0)
        )
        self.volume_label = ttk.Label(frame, text="100%", style="Panel.TLabel")
        self.volume_label.grid(row=3, column=1, sticky="e", pady=(7, 0))
        self.volume_var = tk.DoubleVar(value=100.0)
        ttk.Scale(
            frame,
            from_=0.0,
            to=200.0,
            variable=self.volume_var,
            command=lambda _value: self._on_voice_settings(),
        ).grid(row=4, column=0, columnspan=2, sticky="ew")

        self.mute_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(
            frame,
            text="Убрать звук полностью",
            variable=self.mute_var,
            command=self._on_voice_settings,
        ).grid(row=5, column=0, columnspan=2, sticky="w", pady=(7, 0))

        ttk.Label(
            frame,
            text="На таймлайне: Б — блюр, Г — голос. Вне зон — оригинал.",
            style="PanelHint.TLabel",
            wraplength=280,
            justify="left",
        ).grid(row=6, column=0, columnspan=2, sticky="w", pady=(7, 0))

        return frame

    def _build_export_panel(self, parent: ttk.Frame) -> ttk.LabelFrame:
        frame = ttk.LabelFrame(parent, text="Экспорт", padding=8)
        frame.grid_columnconfigure(0, weight=1)

        self.export_btn = ttk.Button(
            frame,
            text="Экспорт MP4…",
            style="Accent.TButton",
            command=self._export,
            state="disabled",
        )
        self.export_btn.grid(row=0, column=0, sticky="ew")
        self.cancel_btn = ttk.Button(
            frame, text="Отмена", command=self._cancel_export, state="disabled"
        )
        self.cancel_btn.grid(row=1, column=0, sticky="ew", pady=(6, 0))

        self.progress = ttk.Progressbar(frame, maximum=100.0, mode="determinate")
        self.progress.grid(row=2, column=0, sticky="ew", pady=(9, 5))
        self.status_label = ttk.Label(
            frame,
            text="Детекция автоматическая, без разметки.",
            style="PanelHint.TLabel",
            wraplength=285,
            justify="left",
        )
        self.status_label.grid(row=3, column=0, sticky="w")
        return frame

    # -------------------------------------------------------------- video I/O
    def _open_video(self) -> None:
        if self._busy:
            return
        path = filedialog.askopenfilename(
            title="Выберите видео",
            filetypes=[
                ("Видео", "*.mp4 *.mkv *.mov *.avi *.webm *.m4v"),
                ("Все файлы", "*"),
            ],
        )
        if not path:
            return

        self._clear_loaded_video()
        self.status_label.configure(text="Читаю видео…")
        self.update_idletasks()
        try:
            info = probe_video(Path(path))
        except Exception as exc:  # noqa: BLE001 - GUI boundary shows the error
            self.status_label.configure(text="Не удалось открыть видео.")
            messagebox.showerror("Ошибка", str(exc), parent=self)
            return

        try:
            cap = open_video_source(
                Path(path),
                width=info.width,
                height=info.height,
                fps=info.fps,
                duration=info.duration,
                decode_with_ffmpeg=info.decode_with_ffmpeg,
            )
        except Exception as exc:  # noqa: BLE001 - GUI boundary shows the error
            self.status_label.configure(text="Не удалось открыть видео.")
            messagebox.showerror("Ошибка", str(exc), parent=self)
            return
        if not cap.isOpened():
            cap.release()
            self.status_label.configure(text="Не удалось открыть видео.")
            messagebox.showerror("Ошибка", "Не удалось открыть видео.", parent=self)
            return

        self.info = info
        self.cap = cap
        self.current_time = 0.0
        self.file_label.configure(text=Path(path).name)
        audio_text = "есть" if info.has_audio else "нет"
        self.video_info_label.configure(
            text=(
                f"{info.width}×{info.height} · {info.fps:.2f} fps · "
                f"{format_time(info.duration)} · звук: {audio_text}"
            )
        )
        self.in_var.set("0.00")
        self.out_var.set(f"{info.duration:.2f}")
        for entry in (self.in_entry, self.out_entry):
            entry.configure(to=info.duration)
        self.timeline.set_range(info.duration, 0.0, info.duration)
        self.timeline.set_current(0.0)
        self.effect_regions = [EffectRegion(0.0, info.duration, True, True)]
        self._publish_effect_visuals()
        self.ranges_btn.configure(state="normal")
        self._preview_sound.set(info.has_audio)
        self.sound_check.configure(
            state="normal" if info.has_audio else "disabled"
        )
        self.audio_status_label.configure(
            text="звук: есть" if info.has_audio else "звук: нет"
        )
        self._last_preview_blur_active = False
        self._update_time_label(0.0)
        self.play_btn.configure(state="normal")
        self.export_btn.configure(state="normal")
        self.progress["value"] = 0
        self.status_label.configure(text="Загружаю детектор лиц…")
        self.update_idletasks()

        try:
            self.anonymizer = FaceAnonymizer(
                model_path=default_model_path(),
                method=self._blur_method(),
                strength=self.strength_var.get(),
                margin=self.margin_var.get() / 100.0,
                detect_width=PREVIEW_DETECT_WIDTH,
            )
        except Exception as exc:  # noqa: BLE001 - Haar fallback is created inside
            self.status_label.configure(text="Детектор лиц недоступен.")
            messagebox.showerror("Ошибка детектора", str(exc), parent=self)
            self._clear_loaded_video()
            return

        if self.anonymizer.warning:
            self.status_label.configure(text=self.anonymizer.warning)
            messagebox.showwarning("Детектор лиц", self.anonymizer.warning, parent=self)
        else:
            self.status_label.configure(
                text=f"Готово. Детектор: {self.anonymizer.backend_name}."
            )

        if not self._render_at_time(0.0, reset_tracker=True):
            detail = cap.error_text() if isinstance(cap, FFmpegFrameReader) else ""
            self._clear_loaded_video()
            self.status_label.configure(text="Не удалось прочитать видео.")
            messagebox.showerror(
                "Ошибка декодирования",
                "Не удалось прочитать первый кадр видео."
                + (f"\n\n{detail[-2000:]}" if detail else ""),
                parent=self,
            )

    def _clear_loaded_video(self) -> None:
        self._stop_playback()
        if self._preview_after is not None:
            try:
                self.after_cancel(self._preview_after)
            except tk.TclError:
                pass
            self._preview_after = None
        if self._ranges_dialog is not None:
            try:
                self._ranges_dialog.destroy()
            except tk.TclError:
                pass
            self._ranges_dialog = None
        if self.cap is not None:
            self.cap.release()
            self.cap = None
        self.info = None
        self.anonymizer = None
        self._photo = None
        self.current_time = 0.0
        self.effect_regions = []
        self._last_preview_blur_active = False
        self.file_label.configure(text="Файл не выбран")
        self.video_info_label.configure(text="")
        self.in_var.set("0.00")
        self.out_var.set("0.00")
        self.timeline.set_range(0.0, 0.0, 0.0)
        self.timeline.set_current(0.0)
        self.timeline.set_effects([], [])
        self.ranges_btn.configure(text="Диапазоны эффектов…", state="disabled")
        self.sound_check.configure(state="disabled")
        self._preview_sound.set(True)
        self.audio_status_label.configure(text="")
        self.time_label.configure(text="00:00.00 / 00:00.00")
        self.play_btn.configure(state="disabled")
        self.export_btn.configure(state="disabled")
        self.progress["value"] = 0
        self._show_placeholder()

    def _on_canvas_configure(self, _event: tk.Event) -> None:
        if self.info is None:
            self._show_placeholder()
        elif self._photo is not None and not self._playing:
            self._schedule_preview()

    def _show_placeholder(self) -> None:
        self.preview.delete("all")
        width = max(1, self.preview.winfo_width())
        height = max(1, self.preview.winfo_height())
        self.preview.create_text(
            width // 2,
            height // 2,
            text="Откройте видео, чтобы начать",
            fill="#8b95a5",
            font=("TkDefaultFont", 14),
        )

    # --------------------------------------------------------------- effects
    def _blur_intervals(self) -> list[tuple[float, float]]:
        if self.info is None:
            return []
        return regions_to_intervals(
            self.effect_regions, "blur", self.info.duration
        )

    def _voice_intervals(self) -> list[tuple[float, float]]:
        if self.info is None:
            return []
        return regions_to_intervals(
            self.effect_regions, "voice", self.info.duration
        )

    def _publish_effect_visuals(self) -> None:
        if self.info is None:
            return
        self.timeline.set_effects(self._blur_intervals(), self._voice_intervals())
        active = [
            region
            for region in self.effect_regions
            if region.end > region.start and (region.blur or region.voice)
        ]
        if not active:
            label = "Диапазоны: эффекты выкл…"
        elif (
            len(active) == 1
            and active[0].start <= 0.0
            and active[0].end >= self.info.duration
            and active[0].blur
            and active[0].voice
        ):
            label = "Диапазоны: весь ролик…"
        else:
            label = f"Диапазоны эффектов ({len(self.effect_regions)})…"
        self.ranges_btn.configure(text=label)

    def _on_effects_changed(self) -> None:
        if self.info is None:
            return
        if self._playing:
            self._stop_playback(update_position=True)
        self._last_preview_blur_active = False
        if self.anonymizer is not None:
            self.anonymizer.reset()
        self._publish_effect_visuals()
        self._schedule_preview()

    def _open_effect_ranges(self) -> None:
        if self.info is None:
            return
        if self._ranges_dialog is not None and self._ranges_dialog.winfo_exists():
            self._ranges_dialog.lift()
            self._ranges_dialog.focus_set()
            return
        self._ranges_dialog = EffectRangesDialog(self)

    def _on_voice_settings(self) -> None:
        self._update_voice_labels()
        if self._playing:
            self._stop_playback(update_position=True)
            self._schedule_preview()

    # --------------------------------------------------------------- preview
    def _blur_method(self) -> str:
        return "pixelate" if self.method_var.get() == "Пиксели" else "gaussian"

    def _on_preview_settings(self) -> None:
        self.strength_label.configure(text=f"{self.strength_var.get():.1f}")
        self.margin_label.configure(text=f"{self.margin_var.get():.0f}%")
        if self.anonymizer is not None:
            self.anonymizer.method = self._blur_method()
            self.anonymizer.strength = self.strength_var.get()
            self.anonymizer.margin = self.margin_var.get() / 100.0
        if not self._playing:
            self._schedule_preview()

    def _update_voice_labels(self) -> None:
        semitones = self.voice_var.get()
        self.voice_label.configure(text=f"{semitones:+.1f} полутона")
        self.volume_label.configure(text=f"{self.volume_var.get():.0f}%")

    def _schedule_preview(self) -> None:
        if self.info is None:
            return
        if self._preview_after is not None:
            self.after_cancel(self._preview_after)
        self._preview_after = self.after(60, self._render_scheduled)

    def _render_scheduled(self) -> None:
        self._preview_after = None
        if not self._playing:
            self._render_at_time(self.current_time, reset_tracker=True)

    def _render_at_time(
        self,
        seconds: float,
        reset_tracker: bool = True,
        preserve_time: bool = False,
    ) -> bool:
        if self.info is None or self.cap is None or self._playing:
            return False
        requested = min(max(0.0, seconds), self.info.duration)
        # The last frame starts one frame interval before the container end.
        last_frame_time = max(0.0, self.info.duration - 1.0 / self.info.fps)
        frame_time = min(requested, last_frame_time)
        self.cap.set(cv2.CAP_PROP_POS_MSEC, frame_time * 1000.0)
        ok, frame = self.cap.read()
        if not ok or frame is None:
            return False
        if reset_tracker and self.anonymizer is not None:
            self.anonymizer.reset()
        self._display_frame(frame, frame_time)
        if preserve_time:
            self.current_time = requested
        else:
            self.current_time = frame_time
        self.timeline.set_current(self.current_time)
        self._update_time_label(self.current_time)
        return True

    def _display_frame(self, frame, frame_time: float | None = None) -> None:
        if self.anonymizer is not None:
            show_boxes = self.show_boxes_var.get()
            blur_enabled = self.blur_var.get() and (
                frame_time is None or is_active(self._blur_intervals(), frame_time)
            )
            # Keep detecting outside blur ranges when boxes are requested. This
            # both shows where faces are and keeps the tracker warm at the next
            # range boundary, avoiding a visible re-detection gap.
            tracking_enabled = blur_enabled or show_boxes
            if tracking_enabled:
                if (
                    blur_enabled
                    and not self._last_preview_blur_active
                    and not show_boxes
                ):
                    self.anonymizer.reset()
                frame, _boxes = self.anonymizer.process(
                    frame,
                    draw_boxes=show_boxes,
                    blur=blur_enabled,
                )
            elif self._last_preview_blur_active:
                self.anonymizer.reset()
            self._last_preview_blur_active = blur_enabled

        canvas_w = max(1, self.preview.winfo_width())
        canvas_h = max(1, self.preview.winfo_height())
        frame_h, frame_w = frame.shape[:2]
        scale = min(canvas_w / frame_w, canvas_h / frame_h)
        if scale <= 0:
            return
        new_w = max(1, round(frame_w * scale))
        new_h = max(1, round(frame_h * scale))
        rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
        image = Image.fromarray(rgb)
        if (new_w, new_h) != (frame_w, frame_h):
            image = image.resize((new_w, new_h), Image.Resampling.BILINEAR)
        self._photo = ImageTk.PhotoImage(image)
        self.preview.delete("all")
        self.preview.create_image(
            canvas_w // 2, canvas_h // 2, image=self._photo, anchor="center"
        )

    # --------------------------------------------------------------- playback
    def _toggle_play(self) -> None:
        if self.info is None or self.cap is None or self._busy:
            return
        if self._playing:
            self._stop_playback(update_position=True)
            self._schedule_preview()
            return

        start, end = self._trim_values(report=False)
        if self.current_time < start or self.current_time >= end - 0.01:
            self.current_time = start
        self.cap.set(cv2.CAP_PROP_POS_MSEC, self.current_time * 1000.0)
        if self.anonymizer is not None:
            self.anonymizer.reset()
        self._last_preview_blur_active = False
        self._playing = True
        self.play_btn.configure(text="⏸ Пауза")
        self._play_start_time = self.current_time
        self._play_end = end
        self._start_preview_audio(self.current_time, end)
        self._play_start_wall = time.monotonic()
        self._play_tick()

    def _play_tick(self) -> None:
        if not self._playing or self.cap is None or self.info is None:
            return
        tick_started = time.monotonic()
        target = self._play_start_time + (tick_started - self._play_start_wall)
        if target >= self._play_end:
            self._finish_playback()
            return

        result = self._read_frame_at(target)
        if result is None:
            self._finish_playback()
            return
        frame, frame_time = result

        self._display_frame(frame, frame_time)
        self.current_time = min(self._play_end, target)
        self.timeline.set_current(self.current_time)
        self._update_time_label(self.current_time)

        elapsed = time.monotonic() - tick_started
        delay = max(5, round(1000.0 / self.info.fps - elapsed * 1000.0))
        self._play_after = self.after(delay, self._play_tick)

    def _read_frame_at(
        self, target: float
    ) -> tuple[object, float] | None:
        if self.cap is None or self.info is None:
            return None
        # If rendering/detection is slower than real time, skip frames instead
        # of letting video lag behind audio. Cap the burst to keep the GUI alive.
        max_grabs = max(2, round(self.info.fps * 0.5))
        position = target
        for _ in range(max_grabs + 1):
            # Query before grab: afterwards OpenCV/FFmpeg may already report the
            # next frame's timestamp, which would make timed effects flicker one
            # frame early at a boundary.
            before = self.cap.get(cv2.CAP_PROP_POS_MSEC) / 1000.0
            if not self.cap.grab():
                return None
            position = before if before > 0 else target
            if position + 0.001 >= target:
                break
        ok, frame = self.cap.retrieve()
        if not ok or frame is None:
            return None
        return frame, position

    def _finish_playback(self) -> None:
        end = self._play_end
        self._stop_playback()
        if self.info is None:
            return
        self.current_time = min(end, self.info.duration)
        self.timeline.set_current(self.current_time)
        self._update_time_label(self.current_time)
        # Show the final frame but keep the playhead exactly at the trim end.
        self._render_at_time(
            self.current_time, reset_tracker=True, preserve_time=True
        )

    def _start_preview_audio(self, start: float, end: float) -> None:
        if self.info is None or not self.info.has_audio:
            self.audio_status_label.configure(text="звук: нет")
            return
        if not self._preview_sound.get() or self.mute_var.get():
            self.audio_status_label.configure(text="звук: выкл")
            return
        duration = end - start
        if duration <= 0.02:
            return

        error = self._preview_audio.start(
            source=self.info.path,
            start=start,
            duration=duration,
            voice_intervals=self._voice_intervals(),
            semitones=self.voice_var.get(),
            preserve_formants=self.formant_var.get(),
            volume=self.volume_var.get() / 100.0,
            sample_rate=self.info.sample_rate,
        )
        if error:
            self._show_audio_error(error)
            return

        self.audio_status_label.configure(text="звук: вкл")
        if self._audio_check_after is not None:
            try:
                self.after_cancel(self._audio_check_after)
            except tk.TclError:
                pass
        self._audio_check_after = self.after(600, self._check_preview_audio)

    def _check_preview_audio(self) -> None:
        self._audio_check_after = None
        if not self._playing:
            return
        error = self._preview_audio.poll_error()
        if error:
            self._show_audio_error(error)
            return
        if self._preview_audio.running:
            self.audio_status_label.configure(text="звук: вкл")
            self._audio_check_after = self.after(800, self._check_preview_audio)

    def _show_audio_error(self, error: str) -> None:
        self._stop_preview_audio()
        first_line = next(
            (line.strip() for line in error.splitlines() if line.strip()),
            "неизвестная ошибка",
        )
        if sys.stdout is not None:
            print(f"Anomizer preview audio: {error}", flush=True)
        self.audio_status_label.configure(text="звук: недоступен")
        self.status_label.configure(
            text=f"Предпросмотр без звука: {first_line[:220]}"
        )

    def _stop_preview_audio(self) -> None:
        if self._audio_check_after is not None:
            try:
                self.after_cancel(self._audio_check_after)
            except tk.TclError:
                pass
            self._audio_check_after = None
        self._preview_audio.stop()

    def _on_sound_toggle(self) -> None:
        if not self._preview_sound.get():
            self._stop_preview_audio()
            self.audio_status_label.configure(text="звук: выкл")
            return
        if self.info is None or not self.info.has_audio:
            self.audio_status_label.configure(text="звук: нет")
            return
        if not self._playing:
            self.audio_status_label.configure(text="звук: есть")
            return
        target = min(
            self._play_end,
            self._play_start_time + max(0.0, time.monotonic() - self._play_start_wall),
        )
        self._start_preview_audio(target, self._play_end)

    def _stop_playback(self, update_position: bool = False) -> None:
        if update_position and self._playing and self.info is not None:
            position = min(
                self._play_end,
                self._play_start_time
                + max(0.0, time.monotonic() - self._play_start_wall),
            )
            self.current_time = max(0.0, position)
            self.timeline.set_current(self.current_time)
            self._update_time_label(self.current_time)

        self._playing = False
        if self._play_after is not None:
            try:
                self.after_cancel(self._play_after)
            except tk.TclError:
                pass
            self._play_after = None
        self._stop_preview_audio()
        self.play_btn.configure(text="▶ Смотреть")

    def _on_timeline_seek(self, seconds: float) -> None:
        if self.info is None:
            return
        if self._playing:
            self._stop_playback()
        self.current_time = max(0.0, min(seconds, self.info.duration))
        self.timeline.set_current(self.current_time)
        self._update_time_label(self.current_time)
        self._schedule_preview()

    def _update_time_label(self, seconds: float) -> None:
        duration = self.info.duration if self.info else 0.0
        self.time_label.configure(
            text=f"{format_time(seconds)} / {format_time(duration)}"
        )

    # ------------------------------------------------------------------ trim
    def _trim_values(self, report: bool) -> tuple[float, float]:
        duration = self.info.duration if self.info else 0.0
        try:
            start = float(self.in_var.get().replace(",", "."))
            end = float(self.out_var.get().replace(",", "."))
        except ValueError:
            if report:
                messagebox.showerror(
                    "Ошибка", "Начало и конец фрагмента должны быть числами.", parent=self
                )
            return 0.0, duration

        start = min(max(0.0, start), duration)
        end = min(max(0.0, end), duration)
        if end <= start:
            if report:
                messagebox.showerror(
                    "Ошибка", "Конец фрагмента должен быть больше начала.", parent=self
                )
            return 0.0, duration
        return start, end

    def _refresh_timeline_range(self) -> None:
        if self.info is None:
            return
        if self._playing:
            self._stop_playback(update_position=True)
        start, end = self._trim_values(report=False)
        if end <= start:
            return
        self.timeline.set_range(self.info.duration, start, end)

    def _set_in_from_current(self) -> None:
        if self.info is None:
            return
        self.in_var.set(f"{self.current_time:.2f}")
        start, end = self._trim_values(report=False)
        if end <= start:
            self.out_var.set(f"{min(self.info.duration, start + 0.1):.2f}")
        self._refresh_timeline_range()

    def _set_out_from_current(self) -> None:
        if self.info is None:
            return
        self.out_var.set(f"{self.current_time:.2f}")
        start, end = self._trim_values(report=False)
        if end <= start:
            self.in_var.set(f"{max(0.0, end - 0.1):.2f}")
        self._refresh_timeline_range()

    # ---------------------------------------------------------------- export
    def _export(self) -> None:
        if self.info is None or self._busy:
            return
        start, end = self._trim_values(report=True)
        if end <= start:
            return

        source = self.info.path
        initial = source.with_name(f"{source.stem}_anonymized.mp4")
        selected = filedialog.asksaveasfilename(
            title="Сохранить результат",
            initialdir=str(source.parent),
            initialfile=initial.name,
            defaultextension=".mp4",
            filetypes=[("MP4 video", "*.mp4")],
        )
        if not selected:
            return
        output = Path(selected)
        if output.suffix.lower() != ".mp4":
            output = output.with_suffix(".mp4")
        if output.resolve() == source.resolve():
            messagebox.showerror(
                "Ошибка", "Нельзя перезаписать исходный файл.", parent=self
            )
            return
        if output.exists() and not messagebox.askyesno(
            "Перезаписать?", f"Файл уже существует:\n{output}\n\nПерезаписать?",
            parent=self,
        ):
            return

        config = ProcessConfig(
            input_path=source,
            output_path=output,
            start=start,
            end=end,
            blur_faces=self.blur_var.get(),
            blur_method=self._blur_method(),
            blur_strength=self.strength_var.get(),
            blur_margin=self.margin_var.get() / 100.0,
            blur_ranges=self._blur_intervals(),
            pitch_semitones=self.voice_var.get(),
            voice_ranges=self._voice_intervals(),
            preserve_formants=self.formant_var.get(),
            volume=self.volume_var.get() / 100.0,
            mute=self.mute_var.get(),
            show_boxes=False,
        )

        self._stop_playback()
        self._cancel_event.clear()
        self._set_busy(True)
        self.progress["value"] = 0
        self.status_label.configure(text="Обработка…")
        self._export_thread = threading.Thread(
            target=self._export_worker, args=(config,), daemon=True
        )
        self._export_thread.start()

    def _export_worker(self, config: ProcessConfig) -> None:
        try:
            output = process_video(
                config,
                progress=lambda fraction, message: self._queue.put(
                    ("progress", fraction, message)
                ),
                cancel_event=self._cancel_event,
            )
            self._queue.put(("done", str(output)))
        except ProcessingCancelled:
            self._queue.put(("cancelled", None))
        except Exception as exc:  # noqa: BLE001 - worker reports errors via a queue
            if sys.stderr is not None:
                traceback.print_exc()
            self._queue.put(("error", str(exc)))

    def _cancel_export(self) -> None:
        if self._busy:
            self._cancel_event.set()
            self.cancel_btn.configure(state="disabled")
            self.status_label.configure(text="Останавливаю обработку…")

    def _set_busy(self, busy: bool) -> None:
        self._busy = busy
        self.open_btn.configure(state="disabled" if busy else "normal")
        has_video = self.info is not None
        self.export_btn.configure(
            state="normal" if (has_video and not busy) else "disabled"
        )
        self.cancel_btn.configure(state="normal" if busy else "disabled")
        self.play_btn.configure(
            state="disabled" if (busy or not has_video) else "normal"
        )
        self.ranges_btn.configure(
            state="normal" if (has_video and not busy) else "disabled"
        )
        sound_available = (
            has_video and self.info is not None and self.info.has_audio
        )
        self.sound_check.configure(
            state="normal" if (sound_available and not busy) else "disabled"
        )

    def _drain_queue(self) -> None:
        if self._closing:
            return
        try:
            while True:
                item = self._queue.get_nowait()
                kind = item[0]
                if kind == "progress":
                    _, fraction, message = item
                    self.progress["value"] = max(0.0, min(100.0, fraction * 100.0))
                    self.status_label.configure(text=message)
                elif kind == "done":
                    _, output_path = item
                    self.progress["value"] = 100
                    self.status_label.configure(text="Готово.")
                    self._set_busy(False)
                    messagebox.showinfo(
                        "Готово", f"Видео сохранено:\n{output_path}", parent=self
                    )
                elif kind == "cancelled":
                    self.progress["value"] = 0
                    self.status_label.configure(text="Обработка отменена.")
                    self._set_busy(False)
                elif kind == "error":
                    _, error_text = item
                    self.progress["value"] = 0
                    self.status_label.configure(text="Ошибка обработки.")
                    self._set_busy(False)
                    messagebox.showerror("Ошибка обработки", error_text, parent=self)
        except queue.Empty:
            pass
        self.after(100, self._drain_queue)

    # ------------------------------------------------------------------ close
    def _on_close(self) -> None:
        self._closing = True
        self._cancel_event.set()
        self._stop_playback()
        if self._ranges_dialog is not None:
            try:
                self._ranges_dialog.destroy()
            except tk.TclError:
                pass
            self._ranges_dialog = None
        if self.cap is not None:
            self.cap.release()
            self.cap = None
        self.destroy()


def run() -> None:
    app = AnomizerApp()
    app.mainloop()
