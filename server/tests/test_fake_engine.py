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


# ---------------------------------------------------------------- 랩 표 · 품질 · 파일 (F6 · F8 · F12)

def test_laps_follow_the_contract_rules(fake):
    for row in fake.list_sessions():
        laps = fake.get_laps(row["sid"])
        rows = laps["rows"]
        assert laps["best_lap"] == fake.get_session(row["sid"])["best_lap"]
        if not row["has_track"] or not row["laps"]:
            assert rows == [] and laps["sectors"] is None
            assert all(c["block"] != "gps" for c in laps["columns"]) or row["has_track"]
            continue
        assert [r["lap"] for r in rows] == list(range(1, row["laps"] + 1))
        for r in rows:
            assert abs(r["s1_s"] + r["s2_s"] + r["s3_s"] - r["lap_time_s"]) < 1e-6      # 구간 셋의 합 = 랩타임
            assert r["t_end_s"] > r["t_start_s"]
        best = [r for r in rows if r["best"]]
        assert len(best) == 1 and best[0]["lap_time_s"] == row["best_lap_s"] == min(r["lap_time_s"] for r in rows)
        sec = laps["sectors"]
        for n, k in enumerate(("s1_s", "s2_s", "s3_s"), 1):
            fastest = min(rows, key=lambda r: r[k])
            assert sec["best"][k] == {"value": fastest[k], "lap": fastest["lap"]}
            assert [r["lap"] for r in rows if n in r["best_sectors"]] == [fastest["lap"]]
        assert abs(sec["theoretical_best_s"] - sum(sec["best"][k]["value"] for k in ("s1_s", "s2_s", "s3_s"))) < 1e-6
        assert sec["n_laps"] == len(rows)


def test_lap_columns_only_for_blocks_the_session_has(fake):
    for row in fake.list_sessions():
        have = set(row["blocks"]["have"] + row["blocks"]["warn"])
        cols = fake.get_laps(row["sid"])["columns"]
        assert {c["block"] for c in cols} <= have
        for r in fake.get_laps(row["sid"])["rows"]:
            assert set(r) - {"lap", "t_start_s", "t_end_s", "best", "best_sectors"} == {c["key"] for c in cols}


def test_quality_points_at_blocks_of_the_session(fake):
    seen = 0
    for row in fake.list_sessions():
        blocks = {d["block"] for d in row["data_status"]}
        for q in fake.get_quality(row["sid"]):
            seen += 1
            assert q["level"] in ("info", "warn", "error") and q["block"] in blocks
            assert q["t_ranges"] is None or all(0 <= a < b <= row["duration_s"] for a, b in q["t_ranges"])
    assert seen >= 3


def test_every_listed_file_opens_with_its_size(fake):
    opened = 0
    for row in fake.list_sessions():
        for b in fake.get_session(row["sid"])["blocks"]:
            for f in b["raw"] + b["exports"]:
                if f["file_id"] is None:                       # 있는 곳만 적힌 원본
                    assert f["where"]
                    continue
                got = fake.open_file(f["file_id"])
                assert got["name"] == f["name"] and got["size_bytes"] == f["size_bytes"]
                opened += 1
    assert opened > 10


@pytest.mark.parametrize("file_id", ["DEMO-1_2027-05-08_S1/files/nope.csv", "DEMO-1_2027-05-08_S1/../x", "nofile", "../etc/passwd"])
def test_unknown_files(fake, file_id):
    with pytest.raises(EngineError) as e:
        fake.open_file(file_id)
    assert e.value.code == "unknown_file"


# ---------------------------------------------------------------- 그래프 값 (F5)

S4 = "DEMO-1_2027-05-08_S4"


def test_series_shape_and_ranges(fake):
    r = fake.get_series(S4, ["gps.speed_kmh", "suspension.rr_mm"], x="time", max_points=800)
    xs = r["x"]["values"]
    assert r["x"]["kind"] == "time" and len(xs) == 800 and xs == sorted(xs)
    assert all(len(s["values"]) == len(s["y_min"]) == len(s["y_max"]) == len(xs) for s in r["series"])
    for s in r["series"]:
        for v, lo, hi in zip(s["values"], s["y_min"], s["y_max"]):
            assert (v is None) == (lo is None) == (hi is None)
            assert v is None or lo <= v <= hi                    # 줄여도 최소 · 최대 안에
    # 품질 경고 구간(t_ranges)의 블록은 비어 있다 — 화면이 선을 끊는다
    gap = next(q for q in fake.get_quality(S4) if q["block"] == "suspension")["t_ranges"][0]
    sus = r["series"][1]["values"]
    assert any(v is None for x, v in zip(xs, sus) if gap[0] <= x <= gap[1])
    assert all(v is not None for v in r["series"][0]["values"])


def test_series_lap_and_distance(fake):
    laps = fake.get_laps(S4)["rows"]
    r = fake.get_series(S4, ["gps.speed_kmh"], x="time", lap=3)
    assert r["laps"] == [{"lap": 3, "x_start": laps[2]["t_start_s"], "x_end": laps[2]["t_end_s"]}]
    assert laps[2]["t_start_s"] <= r["x"]["values"][0] and r["x"]["values"][-1] <= laps[2]["t_end_s"]
    d = fake.get_series(S4, ["gps.speed_kmh"], x="distance", lap=3, d0=100, d1=300)
    assert d["x"]["frame_id"] and 100 <= d["x"]["values"][0] and d["x"]["values"][-1] <= 300


@pytest.mark.parametrize("kw, code", [
    ({"channel_ids": ["nope.x"]}, "unknown_channel"),
    ({"channel_ids": ["gps.speed_kmh"], "x": "distance"}, "bad_argument"),            # 거리축은 랩이 있어야
    ({"channel_ids": ["gps.speed_kmh"], "lap": 99}, "bad_argument"),
    ({"channel_ids": ["gps.speed_kmh"], "t0": 1, "d0": 2}, "bad_argument"),
    ({"channel_ids": ["gps.speed_kmh"], "max_points": 1}, "bad_argument"),
    ({"channel_ids": []}, "bad_argument"),
])
def test_series_bad_calls(fake, kw, code):
    with pytest.raises(EngineError) as e:
        fake.get_series(S4, **kw)
    assert e.value.code == code


# ---------------------------------------------------------------- 코스 · 구간 · 시간 차 (F7 · F13 · F14)

def test_track_points_line_up(fake):
    t = fake.get_track(S4, lap=3, color="gps.speed_kmh")
    n = len(t["x_m"])
    assert n > 100 and all(len(t[k]) == n for k in ("y_m", "t_s", "dist_m", "ref_m")) and len(t["color"]["values"]) == n
    assert t["t_s"] == sorted(t["t_s"]) and t["ref_m"][0] == 0 and t["lap"] == 3
    row = fake.get_laps(S4)["rows"][2]
    assert t["t_s"][0] == row["t_start_s"] and abs(t["t_s"][-1] - row["t_end_s"]) < 0.01      # 첫 점 · 끝 점 = 계측선
    assert fake.get_track(S4)["lap"] == fake.get_session(S4)["best_lap"]                        # 안 주면 최속 랩
    whole = fake.get_track(S4, lap="all")
    assert whole["lap"] is None and whole["ref_m"] is None and len(whole["x_m"]) > n


def test_track_needs_a_course(fake):
    with pytest.raises(EngineError) as e:
        fake.get_track("DEMO-2_2026-11-14_S2")                                                 # 시각 구간 세션
    assert e.value.code == "bad_argument"


def test_sectors_agree_with_the_lap_table(fake):
    sec = fake.get_sectors(S4, mini_m=25)
    rows = fake.get_laps(S4)["rows"]
    for L, r in zip(sec["laps"], rows):
        assert L["sector_s"] == [r["s1_s"], r["s2_s"], r["s3_s"]]
        assert abs(sum(L["mini_s"]) - r["lap_time_s"]) < 0.01                                   # 미니 구간 합 = 랩타임
    assert len(sec["mini"]["edges_ref_m"]) == sec["mini"]["count"] + 1
    assert sec["best"]["mini_s"] == [min(L["mini_s"][i] for L in sec["laps"]) for i in range(sec["mini"]["count"])]
    with pytest.raises(EngineError):
        fake.get_sectors(S4, mini_m=30)
    assert fake.get_sectors("DEMO-1_2027-04-24_S1")["laps"] == []                               # 랩이 없는 세션


def test_delta_rules(fake):
    rows = {r["lap"]: r for r in fake.get_laps(S4)["rows"]}
    same = fake.get_delta(S4, 3, S4, 3)
    assert set(same["delta_s"]) == {0} and same["total_s"] == 0
    d = fake.get_delta(S4, 3, S4, 12)
    assert d["delta_s"][0] == 0 and abs(d["delta_s"][-1] - d["total_s"]) < 0.002
    assert d["total_s"] == round(rows[3]["lap_time_s"] - rows[12]["lap_time_s"], 3)
    assert len(d["on_track"]["values"]) == len(fake.get_track(S4, lap=3)["x_m"])
    assert abs(sum(d["mini"]["delta_s"]) - d["total_s"]) < 0.01
    assert fake.get_delta("DEMO-2_2026-11-14_S1", 2, S4, 12)["frame_id"] == "venue/demo-circuit"   # 같은 경기장이면 다른 차 랩끼리도
    with pytest.raises(EngineError) as e:
        fake.get_delta("DEMO-1_2027-04-24_S3", 1, S4, 12)
    assert e.value.code == "frame_mismatch"
