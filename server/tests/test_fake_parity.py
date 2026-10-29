"""가짜 연결부 ↔ 진짜 엔진의 모양 맞대기 — 키 이름만 본다 (값은 보지 않는다).

엔진(lefdata)과 세션 저장소가 있는 PC 에서만 돈다 (없으면 건너뛴다). 엔진이 계약에 필드를 더하면 여기서 깨진다 → 가짜에도 더한다.
"""

import pytest

from lefsite.engines.fake import FakeEngine

try:
    from lefsite.engines.real import RealEngine
    real = RealEngine()
    rows = real.list_sessions()
except Exception:   # 엔진이 없거나 저장소가 비었다
    real, rows = None, []

pytestmark = pytest.mark.skipif(not rows, reason="엔진 · 세션 저장소 없음 (데이터가 있는 PC 에서만)")


@pytest.fixture(scope="module")
def fake():
    return FakeEngine()


def keys(dicts) -> set[str]:
    return set().union(*(d.keys() for d in dicts))


def test_same_contract_version(fake):
    assert fake.engine_info()["contract"] == real.engine_info()["contract"]


def test_engine_info(fake):
    a, b = fake.engine_info(), real.engine_info()
    assert a.keys() == b.keys() and a["store"].keys() == b["store"].keys() and a["engine"].keys() == b["engine"].keys()


def test_session_rows(fake):
    f = fake.list_sessions()
    assert keys(f) == keys(rows)
    assert keys(d for r in f for d in r["data_status"]) == keys(d for r in rows for d in r["data_status"])
    assert keys(r["blocks"] for r in f) == keys(r["blocks"] for r in rows)
    assert keys(r["stats"] for r in f) <= keys(r["stats"] for r in rows)
    assert {r["split_by"] for r in f} <= {"gps", "time"}


def test_session_page(fake):
    sample = [rows[0]["sid"]] + [r["sid"] for r in rows if not r["has_track"]][:1] + [r["sid"] for r in rows if r["laps"] == 0][:1]
    real_pages = [real.get_session(sid) for sid in sample]
    fake_pages = [fake.get_session(r["sid"]) for r in fake.list_sessions()]
    assert keys(fake_pages) == keys(real_pages)
    assert keys(b for p in fake_pages for b in p["blocks"]) == keys(b for p in real_pages for b in p["blocks"])
    assert keys(m for p in fake_pages for m in p["missing"]) == keys(m for p in real_pages for m in p["missing"])
    assert keys(p["made"] for p in fake_pages) == keys(p["made"] for p in real_pages)
    real_views = keys(b["default_views"] for p in real_pages for b in p["blocks"])
    assert keys(b["default_views"] for p in fake_pages for b in p["blocks"]) == real_views


def test_channels(fake):
    f = [c for r in fake.list_sessions() for c in fake.list_channels(r["sid"])]
    g = [c for r in rows[:5] for c in real.list_channels(r["sid"])]
    optional = {"levels"}            # 정해진 값만 갖는 채널에만 (v0.5)
    assert keys(f) - optional == keys(g) - optional
    assert keys(c["source"] for c in f) == keys(c["source"] for c in g)


def test_resolve(fake):
    sid = fake.list_sessions()[0]["sid"]
    assert fake.resolve_session(sid).keys() == real.resolve_session(rows[0]["sid"]).keys()
