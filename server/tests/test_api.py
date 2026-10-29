"""HTTP 층 — 엔진 JSON 을 그대로 넘기고, 오류는 계약 §8 모양 + 맞는 HTTP 상태로."""

import pytest
from fastapi.testclient import TestClient

from lefsite.app import create_app
from lefsite.engines.fake import RETIRED

from .conftest import BASE, settings


def test_sessions_and_session(client):
    rows = client.get("/api/sessions").json()
    assert rows and rows == client.app.state.engine.list_sessions()
    sid = rows[0]["sid"]
    assert client.get(f"/api/sessions/{sid}").json()["summary"]["sid"] == sid
    assert client.get(f"/api/sessions/{sid}/channels").status_code == 200
    assert client.get("/api/sessions", params={"limit": 2, "offset": 1}).json() == rows[1:3]


def test_engine_errors_keep_the_contract_shape(client):
    r = client.get("/api/sessions/DEMO-1_2099-01-01_S1")
    assert r.status_code == 404 and r.json()["error"]["code"] == "unknown_session"
    assert client.get("/api/sessions/a..b").json()["error"]["code"] == "unknown_session"
    assert client.get("/api/sessions", params={"limit": 0}).status_code == 400


def test_retired_sid_is_410_with_the_way_forward(client):
    sid = next(s for s, r in RETIRED.items() if r["why"] == "replaced")
    r = client.get(f"/api/sessions/{sid}")
    assert r.status_code == 410
    err = r.json()["error"]
    assert err["code"] == "session_retired" and err["detail"]["current"] == RETIRED[sid]["replaced_by"]
    assert client.get(f"/api/sessions/{sid}/resolve").json()["status"] == "retired"


def test_info_and_unknown_api(client):
    info = client.get("/api/info").json()
    assert info["connector"] == "fake" and info["engine"]["contract"]
    r = client.get("/api/nope")
    assert r.status_code == 404 and r.json()["error"]["code"] == "not_found"


def test_security_headers(client):
    h = client.get("/api/me").headers
    assert h["x-content-type-options"] == "nosniff" and h["x-frame-options"] == "DENY" and h["cache-control"] == "no-store"
    cookie = h["set-cookie"].lower()
    assert "httponly" in cookie and "samesite=lax" in cookie


def test_cross_site_writes_are_refused(client):
    assert client.post("/auth/logout", headers={"origin": "https://evil.example"}).status_code == 403
    assert client.post("/auth/logout", headers={"origin": BASE}).status_code == 204
    assert client.post("/auth/logout", headers={"origin": "http://127.0.0.1:5173"}).status_code == 204   # Vite 개발 서버


def test_foreign_host_header_is_refused(client):
    assert client.get("/api/me", headers={"host": "evil.example"}).status_code == 400


def test_web_pages_fall_back_to_index(tmp_path):
    dist = tmp_path / "dist"
    (dist / "assets").mkdir(parents=True)
    (dist / "index.html").write_text("<!doctype html><title>x</title>", encoding="utf-8")
    (dist / "assets" / "a.js").write_text("1", encoding="utf-8")
    (tmp_path / "secret.txt").write_text("no", encoding="utf-8")
    c = TestClient(create_app(settings(tmp_path, LEF_SITE_WEB_DIST=str(dist))), base_url=BASE)
    page = c.get("/sessions/DEMO-1_2027-05-08_S1")
    assert page.status_code == 200 and "<title>x</title>" in page.text and "content-security-policy" in page.headers
    assert c.get("/assets/a.js").text == "1"
    assert "no" != c.get("/..%2Fsecret.txt").text
    assert c.get("/api/sessions/x/y/z").status_code == 404


def test_without_a_build_the_page_says_how_to_make_one(client):
    r = client.get("/sessions")
    assert r.status_code == 503 and "npm run build" in r.text


# ---------------------------------------------------------------- 랩 표 · 품질 · 내려받기

FILE = "DEMO-1_2027-05-08_S1/files/laps.csv"


def test_laps_and_quality(client):
    laps = client.get("/api/sessions/DEMO-1_2027-05-08_S1/laps").json()
    assert laps["rows"] and laps["columns"][0]["key"] == "lap_time_s"
    assert client.get("/api/sessions/DEMO-1_2027-05-08_S4/quality").json()[0]["level"] == "warn"
    sid = next(s for s, r in RETIRED.items() if r["why"] == "replaced")
    assert client.get(f"/api/sessions/{sid}/laps").status_code == 410


def test_member_cannot_download_and_nothing_is_logged(make_client):
    c = make_client(LEF_SITE_DEV_ROLE="member")
    for method in ("GET", "HEAD"):
        r = c.request(method, f"/api/files/{FILE}")
        assert r.status_code == 403
    assert c.get(f"/api/files/{FILE}").json()["error"]["message"] == "내려받기 권한 없음 — 팀장만 할 수 있습니다"
    assert c.app.state.db.recent_downloads() == []


def test_lead_downloads_and_it_is_logged(make_client):
    c = make_client(LEF_SITE_DEV_ROLE="lead")
    head = c.head(f"/api/files/{FILE}")
    assert head.status_code == 200 and c.app.state.db.recent_downloads() == []      # HEAD 는 확인만, 기록 안 함
    r = c.get(f"/api/files/{FILE}")
    assert r.status_code == 200 and r.content.startswith(b"lap,")
    assert 'filename="laps.csv"' in r.headers["content-disposition"] and r.headers["content-type"].startswith("text/csv")
    log = c.app.state.db.recent_downloads()
    assert [(d["email"], d["file_id"], d["sid"], d["name"]) for d in log] == [("dev@localhost", FILE, "DEMO-1_2027-05-08_S1", "laps.csv")]


@pytest.mark.parametrize("file_id", ["DEMO-1_2027-05-08_S1/files/nope.csv", "DEMO-1_2027-05-08_S1/..%2F..%2Fsecret", "x"])
def test_unknown_or_odd_file_ids(client, file_id):
    r = client.get(f"/api/files/{file_id}")
    assert r.status_code == 404 and r.json()["error"]["code"] in ("unknown_file", "not_found")
