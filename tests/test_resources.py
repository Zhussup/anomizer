"""Resource lookup for source checkouts and packaged builds."""

from __future__ import annotations

from pathlib import Path

import pytest

from anomizer import faces, ffmpeg_utils, resources


def test_bundle_override_finds_static_model_and_ffmpeg(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bundle = tmp_path / "bundle"
    (bundle / "anomizer" / "web" / "static").mkdir(parents=True)
    (bundle / "anomizer" / "web" / "static" / "index.html").write_text(
        "ok", encoding="utf-8"
    )
    (bundle / "models").mkdir()
    model = bundle / "models" / faces.MODEL_FILENAME
    model.write_bytes(b"model")
    (bundle / "bin").mkdir()
    ffmpeg = bundle / "bin" / "ffmpeg-linux-x86_64-v7.0.2"
    ffmpeg.write_bytes(b"binary")

    monkeypatch.setenv("ANOMIZER_BUNDLE_DIR", str(bundle))
    assert resources.bundle_root() == bundle.resolve()
    assert resources.static_dir() == bundle / "anomizer" / "web" / "static"
    assert resources.bundled_model_path(faces.MODEL_FILENAME) == model
    assert faces.default_model_path() == model
    assert resources.bundled_ffmpeg_path() == ffmpeg


def test_windows_style_ffmpeg_name_is_found(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "bin").mkdir()
    exe = tmp_path / "bin" / "ffmpeg.exe"
    exe.write_bytes(b"binary")
    monkeypatch.setenv("ANOMIZER_BUNDLE_DIR", str(tmp_path))
    assert resources.bundled_ffmpeg_path() == exe


def test_user_data_dir_override(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ANOMIZER_DATA_DIR", str(tmp_path / "data"))
    assert resources.user_data_dir() == (tmp_path / "data").resolve()


def test_missing_model_is_not_downloaded_when_disabled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ANOMIZER_DATA_DIR", str(tmp_path / "data"))
    with pytest.raises(FileNotFoundError):
        faces.ensure_model(tmp_path / "missing.onnx", allow_download=False)
    assert not (tmp_path / "missing.onnx").exists()


def test_corrupt_bundled_model_is_never_deleted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    model = tmp_path / "models" / faces.MODEL_FILENAME
    model.parent.mkdir()
    model.write_bytes(b"not a real model")
    monkeypatch.setenv("ANOMIZER_BUNDLE_DIR", str(tmp_path))
    monkeypatch.setenv("ANOMIZER_DATA_DIR", str(tmp_path / "data"))

    with pytest.raises(FileNotFoundError):
        faces.ensure_model(allow_download=False)
    assert model.is_file()


def test_find_ffmpeg_prefers_bundled_binary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "bin").mkdir()
    bundled = tmp_path / "bin" / "ffmpeg-test"
    bundled.write_bytes(b"binary")
    monkeypatch.setenv("ANOMIZER_BUNDLE_DIR", str(tmp_path))
    ffmpeg_utils.find_ffmpeg.cache_clear()
    try:
        assert ffmpeg_utils.find_ffmpeg() == str(bundled)
    finally:
        ffmpeg_utils.find_ffmpeg.cache_clear()
