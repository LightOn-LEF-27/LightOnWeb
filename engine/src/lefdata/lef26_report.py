"""LEF-26 의 하루치 데이터를 읽어 시험 주행 보고서 자료(`report.DayReport`)를 채운다.

LEF-26 은 장비가 둘이다 — 자작 로거(전류 · 배터리 · 서스펜션 · 가속도)와 온보드 영상(GPS). 그래서
    주행 구간      로거 가속도계로 찾는다 (영상이 없는 주행도 로거에는 남아 있다)
    랩 · 속도      영상 GPS (`laps.csv` · `lap_logger.csv`)
    그 밖의 값     로거
로 나눠 읽는다. LEF-27 은 로거 하나에 GPS 까지 있을 것이므로 이 파일에 해당하는 것을 새로 쓰고,
`report.py` · `report_charts.py` 는 그대로 쓴다.

먼저 `lefdata lef26` · `gopro --laps` · `events` · `lef26-clock` · `lef26-laps` 가 끝나 있어야 한다.
"""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pandas as pd
import pyarrow.parquet as pq

from . import laps as lapmod, lef26_clock as lc, lef26_laps as ll, sync
from .report import BAD, INFO, OK, WARN, DayReport, Session, load_notes
from .writer import TZ

DRIVE_LAT_G = 0.12          # 10 초 평균 |횡가속도| 가 이보다 크면 달리는 중 (정지 · 직진 대기와 갈린다)
DRIVE_SMOOTH_S = 10
DRIVE_MERGE_S = 30          # 이보다 짧은 끊김은 같은 주행
DRIVE_MIN_S = 20
THERM_MIN_ROWS = 3          # 써미스터별 온도 차를 낼 때 필요한 최소 표본 수
GG_STEP_S = 0.1
HV_EDGE_S = 5.0             # 고전압이 켜지고 꺼지는 앞뒤 이만큼은 팩 전압 통계에서 뺀다 (충전 · 방전 중인 과도 구간)
KM_PER_TURN_FALLBACK = 0.95
SIGNAL_NAMES = [("imu", "가속도 (IMU)"), ("lin", "서스펜션"), ("bms", "BMS"), ("therm", "써미스터"),
                ("n_act_pct", "모터 속도"), ("iq_act_a", "모터 전류"), ("vdc_v", "팩 전압"),
                ("t_motor_c", "모터 온도"), ("t_igbt_c", "인버터 온도")]
FRESH_ONLY = {"n_act_pct", "iq_act_a", "vdc_v"}      # 빠르게 변하는 신호는 '새 값' 이 온 칸만 센다


def _ts(epoch_s) -> pd.Timestamp:
    return pd.Timestamp(float(epoch_s), unit="s", tz="UTC").tz_convert(TZ)


def driving_segments(t_s: np.ndarray, lat_g: np.ndarray) -> list[tuple[float, float]]:
    """가속도계로 '차가 달리고 있던' 구간을 찾는다 → [(시작, 끝)] (로거 경과시간, 초).

    트랙을 돌면 횡가속도가 계속 출렁인다. 1 초 평균을 낸 뒤 10 초 동안의 평균 |횡가속도| 로 가린다.
    직선만 달리는 주행(가속 종목)은 이 방법으로 안 잡힌다.
    """
    if len(t_s) < 100:
        return []
    k = np.floor(t_s).astype(int)
    n = int(k.max()) + 1
    count = np.bincount(k, minlength=n)
    with np.errstate(invalid="ignore", divide="ignore"):
        lat = np.bincount(k, weights=lat_g, minlength=n) / count
    act = pd.Series(np.abs(lat - np.nanmedian(lat))).rolling(DRIVE_SMOOTH_S, center=True, min_periods=1).mean().to_numpy()
    idx = np.flatnonzero(act > DRIVE_LAT_G)
    if not len(idx):
        return []
    br = np.flatnonzero(np.diff(idx) > DRIVE_MERGE_S)
    starts, ends = np.concatenate([[idx[0]], idx[br + 1]]), np.concatenate([idx[br], [idx[-1]]])
    return [(float(a), float(b + 1)) for a, b in zip(starts, ends) if b - a >= DRIVE_MIN_S]


class _Rec:
    """로거 레코딩 하나와, 로거 경과시간 ↔ 시각 변환."""

    def __init__(self, rec_dir: Path, cm, win_true):
        self.name = rec_dir.name
        self.log = ll.read_logger(rec_dir, cm, win_true)
        imu = pq.read_table(rec_dir / "imu.parquet", columns=["t_rel_ms", "t_abs", "yaw_dps"]).to_pandas()
        self.yaw = imu["yaw_dps"].to_numpy()
        have = imu["t_abs"].notna().to_numpy()
        self.rel = imu["t_rel_ms"].to_numpy()[have] / 1000.0
        self.abs = lc._epoch_s(imu["t_abs"][have]) if have.any() else np.array([])
        order = np.argsort(self.rel, kind="stable")
        self.rel, self.abs = self.rel[order], self.abs[order]
        th = pq.read_table(rec_dir / "thermistor.parquet",
                           columns=["t_rel_ms", "source", "thermistor_id", "temp_c", "lowest_c", "highest_c"]).to_pandas()
        self.single = th[th["source"] == "single"].assign(t_s=lambda d: d["t_rel_ms"] / 1000.0)
        self.vdc_steady, self.hv_off = _hv_steady(self.log.motor.get("vdc_v"))

    @property
    def timed(self) -> bool:
        return self.log.cm is not None or len(self.abs) > 0

    def to_abs(self, t_rel):
        """로거 경과시간 → 시각(UTC 초). 영상에 맞춘 지도가 있으면 그것을, 없으면 로거의 시각을 쓴다."""
        if self.log.cm is not None:
            return self.log.cm.to_true(t_rel)
        return np.interp(np.asarray(t_rel, float), self.rel, self.abs)

    def to_rel(self, t_abs):
        if self.log.cm is not None:
            return self.log.cm.to_rel(t_abs)
        return np.interp(np.asarray(t_abs, float), self.abs, self.rel)

    def turns(self, r0: float, r1: float) -> float:
        """요레이트를 누적한 회전수. 폐회로 한 바퀴가 1."""
        m = (self.log.imu_t >= r0) & (self.log.imu_t < r1)
        t = self.log.imu_t[m]
        if len(t) < 2:
            return float("nan")
        _, y = sync.grid(t - r0, self.yaw[m], 1.0)
        return float(np.nansum(y) / 360.0)


def _hv_steady(vdc: pd.DataFrame | None):
    """팩 전압 표본 중 고전압이 안정적으로 켜져 있던 것만 → (표, 꺼진 시각들[로거 경과시간]).

    켜지는 순간(프리차지)과 꺼지는 순간(방전)에는 전압이 0 과 팩 전압 사이를 지나간다. 그 값이 '부하에 의한 전압 강하' 로
    읽히지 않게, 꺼져 있던 표본의 앞뒤 HV_EDGE_S 는 뺀다.
    """
    if vdc is None or vdc.empty:
        return None, np.array([])
    d = vdc.sort_values("t_s")
    t, on = d["t_s"].to_numpy(), d["value"].to_numpy() > ll.HV_ON_V
    t_off = t[~on]
    if len(t_off):
        i = np.searchsorted(t_off, t)
        near = np.minimum(np.abs(t - t_off[np.clip(i - 1, 0, len(t_off) - 1)]), np.abs(t_off[np.clip(i, 0, len(t_off) - 1)] - t))
        keep = on & (near > HV_EDGE_S)
    else:
        keep = on
    falls = t[1:][on[:-1] & ~on[1:]]                       # 켜져 있다가 꺼진 순간
    if len(falls):                                         # 같은 꺼짐에서 값이 문턱을 오르내린 것은 하나로
        falls = falls[np.concatenate([[True], np.diff(falls) > 2 * HV_EDGE_S])]
    return d[keep], falls


def _second_series(t_abs_s: np.ndarray, values: np.ndarray, how: str) -> pd.DataFrame:
    """불규칙한 표본 → 1 초 간격 (t, v). 표본이 없는 초는 NaN 으로 남겨 선이 끊기게 한다."""
    if not len(t_abs_s):
        return pd.DataFrame({"t": pd.Series(dtype=f"datetime64[ns, {TZ}]"), "v": pd.Series(dtype=float)})
    k = np.floor(t_abs_s).astype(np.int64)
    g = pd.Series(values, dtype=float).groupby(k).agg(how)
    full = g.reindex(np.arange(k.min(), k.max() + 1))
    return pd.DataFrame({"t": pd.to_datetime(full.index, unit="s", utc=True).tz_convert(TZ), "v": full.to_numpy()})


def _gap_row() -> pd.DataFrame:
    return pd.DataFrame({"t": pd.Series([pd.NaT], dtype=f"datetime64[ns, {TZ}]"), "v": [np.nan]})


def _coverage(t_s, r0: float, r1: float) -> float:
    n = max(int(np.ceil(r1 - r0)), 1)
    t = np.asarray(t_s, float)
    k = np.floor(t[(t >= r0) & (t < r1)] - r0).astype(int)
    return float(len(np.unique(k)) / n)


def build_day(event_dir: Path, day: str, car: str = "LEF-26") -> DayReport:
    norm, derived = event_dir / "normalized", event_dir / "derived"
    notes = load_notes(event_dir, day)
    rep = DayReport(car=car, day=day, place=notes.get("meta", {}).get("place", ""), notes=notes)

    index = pd.read_csv(norm / "recordings.csv", encoding="utf-8-sig")
    index = index[index["recording_id"].str.startswith(day)].reset_index(drop=True)
    videos = pd.read_csv(norm / "recordings_video.csv", encoding="utf-8-sig", dtype={"imu_shift_s": str})
    videos = videos[videos["recording_id"].str.startswith(day)].reset_index(drop=True)
    windows = pd.read_csv(derived / "logger_clock_windows.csv", encoding="utf-8-sig")
    pairs = pd.read_csv(derived / "logger_clock_pairs.csv", encoding="utf-8-sig")
    pairs = pairs[pairs["accepted"]]
    lap_log = pd.read_csv(derived / "lap_logger.csv", encoding="utf-8-sig")
    lap_log = lap_log[lap_log["recording_id"].str.startswith(day)]
    laps_csv = pd.read_csv(derived / "laps.csv", encoding="utf-8-sig")
    runs = pd.read_csv(derived / "runs.csv", encoding="utf-8-sig")
    runs = runs[runs["recording_id"].str.startswith(day)]
    gate = json.loads((derived / "gate.json").read_text(encoding="utf-8"))
    drivers = notes.get("drivers", {})

    recs: dict[str, _Rec] = {}
    for rid in index["recording_id"]:
        cm = lc.clock_map(windows, pairs, rid)
        recs[rid] = _Rec(norm / rid, cm, windows.loc[windows["recording_id"] == rid, "true_s"].to_numpy())
    vids = {v: ll.read_video(norm / v) for v in videos["recording_id"]}

    # ---- 그날 장비가 기록한 구간 (일정표)
    lanes = []
    for rid, r in recs.items():
        if r.timed and len(r.log.imu_t):
            a, b = (float(x) for x in r.to_abs([r.log.imu_t[0], r.log.imu_t[-1]]))
            lanes.append({"lane": "로거", "name": rid, "start": _ts(a), "end": _ts(b)})
    for v, vid in vids.items():
        lanes.append({"lane": "영상", "name": v, "start": _ts(vid.t_true[0]), "end": _ts(vid.t_true[-1])})
    rep.recordings = pd.DataFrame(lanes)

    # ---- 주행 구간: 로거 가속도계로 찾고, 영상 GPS 주행과 겹치면 랩 기록이 있는 구간이다
    gps_runs = []
    for _, g in runs.iterrows():
        vid = vids[g["recording_id"]]
        a, b = (float(x) for x in vid.to_true([g["t_start_s"], g["t_end_s"]]))
        gps_runs.append({"video": g["recording_id"], "run": int(g["run"]), "a": a, "b": b, "row": g})
    found = []
    for rid, r in recs.items():
        if not r.timed:
            continue
        for r0, r1 in driving_segments(r.log.imu_t, -r.log.lat_left_g):
            a, b = (float(x) for x in r.to_abs([r0, r1]))
            hit = [g for g in gps_runs if min(b, g["b"]) - max(a, g["a"]) > 0.5 * (g["b"] - g["a"])]
            found.append((a, b, r, hit[0] if hit else None))
    found.sort(key=lambda x: x[0])

    off_by_rec = lap_log.groupby("logger_recording")[["imu_long_off_g", "imu_lat_off_g"]].median()
    for i, (a, b, r, g) in enumerate(found, 1):
        if g is not None:                                   # 영상 GPS 가 본 주행 구간이 경계다
            a, b = g["a"], g["b"]
        r0, r1 = (float(x) for x in r.to_rel([a, b]))
        off_long, off_lat = (off_by_rec.loc[r.name] if r.name in off_by_rec.index else (0.0, 0.0))
        st = {**ll.imu_stats(r.log, r0, r1), **ll.lin_stats(r.log, r0, r1), **ll.battery_stats(r.log, r0, r1),
              **ll.motor_stats(r.log, None, r0, r1), "duration_s": b - a, "turns": round(r.turns(r0, r1), 2)}
        v = ll._cut(r.vdc_steady, r0, r1)
        st.update({"vdc_min_v": round(float(v["value"].min()), 1), "vdc_max_v": round(float(v["value"].max()), 1)} if v is not None and len(v)
                  else {"vdc_min_v": None, "vdc_max_v": None})
        _debias(st, off_long, off_lat)
        s = Session(key=f"S{i}", kind="gps" if g is not None else "logger", start=_ts(a), end=_ts(b), recording=r.name, stats=st)
        if g is not None:
            s.video, s.driver = g["video"], drivers.get(g["video"])
            lp = lap_log[(lap_log["recording_id"] == g["video"]) & (lap_log["run"] == g["run"])].reset_index(drop=True).copy()
            lp["long_g_max"] -= lp["imu_long_off_g"]
            lp["long_g_min"] -= lp["imu_long_off_g"]
            lp["lat_g_left_max"] -= lp["imu_lat_off_g"]
            lp["lat_g_right_max"] += lp["imu_lat_off_g"]
            s.laps = lp
            st.update({"laps_n": len(lp), "distance_km": float(g["row"]["distance_m"]) / 1000.0,
                       "vmax_kmh": float(g["row"]["vmax_kmh"])})
            if len(lp):
                best = lp.loc[lp["lap_time_s"].idxmin()]
                st["best_lap_s"] = float(best["lap_time_s"])
                s.best_lap_no = int(best["lap"])
                src = laps_csv[(laps_csv["recording_id"] == g["video"]) & (laps_csv["run"] == g["run"])
                               & (laps_csv["lap"] == best["lap"])].iloc[0]
                s.best_lap = _lap_trace(norm / g["video"], gate, r, src, float(best["gps_lag_s"]), off_long, off_lat)
        rep.sessions.append(s)
        rep.gg[s.key] = _gg(r, r0, r1, off_long, off_lat)
        if r.log.lin is not None:
            d = ll._cut(r.log.lin, r0, r1)
            rep.susp[s.key] = pd.DataFrame({"rl_mm": d["lin_rl_mm"].to_numpy(), "rr_mm": d["lin_rr_mm"].to_numpy()})

    _hv_events(rep, recs, vids)
    range_facts = _range_to_empty(rep, recs)
    _series(rep, recs)
    _thermistor_snapshots(rep, recs)
    _coverage_table(rep, recs)
    _quality(rep, recs, index, videos, lap_log)
    _kpis_and_facts(rep)
    rep.facts.extend(range_facts)
    rep.sources = [f"로거 레코딩 {len(index)}개 (normalized/{day}_*) — 자작 STM32 로거, CAN · IMU · 서스펜션",
                   f"온보드 영상 {len(videos)}개 (GoPro, GPS 18 Hz) — 랩 · 속도 · 주행선",
                   "derived/laps.csv · lap_logger.csv · logger_clock*.csv (lefdata gopro --laps · lef26-clock · lef26-laps)"]
    rep.methods = [
        "주행 구간: 로거 횡가속도의 10 초 평균 크기가 0.12 g 를 넘는 구간 (30 초 이하 끊김은 이어 붙임, 20 초 미만 제외). "
        "영상 GPS 가 본 주행과 겹치면 GPS 의 시작 · 끝을 구간 경계로 사용",
        "랩: 영상 GPS 가 계측선을 같은 방향으로 지날 때마다 1 랩. 통과 시각은 앞뒤 표본 사이 보간",
        "로거 ↔ 영상 시각: 두 장비의 요레이트 파형을 30 초 창으로 맞춰 정렬. 랩마다 로거 가속도계 ↔ GPS 로 재확인",
        "회전수: 로거 요레이트를 누적해 360° 로 나눈 값. GPS 랩이 있는 구간과 견주면 1 ~ 3 % 작게 나옴",
        "가속도: 로거 가속도계. 0.1 초 평균 후 0.5 초 이동평균. 센서 장착 기울기에 해당하는 치우침(로거 − GPS 평균)을 뺀 값",
        "전류 · 전압 · 배터리 값: 구간 안에 기록된 표본의 최대 · 최소. CAN 표본이 매초 있지 않으므로 실제 피크보다 작을 수 있음 (데이터 품질 절의 기록률)",
        "팩 전압: 고전압이 켜지고 꺼지는 순간의 앞뒤 5 초는 제외 (충전 · 방전 중의 과도 전압을 전압 강하로 읽지 않기 위함)",
        "써미스터별 온도: 개별 프레임에 같이 실린 '그 순간의 팩 최고 온도' 와의 차이를 구간 중앙값으로 산출",
        "SOC: 구간 처음 · 끝 50행의 중앙값. BMS 가 보고한 값이며 0.5 % 단위",
    ]
    return rep


def _debias(st: dict, off_long: float, off_lat: float) -> None:
    """가속도 요약에서 센서 장착 기울기만큼의 치우침을 뺀다 (횡은 좌회전 양수 기준)."""
    for k in ("long_g_mean", "long_g_max", "long_g_min"):
        if k in st:
            st[k] = round(st[k] - off_long, 3)
    for k, sign in (("lat_g_mean", -1), ("lat_g_left_max", -1), ("lat_g_right_max", +1)):
        if k in st:
            st[k] = round(st[k] + sign * off_lat, 3)


def _smooth_imu(r: _Rec, r0: float, r1: float, off_long: float, off_lat: float):
    """구간의 가속도 → 0.1 초 칸 평균, 0.5 초 이동평균, 치우침 제거. → (칸 가운데 시각, 종, 횡[좌회전 양수])"""
    m = (r.log.imu_t >= r0) & (r.log.imu_t < r1)
    t = r.log.imu_t[m]
    _, lg = sync.grid(t - r0, r.log.long_g[m], GG_STEP_S)
    _, lt = sync.grid(t - r0, r.log.lat_left_g[m], GG_STEP_S)
    tc = r0 + (np.arange(len(lg)) + 0.5) * GG_STEP_S
    return tc, ll._smooth(lg, 5) - off_long, ll._smooth(lt, 5) - off_lat


def _gg(r: _Rec, r0: float, r1: float, off_long: float, off_lat: float) -> pd.DataFrame:
    _, lg, lt = _smooth_imu(r, r0, r1, off_long, off_lat)
    ok = np.isfinite(lg) & np.isfinite(lt)
    return pd.DataFrame({"long_g": lg[ok], "lat_g": lt[ok]})


def _lap_trace(video_dir: Path, gate: dict, r: _Rec, lap: pd.Series, gps_lag_s: float, off_long: float, off_lat: float) -> pd.DataFrame:
    """랩 하나의 궤적: 계측선부터의 거리마다 위치 · 속도(GPS)와 가속도(로거)."""
    g = pq.read_table(video_dir / "gps.parquet", columns=["t_rel_ms", "t_abs", "fix", "lat", "lon", "speed_kmh"]).to_pandas()
    g = g[g["fix"]]
    t = g["t_rel_ms"].to_numpy() / 1000.0
    m = (t >= lap["t_start_s"]) & (t <= lap["t_end_s"])
    x, y = lapmod.to_local_m(g["lat"].to_numpy()[m], g["lon"].to_numpy()[m], gate["origin"]["lat0"], gate["origin"]["lon0"])
    dist = np.concatenate([[0.0], np.cumsum(np.hypot(np.diff(x), np.diff(y)))])
    true = lc._epoch_s(g["t_abs"][m])
    # GPS 는 로거보다 gps_lag_s 늦게 본다 → 같은 순간의 로거 값은 그만큼 앞 시각에 있다
    r_at = r.to_rel(true - gps_lag_s)
    tc, lg, lt = _smooth_imu(r, float(r_at[0]) - 2.0, float(r_at[-1]) + 2.0, off_long, off_lat)
    return pd.DataFrame({"dist_m": dist, "t_s": t[m] - t[m][0], "x_m": np.asarray(x), "y_m": np.asarray(y),
                         "speed_kmh": g["speed_kmh"].to_numpy()[m],
                         "long_g": np.interp(r_at, tc, np.nan_to_num(lg)), "lat_g": np.interp(r_at, tc, np.nan_to_num(lt))})


def _hv_events(rep: DayReport, recs: dict[str, _Rec], vids: dict) -> None:
    """주행 구간 안에서 고전압이 꺼진 일을 찾는다 (차가 선 뒤에 끄는 정상 종료는 구간 밖이라 걸리지 않는다)."""
    for s in rep.sessions:
        r = recs[s.recording]
        r0, r1 = (float(x) for x in r.to_rel([s.start.timestamp(), s.end.timestamp()]))
        for t_off in r.hv_off[(r.hv_off > r0) & (r.hv_off < r1)]:
            at = float(r.to_abs(t_off))
            b = ll._cut(r.log.bms, t_off - 30.0, t_off)
            ev = {"time": _ts(at), "session": s.key, "what": "주행 중 고전압(HV) 소실",
                  "cell_min_v": round(float(b["cell_min_v"].min()), 3) if len(b) else None,
                  "soc_pct": float(b["soc_pct"].median()) if len(b) else None,
                  "left_s": round(s.end.timestamp() - at, 1), "speed_kmh": None}
            if s.video in vids:
                vid = vids[s.video]
                ev["speed_kmh"] = round(float(np.interp(at, vid.t_true, vid.speed_kmh)), 1)
            v = ll._cut(r.vdc_steady, t_off - 30.0, t_off)
            ev["vdc_min_v"] = round(float(v["value"].min()), 1) if v is not None and len(v) else None
            rep.events.append(ev)


def _range_to_empty(rep: DayReport, recs: dict[str, _Rec]) -> list[str]:
    """주행 묶음(오전 · 오후)마다: 처음 SOC 에서 0 % 표시까지 몇 바퀴, 몇 km 를 달렸나 (거리는 추정)."""
    gps = [s for s in rep.sessions if s.kind == "gps" and s.stats.get("distance_km") and s.stats.get("turns")]
    km_per_turn = (sum(s.stats["distance_km"] for s in gps) / sum(s.stats["turns"] for s in gps)) if gps else None
    groups: list[list[Session]] = []
    for s in rep.sessions:
        if groups and (s.start - groups[-1][-1].end).total_seconds() <= 3600:
            groups[-1].append(s)
        else:
            groups.append([s])
    out = []
    for g in groups:
        soc0 = g[0].stats.get("soc_start_pct")
        zero_at, turns = None, 0.0
        for s in g:
            r = recs[s.recording]
            r0, r1 = (float(x) for x in r.to_rel([s.start.timestamp(), s.end.timestamp()]))
            b = ll._cut(r.log.bms, r0, r1)
            z = b[b["soc_pct"] <= 0.0]
            if len(z):
                zero_at = _ts(float(r.to_abs(z["t_s"].iloc[0])))
                turns += r.turns(r0, float(z["t_s"].iloc[0]))
                break
            turns += s.stats.get("turns") or 0.0
        if zero_at is None or soc0 is None:
            continue
        text = f"{g[0].key} ~ {g[-1].key}: SOC {soc0:g} % 에서 0 % 표시({zero_at:%H:%M:%S})까지 약 {turns:.0f} 바퀴"
        if km_per_turn:
            text += f", 약 {turns * km_per_turn:.0f} km (추정 — GPS 구간의 바퀴당 {km_per_turn:.2f} km 적용)"
        after = sum(max(0.0, (s.end - max(s.start, zero_at)).total_seconds()) for s in g if s.end > zero_at)
        out.append(text + f". 0 % 표시 이후 {after / 60:.1f} 분 추가 주행")
    return out


def _series(rep: DayReport, recs: dict[str, _Rec]) -> None:
    """하루 전체의 시간 추이 (1 초 간격). 레코딩 사이는 끊는다."""
    parts: dict[str, list] = {}

    def add(name, r, t_s, values, how):
        if len(t_s):
            parts.setdefault(name, []).extend([_second_series(np.asarray(r.to_abs(t_s), float), np.asarray(values, float), how), _gap_row()])

    for r in recs.values():
        if not r.timed:
            continue
        b = r.log.bms
        add("soc", r, b["t_s"].to_numpy(), b["soc_pct"].to_numpy(), "median")
        add("cell_min", r, b["t_s"].to_numpy(), b["cell_min_v"].to_numpy(), "min")
        add("cell_max", r, b["t_s"].to_numpy(), b["cell_max_v"].to_numpy(), "max")
        th = r.log.therm
        for name, col, how in (("batt_high", "highest_c", "max"), ("batt_low", "lowest_c", "min"), ("batt_avg", "average_c", "mean")):
            d = th.dropna(subset=[col])
            add(name, r, d["t_s"].to_numpy(), d[col].to_numpy(), how)
        mo = r.log.motor
        if r.vdc_steady is not None:
            v = r.vdc_steady
            add("vdc_med", r, v["t_s"].to_numpy(), v["value"].to_numpy(), "median")
            add("vdc_min", r, v["t_s"].to_numpy(), v["value"].to_numpy(), "min")
        for name, sig in (("t_motor", "t_motor_c"), ("t_igbt", "t_igbt_c")):
            if sig in mo:
                d = mo[sig].dropna(subset=["value"])
                add(name, r, d["t_s"].to_numpy(), d["value"].to_numpy(), "max")
    rep.series = {k: pd.concat(v[:-1], ignore_index=True) for k, v in parts.items()}


def _thermistor_snapshots(rep: DayReport, recs: dict[str, _Rec]) -> None:
    """써미스터 하나하나가 팩에서 가장 뜨거운 곳보다 얼마나 낮은가 — 주행 묶음(오전 · 오후)의 마지막 구간에서.

    써미스터 값은 하나씩 돌아가며 들어오고 기록이 성겨서, 온도가 오르는 주행 중에는 '같은 순간' 의 32개를 모을 수 없다.
    그런데 개별 프레임에는 그 순간의 팩 최고 온도가 같이 실려 있다 → (자기 온도 − 그 순간의 최고 온도)는
    시각이 달라도 서로 견줄 수 있다. 써미스터마다 그 차이의 중앙값을 낸다.
    """
    groups: list[list[Session]] = []
    for s in rep.sessions:                                   # 1 시간 넘게 끊기면 다른 묶음
        if groups and (s.start - groups[-1][-1].end).total_seconds() <= 3600:
            groups[-1].append(s)
        else:
            groups.append([s])
    for g in groups:
        last = g[-1]
        r = recs[last.recording]
        r0, r1 = (float(x) for x in r.to_rel([last.start.timestamp(), last.end.timestamp()]))
        d = ll._cut(r.single, r0, r1).dropna(subset=["temp_c", "highest_c"])
        if d.empty:
            continue
        d = d.assign(below=d["temp_c"] - d["highest_c"])
        snap = d.groupby("thermistor_id")["below"].agg(below_hot_c="median", n="size").reset_index().rename(columns={"thermistor_id": "id"})
        snap = snap[snap["n"] >= THERM_MIN_ROWS]
        if len(snap) < 8:
            continue
        snap["id"] = snap["id"].astype(int)
        spread = (d["highest_c"] - d["lowest_c"]).dropna()
        snap.attrs.update({"session": last.key, "start": last.start, "end": last.end,
                           "spread_c": float(spread.median()) if len(spread) else None})
        rep.thermistors[f"{last.key} ({last.start:%H:%M} ~ {last.end:%H:%M})"] = snap


def _coverage_table(rep: DayReport, recs: dict[str, _Rec]) -> None:
    rows = {}
    for s in rep.sessions:
        r = recs[s.recording]
        r0, r1 = (float(x) for x in r.to_rel([s.start.timestamp(), s.end.timestamp()]))
        row = {"가속도 (IMU)": _coverage(r.log.imu_t, r0, r1)}
        row["서스펜션"] = _coverage(r.log.lin["t_s"], r0, r1) if r.log.lin is not None else np.nan
        row["BMS"] = _coverage(r.log.bms["t_s"], r0, r1)
        row["써미스터"] = _coverage(r.log.therm["t_s"], r0, r1)
        for sig, name in SIGNAL_NAMES[4:]:
            if sig not in r.log.motor:
                row[name] = np.nan
                continue
            d = r.log.motor[sig]
            keep = ll._fresh(d["raw"].to_numpy()) if sig in FRESH_ONLY else np.ones(len(d), bool)
            row[name] = _coverage(d["t_s"].to_numpy()[keep], r0, r1)
        rows[s.key] = row
    rep.coverage = pd.DataFrame(rows).T[[n for _, n in SIGNAL_NAMES]] if rows else None


def _quality(rep: DayReport, recs: dict[str, _Rec], index: pd.DataFrame, videos: pd.DataFrame, lap_log: pd.DataFrame) -> None:
    q = rep.quality
    norm_gps = sum(int(c) for c in index.get("gps_rows", pd.Series(dtype=int)).fillna(0)) if "gps_rows" in index else None
    timed = int(index["time_trusted"].sum())
    q.append({"item": "로거 절대 시각", "status": OK, "value": f"{timed} / {len(index)} 레코딩",
              "note": "주행이 든 레코딩은 전부 시각 확보" + (f". 나머지 {len(index) - timed}개는 주행 없는 짧은 기록" if timed < len(index) else "")})
    names = {"gps_pps": "GPS 가 매초 보정", "free": "자체 시계(7 % 느림, 변환 시 보정)", "mixed": "도중에 바뀜(보정 불가)", "unknown": "판정 불가"}
    mode_of = index.set_index("recording_id")["clock_mode"]
    used = {mode_of[s.recording] for s in rep.sessions}
    q.append({"item": "로거 시계", "status": OK if used <= {"gps_pps"} else WARN,
              "value": " · ".join(names.get(m, m) for m in sorted(used)),
              "note": "주행이 든 레코딩 기준. 그 밖의 레코딩: " + ", ".join(
                  f"{names.get(k, k)} {v}개" for k, v in mode_of[~mode_of.index.isin([s.recording for s in rep.sessions])].value_counts().items())})
    if len(lap_log):
        q.append({"item": "로거 ↔ 영상 시각 정렬", "status": OK if lap_log["align_corr"].min() > 0.95 else WARN,
                  "value": f"상관 {lap_log['align_corr'].min():.3f} ~ {lap_log['align_corr'].max():.3f}",
                  "note": f"랩 {len(lap_log)}개에서 로거 가속도계 ↔ GPS 로 재확인. GPS 지연 {lap_log['gps_lag_s'].median():.2f} s"})
    logger_only = [s for s in rep.sessions if s.kind == "logger"]
    q.append({"item": "위치 · 랩 기록", "status": WARN if logger_only else OK,
              "value": f"{len(rep.sessions) - len(logger_only)} / {len(rep.sessions)} 구간",
              "note": "로거 GPS 미수신. 위치는 온보드 영상 GPS 에만 있으며, 영상이 끊긴 뒤의 주행은 랩 기록 없음" if logger_only else ""})
    dup = float(index["dup"].sum() / index["frames"].sum())
    q.append({"item": "CAN 프레임 반복", "status": WARN if dup > 0.2 else OK, "value": f"{dup * 100:.0f} %",
              "note": "직전 줄과 완전히 같은 줄의 비율. 같은 프레임이 여러 번 기록됨 → 전류 · 전압 표본을 하나하나 믿지 말 것"})
    for side, name in (("rl", "리어 좌"), ("rr", "리어 우")):
        hit = [(s.key, s.stats[f"lin_{side}_clip_share"]) for s in rep.sessions
               if (s.stats.get(f"lin_{side}_clip_share") or 0) > 0.05]
        if hit:
            worst = max(c for _, c in hit)
            keys = hit[0][0] if len(hit) == 1 else f"{hit[0][0]} ~ {hit[-1][0]}"
            q.append({"item": f"서스펜션 센서 ({name})", "status": BAD if worst > 0.5 else WARN,
                      "value": (f"{keys}: {min(c for _, c in hit) * 100:.0f} ~ {worst * 100:.0f} %" if len(hit) > 1
                                else f"{keys}: {worst * 100:.0f} %"),
                      "note": "센서 상한(75 mm)에 붙어 있던 시간의 비율. 그 구간의 스트로크 값은 사용 불가"})
    for item in rep.notes.get("missing_channels", []):
        q.append({"item": item.get("item", ""), "status": BAD, "value": "미기록", "note": item.get("note", "")})


def _kpis_and_facts(rep: DayReport) -> None:
    ss, gps = rep.sessions, rep.gps_sessions()
    if not ss:
        return
    limits = rep.notes.get("limits", {})
    total_min = sum(s.duration_s for s in ss) / 60
    turns = sum(s.stats.get("turns", 0) or 0 for s in ss)
    k, f = rep.kpis, rep.facts
    k.append({"label": "주행", "value": f"{len(ss)}", "unit": "구간", "note": f"합계 {total_min:.0f} 분 · 약 {turns:.0f} 바퀴"})
    f.append(f"주행 {len(ss)}구간, 합계 {total_min:.1f} 분. 랩 기록이 있는 구간 {len(gps)}개, 로거 단독 {len(ss) - len(gps)}개")
    if gps:
        best = min(gps, key=lambda s: s.stats["best_lap_s"])
        n_laps = sum(len(s.laps) for s in gps)
        km = sum(s.stats.get("distance_km", 0) for s in gps)
        k.append({"label": "완주 랩", "value": f"{n_laps}", "unit": "랩", "note": f"GPS 기록 {km:.1f} km"})
        k.append({"label": "최속 랩", "value": f"{best.stats['best_lap_s']:.2f}", "unit": "s", "note": f"{best.key} {best.best_lap_no}랩"})
        k.append({"label": "최고속도", "value": f"{max(s.stats['vmax_kmh'] for s in gps):.1f}", "unit": "km/h", "note": "영상 GPS"})
        f.append(f"완주 {n_laps}랩, 최속 {best.stats['best_lap_s']:.2f} s ({best.key} {best.best_lap_no}랩). "
                 + " · ".join(f"{s.key} {len(s.laps)}랩 최속 {s.stats['best_lap_s']:.2f} s" for s in gps))
        for s in gps:
            used = (s.laps["soc_start_pct"] - s.laps["soc_end_pct"])
            ok = used[(s.laps["soc_end_pct"] > 0.5) & (used < 6)]
            if len(ok):
                f.append(f"{s.key} 랩당 SOC 소모 {ok.min():.1f} ~ {ok.max():.1f} % (평균 {ok.mean():.1f} %, {len(ok)}랩)")

    def extreme(name, how):
        d = rep.series.get(name)
        if d is None or d["v"].dropna().empty:
            return None, None
        i = d["v"].idxmax() if how == "max" else d["v"].idxmin()
        return float(d.loc[i, "v"]), d.loc[i, "t"]

    hi, hi_t = extreme("batt_high", "max")
    if hi is not None:
        lim = limits.get("batt_temp_max_c")
        k.append({"label": "배터리 최고 온도", "value": f"{hi:.0f}", "unit": "℃",
                  "note": f"{hi_t:%H:%M} · 한계 {lim:g} ℃ 대비 여유 {lim - hi:.0f} ℃" if lim else f"{hi_t:%H:%M}"})
        f.append(f"배터리 최고 온도 {hi:.0f} ℃ ({hi_t:%H:%M:%S})" + (f", 한계 {lim:g} ℃ 대비 여유 {lim - hi:.0f} ℃" if lim else ""))
    if {"batt_high", "batt_low"} <= set(rep.series):
        both = rep.series["batt_high"].dropna().merge(rep.series["batt_low"].dropna(), on="t")
        sp = both["v_x"] - both["v_y"]
        if len(sp):
            k.append({"label": "팩 내 온도 편차 최대", "value": f"{sp.max():.0f}", "unit": "℃", "note": f"{both.loc[sp.idxmax(), 't']:%H:%M} 이후"})
            f.append(f"팩 내 온도 편차(같은 순간 최고 − 최저) 최대 {sp.max():.0f} ℃ ({both.loc[sp.idxmax(), 't']:%H:%M:%S} 부터)")
    lo, lo_t = extreme("cell_min", "min")
    if lo is not None:
        lim = limits.get("cell_min_v")
        k.append({"label": "최저 셀 전압", "value": f"{lo:.2f}", "unit": "V",
                  "note": f"{lo_t:%H:%M}" + (f" · 하한 {lim:g} V" if lim else " · 하한 사양 미확인")})
        f.append(f"최저 셀 전압 {lo:.3f} V ({lo_t:%H:%M:%S})")
    iq = [s.stats.get("iq_peak_a") for s in ss if s.stats.get("iq_peak_a") is not None]
    if iq:
        k.append({"label": "모터 전류 최대", "value": f"{max(iq):.0f}", "unit": "A", "note": "기록된 표본 중"})
        f.append(f"모터 전류(Iq) 최대 {max(iq):.0f} A, 지령 최대 {max(s.stats.get('iq_cmd_peak_a') or 0 for s in ss):.0f} A")
    vmin, vmin_t = extreme("vdc_min", "min")
    if vmin is not None:
        f.append(f"팩 전압 최저 {vmin:.1f} V ({vmin_t:%H:%M:%S})")
    for name, label in (("t_motor", "모터"), ("t_igbt", "인버터")):
        v, t = extreme(name, "max")
        if v is not None:
            f.append(f"{label} 최고 온도 {v:.0f} ℃ ({t:%H:%M:%S})")
