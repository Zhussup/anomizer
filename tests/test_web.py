"""Headless tests for the local web interface.

These tests never open a browser, Tkinter, or a public socket: FastAPI's
TestClient calls the ASGI app directly with a loopback Host header.
"""

from __future__ import annotations

import hashlib
import subprocess
import time
from pathlib import Path
from urllib.parse import quote

import pytest
from fastapi.testclient import TestClient

from anomizer.ffmpeg_utils import find_ffmpeg
from anomizer.web.server import (
    COOKIE_NAME,
    REQUEST_HEADER,
    TOKEN_HEADER,
    create_app,
)

BASE_URL = "http://127.0.0.1:8765"
TOKEN = "test-token-value"


@pytest.fixture(scope="module")
def video_path(tmp_path_factory: pytest.TempPathFactory) -> Path:
    path = tmp_path_factory.mktemp("media") / "sample.mp4"
    result = subprocess.run(
        [
            find_ffmpeg(),
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "testsrc2=size=160x120:rate=10:duration=1",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=220:duration=1",
            "-c:v",
            "libx264",
            "-preset",
            "ultrafast",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            "-shortest",
            str(path),
        ],
        check=False,
        capture_output=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stderr.decode(errors="replace")
    return path


@pytest.fixture
def app(tmp_path: Path):
    return create_app(token=TOKEN, port=8765, temp_dir=tmp_path / "web")


@pytest.fixture
def client(app):
    with TestClient(app, base_url=BASE_URL) as test_client:
        response = test_client.post(
            "/api/auth",
            json={"token": TOKEN},
            headers={REQUEST_HEADER: "1"},
        )
        assert response.status_code == 200, response.text
        yield test_client


def open_source(client: TestClient, path: Path) -> dict:
    response = client.post(
        "/api/open",
        json={"path": str(path)},
        headers={REQUEST_HEADER: "1"},
    )
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["loaded"] is True
    return payload


def save_settings(client: TestClient, payload: dict, version: int) -> dict:
    response = client.put(
        f"/api/settings?v={version}",
        json=payload,
        headers={REQUEST_HEADER: "1"},
    )
    assert response.status_code == 200, response.text
    return response.json()


def quiet_settings(payload: dict, *, end: float | None = None) -> dict:
    settings = dict(payload["settings"])
    settings.update(
        start=0.0,
        end=end if end is not None else payload["info"]["duration"],
        blur_enabled=False,
        show_boxes=False,
        voice_semitones=0.0,
        volume=1.0,
        mute=False,
        preserve_formants=False,
        regions=[],
        crf=30,
        preset="ultrafast",
    )
    return settings


def test_security_headers_and_host_allowlist(client: TestClient) -> None:
    response = client.get("/")
    assert response.status_code == 200
    assert "Content-Security-Policy" in response.headers
    assert response.headers["X-Frame-Options"] == "DENY"
    assert response.headers["Referrer-Policy"] == "no-referrer"
    assert "unsafe-inline" not in response.headers["Content-Security-Policy"]
    assert "__ANOMIZER_VERSION__" not in response.text

    rejected = client.get("/", headers={"Host": "evil.example:8765"})
    assert rejected.status_code == 400

    missing_header = client.post("/api/close")
    assert missing_header.status_code == 403

    foreign_origin = client.post(
        "/api/close",
        headers={REQUEST_HEADER: "1", "Origin": "http://evil.example"},
    )
    assert foreign_origin.status_code == 403

    cross_site = client.get(
        "/api/state", headers={"Sec-Fetch-Site": "cross-site"}
    )
    assert cross_site.status_code == 403


def test_auth_uses_opaque_session_cookie(client: TestClient) -> None:
    cookie = client.cookies.get(COOKIE_NAME)
    assert cookie
    assert cookie != TOKEN

    # The raw token remains a fallback when a browser blocks cookies.
    client.cookies.clear()
    response = client.get("/api/state", headers={TOKEN_HEADER: TOKEN})
    assert response.status_code == 200

    # Wrong token/header still requires a valid credential.
    response = client.get("/api/state", headers={TOKEN_HEADER: "wrong"})
    assert response.status_code == 401


def test_unauthenticated_state_is_rejected(app, video_path: Path) -> None:
    with TestClient(app, base_url=BASE_URL) as test_client:
        response = test_client.get("/api/state")
        assert response.status_code == 401
        assert response.json()["detail"]

        bad_token = test_client.post(
            "/api/auth",
            json={"token": "nope"},
            headers={REQUEST_HEADER: "1"},
        )
        assert bad_token.status_code == 401
        assert COOKIE_NAME not in test_client.cookies


def test_open_frame_and_settings(client: TestClient, video_path: Path) -> None:
    payload = open_source(client, video_path)
    assert payload["info"]["name"] == video_path.name
    assert payload["info"]["has_audio"] is True
    assert payload["info"]["width"] == 160
    assert payload["info"]["height"] == 120
    assert len(payload["blur_intervals"]) == 1
    assert len(payload["voice_intervals"]) == 1

    version = payload["version"]
    frame = client.get(f"/api/frame?t=0.2&v={version}")
    assert frame.status_code == 200, frame.text
    assert frame.headers["content-type"] == "image/jpeg"
    assert frame.content.startswith(b"\xff\xd8")
    assert "X-Frame-Time" in frame.headers

    stale = client.get(f"/api/frame?t=0.2&v={max(0, version - 1)}")
    assert stale.status_code == 409

    settings = dict(payload["settings"])
    settings["regions"] = []
    settings["end"] = None
    saved = save_settings(client, settings, version)
    assert saved["version"] > version
    assert saved["blur_intervals"] == []
    assert saved["voice_intervals"] == []
    assert saved["settings"]["end"] == pytest.approx(
        payload["info"]["duration"], abs=0.01
    )

    full = dict(saved["settings"])
    full["regions"] = [
        {
            "start": 0.0,
            "end": payload["info"]["duration"],
            "blur": True,
            "voice": True,
        }
    ]
    restored = save_settings(client, full, saved["version"])
    assert restored["blur_intervals"]
    assert restored["voice_intervals"]

    # A stale write is refused instead of silently overwriting.
    stale_write = client.put(
        f"/api/settings?v={version}",
        json=full,
        headers={REQUEST_HEADER: "1"},
    )
    assert stale_write.status_code == 409


def test_stream_and_audio_end_at_trim(client: TestClient, video_path: Path) -> None:
    payload = open_source(client, video_path)
    settings = quiet_settings(payload, end=min(0.3, payload["info"]["duration"]))
    saved = save_settings(client, settings, payload["version"])
    version = saved["version"]

    stream = client.get(f"/api/stream?from=0&v={version}")
    assert stream.status_code == 200, stream.text
    assert stream.headers["content-type"].startswith(
        "multipart/x-mixed-replace"
    )
    assert b"--frame" in stream.content
    assert b"Content-Type: image/jpeg" in stream.content
    assert b"\xff\xd8" in stream.content

    stale = client.get(f"/api/stream?from=0&v={version - 1}")
    assert stale.status_code == 409

    audio = client.get(f"/api/audio?from=0&v={version}")
    assert audio.status_code == 200, audio.text
    assert audio.headers["content-type"] in {"audio/mpeg", "audio/aac"}
    assert len(audio.content) > 0

    muted = dict(saved["settings"])
    muted["mute"] = True
    muted_saved = save_settings(client, muted, version)
    no_audio = client.get(f"/api/audio?from=0&v={muted_saved['version']}")
    assert no_audio.status_code == 404


def test_upload_is_temporary_and_removed(
    client: TestClient, app, video_path: Path
) -> None:
    uploads = app.state.studio.uploads_dir
    response = client.post(
        f"/api/upload?name={quote('uploaded.mp4')}",
        content=video_path.read_bytes(),
        headers={
            REQUEST_HEADER: "1",
            "Content-Type": "application/octet-stream",
        },
    )
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["loaded"] is True
    assert payload["info"]["uploaded"] is True
    assert payload["info"]["name"] == "uploaded.mp4"
    assert list(uploads.iterdir())

    close = client.post("/api/close", headers={REQUEST_HEADER: "1"})
    assert close.status_code == 200
    assert close.json()["loaded"] is False
    assert not list(uploads.iterdir())


def test_source_is_not_modified_by_export(
    client: TestClient, app, video_path: Path
) -> None:
    before = hashlib.sha256(video_path.read_bytes()).hexdigest()
    payload = open_source(client, video_path)
    settings = quiet_settings(
        payload, end=min(0.4, payload["info"]["duration"])
    )
    saved = save_settings(client, settings, payload["version"])

    response = client.post(
        f"/api/export?v={saved['version']}",
        headers={REQUEST_HEADER: "1"},
    )
    assert response.status_code == 200, response.text
    job = response.json()
    assert job["status"] == "running"

    deadline = time.monotonic() + 40
    while time.monotonic() < deadline:
        state = client.get(f"/api/jobs/{job['id']}")
        assert state.status_code == 200, state.text
        job = state.json()
        if job["status"] != "running":
            break
        time.sleep(0.1)
    assert job["status"] == "done", job
    assert job["download_url"]
    assert job["size_bytes"] > 0

    output = app.state.studio.jobs_dir / job["id"] / "result.mp4"
    assert output.is_file()

    download = client.get(job["download_url"])
    assert download.status_code == 200
    assert download.headers["content-type"] == "video/mp4"
    assert len(download.content) == job["size_bytes"]

    after = hashlib.sha256(video_path.read_bytes()).hexdigest()
    assert after == before

    deleted = client.delete(
        f"/api/jobs/{job['id']}",
        headers={REQUEST_HEADER: "1"},
    )
    assert deleted.status_code == 200
    assert not output.exists()


def test_upload_limit_is_enforced(tmp_path: Path) -> None:
    app = create_app(
        token=TOKEN,
        port=8765,
        temp_dir=tmp_path / "limited",
        max_upload_bytes=8,
    )
    with TestClient(app, base_url=BASE_URL) as test_client:
        auth = test_client.post(
            "/api/auth",
            json={"token": TOKEN},
            headers={REQUEST_HEADER: "1"},
        )
        assert auth.status_code == 200
        response = test_client.post(
            "/api/upload?name=big.mp4",
            content=b"0123456789",
            headers={
                REQUEST_HEADER: "1",
                "Content-Type": "application/octet-stream",
            },
        )
        assert response.status_code == 413
        assert not list(app.state.studio.uploads_dir.iterdir())
