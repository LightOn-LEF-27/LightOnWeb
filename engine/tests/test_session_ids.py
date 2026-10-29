"""안 바뀌는 세션 ID (`session_ids`, 계약 §3 v0.9) — 데이터 없이 도는 보편 테스트.

앞쪽은 등록부만: 같은 주행 · 새 세션 · 쪼개기 · 합치기 · 지우기 · 되살리기 · 다른 이벤트.
뒤쪽은 저장소 · API: 없어진 sid 로 부르면 `session_retired` 와 이어지는 세션, `resolve_session`, 목록의 `day_order`, 처음 등록부를 만들 때 있던 sid 를 그대로.
"""

import json

import pandas as pd
import pytest

from lefdata import api, session_ids as ids, session_store as store
from lefdata.api import EngineError
from test_api_contract import circle_session, strict

EV = "LEF-27/2027-06-05_test"
T0 = pd.Timestamp("2027-06-05 10:00", tz="Asia/Seoul")


def runs(*spans, car="LEF-27", day="2027-06-05"):
    """(시작 분, 길이 s) 들 → assign 에 넘길 세션들."""
    return [{"car": car, "day": day, "start": T0 + pd.Timedelta(minutes=m), "duration_s": d, "title": f"주행 {m}"} for m, d in spans]


def fresh():
    return {"format": ids.FORMAT, "sessions": {}}


def S(n):
    return f"LEF-27_2027-06-05_S{n}"


def test_first_build_numbers_by_start_and_a_rebuild_keeps_them():
    reg = fresh()
    sids, gone = ids.assign(reg, EV, runs((30, 120), (0, 120), (10, 60)))
    assert sids == [S(3), S(1), S(2)] and gone == []                       # 처음엔 시작 시각 순 (지금까지의 번호와 같다)
    assert ids.assign(reg, EV, runs((0, 120), (10, 60), (30, 120))) == ([S(1), S(2), S(3)], [])


def test_a_new_session_gets_the_next_number_even_if_it_is_earlier():
    """08-29 처럼 앞쪽에 세션이 더 들어와도 이미 준 번호는 밀리지 않는다 — 새 세션이 뒤 번호."""
    reg = fresh()
    ids.assign(reg, EV, runs((10, 60), (30, 120)))
    sids, gone = ids.assign(reg, EV, runs((0, 90), (10, 60), (20, 40), (30, 120)))
    assert sids == [S(3), S(1), S(4), S(2)] and gone == []


def test_a_clock_shift_of_a_few_seconds_keeps_the_sid():
    reg = fresh()
    ids.assign(reg, EV, runs((0, 25), (1, 30)))
    moved = [{**r, "start": r["start"] + pd.Timedelta(seconds=6)} for r in runs((0, 25), (1, 30))]
    assert ids.assign(reg, EV, moved) == ([S(1), S(2)], [])


def test_split_keeps_the_sid_on_the_part_with_most_of_the_old_run():
    reg = fresh()
    ids.assign(reg, EV, runs((0, 600)))
    assert ids.assign(reg, EV, runs((0, 240), (4, 360))) == ([S(2), S(1)], [])              # 40 % · 60 % — 큰 조각이 S1
    reg = fresh()
    ids.assign(reg, EV, runs((0, 600)))
    assert ids.assign(reg, EV, runs((0, 200), (200 / 60, 200), (400 / 60, 200))) == ([S(1), S(2), S(3)], [])   # 똑같으면 앞 조각


def test_a_longer_rebuild_of_the_same_run_keeps_the_sid():
    """세션이 늘어나도(Run Sheet 끝 시각을 고침 · 주행 구간이 이어짐) 옛 구간을 다 품으면 같은 주행."""
    reg = fresh()
    ids.assign(reg, EV, runs((0, 60)))
    assert ids.assign(reg, EV, runs((0, 900))) == ([S(1)], [])


def test_merge_keeps_the_bigger_sid_and_points_the_other_to_it():
    reg = fresh()
    ids.assign(reg, EV, runs((0, 400), (7, 100)))
    sids, gone = ids.assign(reg, EV, runs((0, 520)))
    assert sids == [S(1)] and gone == [S(2)]
    assert (reg["sessions"][S(2)]["why"], reg["sessions"][S(2)]["replaced_by"]) == ("replaced", [S(1)])


def test_removed_session_comes_back_with_its_sid_and_numbers_are_never_reused():
    reg = fresh()
    ids.assign(reg, EV, runs((0, 60), (10, 60)))
    sids, gone = ids.assign(reg, EV, runs((0, 60)))
    assert gone == [S(2)] and reg["sessions"][S(2)]["why"] == "removed" and ids.resolve(reg, S(2)) == []
    assert ids.assign(reg, EV, runs((0, 60), (10, 60), (20, 60))) == ([S(1), S(2), S(3)], [])      # 되살림 · 새 번호는 3
    ids.assign(reg, EV, runs((0, 60)))
    reg["sessions"][S(3)]["why"] = "split"                                                    # removed 가 아닌 것은 되살리지 않는다
    assert ids.assign(reg, EV, runs((0, 60), (20, 60)))[0] == [S(1), S(4)]


def test_other_events_and_cars_are_left_alone():
    reg = fresh()
    ids.assign(reg, EV, runs((0, 60)))
    ids.assign(reg, "LEF-27/2027-06-05_other", runs((0, 60), car="LEF-26", day="2027-06-05"))
    sids, gone = ids.assign(reg, "LEF-27/2027-06-05_elsewhere", runs((0, 60)))           # 같은 차 · 같은 날 다른 이벤트 — 번호를 같이 센다
    assert sids == [S(2)] and gone == [] and reg["sessions"][S(1)]["status"] == ids.ACTIVE
    assert "LEF-26_2027-06-05_S1" in reg["sessions"]


def test_moved_boundaries_keep_both_sids_and_chains_resolve_to_what_exists_now():
    reg = fresh()
    ids.assign(reg, EV, runs((0, 300), (5, 300)))
    sids, gone = ids.assign(reg, EV, runs((0, 450), (7.5, 150)))                  # S1 · S2 경계가 옮겨짐 — S2 는 두 조각에 반씩
    assert sids == [S(1), S(2)] and gone == []
    sids, gone = ids.assign(reg, EV, runs((0, 450), (7.5, 75), (8.75, 75)))       # S2 (7.5 분 ~) 가 둘로 — 반씩이면 앞 조각
    assert sids == [S(1), S(2), S(3)] and gone == []
    reg = {"format": ids.FORMAT, "sessions": {
        S(1): {"status": ids.RETIRED, "why": "split", "replaced_by": [S(2), S(3)]},
        S(2): {"status": ids.ACTIVE}, S(3): {"status": ids.RETIRED, "why": "replaced", "replaced_by": [S(4)]},
        S(4): {"status": ids.ACTIVE}}}
    assert ids.resolve(reg, S(1)) == [S(2), S(4)] and ids.resolve(reg, S(3)) == [S(4)] and ids.resolve(reg, "없음") == []


# ---------------------------------------------------------------- 저장소 · API


def session_at(minutes: float, lap_s: float = 30.0):
    s = circle_session(lap_s=lap_s)
    s.start = T0 + pd.Timedelta(minutes=minutes)
    return s


@pytest.fixture()
def store_root(tmp_path):
    root = tmp_path / "store"
    api.configure(store_root=root, data_root=tmp_path)
    yield root
    api.configure()


def test_store_keeps_sids_and_answers_old_ones_with_the_new_session(store_root, tmp_path):
    store.write_event([session_at(0), session_at(10)], EV, store_root, tmp_path)
    assert [r["sid"] for r in reversed(api.list_sessions())] == [S(1), S(2)]
    store.write_event([session_at(-20), session_at(0), session_at(10)], EV, store_root, tmp_path)    # 앞쪽에 세션 하나가 더 들어왔다
    rows = list(reversed(strict(api.list_sessions())))
    assert [(r["sid"], r["day_order"]) for r in rows] == [(S(3), 1), (S(1), 2), (S(2), 3)]          # sid 는 그대로, 그날 순서만 바뀐다
    assert strict(api.get_session(S(1)))["summary"]["day_order"] == 2
    long = session_at(0, lap_s=230.0)                                                       # 0 분 세션이 10 분 세션까지 덮었다 (둘이 합쳐짐)
    store.write_event([session_at(-20), long], EV, store_root, tmp_path)
    assert not (store_root / S(2)).exists() and [r["sid"] for r in api.list_sessions()] == [S(1), S(3)]
    for call in (lambda: api.get_session(S(2)), lambda: api.get_laps(S(2)), lambda: api.get_delta(S(1), 1, S(2), 1),
                 lambda: api.open_file(f"{S(2)}/files/gps.csv")):
        with pytest.raises(EngineError) as e:
            call()
        strict(e.value.to_json())
        assert e.value.code == "session_retired" and e.value.detail["current"] == [S(1)] and e.value.detail["why"] == "replaced"
    assert strict(api.resolve_session(S(2)))["current"] == [S(1)] and api.resolve_session(S(1))["status"] == "active"
    with pytest.raises(EngineError) as e:
        api.resolve_session(S(9))
    assert e.value.code == "unknown_session"
    assert api.engine_info()["store"] == {"format": store.STORE_FORMAT, "sessions": 2, "retired_sids": 1}


def test_first_registry_takes_the_sids_already_in_the_store(store_root, tmp_path):
    """등록부가 없던 저장소(지금까지의 81세션)는 있던 sid 가 첫 목록 — 번호가 시작 순서와 달라도 그대로 남는다."""
    for sid, m in ((S(2), 0), (S(1), 10)):
        s = session_at(m)
        s.sid = sid
        store.write_session(s, store_root, tmp_path)
    store.write_index(store_root)
    store.write_event([session_at(0), session_at(10)], EV, store_root, tmp_path)
    reg = json.loads((store_root / ids.FILE).read_text(encoding="utf-8"))
    assert [r["sid"] for r in reversed(api.list_sessions())] == [S(2), S(1)] and set(reg["sessions"]) == {S(1), S(2)}


# ---------------------------------------------------------------- 우리 세션 저장소 (있을 때만)

REAL = store.DEFAULT_ROOT


@pytest.mark.skipif(not (REAL / ids.FILE).exists(), reason="세션 저장소의 등록부 없음 (lefdata sessions 전)")
def test_the_real_store_shows_exactly_the_live_registered_sessions():
    """목록 · 폴더 · 등록부가 같은 세션을 말한다 — 없어진 sid 의 폴더가 남아 옛 세션을 내주는 일이 없다."""
    reg = ids.read(REAL)
    live = {sid for sid, e in reg["sessions"].items() if e["status"] == ids.ACTIVE}
    index = json.loads((REAL / "index.json").read_text(encoding="utf-8"))["sessions"]
    assert {r["sid"] for r in index} == live == {p.parent.name for p in REAL.glob("*/session.json")}
    assert all(ids.SID_RE.match(sid) for sid in reg["sessions"])
    for e in (e for e in reg["sessions"].values() if e["status"] == ids.RETIRED):
        assert e["why"] in ("replaced", "split", "removed") and set(e["replaced_by"]) <= set(reg["sessions"])
