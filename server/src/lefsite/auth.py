"""로그인 · 권한 (`사이트_설계.md` §4).

- Google 로그인: 서명 · `email_verified` · 도메인(`hd` 값과 메일 끝 둘 다) · **명단에 있고 켜져 있는지**를 서버가 본다
- 개발용 자동 로그인(`LEF_SITE_AUTH=dev`, 127.0.0.1 에서만): 로그인 쿠키에 개발용 메일을 넣어 줄 뿐이고,
  그 뒤 명단 · 역할 확인은 Google 로그인과 **같은 길**(`current_user` → `require`)을 지난다
- 비밀번호를 만들거나 보관하지 않는다. 로그인 쿠키는 서명만 된다(내용은 메일 · 로그인 시각) — 요청마다 명단을 다시 보므로 명단에서 끄면 바로 막힌다
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from authlib.integrations.starlette_client import OAuth, OAuthError
from fastapi import APIRouter, Depends, Request
from fastapi.responses import RedirectResponse, Response

from .config import Settings
from .db import Db, now

log = logging.getLogger("lefsite.auth")

PERMISSIONS = {
    "admin": frozenset({"view", "download", "upload", "manage_users"}),
    "lead": frozenset({"view", "download", "upload"}),
    "member": frozenset({"view"}),
}
ROLE_LABEL = {"admin": "관리자", "lead": "팀장", "member": "팀원"}
DENIED = {
    "view": "보기 권한 없음",
    "download": "내려받기 권한 없음 — 팀장만 할 수 있습니다",
    "upload": "올리기 권한 없음 — 팀장만 할 수 있습니다",
    "manage_users": "명단 관리 권한 없음 — 관리자만 할 수 있습니다",
}

# Google 로그인을 거절한 이유 — 로그인 화면이 code 로 안내 글을 고른다 (web/src/pages/Login.tsx)
REFUSALS = ("oauth_failed", "email_unverified", "not_workspace", "wrong_domain", "not_listed", "disabled")


class AuthError(Exception):
    def __init__(self, code: str, message: str, status: int, detail=None):
        super().__init__(message)
        self.code, self.message, self.status, self.detail = code, message, status, detail

    def to_json(self) -> dict:
        return {"error": {"code": self.code, "message": self.message, "detail": self.detail}}


@dataclass(frozen=True)
class User:
    email: str
    name: str
    role: str

    @property
    def permissions(self) -> frozenset[str]:
        return PERMISSIONS[self.role]

    def to_json(self) -> dict:
        return {"email": self.email, "name": self.name, "role": self.role, "role_label": ROLE_LABEL[self.role],
                "permissions": sorted(self.permissions)}


def current_user(request: Request) -> User:
    """로그인한 사람 — 쿠키의 메일로 명단을 다시 본다. 명단에 없거나 꺼졌으면 쿠키를 지우고 막는다."""
    settings: Settings = request.app.state.settings
    db: Db = request.app.state.db
    email = request.session.get("email")
    if not email and settings.auth == "dev":
        email = request.session["email"] = settings.dev_email      # 개발용 자동 로그인 — 아래부터는 Google 과 같은 길
        request.session["login_at"] = now()
    if not email:
        raise AuthError("login_required", "로그인이 필요합니다", 401, {"login": settings.auth})
    user = db.get_user(email)
    if not user or not user["active"]:
        request.session.clear()
        code = "disabled" if user else "not_listed"
        raise AuthError(code, "사용이 꺼진 계정입니다" if user else "명단에 없는 계정입니다", 403, {"login": settings.auth})
    return User(email=user["email"], name=user["name"], role=user["role"])


def require(permission: str):
    """권한 확인 — 화면만 막지 않고 서버도 같은 규칙으로 막는다 (`사이트_설계.md` §7)."""

    def dep(user: User = Depends(current_user)) -> User:
        if permission not in user.permissions:
            raise AuthError("forbidden", DENIED[permission], 403, {"permission": permission, "role": user.role})
        return user

    return dep


def check_google_claims(claims: dict, domain: str) -> str | None:
    """Google ID 토큰(서명 · 발급자 · 대상 · 만료 · nonce 는 Authlib 가 이미 봤다)의 내용 확인 → 거절 code, 통과면 None.

    `hd` 는 Google Workspace 계정에만 붙는다. 로그인 요청의 hd 는 화면 힌트일 뿐이라 토큰의 값을 다시 본다.
    """
    email = str(claims.get("email") or "").lower()
    if not email:
        return "oauth_failed"
    if claims.get("email_verified") is not True:
        return "email_unverified"
    if not claims.get("hd"):
        return "not_workspace"
    if str(claims["hd"]).lower() != domain or not email.endswith("@" + domain):
        return "wrong_domain"
    return None


def _safe_next(path: str | None) -> str:
    """로그인 뒤 돌아갈 곳 — 이 사이트 안의 경로만 (다른 사이트로 튕기지 않게)."""
    if not path or not path.startswith("/") or path.startswith("//") or "\\" in path:
        return "/"
    return path


def router(settings: Settings, db: Db) -> APIRouter:
    r = APIRouter()

    @r.get("/auth/mode")
    def mode() -> dict:
        """로그인 화면이 무엇을 보여 줄지 (로그인 전에도 부른다)."""
        return {"mode": settings.auth}

    @r.post("/auth/logout", status_code=204)
    def logout(request: Request) -> Response:
        request.session.clear()
        return Response(status_code=204)

    if settings.auth != "google":
        return r

    oauth = OAuth()
    oauth.register(
        "google",
        server_metadata_url="https://accounts.google.com/.well-known/openid-configuration",
        client_id=settings.google_client_id,
        client_secret=settings.google_client_secret,
        client_kwargs={"scope": "openid email profile", "code_challenge_method": "S256"},
    )
    callback = settings.base_url + "/auth/google/callback"

    @r.get("/auth/google/login")
    async def google_login(request: Request, next: str | None = None):
        request.session["next"] = _safe_next(next)
        # hd = 학교 계정만 고르게 하는 힌트 (확인은 돌아온 토큰으로 다시 한다)
        return await oauth.google.authorize_redirect(request, callback, hd=settings.allowed_domain, prompt="select_account")

    @r.get("/auth/google/callback")
    async def google_callback(request: Request):
        nxt = _safe_next(request.session.get("next"))
        try:
            token = await oauth.google.authorize_access_token(request)
        except OAuthError as e:
            log.warning("Google 로그인 실패: %s", e.error)
            db.record_login("", "oauth_failed")
            return RedirectResponse("/login?error=oauth_failed", status_code=302)
        claims = dict(token.get("userinfo") or {})
        email = str(claims.get("email") or "").lower()
        refusal = check_google_claims(claims, settings.allowed_domain)
        if refusal is None:
            user = db.get_user(email)
            refusal = "not_listed" if not user else (None if user["active"] else "disabled")
        db.record_login(email, refusal or "ok", name=claims.get("name"))
        if refusal:
            log.info("로그인 거절 %s: %s (hd=%r)", email or "?", refusal, claims.get("hd"))
            request.session.clear()
            return RedirectResponse(f"/login?error={refusal}", status_code=302)
        request.session.clear()                       # 로그인 전 쿠키 내용(상태 값 등)을 이어 쓰지 않는다
        request.session.update({"email": email, "login_at": now()})
        log.info("로그인 %s", email)
        return RedirectResponse(nxt, status_code=302)

    return r
