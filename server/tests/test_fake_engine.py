"""가짜 연결부가 계약(분석엔진_인터페이스.md)의 규칙을 지키는가 — 값이 아니라 모양 · 규칙만 본다."""

import pytest

from lefsite.engines import EngineError
from lefsite.engines.fake import RETIRED, FakeEngine

STATUSES = {"have", "warn", "seek", "none"}


@pytest.fixture
def fake():
    return FakeEngine()


def test_list_is_newest_first_and_pages(fake):
    rows = fake.list_sessions()
    starts = [r["start"] for r in rows]
    assert starts == sorted(starts, reverse=True)
    assert fake.list_sessions(limit=2, offset=1) == rows[1:3]
    assert fake.engine_info()["store"]["sessions"] == len(rows)


@pytest.mark.parametrize("kw", [{"limit": 0}, {"offset": -1}, {"limit": True}, {"limit": "3"}])
def test_bad_paging_is_bad_argument(fake, kw):
    with pytest.raises(EngineError) as e:
        fake.list_sessions(**kw)
    assert e.value.code == "bad_argument"


def test_filters_are_exact_matches(fake):
    rows = fake.list_sessions(discipline="가속")
    assert rows and all(r["discipline"] == "가속" for r in rows)
    car = rows[0]["car"]
    assert all(r["car"] == car for r in fake.list_sessions(car=car))


def test_rows_follow_the_contract_rules(fake):
    for r in fake.list_sessions():
        assert r["sid"].startswith(r["car"] + "_")
        assert r["car"].startswith("DEMO-")                      # 진짜 차와 헷갈리지 않게
        # 블록 요약은 데이터 현황 표의 요약이다
        listed = {d["block"]: d["status"] for d in r["data_status"]}
        for status, blocks in r["blocks"].items():
            assert status in STATUSES and all(listed[b] == status for b in blocks)
        # 코스 기록이 없으면 랩 · 거리 · frame 이 없다 (v0.7)
        if not r["has_track"]:
            assert r["laps"] is None and r["distance_km"] is None and r["best_lap_s"] is None and r["frame_id"] is None
        if r["laps"] == 0:
            assert r["best_lap_s"] is None


def test_day_order_is_start_order_within_car_and_day(fake):
    rows = fake.list_sessions()
    days = {}
    for r in rows:
        days.setdefault((r["car"], r["start"][:10]), []).append(r)
    for group in days.values():
        group.sort(key=lambda r: r["start"])
        assert [r["day_order"] for r in group] == list(range(1, len(group) + 1))
    # sid 의 S 번호가 그날 순서와 다른 세션이 하나는 있다 (계약 §3-1 을 화면이 보여 볼 수 있게)
    assert any(not r["sid"].endswith(f"_S{r['day_order']}") for r in rows)


def test_session_blocks_are_have_or_warn_and_missing_is_the_rest(fake):
    for row in fake.list_sessions():
        s = fake.get_session(row["sid"])
        assert s["summary"] == row
        assert {b["block"] for b in s["blocks"]} == set(row["blocks"]["have"] + row["blocks"]["warn"])
        assert {m["block"] for m in s["missing"]} == set(row["blocks"]["seek"] + row["blocks"]["none"])
        ids = {c["channel_id"] for c in fake.list_channels(row["sid"])}
        assert ids == {c for b in s["blocks"] for c in b["channels"]}


def test_unknown_and_malformed_sids(fake):
    for sid in ("DEMO-1_2099-01-01_S1", "../etc", ""):
        with pytest.raises(EngineError) as e:
            fake.get_session(sid)
        assert e.value.code == "unknown_session"


@pytest.mark.parametrize("sid", sorted(RETIRED))
def test_retired_sids_resolve_to_current_sessions(fake, sid):
    r = fake.resolve_session(sid)
    assert r["status"] == "retired" and r["sid"] == sid
    live = {x["sid"] for x in fake.list_sessions()}
    assert set(r["current"]) <= live
    assert (r["why"] == "removed") == (r["current"] == [])
    with pytest.raises(EngineError) as e:
        fake.get_session(sid)
    assert e.value.code == "session_retired" and e.value.detail["current"] == r["current"]


def test_resolve_live_sid_is_itself(fake):
    sid = fake.list_sessions(limit=1)[0]["sid"]
    assert fake.resolve_session(sid) == {"sid": sid, "status": "active", "why": None, "retired_at": None, "replaced_by": [],
                                         "current": [sid]}
