"""FastAPI 앱 — 로그인 · 권한을 지난 요청만 엔진에 넘기고, 엔진이 준 JSON 을 그대로 돌려준다.

화면(web/dist)이 빌드돼 있으면 같은 주소에서 내준다. 개발 중에는 Vite 개발 서버가 /api · /auth 를 여기로 넘긴다.
"""

from __future__ import annotations

import logging
from pathlib import Path
from urllib.parse import urlsplit

from fastapi import APIRouter, Depends, FastAPI, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response
from starlette.middleware.sessions import SessionMiddleware
from starlette.middleware.trustedhost import TrustedHostMiddleware

from . import __version__, auth, config
from .auth import AuthError, User, current_user, require
from .db import Db
from .engines import Engine, EngineError, make_engine

log = logging.getLogger("lefsite")

CSP = ("default-src 'self'; img-src 'self' data:; style-src 'self' 'unsafe-inline'; font-src 'self' data:; "
       "connect-src 'self'; frame-ancestors 'none'; base-uri 'self'; form-action 'self'")


def _origin(url: str) -> str:
    p = urlsplit(url)
    return f"{p.scheme}://{p.netloc}"


def api_router(engine: Engine, settings: config.Settings, db: Db) -> APIRouter:
    r = APIRouter(prefix="/api")

    @r.get("/me")
    def me(user: User = Depends(current_user)) -> dict:
        return {**user.to_json(), "auth": settings.auth}

    @r.get("/info")
    def info(user: User = Depends(require("view"))) -> dict:
        return {"site": {"version": __version__, "auth": settings.auth}, "connector": engine.kind, "engine": engine.engine_info()}

    @r.get("/sessions")
    def sessions(car: str | None = None, event_id: str | None = None, discipline: str | None = None, limit: int | None = None,
                 offset: int = 0, user: User = Depends(require("view"))) -> list[dict]:
        return engine.list_sessions(car=car, event_id=event_id, discipline=discipline, limit=limit, offset=offset)

    @r.get("/sessions/{sid}")
    def session(sid: str, user: User = Depends(require("view"))) -> dict:
        return engine.get_session(sid)

    @r.get("/sessions/{sid}/channels")
    def channels(sid: str, user: User = Depends(require("view"))) -> list[dict]:
        return engine.list_channels(sid)

    @r.get("/sessions/{sid}/laps")
    def laps(sid: str, user: User = Depends(require("view"))) -> dict:
        return engine.get_laps(sid)

    @r.get("/sessions/{sid}/quality")
    def quality(sid: str, user: User = Depends(require("view"))) -> list[dict]:
        return engine.get_quality(sid)

    @r.api_route("/files/{file_id:path}", methods=["GET", "HEAD"])
    def download(file_id: str, request: Request, user: User = Depends(require("download"))) -> Response:
        """내려받기 — 팀장 · 관리자만 (화면만 막지 않고 서버가 막는다). HEAD 는 받을 수 있는지만 보고 기록하지 않는다."""
        f = engine.open_file(file_id)
        if request.method == "GET":
            db.record_download(user.email, file_id, f["name"])
        return FileResponse(f["path"], filename=f["name"], media_type=f["content_type"])

    @r.get("/sessions/{sid}/resolve")
    def resolve(sid: str, user: User = Depends(require("view"))) -> dict:
        return engine.resolve_session(sid)

    @r.get("/admin/users")
    def users(user: User = Depends(require("manage_users"))) -> list[dict]:
        return db.list_users()

    @r.get("/{rest:path}", include_in_schema=False)
    def unknown(rest: str) -> JSONResponse:
        return JSONResponse({"error": {"code": "not_found", "message": f"없는 API: /api/{rest}", "detail": None}}, status_code=404)

    return r


def _web(app: FastAPI, dist: Path) -> None:
    """빌드한 화면 내주기. 화면 주소(/sessions/… 등)는 전부 index.html — 주소 해석은 화면이 한다."""
    index = dist / "index.html"

    @app.get("/{path:path}", include_in_schema=False)
    def web(path: str):
        if path.startswith(("api/", "auth/")):
            return JSONResponse({"error": {"code": "not_found", "message": f"없는 주소: /{path}", "detail": None}}, status_code=404)
        if not index.exists():
            return HTMLResponse("<!doctype html><meta charset='utf-8'><title>LightOn Data</title>"
                                "<p>화면이 아직 빌드되지 않았습니다. <code>cd web &amp;&amp; npm run build</code> 를 실행하거나, 개발 중이면 "
                                "<code>npm run dev</code> 의 주소(http://127.0.0.1:5173)를 여세요.</p>", status_code=503)
        f = (dist / path).resolve()
        if path and f.is_file() and dist.resolve() in f.parents:
            return FileResponse(f)
        return FileResponse(index, headers={"Cache-Control": "no-cache"})


def create_app(settings: config.Settings | None = None, engine: Engine | None = None) -> FastAPI:
    settings = settings or config.load()
    db = Db(settings.db_path)
    db.migrate()
    if settings.auth == "dev":
        # 개발용 사용자를 명단에 넣는다 — 역할은 LEF_SITE_DEV_ROLE (팀원 화면을 볼 때 member)
        db.put_user(settings.dev_email, settings.dev_role, name=settings.dev_name, note="개발용 자동 로그인")
    engine = engine or make_engine(settings)

    dev = settings.auth == "dev"
    app = FastAPI(title="LightOn Data", version=__version__, docs_url="/api/docs" if dev else None, redoc_url=None,
                  openapi_url="/api/openapi.json" if dev else None)
    app.state.settings, app.state.db, app.state.engine = settings, db, engine

    @app.exception_handler(EngineError)
    def engine_error(request: Request, e: EngineError) -> JSONResponse:
        return JSONResponse(e.to_json(), status_code=e.status)

    @app.exception_handler(AuthError)
    def auth_error(request: Request, e: AuthError) -> JSONResponse:
        return JSONResponse(e.to_json(), status_code=e.status)

    @app.exception_handler(Exception)
    def crash(request: Request, e: Exception) -> JSONResponse:
        log.exception("처리 중 오류: %s %s", request.method, request.url.path)
        return JSONResponse({"error": {"code": "internal", "message": "서버 오류가 발생했습니다. 서버 콘솔 기록을 확인하세요", "detail": None}}, status_code=500)

    base = _origin(settings.base_url)
    local = config.is_loopback(settings.host)

    @app.middleware("http")
    async def guard(request: Request, call_next):
        # 다른 사이트가 이 사이트로 보내는 쓰기 요청(CSRF)을 막는다. 127.0.0.1 에 묶였을 때는 이 PC 의 다른 포트(Vite 개발 서버)도 받는다
        if request.method not in ("GET", "HEAD", "OPTIONS"):
            origin = request.headers.get("origin")
            if origin and origin != base and not (local and config.is_loopback(urlsplit(origin).hostname or "")):
                return JSONResponse({"error": {"code": "forbidden", "message": "다른 사이트에서 보낸 요청은 받지 않습니다", "detail": None}}, status_code=403)
        response = await call_next(request)
        h = response.headers
        h.setdefault("X-Content-Type-Options", "nosniff")
        h.setdefault("X-Frame-Options", "DENY")
        h.setdefault("Referrer-Policy", "same-origin")
        if h.get("content-type", "").startswith("text/html"):
            h.setdefault("Content-Security-Policy", CSP)
        if request.url.path.startswith(("/api/", "/auth/")):
            h.setdefault("Cache-Control", "no-store")
        return response

    app.add_middleware(SessionMiddleware, secret_key=config.secret_key(settings), session_cookie="lefsite",
                       max_age=settings.session_max_age_s, same_site="lax", https_only=settings.https)
    hosts = {urlsplit(settings.base_url).hostname or "127.0.0.1"} | ({"127.0.0.1", "localhost"} if local else set())
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=sorted(hosts))   # 127.0.0.1 에서 DNS 다시 묶기 공격을 막는다

    app.include_router(auth.router(settings, db))
    app.include_router(api_router(engine, settings, db))
    _web(app, settings.web_dist)
    return app
