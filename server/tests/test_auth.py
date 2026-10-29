"""로그인 · 권한 — 개발용 자동 로그인도 Google 로그인과 같은 확인 길을 지난다."""

import pytest
from authlib.integrations.starlette_client.apps import StarletteOAuth2App

from lefsite.auth import _safe_next, check_google_claims

GOOGLE = {"LEF_SITE_AUTH": "google", "LEF_SITE_GOOGLE_CLIENT_ID": "test-client", "LEF_SITE_GOOGLE_CLIENT_SECRET": "test-secret",
          "LEF_SITE_ALLOWED_DOMAIN": "example.ac.kr"}


def test_dev_login_is_automatic_admin(client):
    me = client.get("/api/me")
    assert me.status_code == 200
    assert me.json()["role"] == "admin" and me.json()["auth"] == "dev"
    assert "lefsite" in client.cookies


def test_member_cannot_manage_users_but_can_view(make_client):
    c = make_client(LEF_SITE_DEV_ROLE="member")
    assert c.get("/api/sessions").status_code == 200
    r = c.get("/api/admin/users")
    assert r.status_code == 403
    assert r.json()["error"]["code"] == "forbidden" and "관리자" in r.json()["error"]["message"]


def test_disabling_a_user_blocks_the_next_request(client):
    assert client.get("/api/me").status_code == 200
    client.app.state.db.set_active("dev@localhost", False)
    r = client.get("/api/me")
    assert r.status_code == 403 and r.json()["error"]["code"] == "disabled"


def test_google_mode_needs_login(make_client):
    c = make_client(**GOOGLE)
    assert c.get("/auth/mode").json() == {"mode": "google"}
    r = c.get("/api/sessions")
    assert r.status_code == 401
    assert r.json()["error"] == {"code": "login_required", "message": "로그인이 필요합니다", "detail": {"login": "google"}}


@pytest.mark.parametrize("claims, refusal", [
    ({"email": "kim@example.ac.kr", "email_verified": True, "hd": "example.ac.kr"}, None),
    ({"email": "Kim@Example.ac.kr", "email_verified": True, "hd": "EXAMPLE.ac.kr"}, None),
    ({"email": "kim@example.ac.kr", "email_verified": False, "hd": "example.ac.kr"}, "email_unverified"),
    ({"email": "kim@example.ac.kr", "email_verified": "true", "hd": "example.ac.kr"}, "email_unverified"),
    ({"email": "kim@gmail.com", "email_verified": True}, "not_workspace"),
    ({"email": "kim@example.ac.kr", "email_verified": True}, "not_workspace"),      # 같은 도메인 메일이어도 Workspace 계정이 아니면
    ({"email": "kim@other.ac.kr", "email_verified": True, "hd": "other.ac.kr"}, "wrong_domain"),
    ({"email": "kim@other.ac.kr", "email_verified": True, "hd": "example.ac.kr"}, "wrong_domain"),
    ({"email_verified": True, "hd": "example.ac.kr"}, "oauth_failed"),
])
def test_google_claims(claims, refusal):
    assert check_google_claims(claims, "example.ac.kr") == refusal


@pytest.mark.parametrize("path, safe", [
    ("/sessions/DEMO-1_2027-05-08_S1", "/sessions/DEMO-1_2027-05-08_S1"),
    (None, "/"), ("https://evil.example", "/"), ("//evil.example", "/"), ("/\\evil.example", "/"), ("sessions", "/"),
])
def test_next_stays_on_this_site(path, safe):
    assert _safe_next(path) == safe


METADATA = {"issuer": "https://accounts.google.com", "authorization_endpoint": "https://accounts.google.com/o/oauth2/v2/auth",
            "token_endpoint": "https://oauth2.googleapis.com/token", "jwks_uri": "https://www.googleapis.com/oauth2/v3/certs"}


@pytest.fixture
def google(make_client, monkeypatch):
    """Google 과 통신하지 않는 Google 로그인 — 메타데이터와 토큰 교환(서명 확인 포함)을 바꿔 끼운다."""
    claims = {}

    async def metadata(self):
        return METADATA

    async def exchange(self, request, **kw):
        return {"access_token": "x", "userinfo": dict(claims)}

    monkeypatch.setattr(StarletteOAuth2App, "load_server_metadata", metadata)
    monkeypatch.setattr(StarletteOAuth2App, "authorize_access_token", exchange)
    c = make_client(**GOOGLE)
    c.follow_redirects = False
    return c, claims


def test_google_login_redirect_asks_for_the_school_domain(google):
    c, _ = google
    r = c.get("/auth/google/login", params={"next": "/sessions"})
    assert r.status_code == 302
    loc = r.headers["location"]
    assert loc.startswith(METADATA["authorization_endpoint"])
    for part in ("hd=example.ac.kr", "code_challenge_method=S256", "nonce=", "state=",
                 "redirect_uri=http%3A%2F%2F127.0.0.1%3A8000%2Fauth%2Fgoogle%2Fcallback"):
        assert part in loc


def test_google_callback_listed_user_logs_in(google):
    c, claims = google
    c.app.state.db.put_user("kim@example.ac.kr", "lead")
    claims.update(email="kim@example.ac.kr", email_verified=True, hd="example.ac.kr", name="김")
    c.get("/auth/google/login", params={"next": "/sessions"})
    r = c.get("/auth/google/callback")
    assert (r.status_code, r.headers["location"]) == (302, "/sessions")
    me = c.get("/api/me").json()
    assert (me["email"], me["role"], me["name"]) == ("kim@example.ac.kr", "lead", "김")
    assert c.app.state.db.recent_logins()[0]["result"] == "ok"


@pytest.mark.parametrize("setup, refusal", [(None, "not_listed"), ("off", "disabled")])
def test_google_callback_refuses_outside_the_list(google, setup, refusal):
    c, claims = google
    if setup == "off":
        c.app.state.db.put_user("kim@example.ac.kr", "member", active=False)
    claims.update(email="kim@example.ac.kr", email_verified=True, hd="example.ac.kr")
    r = c.get("/auth/google/callback")
    assert r.headers["location"] == f"/login?error={refusal}"
    assert c.get("/api/me").status_code == 401
    assert c.app.state.db.recent_logins()[0]["result"] == refusal
