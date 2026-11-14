"""짐카나 · 스키드패드 8자 (`lefdata.events`) — 만든 GPS 궤적으로 (데이터 없이).

짐카나: 입구에서 몇 초 섰다가 출발해 콘 코스를 돌고 같은 자리로 돌아와 선다. 스키드패드: 입구에서 섰다가 우 2바퀴 · 좌 2바퀴 · 밖으로.
궤적은 0.1 s 간격의 위치로 만들고, 속도 · 진행 방위는 위치의 변화에서 낸다 (GPS 처럼).
"""

import json

import numpy as np
import pandas as pd
import pytest

from lefdata import catalog_build as cb, events, laps

DT, LAT0, LON0 = 0.1, 37.5, 127.0
T0 = pd.Timestamp("2027-06-05 10:00:00", tz="Asia/Seoul")
R8 = 9.0                                                    # 8자 원의 반지름 (FSAE 중심선 근처)


def stop(p, s):
    return np.repeat([np.asarray(p, float)], int(round(s / DT)), axis=0)


def line(p, q, v0, v1):
    """p → q, 속도 v0 → v1 (m/s, 시간에 따라 고르게)."""
    p, q = np.asarray(p, float), np.asarray(q, float)
    L = float(np.hypot(*(q - p)))
    T = 2 * L / (v0 + v1)
    tau = np.arange(1, max(2, int(T / DT)) + 1) * DT
    s = np.clip(v0 * tau + (v1 - v0) / (2 * T) * tau ** 2, 0, L)
    return p + s[:, None] / L * (q - p)


def arc(c, th0, turns, sgn, v0, r0=R8, r1=None, v1=None):
    """중심 c 둘레를 각도 th0 부터 turns 바퀴 (sgn +1 반시계 · −1 시계). 반지름 r0 → r1, 속도 v0 → v1."""
    r1, v1 = r0 if r1 is None else r1, v0 if v1 is None else v1
    T = 2 * np.pi * (r0 + r1) / 2 * turns / ((v0 + v1) / 2)
    n = max(2, int(T / DT))
    f = np.arange(1, n + 1) / n
    vel = v0 + (v1 - v0) * f
    frac = np.cumsum(vel) / vel.sum()                       # 속도가 바뀌면 같은 시간에 도는 각도도 바뀐다
    th, r = th0 + sgn * 2 * np.pi * turns * frac, r0 + (r1 - r0) * frac
    return np.column_stack([c[0] + r * np.cos(th), c[1] + r * np.sin(th)])


def gps(xy) -> pd.DataFrame:
    xy = np.asarray(xy, float)
    t = np.arange(len(xy)) * DT
    d = np.gradient(xy, axis=0) / DT
    return pd.DataFrame({"t_rel_ms": np.round(t * 1000).astype("int64"), "t_abs": T0 + pd.to_timedelta(t, unit="s"), "fix": True,
                         "lat": LAT0 + xy[:, 1] / 110_950.0, "lon": LON0 + xy[:, 0] / (np.cos(np.radians(LAT0)) * 111_320.0),
                         "speed_kmh": np.hypot(d[:, 0], d[:, 1]) * 3.6, "course_deg": np.degrees(np.arctan2(d[:, 0], d[:, 1])) % 360.0})


def figure_eight(drift=True):
    """입구에서 5 s 정지 → 교차점(0, 0) 으로 → 우 2바퀴 (중심 (9, 0)) → 좌 2바퀴 (중심 (−9, 0)) → 나감.
    drift = 마지막 원을 나가며 제동해 바깥으로 밀린다 (계측선을 원에서 3 m 넘게 벗어나 지난다 — 예전 코드가 2바퀴째를 놓치던 모양)."""
    parts = [stop((0, -12), 5), line((0, -12), (0, 0), 0.0, 10.0), arc((R8, 0), np.pi, 2, -1, 10.0)]
    if drift:
        parts += [arc((-R8, 0), 0.0, 1.9, +1, 10.0), arc((-R8, 0), 1.9 * 2 * np.pi, 0.2, +1, 10.0, r1=R8 + 5, v1=3.0)]
    else:
        parts += [arc((-R8, 0), 0.0, 2, +1, 10.0)]
    end = parts[-1][-1]
    th = np.arctan2(end[1], end[0] + R8)
    out = end + 12 * np.array([-np.sin(th), np.cos(th)])
    return np.concatenate(parts + [line(end, out, 3.0 if drift else 10.0, 0.5), stop(out, 5)])


def loop(start=(300.0, 0.0), back=8.0, donut=False):
    """짐카나 — start 에서 5 s 정지 → 동쪽 60 m → 반원 → 서쪽 60 m (donut 이면 가운데서 반경 6 m 로 한 바퀴) → 반원 → 동쪽으로 back m 가서 섬.
    (곧은 길을 75 m 넘게 두지 않는다 — 정지에서 75 m 곧게 가속하면 가속 시도로 본다)"""
    x0, y0 = start
    parts = [stop(start, 5), line(start, (x0 + 60, y0), 0.0, 10.0), arc((x0 + 60, y0 + 10), -np.pi / 2, 0.5, +1, 10.0, r0=10)]
    if donut:
        parts += [line((x0 + 60, y0 + 20), (x0 + 30, y0 + 20), 10.0, 8.0), arc((x0 + 30, y0 + 14), np.pi / 2, 1.0, +1, 8.0, r0=6),
                  line((x0 + 30, y0 + 20), (x0, y0 + 20), 8.0, 10.0)]
    else:
        parts += [line((x0 + 60, y0 + 20), (x0, y0 + 20), 10.0, 10.0)]
    parts += [arc((x0, y0 + 10), np.pi / 2, 0.5, +1, 10.0, r0=10), line((x0, y0), (x0 + back, y0), 10.0, 0.5), stop((x0 + back, y0), 10)]
    return np.concatenate(parts)


# ---------------------------------------------------------------- 스키드패드

@pytest.mark.parametrize("drift", [True, False])
def test_figure_eight_counts_two_laps_per_circle_at_the_crossing(drift):
    g = gps(figure_eight(drift))
    lp, arcs = events.skidpad_laps(g, LAT0, LON0, 0.0, g["t_rel_ms"].iloc[-1] / 1000.0)
    assert lp.groupby("turn").size().to_dict() == {"left": 2, "right": 2}
    used = arcs[arcs["used"]]
    assert set(used["lap_from"]) == {"crossing"} and set(used["laps"]) == {2}
    lap = 2 * np.pi * R8 / 10.0
    right = lp[lp["turn"] == "right"]["lap_time_s"].to_numpy()
    assert right == pytest.approx([lap, lap], abs=0.1)                      # 바퀴 경계 = 교차점 — 정지 출발은 첫 바퀴 앞에서 끝났다
    left = lp[lp["turn"] == "left"]["lap_time_s"].to_numpy()
    assert left[0] == pytest.approx(lap, abs=0.1) and (left[1] > lap + 0.2 if drift else left[1] == pytest.approx(lap, abs=0.1))


def test_a_single_circle_without_a_partner_counts_from_the_arc_start():
    g = gps(np.concatenate([stop((0, -12), 3), line((0, -12), (0, 0), 0.0, 10.0), arc((R8, 0), np.pi, 2.3, -1, 10.0)]))
    lp, arcs = events.skidpad_laps(g, LAT0, LON0, 0.0, g["t_rel_ms"].iloc[-1] / 1000.0)
    assert len(lp) == 2 and set(arcs[arcs["used"]]["lap_from"]) == {"arc_start"}


# ---------------------------------------------------------------- 짐카나

def test_stop_go_and_back_to_the_same_place_is_a_gymkhana():
    xy = loop()
    rows = events.gymkhana_runs(gps(xy), LAT0, LON0)
    assert len(rows) == 1
    r = rows.iloc[0]
    v = gps(xy)["speed_kmh"].to_numpy()
    moving = np.flatnonzero(v > events.STOP_KMH)
    assert r["time_s"] == pytest.approx((moving[-1] - moving[0] + 2) * DT, abs=0.25)   # 2 km/h 를 넘은 첫 표본 앞 ~ 마지막 표본 뒤
    assert r["return_gap_m"] == pytest.approx(8.0, abs=1.0) and r["distance_m"] == pytest.approx(60 + 31.4 + 60 + 31.4 + 8, rel=0.05)
    assert r["stop_before_s"] == pytest.approx(5.0, abs=0.6) and r["v_max_kmh"] == pytest.approx(36.0, abs=1.0)


def test_moving_the_car_or_stopping_elsewhere_is_not_a_gymkhana():
    pit = np.concatenate([stop((0, 0), 5), line((0, 0), (200, 0), 0.0, 15.0), line((200, 0), (220, 0), 15.0, 0.5), stop((220, 0), 5)])
    assert events.gymkhana_runs(gps(pit), LAT0, LON0).empty
    assert events.gymkhana_runs(gps(loop(back=30.0)), LAT0, LON0).empty               # 30 m 앞에서 섬 — 같은 자리가 아니다


# 짐카나 끝 자리 → 스키드패드 입구: 피트 이동처럼 천천히 (21.6 km/h) 두 번 꺾어서 — 가속 시도로 보이지 않게
TRANSFER = np.concatenate([line((308, 0), (308, -40), 0.6, 6.0), line((308, -40), (0, -40), 6.0, 6.0), line((0, -40), (0, -12), 6.0, 0.5)])


def _event(tmp_path, xy):
    ev = tmp_path / "LEF-27" / "2027-06-05_test"
    rec = ev / "normalized" / "2027-06-05_gps"
    rec.mkdir(parents=True)
    g = gps(xy)
    g.to_parquet(rec / "gps.parquet", index=False)
    (rec / "meta.json").write_text(json.dumps({"car": "LEF-27", "label": "practice", "source": {"logger": "GoPro 온보드 영상 (GPMF)", "file": "raw/x.mp4"},
                                               "time": {"t_abs_start": T0.isoformat(), "duration_s": float(len(g) * DT)}}, ensure_ascii=False), encoding="utf-8")
    return ev


def test_a_donut_inside_a_gymkhana_keeps_it_a_gymkhana_and_a_figure_eight_is_not_one(tmp_path):
    """짐카나 안의 한 바퀴 선회는 스키드패드가 아니다 (원 2바퀴 미만). 8자는 짐카나 표에 들어가지 않는다 (같은 자리로 돌아와 서도)."""
    eight = figure_eight(drift=False)
    back = np.concatenate([line(eight[-1], (0, -12), 2.0, 0.5), stop((0, -12), 10)])      # 8자를 돌고 입구로 돌아와 섬
    ev = _event(tmp_path, np.concatenate([loop(donut=True), TRANSFER, stop((0, -12), 12), eight, back]))
    laps.run_event(ev)
    events.run_event(ev)
    gym = pd.read_csv(ev / "derived" / "gymkhana_runs.csv", encoding="utf-8-sig")
    skid = pd.read_csv(ev / "derived" / "skidpad_laps.csv", encoding="utf-8-sig")
    assert len(gym) == 1 and gym["t_end_s"].iloc[0] < 60                        # 짐카나 하나 (8자 주행은 빠짐)
    assert (skid["t_end_s"] < 60).sum() == 1 and (skid["t_end_s"] > 60).sum() == 4   # 짐카나 안 선회 1바퀴 · 8자 4바퀴


def test_sessions_name_the_gymkhana_and_note_its_time(tmp_path):
    """세션 — 원 2바퀴 미만인 정지 → 같은 자리 정지 주행은 짐카나, 기록은 메모에. 8자는 스키드패드."""
    eight = figure_eight(drift=False)
    ev = _event(tmp_path, np.concatenate([loop(donut=True), TRANSFER, stop((0, -12), 12), eight]))
    laps.run_event(ev)
    events.run_event(ev)
    (ev / cb.NOTES_NAME).write_text('[event]\ncar = "LEF-27"\nsessions = "auto"\n', encoding="utf-8")
    report = {}
    sessions = cb.build_event(ev, report)
    kinds = [s.discipline for s in sessions]
    assert kinds == ["짐카나", "스키드패드"], (kinds, report.get("dropped_runs"))
    gym = pd.read_csv(ev / "derived" / "gymkhana_runs.csv", encoding="utf-8-sig").iloc[0]
    assert any(n.startswith(f"짐카나 {gym['time_s']:.2f} s") for n in sessions[0].notes)
