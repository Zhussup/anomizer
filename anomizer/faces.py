"""Automatic face detection, tracking and blurring.

The default detector is YuNet, a small pretrained OpenCV model. It needs no
training and no per-frame manual annotation. If the model cannot be loaded, the
code falls back to OpenCV's built-in Haar cascade.
"""

from __future__ import annotations

import hashlib
import math
import os
import tempfile
import urllib.request
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

import cv2
import numpy as np

from . import resources

Box = tuple[int, int, int, int]

MODEL_FILENAME = "face_detection_yunet_2023mar.onnx"
MODEL_URL = (
    "https://github.com/opencv/opencv_zoo/raw/main/models/"
    "face_detection_yunet/face_detection_yunet_2023mar.onnx"
)
MODEL_SHA256 = "8f2383e4dd3cfbb4553ea8718107fc0423210dc964f9f4280604804ed2552fa4"


@dataclass(frozen=True)
class DetectedFace:
    box: Box
    score: float


def default_model_path() -> Path:
    """Return the bundled model, or a writable per-user path in a slim build."""
    bundled = resources.bundled_model_path(MODEL_FILENAME)
    if bundled.is_file():
        return bundled
    return resources.user_data_dir() / "models" / MODEL_FILENAME


def _is_valid_model(path: Path) -> bool:
    try:
        if not path.is_file() or path.stat().st_size <= 100_000:
            return False
        return hashlib.sha256(path.read_bytes()).hexdigest() == MODEL_SHA256
    except OSError:
        return False


def _remove_stale_model(path: Path) -> None:
    """Delete a corrupt download, but never touch a read-only bundle."""
    if not path.exists() or resources.is_bundled_path(path):
        return
    try:
        path.unlink()
    except OSError:
        pass


def ensure_model(
    model_path: Path | None = None, *, allow_download: bool = True
) -> Path:
    """Return the YuNet model path, downloading it once if allowed.

    Packaged builds include the model.  If a bundled/invalid model is absent,
    the fallback download goes to the user's data directory, never inside the
    application folder.  The web server always passes ``allow_download=False``:
    a privacy tool must not quietly contact the network.
    """
    if model_path is not None:
        target = Path(model_path)
        if _is_valid_model(target):
            return target
        _remove_stale_model(target)
        if resources.is_bundled_path(target):
            target = resources.user_data_dir() / "models" / MODEL_FILENAME
            if _is_valid_model(target):
                return target
            _remove_stale_model(target)
    else:
        bundled = resources.bundled_model_path(MODEL_FILENAME)
        if _is_valid_model(bundled):
            return bundled
        target = resources.user_data_dir() / "models" / MODEL_FILENAME
        if _is_valid_model(target):
            return target
        _remove_stale_model(target)

    if not allow_download:
        raise FileNotFoundError(
            "Модель YuNet не найдена. В веб-режиме скачивание отключено; "
            "положите файл models/face_detection_yunet_2023mar.onnx рядом "
            "с программой или используйте собранную версию."
        )

    target.parent.mkdir(parents=True, exist_ok=True)
    request = urllib.request.Request(
        MODEL_URL, headers={"User-Agent": "anomizer/0.1 (+local app)"}
    )
    fd, tmp_name = tempfile.mkstemp(
        prefix=target.name, suffix=".part", dir=target.parent
    )
    os.close(fd)
    tmp = Path(tmp_name)
    try:
        with urllib.request.urlopen(request, timeout=60) as response, tmp.open(
            "wb"
        ) as f:
            while True:
                chunk = response.read(1024 * 128)
                if not chunk:
                    break
                f.write(chunk)

        digest = hashlib.sha256(tmp.read_bytes()).hexdigest()
        if digest != MODEL_SHA256:
            raise RuntimeError(
                "Скачанный файл модели повреждён (не совпала контрольная сумма)."
            )
        tmp.replace(target)
    except Exception:
        tmp.unlink(missing_ok=True)
        raise

    return target


class YuNetFaceDetector:
    """Thin wrapper around cv2.FaceDetectorYN with downscaled inference."""

    def __init__(
        self,
        model_path: Path | None = None,
        score_threshold: float = 0.6,
        nms_threshold: float = 0.3,
        detect_width: int = 960,
        allow_download: bool = True,
    ) -> None:
        if not hasattr(cv2, "FaceDetectorYN"):
            raise RuntimeError("Эта сборка OpenCV не содержит FaceDetectorYN.")
        self.model_path = ensure_model(model_path, allow_download=allow_download)
        self.score_threshold = float(score_threshold)
        self.detect_width = max(160, int(detect_width))
        self._detector = cv2.FaceDetectorYN.create(
            str(self.model_path),
            "",
            (320, 320),
            self.score_threshold,
            float(nms_threshold),
            50,
        )
        self._input_size: tuple[int, int] | None = None
        self._scale = 1.0

    def detect(self, frame_bgr: np.ndarray) -> list[DetectedFace]:
        height, width = frame_bgr.shape[:2]
        scale = min(1.0, self.detect_width / float(width))
        detect_w = max(1, round(width * scale))
        detect_h = max(1, round(height * scale))

        if (detect_w, detect_h) != self._input_size:
            self._detector.setInputSize((detect_w, detect_h))
            self._input_size = (detect_w, detect_h)
            self._scale = scale

        if scale < 1.0:
            small = cv2.resize(
                frame_bgr, (detect_w, detect_h), interpolation=cv2.INTER_AREA
            )
        else:
            small = frame_bgr

        _, faces = self._detector.detect(small)
        if faces is None:
            return []

        result: list[DetectedFace] = []
        inv_scale = 1.0 / self._scale
        for face in faces:
            score = float(face[-1])
            if score < self.score_threshold:
                continue
            x, y, w, h = (float(v) * inv_scale for v in face[:4])
            box = _clamp_box((x, y, w, h), width, height)
            if box is not None:
                result.append(DetectedFace(box=box, score=score))
        return result


class HaarFaceDetector:
    """Offline fallback: built into OpenCV, frontal faces only."""

    def __init__(self) -> None:
        cascade_path = Path(cv2.data.haarcascades) / "haarcascade_frontalface_default.xml"
        self._cascade = cv2.CascadeClassifier(str(cascade_path))
        if self._cascade.empty():
            raise RuntimeError("Не удалось загрузить каскад Хаара из OpenCV.")

    def detect(self, frame_bgr: np.ndarray) -> list[DetectedFace]:
        gray = cv2.cvtColor(frame_bgr, cv2.COLOR_BGR2GRAY)
        gray = cv2.equalizeHist(gray)
        faces = self._cascade.detectMultiScale(
            gray, scaleFactor=1.1, minNeighbors=5, minSize=(36, 36)
        )
        height, width = frame_bgr.shape[:2]
        result: list[DetectedFace] = []
        for x, y, w, h in faces:
            box = _clamp_box((float(x), float(y), float(w), float(h)), width, height)
            if box is not None:
                result.append(DetectedFace(box=box, score=1.0))
        return result


def _clamp_box(box: Iterable[float], width: int, height: int) -> Box | None:
    x, y, w, h = box
    x1 = max(0, min(width - 1, math.floor(x)))
    y1 = max(0, min(height - 1, math.floor(y)))
    x2 = max(0, min(width, math.ceil(x + w)))
    y2 = max(0, min(height, math.ceil(y + h)))
    if x2 - x1 < 2 or y2 - y1 < 2:
        return None
    return x1, y1, x2 - x1, y2 - y1


def _iou(a: Box, b: Box) -> float:
    ax, ay, aw, ah = a
    bx, by, bw, bh = b
    x1 = max(ax, bx)
    y1 = max(ay, by)
    x2 = min(ax + aw, bx + bw)
    y2 = min(ay + ah, by + bh)
    inter = max(0, x2 - x1) * max(0, y2 - y1)
    if inter == 0:
        return 0.0
    union = aw * ah + bw * bh - inter
    return inter / float(union)


@dataclass
class _Track:
    box: Box
    score: float
    missing: int = 0


class FaceTracker:
    """Keeps a face box for a few missed frames to avoid blinking.

    Detection boxes are used as-is (no position smoothing), because a lagging
    box could expose part of the face during fast movement.
    """

    def __init__(self, max_missing: int = 8, iou_threshold: float = 0.2) -> None:
        self.max_missing = max_missing
        self.iou_threshold = iou_threshold
        self._tracks: list[_Track] = []

    def reset(self) -> None:
        self._tracks.clear()

    def update(self, detections: list[DetectedFace]) -> list[Box]:
        used: set[int] = set()

        for track in self._tracks:
            best_index = -1
            best_iou = self.iou_threshold
            for index, detection in enumerate(detections):
                if index in used:
                    continue
                overlap = _iou(track.box, detection.box)
                if overlap >= best_iou:
                    best_iou = overlap
                    best_index = index

            if best_index >= 0:
                detection = detections[best_index]
                track.box = detection.box
                track.score = detection.score
                track.missing = 0
                used.add(best_index)
            else:
                track.missing += 1

        for index, detection in enumerate(detections):
            if index not in used:
                self._tracks.append(_Track(box=detection.box, score=detection.score))

        self._tracks = [t for t in self._tracks if t.missing <= self.max_missing]
        return [track.box for track in self._tracks]


class FaceAnonymizer:
    """Detect faces and blur/pixelate them in BGR frames."""

    def __init__(
        self,
        model_path: Path | None = None,
        method: str = "gaussian",
        strength: float = 3.0,
        margin: float = 0.22,
        score_threshold: float = 0.6,
        detect_width: int = 960,
        allow_download: bool = True,
    ) -> None:
        self.method = method
        self.strength = min(5.0, max(1.0, float(strength)))
        self.margin = min(0.45, max(0.0, float(margin)))
        self.tracker = FaceTracker()
        self.warning: str | None = None

        try:
            self.detector = YuNetFaceDetector(
                model_path=model_path,
                score_threshold=score_threshold,
                detect_width=detect_width,
                allow_download=allow_download,
            )
            self.backend_name = "YuNet"
        except Exception as exc:  # noqa: BLE001 - fall back when YuNet is unavailable
            self.detector = HaarFaceDetector()
            self.backend_name = "Haar (резервный)"
            self.warning = (
                "Не удалось загрузить YuNet, используется резервный детектор Хаара. "
                f"Он хуже находит профиль и наклонённые лица. Причина: {exc}"
            )

    def reset(self) -> None:
        self.tracker.reset()

    def process(
        self,
        frame_bgr: np.ndarray,
        draw_boxes: bool = False,
        blur: bool = True,
    ) -> tuple[np.ndarray, list[Box]]:
        detections = self.detector.detect(frame_bgr)
        boxes = self.tracker.update(detections)

        height, width = frame_bgr.shape[:2]
        if blur:
            for box in boxes:
                expanded = _expand_box(box, self.margin, width, height)
                _blur_region(frame_bgr, expanded, self.method, self.strength)

        if draw_boxes:
            for box in boxes:
                expanded = _expand_box(box, self.margin, width, height)
                x, y, w, h = expanded
                cv2.rectangle(frame_bgr, (x, y), (x + w, y + h), (80, 220, 120), 2)

        return frame_bgr, boxes


def _expand_box(box: Box, margin: float, width: int, height: int) -> Box:
    x, y, w, h = box
    pad_x = round(w * margin)
    pad_y = round(h * margin)
    x1 = max(0, x - pad_x)
    y1 = max(0, y - pad_y)
    x2 = min(width, x + w + pad_x)
    y2 = min(height, y + h + pad_y)
    return x1, y1, max(1, x2 - x1), max(1, y2 - y1)


def _blur_region(
    frame_bgr: np.ndarray, box: Box, method: str, strength: float
) -> None:
    x, y, w, h = box
    roi = frame_bgr[y : y + h, x : x + w]
    if roi.size == 0:
        return

    strength = min(5.0, max(1.0, float(strength)))
    if method == "pixelate":
        # Stronger setting -> larger blocks (fewer pixels in the downscale).
        block = max(5, round(min(w, h) / (11.0 - strength)))
        small_w = max(1, w // block)
        small_h = max(1, h // block)
        small = cv2.resize(roi, (small_w, small_h), interpolation=cv2.INTER_AREA)
        frame_bgr[y : y + h, x : x + w] = cv2.resize(
            small, (w, h), interpolation=cv2.INTER_NEAREST
        )
    else:
        sigma = max(2.5, min(w, h) * (0.012 + 0.022 * strength))
        frame_bgr[y : y + h, x : x + w] = cv2.GaussianBlur(roi, (0, 0), sigma)
