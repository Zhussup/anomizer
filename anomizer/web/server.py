"""Local-only FastAPI application and launcher for Anomizer."""

from __future__ import annotations

import argparse
import asyncio
import collections
import secrets
import socket
import threading
import time
import webbrowser
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import quote

import uvicorn
from fastapi import APIRouter, Depends, FastAPI, HTTPException, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import (
    FileResponse,
    HTMLResponse,
    JSONResponse,
    Response,
    StreamingResponse,
)
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field
from starlette.exceptions import HTTPException as StarletteHTTPException

from .. import __version__, resources
from .models import PathRequest, SettingsModel
from .studio import (
    MAX_UPLOAD_BYTES_DEFAULT,
    MJPEG_BOUNDARY,
    Studio,
    StudioError,
    TooLargeError,
)

COOKIE_NAME = "anomizer_session"
REQUEST_HEADER = "X-Anomizer-Request"
TOKEN_HEADER = "X-Anomizer-Token"
SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}
API_PREFIX = "/api/"
SESSION_TTL_SECONDS = 24 * 60 * 60

CSP = (
    "default-src 'self'; "
    "img-src 'self' blob:; "
    "media-src 'self'; "
    "connect-src 'self'; "
    "script-src 'self'; "
    "style-src 'self'; "
    "font-src 'self'; "
    "object-src 'none'; "
    "base-uri 'none'; "
    "frame-ancestors 'none'; "
    "form-action 'self'"
)


class TokenRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    token: str = Field(min_length=1, max_length=256)


class SessionStore:
    """Opaque browser sessions, revocable without revealing the access token.

    The cookie contains a random session id.  The real token only ever travels
    in the URL fragment, in the POST /api/auth body, and optionally in an
    ``X-Anomizer-Token`` header used as a fallback when cookies are blocked.
    """

    def __init__(self, ttl: float = SESSION_TTL_SECONDS) -> None:
        self.ttl = max(60.0, float(ttl))
        self._sessions: dict[str, float] = {}
        self._lock = threading.Lock()

    def create(self) -> str:
        session_id = secrets.token_urlsafe(32)
        now = time.monotonic()
        with self._lock:
            expired = [
                key for key, expiry in self._sessions.items() if expiry <= now
            ]
            for key in expired:
                self._sessions.pop(key, None)
            self._sessions[session_id] = now + self.ttl
        return session_id

    def valid(self, session_id: str | None) -> bool:
        if not session_id:
            return False
        now = time.monotonic()
        with self._lock:
            expiry = self._sessions.get(session_id)
            if expiry is None:
                return False
            if expiry <= now:
                self._sessions.pop(session_id, None)
                return False
            return True

    def revoke(self, session_id: str | None) -> None:
        if not session_id:
            return
        with self._lock:
            self._sessions.pop(session_id, None)


class AuthLimiter:
    """Small in-memory limiter, enough to make token guessing unattractive."""

    def __init__(self, max_failures: int = 5, window: float = 10.0) -> None:
        self.max_failures = max(1, int(max_failures))
        self.window = max(1.0, float(window))
        self._failures: collections.deque[float] = collections.deque()
        self._lock = threading.Lock()

    def _trim_locked(self, now: float) -> None:
        while self._failures and now - self._failures[0] > self.window:
            self._failures.popleft()

    def locked(self) -> bool:
        now = time.monotonic()
        with self._lock:
            self._trim_locked(now)
            return len(self._failures) >= self.max_failures

    def fail(self) -> None:
        now = time.monotonic()
        with self._lock:
            self._trim_locked(now)
            self._failures.append(now)

    def reset(self) -> None:
        with self._lock:
            self._failures.clear()


def _allowed_hosts(port: int) -> set[str]:
    hosts = {
        f"127.0.0.1:{port}",
        f"localhost:{port}",
        f"[::1]:{port}",
    }
    # A browser omits the port for the default HTTP port.
    if port == 80:
        hosts.update({"127.0.0.1", "localhost", "[::1]"})
    return hosts


def _allowed_origins(hosts: set[str]) -> set[str]:
    return {f"http://{host}" for host in hosts}


def _looks_like_api(path: str) -> bool:
    return path == "/api" or path.startswith(API_PREFIX)


def _security_headers(response: Response) -> None:
    response.headers["Content-Security-Policy"] = CSP
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["Cross-Origin-Opener-Policy"] = "same-origin"
    response.headers["Cross-Origin-Resource-Policy"] = "same-origin"
    response.headers["Permissions-Policy"] = (
        "camera=(), microphone=(), geolocation=(), payment=()"
    )


def _json_error(message: str, status_code: int) -> JSONResponse:
    response = JSONResponse({"detail": message}, status_code=status_code)
    _security_headers(response)
    response.headers["Cache-Control"] = "no-store"
    return response


def create_app(
    *,
    token: str,
    port: int,
    temp_dir: Path | None = None,
    max_upload_bytes: int | None = None,
    static_dir: Path | None = None,
) -> FastAPI:
    """Build the application.

    ``port`` is used for an exact Host allowlist.  The launcher always binds
    Uvicorn to ``127.0.0.1``; this app never accepts a configurable public host.
    """
    if not token:
        raise ValueError("Токен веб-сервера не может быть пустым.")
    if not 0 <= int(port) <= 65535:
        raise ValueError("Некорректный порт веб-сервера.")

    static_path = (
        Path(static_dir) if static_dir is not None else resources.static_dir()
    )
    index_path = static_path / "index.html"
    if not index_path.is_file():
        raise RuntimeError(f"Не найдены статические файлы веб-интерфейса: {static_path}")
    index_html = index_path.read_text(encoding="utf-8").replace(
        "__ANOMIZER_VERSION__", __version__
    )

    studio = Studio(
        temp_dir=temp_dir,
        max_upload_bytes=(
            MAX_UPLOAD_BYTES_DEFAULT
            if max_upload_bytes is None
            else max_upload_bytes
        ),
    )
    allowed_hosts = _allowed_hosts(int(port))
    allowed_origins = _allowed_origins(allowed_hosts)
    limiter = AuthLimiter()
    sessions = SessionStore()

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        yield
        await asyncio.to_thread(studio.shutdown)

    app = FastAPI(
        title="Anomizer",
        version=__version__,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        lifespan=lifespan,
    )
    app.state.studio = studio
    app.state.token = token
    app.state.allowed_hosts = allowed_hosts
    app.state.allowed_origins = allowed_origins
    app.state.auth_limiter = limiter
    app.state.sessions = sessions

    @app.middleware("http")
    async def local_guard(request: Request, call_next):
        host = (request.headers.get("host") or "").strip().lower()
        if host not in allowed_hosts:
            return _json_error("Некорректный заголовок Host.", 400)

        path = request.url.path
        if _looks_like_api(path):
            fetch_site = (request.headers.get("sec-fetch-site") or "").lower()
            if fetch_site and fetch_site not in {"same-origin", "none"}:
                return _json_error("Запрос с другого сайта отклонён.", 403)

        if request.method not in SAFE_METHODS and _looks_like_api(path):
            if request.headers.get(REQUEST_HEADER) != "1":
                return _json_error("Не хватает защитного заголовка запроса.", 403)
            origin = request.headers.get("origin")
            if origin is not None and origin not in allowed_origins:
                return _json_error("Запрос с другого сайта отклонён.", 403)

        response = await call_next(request)
        _security_headers(response)
        if path == "/" or _looks_like_api(path):
            response.headers["Cache-Control"] = "no-store"
        return response

    @app.exception_handler(StudioError)
    async def studio_error_handler(_request: Request, exc: StudioError):
        return _json_error(str(exc), exc.status_code)

    @app.exception_handler(RequestValidationError)
    async def validation_error_handler(
        _request: Request, exc: RequestValidationError
    ):
        # Pydantic's raw messages are English and include implementation
        # details.  The UI validates first; this is the final safety net.
        del exc
        return _json_error("Некорректные параметры запроса.", 422)

    @app.exception_handler(StarletteHTTPException)
    async def http_error_handler(_request: Request, exc: StarletteHTTPException):
        detail = exc.detail
        if not isinstance(detail, str):
            detail = "Ошибка запроса."
        response = _json_error(detail, int(exc.status_code))
        if exc.headers:
            for key, value in exc.headers.items():
                response.headers[key] = value
        return response

    def require_session(request: Request) -> None:
        if sessions.valid(request.cookies.get(COOKIE_NAME)):
            return
        supplied = request.headers.get(TOKEN_HEADER)
        if supplied and secrets.compare_digest(str(supplied), token):
            return
        raise HTTPException(status_code=401, detail="Нужен доступ к веб-интерфейсу.")

    @app.get("/", response_class=HTMLResponse)
    def index() -> HTMLResponse:
        return HTMLResponse(
            index_html,
            headers={"Cache-Control": "no-store"},
        )

    @app.get("/favicon.ico")
    def favicon() -> Response:
        return Response(status_code=204)

    @app.post("/api/auth")
    def auth(payload: TokenRequest) -> JSONResponse:
        if limiter.locked():
            raise HTTPException(
                status_code=429,
                detail="Слишком много попыток. Подождите несколько секунд.",
            )
        if not secrets.compare_digest(payload.token, token):
            limiter.fail()
            raise HTTPException(status_code=401, detail="Неверный код доступа.")
        limiter.reset()
        session_id = sessions.create()
        response = JSONResponse(
            {
                "ok": True,
                "version": studio.state_payload().get("version", 0),
            }
        )
        response.set_cookie(
            COOKIE_NAME,
            session_id,
            httponly=True,
            samesite="strict",
            path="/",
        )
        response.headers["Cache-Control"] = "no-store"
        return response

    @app.post("/api/auth/logout")
    def logout(request: Request) -> JSONResponse:
        sessions.revoke(request.cookies.get(COOKIE_NAME))
        response = JSONResponse({"ok": True})
        response.delete_cookie(COOKIE_NAME, path="/")
        response.headers["Cache-Control"] = "no-store"
        return response

    api = APIRouter(prefix="/api", dependencies=[Depends(require_session)])

    @api.get("/state")
    def state() -> dict:
        return studio.state_payload()

    @api.post("/open")
    def open_path(payload: PathRequest) -> dict:
        return studio.open_path(payload.path)

    @api.post("/upload")
    async def upload(
        request: Request,
        name: str = Query(default="video.mp4", max_length=512),
    ) -> dict:
        studio.ensure_can_replace_source()
        directory, target = await asyncio.to_thread(
            studio.new_upload_target, name
        )
        limit = studio.max_upload_bytes
        declared = request.headers.get("content-length")
        if declared and limit:
            try:
                if int(declared) > limit:
                    studio.remove_upload_dir(directory)
                    raise TooLargeError("Файл слишком большой для веб-загрузки.")
            except ValueError:
                pass
        total = 0
        try:
            with target.open("wb") as file:
                async for chunk in request.stream():
                    total += len(chunk)
                    if limit and total > limit:
                        raise TooLargeError(
                            "Файл слишком большой для веб-загрузки."
                        )
                    file.write(chunk)
            if total == 0:
                raise StudioError("Загруженный файл пустой.")
        except BaseException:
            studio.remove_upload_dir(directory)
            raise
        try:
            return await asyncio.to_thread(
                studio.open_upload, target, name
            )
        except BaseException:
            studio.remove_upload_dir(directory)
            raise

    @api.post("/close")
    def close_source() -> dict:
        return studio.close_source()

    @api.put("/settings")
    def update_settings(
        payload: SettingsModel,
        version: int | None = Query(default=None, ge=0, alias="v"),
    ) -> dict:
        return studio.update_settings(payload, expected_version=version)

    @api.get("/frame")
    def frame(
        t: float = Query(default=0.0, ge=0.0),
        v: int | None = Query(default=None, ge=0),
    ) -> Response:
        jpeg, frame_time, version = studio.frame_jpeg(t, expected_version=v)
        return Response(
            content=jpeg,
            media_type="image/jpeg",
            headers={
                "Cache-Control": "no-store",
                "X-Frame-Time": f"{frame_time:.3f}",
                "X-Anomizer-Version": str(version),
            },
        )

    @api.get("/stream")
    async def stream(
        from_time: float | None = Query(default=None, alias="from", ge=0.0),
        v: int | None = Query(default=None, ge=0),
    ) -> StreamingResponse:
        spec = studio.prepare_stream(
            from_time=from_time, expected_version=v
        )
        return StreamingResponse(
            studio.mjpeg_stream(spec),
            media_type=f"multipart/x-mixed-replace; boundary={MJPEG_BOUNDARY}",
            headers={
                "Cache-Control": "no-store",
                "X-Accel-Buffering": "no",
                "Accept-Ranges": "none",
            },
        )

    @api.get("/audio")
    async def audio(
        from_time: float | None = Query(default=None, alias="from", ge=0.0),
        v: int | None = Query(default=None, ge=0),
    ) -> StreamingResponse:
        spec = await asyncio.to_thread(
            studio.prepare_audio,
            from_time=from_time,
            expected_version=v,
        )
        return StreamingResponse(
            studio.audio_stream(spec),
            media_type=spec.media_type,
            headers={
                "Cache-Control": "no-store",
                "X-Accel-Buffering": "no",
                "Accept-Ranges": "none",
            },
        )

    @api.post("/export")
    def export(
        v: int | None = Query(default=None, ge=0),
    ) -> dict:
        return studio.start_export(expected_version=v)

    @api.get("/jobs/{job_id}")
    def job_state(job_id: str) -> dict:
        return studio.job_state(job_id)

    @api.post("/jobs/{job_id}/cancel")
    def cancel_job(job_id: str) -> dict:
        return studio.cancel_job(job_id)

    @api.get("/jobs/{job_id}/download")
    def download_job(job_id: str) -> FileResponse:
        output, download_name = studio.job_download(job_id)
        return FileResponse(
            output,
            media_type="video/mp4",
            filename=download_name,
            headers={"Cache-Control": "no-store"},
        )

    @api.delete("/jobs/{job_id}")
    def delete_job(job_id: str) -> dict:
        studio.delete_job(job_id)
        return {"ok": True}

    app.include_router(api)
    app.mount("/static", StaticFiles(directory=static_path), name="static")
    return app


def _choose_port(requested: int) -> int:
    if not 0 <= requested <= 65535:
        raise SystemExit("Порт должен быть от 0 до 65535.")
    if requested == 0:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.bind(("127.0.0.1", 0))
            return int(sock.getsockname()[1])
    for candidate in range(requested, min(65535, requested + 50) + 1):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            try:
                sock.bind(("127.0.0.1", candidate))
            except OSError:
                continue
            return candidate
    raise SystemExit(
        f"Не удалось найти свободный порт, начиная с {requested}."
    )


def main(argv: list[str] | None = None) -> int:
    resources.configure_console()
    parser = argparse.ArgumentParser(
        prog="anomizer-web",
        description="Локальный веб-интерфейс Anomizer (только 127.0.0.1).",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=8765,
        help="порт на 127.0.0.1 (0 — выбрать автоматически)",
    )
    parser.add_argument(
        "--no-browser",
        action="store_true",
        help="не открывать браузер автоматически",
    )
    parser.add_argument(
        "--token",
        default=None,
        help=argparse.SUPPRESS,
    )
    parser.add_argument(
        "--temp-dir",
        type=Path,
        default=None,
        help=argparse.SUPPRESS,
    )
    args = parser.parse_args(argv)

    port = _choose_port(args.port)
    token = args.token or secrets.token_urlsafe(32)
    app = create_app(token=token, port=port, temp_dir=args.temp_dir)
    url = f"http://127.0.0.1:{port}/#{quote(token, safe='')}"

    print(f"Anomizer — локальный веб-интерфейс: {url}", flush=True)
    print("Открыт только этот компьютер. Остановка: Ctrl+C.", flush=True)

    if not args.no_browser:
        timer = threading.Timer(0.8, _open_browser, args=(url,))
        timer.daemon = True
        timer.start()

    config = uvicorn.Config(
        app,
        host="127.0.0.1",
        port=port,
        log_level="warning",
        access_log=False,
        proxy_headers=False,
    )
    server = uvicorn.Server(config)
    try:
        server.run()
    except KeyboardInterrupt:
        pass
    return 0


def _open_browser(url: str) -> None:
    try:
        webbrowser.open(url, new=2)
    except Exception:  # noqa: BLE001 - opening a browser is best-effort
        return
