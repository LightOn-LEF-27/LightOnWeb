"""LEF-26 로거 시계를 검증한다 — 로그 자체에 남은 흔적 + 온보드 영상(GoPro)의 GPS 시각.

배경 (2026-10-01 발견, 근거: 26팀 펌웨어 main.c · rtc.c)
    로거 안에는 시계가 셋 있고, 셋 다 다른 속도로 간다.

        RTC       줄마다 찍히는 시각(HH:MM:SS.mmm). 내부 RC 발진기 **LSI** 로 돈다 (`LSEState = RCC_LSE_OFF`).
                  분주비 127 / 255 는 32.768 kHz 크리스털용 값인데 LSI 는 정격 32 kHz 이고 이 칩은 약 30.5 kHz 다
                  → **약 7 % 느리다** (1 분에 4 초)
        MCU 틱    HAL_GetTick. 내부 RC 발진기 **HSI** → PLL. IMU 5 ms · SD 동기화 5000 ms 가 이 틱으로 돈다
                  → **약 2 % 빠르다** (영상으로 잰 값)
        GPS       PPS(1 초 펄스)가 올 때마다 펌웨어가 RTC 를 GPS 시각으로 다시 맞춘다

    그래서
        GPS 가 살아 있으면  '초' 는 정확하다. 밀리초만 0 → 약 930 까지 가다가 다음 초로 넘어간다
        GPS 가 죽으면      RTC 가 혼자 돌면서 계속 늦어진다 (밀리초는 996 까지 간다)
    `RTC-UTC SYNC` 이벤트는 처음 한 번만 기록된다. 그래서 로그의 이벤트만 봐서는 계속 맞추고 있는지 알 수 없었다.

세 가지로 잰다.
    signature()      밀리초 값의 분포            → GPS 가 시계를 잡고 있었나 (로그만으로)
    sync_period()    SD 동기화가 남긴 공백의 주기  → RTC 가 MCU 틱보다 얼마나 느린가 (로그만으로)
    measure_event()  로거 요레이트 ↔ 카메라 자이로 → 참 시각 대비 시계 속도 f 와 어긋남 (영상 필요)

    로거 경과시간 t_rel = a + f × 참 시각(UTC 초)        f = 1 이면 정상, 0.93 이면 7 % 느림

변환기와의 관계 (시계 모델 v2, 2026-10-01)
    변환기(`lef26.py`)가 앞의 두 가지(밀리초 분포, SD 동기화 주기)로 시계 속도를 정해 **이미 바로잡는다.**
    normalized/ 의 t_rel_ms · t_abs 는 바로잡은 값이고, 로거가 찍은 그대로는 frames.t_log_ms 에 있다.
    이 모듈은 그 보정을 **영상으로 검증**한다: 바로잡은 시간이 참 시간과 얼마나 남았는지(f_residual)를 잰다.
        f = f_applied × f_residual        로거 시계의 실제 속도 (영상 기준)
    결과는 해석이므로 derived/ 에 둔다.
"""

from __future__ import annotations

import csv
import json
import math
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pandas as pd
import pyarrow as pa
import pyarrow.compute as pc
import pyarrow.parquet as pq

from . import gopro, lef26, sync, video
from .writer import TZ

# 시계 상수와 로그만으로 재는 함수는 변환기에 있다 (변환할 때 쓰므로). 여기서는 같은 것을 그대로 쓴다
F_FREE, LATE_MS, FREE_LATE_SHARE = lef26.F_FREE, lef26.LATE_MS, lef26.FREE_LATE_SHARE
TICK_SYNC_S, SYNC_GAP_MIN_S, SYNC_PEAK_HALF_S, SYNC_MIN_SPANS = (
    lef26.TICK_SYNC_S, lef26.SYNC_GAP_MIN_S, lef26.SYNC_PEAK_HALF_S, lef26.SYNC_MIN_SPANS)
TICK_RATE = lef26.TICK_RATE
MIN_CORR = 0.9              # 창 매칭 후보로 인정할 최소 상관
MIN_WINDOWS = 5             # 직선 위에 모여야 하는 창 수
MIN_WINDOWS_UNTIMED = 10    # 절대시각이 없는 레코딩은 로그 전체에서 찾으므로 더 엄격하게
MAX_RMS_S = 0.3
EPOCH = pd.Timestamp(0, tz="UTC")


def _epoch_s(ts: pd.Series) -> np.ndarray:
    """tz 가 붙은 시각 열 → UTC 초 (열의 단위가 ms 든 ns 든 상관없이)."""
    return ((ts - EPOCH) / pd.Timedelta(seconds=1)).to_numpy(float)


def _iso(epoch_s: float | None) -> str | None:
    if epoch_s is None or not np.isfinite(epoch_s):
        return None
    return pd.Timestamp(epoch_s, unit="s", tz="UTC").tz_convert(TZ).isoformat(timespec="milliseconds")


# ---------------------------------------------------------------- 읽기

@dataclass
class Camera:
    rec: str
    k0: int
    yaw: np.ndarray           # UTC 격자 위의 요레이트 (°/s, 좌회전 양수)
    t_start: float
    t_end: float


@dataclass
class Logger:
    rec: str
    t_rel_s: np.ndarray
    yaw: np.ndarray
    rel_sync: float | None    # 로거가 절대시각을 얻은 시점 (t_rel, s)
    abs_sync: float | None    # 그때 로거가 찍은 시각 (UTC 초)
    duration_s: float         # 변환기가 바로잡은 길이
    f_applied: float = 1.0    # 변환기가 적용한 시계 속도. 로거가 찍은 길이 = duration_s × f_applied


def camera_yaw(rec_dir: Path) -> Camera | None:
    """카메라 자이로를 수직 축에 투영한 요레이트. IMU 시각이 GPS 시각에 맞춰진 구간만 쓴다."""
    imu = pq.read_table(rec_dir / "imu.parquet").to_pandas()
    gps = pq.read_table(rec_dir / "gps.parquet", columns=["t_abs", "speed_kmh"]).to_pandas()
    imu = imu[imu["t_quality"] == "gps_utc"]          # 못 맞춘 조각은 최대 2 s 어긋나 있어 시계 검증에 못 쓴다
    if imu.empty or gps.empty:
        return None
    t = _epoch_s(imu["t_abs"])
    tg = _epoch_s(gps["t_abs"])
    up = gopro.vertical_axis(imu[["a0_g", "a1_g", "a2_g"]].to_numpy(), t, tg[gps["speed_kmh"].to_numpy() < 1.0])
    k0, arr = sync.grid(t, gopro.yaw_rate(imu[["g0_dps", "g1_dps", "g2_dps"]].to_numpy(), up))
    return Camera(rec_dir.name, k0, arr, float(tg[0]), float(tg[-1]))


def logger_yaw(rec_dir: Path) -> Logger | None:
    imu = pq.read_table(rec_dir / "imu.parquet", columns=["t_rel_ms", "t_abs", "t_quality", "yaw_dps"]).to_pandas()
    if len(imu) < 2:
        return None
    t_rel = imu["t_rel_ms"].to_numpy() / 1000.0
    synced = np.flatnonzero((imu["t_quality"] == "synced").to_numpy())
    rel_sync = abs_sync = None
    if len(synced):
        i = int(synced[0])
        rel_sync, abs_sync = float(t_rel[i]), float(_epoch_s(imu["t_abs"].iloc[[i]])[0])
    return Logger(rec_dir.name, t_rel, imu["yaw_dps"].to_numpy(), rel_sync, abs_sync, float(t_rel[-1] - t_rel[0]),
                  float(clock_meta(rec_dir).get("f", 1.0)))


def clock_meta(rec_dir: Path) -> dict:
    """변환기가 정한 시계 상태 (meta.json 의 time.clock): mode, f, f_basis, rtc_per_tick …"""
    return json.loads((rec_dir / "meta.json").read_text(encoding="utf-8"))["time"].get("clock", {})


# ---------------------------------------------------------------- 로그 자체의 흔적

def signature(frames_path: Path) -> dict:
    """로그의 밀리초 값만 보고 GPS 가 시계를 잡고 있었는지 가린다.

    RTC 가 혼자 돌면 밀리초는 0 → 996 을 고르게 지난다 (subsecond 256 단계) — 960 이상인 행이 3.9 % 다.
    GPS PPS 가 매초 RTC 를 다시 맞추면, RTC 가 느린 만큼 덜 간 채(약 930)로 다음 초가 시작돼 960 이상이 **한 줄도 없다.**
    그래서 `pps_share = 1 − (960 이상 비율) / 3.9 %` 가 "GPS 가 시계를 잡고 있던 시간의 비율" 이다.

    PPS 가 끊은 초에서는 넘어가기 직전의 밀리초가 곧 RTC 발진기의 실제 속도다:
    1 초 동안 subsecond 가 c 단계 갔으면 발진기는 정격의 (c + 0.5) / 256 배로 돌고 있다.
    """
    pf = pq.ParquetFile(frames_path)
    rows = late = 0
    before_rollover: list[np.ndarray] = []
    prev_sec, prev_ms = None, None
    for g in range(pf.num_row_groups):
        t = pf.read_row_group(g, columns=["t_raw"]).column(0).combine_chunks()
        if not len(t):
            continue
        code = pc.dictionary_encode(pc.utf8_slice_codeunits(t, 0, 8))
        idx = code.indices.to_numpy(zero_copy_only=False)
        ms = pc.cast(pc.utf8_slice_codeunits(t, 9, 12), pa.int16()).to_numpy()
        rows += len(ms)
        late += int((ms >= LATE_MS).sum())
        if prev_sec is not None and code.dictionary[int(idx[0])].as_py() != prev_sec:
            before_rollover.append(np.array([prev_ms]))
        before_rollover.append(ms[np.flatnonzero(idx[1:] != idx[:-1])])
        prev_sec, prev_ms = code.dictionary[int(idx[-1])].as_py(), int(ms[-1])
    if not rows:
        return {"rows": 0, "late_ms_share": None, "pps_share": None, "lsi_ratio_from_pps": None}
    share = late / rows
    pps_share = float(np.clip(1 - share / FREE_LATE_SHARE, 0, 1))
    v = np.concatenate(before_rollover) if before_rollover else np.array([])
    v = v[v < LATE_MS]
    lsi = None
    if pps_share > 0.5 and len(v) >= 20:
        steps = round(float(np.percentile(v, 95)) * 256 / 1000)        # 초가 끊기기 직전까지 간 subsecond 단계 수
        lsi = round((steps + 0.5) / 256, 4)
    return {"rows": rows, "late_ms_share": round(share, 5), "pps_share": round(pps_share, 3), "lsi_ratio_from_pps": lsi}


mode_of = lef26.clock_mode


def sync_spans(t_s) -> tuple[np.ndarray, np.ndarray]:
    """로거 경과시간 배열에서 기록 공백(≥ 30 ms)을 찾아 `lef26.sync_spans_from_gaps` 에 넘긴다 → (구간 시작, 길이)."""
    t = np.asarray(t_s, float)
    if len(t) < 3:
        return np.array([]), np.array([])
    i = np.flatnonzero(np.diff(t) >= SYNC_GAP_MIN_S)
    return lef26.sync_spans_from_gaps(t[i], t[i + 1])


def sync_period(t_s) -> dict:
    """SD 동기화 주기 → rtc_per_tick. **로거가 찍은 그대로의 시간**(frames.t_log_ms)을 줘야 한다."""
    return lef26.sync_period_from_spans(sync_spans(t_s)[1])


def tick_period(frames_path: Path) -> dict:
    return sync_period(pq.read_table(frames_path, columns=["t_log_ms"]).column(0).to_numpy() / 1000.0)


# ---------------------------------------------------------------- 영상과 겹치기

def measure_pair(log: Logger, cam: Camera, f0s=(1.0, F_FREE)) -> tuple[dict, pd.DataFrame]:
    """레코딩 하나와 영상 하나. 반환 (요약 dict, 직선 위에 모인 창들). 여기서 f 는 **바로잡은 시간 기준의 남은 속도**다.

    시계 속도를 모르므로 사전값 f0 마다 로거 시간축을 1/f0 로 편 뒤 창 매칭을 하고, 더 많은 창이 모이는 쪽을 고른다.
    (변환기가 이미 바로잡았으면 1.0 쪽이 맞고, 바로잡지 못한 레코딩이면 F_FREE 쪽이 맞는다.)
    절대시각이 있는 레코딩은 '동기화 시점부터 f0 로 흘렀다면 여기쯤' 이라는 예상 주변만 본다.
    """
    best, best_m = None, None
    for f0 in f0s:
        k0, arr = sync.grid(log.t_rel_s / f0, log.yaw)
        if not len(arr):
            continue
        expect = slack = None
        if log.abs_sync is not None:
            shift = log.rel_sync / f0 - log.abs_sync             # 예상 위치: u = T - abs_sync + rel_sync / f0
            expect = lambda t, shift=shift: t + shift
            slack = lambda t: 30.0 + 0.02 * np.abs(t - log.abs_sync)
        m = sync.match_windows(cam.k0, cam.yaw, k0, arr, expect=expect, slack=slack)
        m = m[m["corr"] >= MIN_CORR].reset_index(drop=True)
        fit = sync.fit_clock(m["ref_s"], m["sig_s"], slopes=np.arange(0.97, 1.0301, 0.0005),
                             tol_s=MAX_RMS_S, min_inliers=MIN_WINDOWS) if len(m) else None
        if fit is None:
            if best is None:
                best, best_m = {"f0": f0, "windows": int(len(m)), "inliers": 0}, m.iloc[:0]
            continue
        cand = {"f0": f0, "windows": int(len(m)), "inliers": fit["n"], "f": fit["f"] * f0, "a": fit["a"] * f0,
                "rms_s": fit["rms_s"] * f0, "span_s": fit["span_s"]}
        if best is None or cand["inliers"] > best["inliers"]:
            best = cand
            best_m = m[fit["inliers"]].assign(t_rel_s=lambda d, f0=f0: d["sig_s"] * f0)
    return best or {"f0": None, "windows": 0, "inliers": 0}, best_m if best_m is not None else pd.DataFrame()


def measure_event(event_dir: Path) -> tuple[pd.DataFrame, pd.DataFrame]:
    """이벤트의 모든 (로거 레코딩, 영상) 쌍 → (쌍 요약, 맞은 창들)."""
    norm = event_dir / "normalized"
    vids = pd.read_csv(norm / video.INDEX_NAME, encoding="utf-8-sig")["recording_id"].tolist()
    cams = {v: c for v in vids if (c := camera_yaw(norm / v)) is not None}
    index = list(csv.DictReader((norm / "recordings.csv").open(encoding="utf-8-sig")))

    pairs, windows = [], []
    for r in index:
        rid = r["recording_id"]
        log = logger_yaw(norm / rid)
        if log is None or np.nanstd(log.yaw) < 1.0:              # 돌지 않은 레코딩은 맞출 파형이 없다
            continue
        if log.abs_sync is not None:                             # 시계가 10 % 느려도 덮도록 넉넉히
            lo = log.abs_sync - log.rel_sync / 0.9 - 60
            hi = log.abs_sync + (log.duration_s - log.rel_sync) / 0.9 + 60
            cands = [c for c in cams.values() if c.t_start < hi and c.t_end > lo]
        else:                                                    # 절대시각이 없으면 같은 날 영상 전부
            cands = [c for c in cams.values() if c.rec[:10] == rid[:10]]
        for cam in cands:
            fit, m = measure_pair(log, cam)
            need = MIN_WINDOWS if log.abs_sync is not None else MIN_WINDOWS_UNTIMED
            ok = fit["inliers"] >= need and fit["inliers"] >= 0.5 * fit["windows"]
            row = {"recording_id": rid, "video": cam.rec, "timed": log.abs_sync is not None,
                   "windows": fit["windows"], "inliers": fit["inliers"], "accepted": bool(ok)}
            if ok:
                t_true = m["ref_s"].to_numpy()
                row.update({"f": round(fit["f"], 5), "rms_s": round(fit["rms_s"], 3),
                            "corr_median": round(float(m["corr"].median()), 4),
                            "true_start": _iso(t_true.min()), "true_end": _iso(t_true.max()),
                            "covered_s": round(float(np.ptp(t_true)), 1)})
                if log.abs_sync is not None:                     # normalized 의 t_abs - 참 시각
                    stamped = log.abs_sync + (fit["a"] + fit["f"] * np.array([t_true.min(), t_true.max()]) - log.rel_sync)
                    err = stamped - np.array([t_true.min(), t_true.max()])
                    row.update({"normalized_minus_true_start_s": round(float(err[0]), 2),
                                "normalized_minus_true_end_s": round(float(err[1]), 2)})
                windows.append(pd.DataFrame({"recording_id": rid, "video": cam.rec, "true_s": m["ref_s"].to_numpy(),
                                             "t_rel_s": m["t_rel_s"].to_numpy(), "corr": m["corr"].to_numpy()}))
            pairs.append(row)
    cols = ["recording_id", "video", "timed", "windows", "inliers", "accepted", "f", "rms_s", "corr_median",
            "true_start", "true_end", "covered_s", "normalized_minus_true_start_s", "normalized_minus_true_end_s"]
    win = pd.concat(windows, ignore_index=True) if windows else pd.DataFrame(
        columns=["recording_id", "video", "true_s", "t_rel_s", "corr"])
    return pd.DataFrame(pairs, columns=cols), win


@dataclass
class ClockMap:
    """한 레코딩의 '로거 경과시간 ↔ 참 시각' 지도. 영상과 맞은 창들을 이은 꺾은선이다.

    영상이 덮은 구간 안에서는 창 사이를 직선으로 잇는다 (오차 약 0.05 s).
    영상이 없는 앞뒤는 가장 가까운 영상에서 잰 속도로 연장한다 — 멀어질수록 불확실하다.
    """
    t_rel: np.ndarray
    true_s: np.ndarray
    f_head: float
    f_tail: float

    def to_true(self, t_rel_s) -> np.ndarray:
        x = np.asarray(t_rel_s, float)
        out = np.interp(x, self.t_rel, self.true_s)
        out = np.where(x < self.t_rel[0], self.true_s[0] + (x - self.t_rel[0]) / self.f_head, out)
        return np.where(x > self.t_rel[-1], self.true_s[-1] + (x - self.t_rel[-1]) / self.f_tail, out)

    def to_rel(self, true_s) -> np.ndarray:
        """참 시각(UTC 초) → 로거 경과시간. `to_true` 의 역이다 (영상에서 잡은 구간을 로거에서 잘라낼 때 쓴다)."""
        x = np.asarray(true_s, float)
        out = np.interp(x, self.true_s, self.t_rel)
        out = np.where(x < self.true_s[0], self.t_rel[0] + (x - self.true_s[0]) * self.f_head, out)
        return np.where(x > self.true_s[-1], self.t_rel[-1] + (x - self.true_s[-1]) * self.f_tail, out)


def clock_map(windows: pd.DataFrame, pairs: pd.DataFrame, rec_id: str) -> ClockMap | None:
    w = windows[windows["recording_id"] == rec_id].sort_values("t_rel_s")
    if len(w) < MIN_WINDOWS:
        return None
    w = w[np.concatenate([[True], np.diff(w["t_rel_s"].to_numpy()) > 0])]
    pr = pairs[(pairs["recording_id"] == rec_id) & pairs["accepted"]].set_index("video")["f"]
    return ClockMap(w["t_rel_s"].to_numpy(), w["true_s"].to_numpy(),
                    float(pr[w["video"].iloc[0]]), float(pr[w["video"].iloc[-1]]))


CLOCK_COLS = ["recording_id", "time_trusted", "mode", "rows", "late_ms_share", "pps_share", "lsi_ratio_from_pps",
              "sync_spans", "sync_span_s", "rtc_per_tick", "tick_rate",
              "logger_duration_s", "true_duration_s", "f", "f_basis", "f_applied", "f_applied_basis", "f_residual",
              "videos", "windows", "line_rms_s",
              "true_start", "true_end", "abs_basis", "first_obs_true", "normalized_minus_true_first_obs_s",
              "last_obs_true", "normalized_minus_true_last_obs_s", "normalized_minus_true_at_sync_s",
              "normalized_minus_true_at_end_s", "logger_minus_true_at_end_s"]


def summarize(event_dir: Path, pairs: pd.DataFrame, windows: pd.DataFrame) -> pd.DataFrame:
    """레코딩마다 한 줄: 시계가 어떤 상태였고, 얼마나 느렸고, 바로잡은 뒤 얼마나 남았나.

    f — 로거 시계의 속도 (참 시간 1 초에 로거 시계가 간 초). f_basis 는 그 근거
        video     영상과 겹쳐 쟀다: f = f_applied × f_residual (가장 믿을 만하다)
        그 밖     변환기가 적용한 값 그대로 (f_applied_basis: gps_pps · sd_sync · assumed)
    f_applied · f_applied_basis   변환기가 정규화할 때 쓴 값 (meta.json 의 time.clock)
    f_residual                    영상으로 잰, 바로잡은 뒤에 남은 속도 오차. 1 이면 보정이 맞았다

    abs_basis — 참 시각(true_start · true_end)을 무엇에 걸었나
        video     영상의 GPS 시각
        sync      normalized 의 t_abs 그대로 (로거의 동기화 시각 + 바로잡은 경과시간). ⚠ 동기화 시각 자체가 틀릴 수 있다
                  (GPS 가 죽은 날 영상과 대 보니 대부분 ±3 s, 한 번은 −16 s)

    `normalized_minus_true_*` = normalized 의 t_abs − 참 시각 (음수 = 늦다). 보정 뒤에 남은 오차다.
        first_obs / last_obs   영상과 겹친 첫·마지막 지점에서 **직접 잰 값**
        at_sync / at_end       그 바깥으로 **연장한 값**
    `logger_minus_true_at_end_s` = 보정하지 않았다면(로거가 찍은 그대로였다면) 레코딩 끝에서의 오차
    """
    norm = event_dir / "normalized"
    index = list(csv.DictReader((norm / "recordings.csv").open(encoding="utf-8-sig")))
    rows = []
    for r in index:
        rid = r["recording_id"]
        frames = norm / rid / "frames.parquet"
        sig = signature(frames)
        mode = mode_of(sig["pps_share"])
        applied = clock_meta(norm / rid)
        log = logger_yaw(norm / rid)
        fa = float(applied.get("f", 1.0))
        row = {"recording_id": rid, "time_trusted": r["time_trusted"] == "True", "mode": mode, **sig,
               **(tick_period(frames) if mode == "free" else sync_period([])),
               "f_applied": fa, "f_applied_basis": applied.get("f_basis"),
               "logger_duration_s": None if log is None else round(log.duration_s * fa, 1),
               "true_duration_s": None if log is None or mode == "mixed" else round(log.duration_s, 1)}
        if mode != "mixed":
            row.update({"f": fa, "f_basis": applied.get("f_basis")})
        w = windows[windows["recording_id"] == rid]
        cm = clock_map(windows, pairs, rid)
        t_first, t_last = (log.t_rel_s[0], log.t_rel_s[-1]) if log is not None else (None, None)
        if cm is not None:
            fit = sync.fit_clock(w["true_s"], w["t_rel_s"], tol_s=5.0, min_inliers=MIN_WINDOWS)
            row.update({"f_residual": round(fit["f"], 5), "f": round(fit["f"] * fa, 5), "f_basis": "video",
                        "videos": int(w["video"].nunique()), "windows": int(len(w)), "line_rms_s": round(fit["rms_s"], 3),
                        "abs_basis": "video",
                        "true_start": _iso(float(cm.to_true(t_first))), "true_end": _iso(float(cm.to_true(t_last))),
                        "true_duration_s": round(float(cm.to_true(t_last) - cm.to_true(t_first)), 1)})
            if row["rtc_per_tick"]:
                row["tick_rate"] = round(row["f"] / row["rtc_per_tick"], 5)
            if log.abs_sync is not None:
                err = lambda t_rel: float(log.abs_sync + (t_rel - log.rel_sync) - cm.to_true(t_rel))
                raw = float(log.abs_sync + (t_last - log.rel_sync) * fa - cm.to_true(t_last))
                row.update({"first_obs_true": _iso(cm.true_s[0]), "normalized_minus_true_first_obs_s": round(err(cm.t_rel[0]), 2),
                            "last_obs_true": _iso(cm.true_s[-1]), "normalized_minus_true_last_obs_s": round(err(cm.t_rel[-1]), 2),
                            "normalized_minus_true_at_sync_s": round(err(log.rel_sync), 2),
                            "normalized_minus_true_at_end_s": round(err(t_last), 2),
                            "logger_minus_true_at_end_s": round(raw, 2)})
        elif log is not None and log.abs_sync is not None and mode != "mixed":
            to_true = lambda t_rel: log.abs_sync + (t_rel - log.rel_sync)
            row.update({"abs_basis": "sync", "true_start": _iso(to_true(t_first)), "true_end": _iso(to_true(t_last)),
                        "logger_minus_true_at_end_s": round(float((t_last - log.rel_sync) * (fa - 1)), 2)})
        rows.append(row)
    return pd.DataFrame(rows).reindex(columns=CLOCK_COLS)


# ---------------------------------------------------------------- 속도 환산 검증

# 문서에서 온 값으로 계산한 '모터 속도 1 % 당 차속': N-100 % = 6000 rpm, 감속비 4.2727, 타이어 반지름 0.2286 m
KMH_PER_PCT_DOC = lef26.N_MAX_RPM / 100 / lef26.FINAL_DRIVE / 60 * 2 * math.pi * lef26.TIRE_RADIUS_M * 3.6
SPEED_MIN_KMH = 25.0        # 이보다 느리면 비율이 잡음에 흔들린다
SPEED_VOTE_KMH = 1.5        # 합의에 넣는 오차
SPEED_FIT_KMH = 3.0         # 다시 맞출 때 쓰는 오차
SPEED_MIN_SAMPLES = 40
SPEED_SUMMARY_MIN = 300     # 이벤트 대표값에는 표본이 이만큼 넘는 영상만 쓴다

SPEED_COLS = ["recording_id", "video", "samples", "repeat_share", "used", "gps_lag_s", "kmh_per_pct", "agree_share",
              "rms_kmh", "kmh_per_rpm", "vs_documented", "v_gps_max_kmh", "n_act_max_pct"]


def fit_speed_ratio(v_ref_at, pct, k_grid, lags=np.arange(-0.6, 0.601, 0.02)) -> dict | None:
    """v ≈ K × pct 가 되는 K 와 시간 어긋남을 **다수결**로 구한다. 표본이 모자라면 None.

    v_ref_at(lag) → pct 표본들의 시각을 lag 초 민 곳에서의 기준 속도 배열. 틀린 표본이 많이 섞여도
    (LEF-26 로그는 옛 값이 다시 적힌다) 맞는 표본끼리는 같은 K 에 모이므로, 가장 많은 표본이 동의하는 쪽을 고른다.
    """
    pct = np.asarray(pct, float)
    best = None
    for lag in lags:
        v = v_ref_at(float(lag))
        ok = (v > SPEED_MIN_KMH) & (pct > 5)
        if ok.sum() < SPEED_MIN_SAMPLES:
            continue
        votes = (np.abs(v[ok][None, :] - k_grid[:, None] * pct[ok][None, :]) < SPEED_VOTE_KMH).sum(axis=1)
        i = int(np.argmax(votes))
        if best is None or votes[i] > best[0]:
            best = (int(votes[i]), float(lag), float(k_grid[i]), int(ok.sum()))
    if best is None:
        return None
    votes, lag, k, used = best
    v = v_ref_at(lag)
    ok = (v > SPEED_MIN_KMH) & (pct > 5)
    near = ok & (np.abs(v - k * pct) < SPEED_FIT_KMH)
    k = float((v[near] * pct[near]).sum() / (pct[near] ** 2).sum())
    return {"k": k, "lag_s": lag, "used": used, "agree_share": votes / used,
            "rms": float(np.sqrt(np.mean((v[near] - k * pct[near]) ** 2)))}


def speed_calibration(event_dir: Path, pairs: pd.DataFrame, windows: pd.DataFrame) -> pd.DataFrame:
    """모터 속도(n_act_pct) ↔ 영상 GPS 속도 → '모터 속도 1 % 당 km/h'. 시각을 맞춘 뒤 같은 순간끼리 비교한다.

    ⚠ 로거의 CAN 기록에는 **옛 값이 다시 적힌 표본**이 섞여 있다: 달리는 중인데 직전과 완전히 같은 값이 이어지고,
       GPS 속도와 한참 다른 값이 찍혀 있는 식이다. 그래서 직전과 같은 표본은 버리고,
       남은 표본도 다수결로 고른다 (fit_speed_ratio).
    gps_lag_s 는 GPS 속도가 모터 속도보다 늦은 정도다 (수신기 내부 필터 + 카메라 IMU 시각 보정의 나머지, 약 0.2 s).
    """
    norm = event_dir / "normalized"
    k_grid = KMH_PER_PCT_DOC * np.arange(0.85, 1.1001, 0.002)
    rows = []
    for rid, grp in pairs[pairs["accepted"]].groupby("recording_id"):
        m = pq.read_table(norm / rid / "motor.parquet", columns=["t_rel_ms", "signal", "value", "raw"],
                          filters=[("signal", "=", "n_act_pct")]).to_pandas()
        cm = clock_map(windows, pairs, rid)
        if m.empty or cm is None:
            continue
        t_rel, pct, raw = m["t_rel_ms"].to_numpy() / 1000.0, m["value"].abs().to_numpy(), m["raw"].to_numpy()
        fresh = np.concatenate([[True], raw[1:] != raw[:-1]])
        for vid in grp["video"]:
            w = windows[(windows["recording_id"] == rid) & (windows["video"] == vid)]
            inside = (t_rel >= w["t_rel_s"].min()) & (t_rel <= w["t_rel_s"].max())
            if inside.sum() < SPEED_MIN_SAMPLES:
                continue
            g = pq.read_table(norm / vid / "gps.parquet", columns=["t_abs", "speed_kmh"]).to_pandas()
            tg, vg = _epoch_s(g["t_abs"]), g["speed_kmh"].to_numpy()
            moving = inside & (pct > 5)
            use = inside & fresh
            t_true, p = cm.to_true(t_rel[use]), pct[use]
            fit = fit_speed_ratio(lambda lag: np.interp(t_true + lag, tg, vg), p, k_grid)
            if fit is None:
                continue
            rows.append({"recording_id": rid, "video": vid, "samples": int(inside.sum()),
                         "repeat_share": round(float(1 - fresh[moving].mean()), 3) if moving.any() else None,
                         "used": fit["used"], "gps_lag_s": round(fit["lag_s"], 2), "kmh_per_pct": round(fit["k"], 4),
                         "agree_share": round(fit["agree_share"], 2), "rms_kmh": round(fit["rms"], 2),
                         "kmh_per_rpm": round(fit["k"] / (lef26.N_MAX_RPM / 100), 5),
                         "vs_documented": round(fit["k"] / KMH_PER_PCT_DOC, 4),
                         "v_gps_max_kmh": round(float(np.interp(t_true + fit["lag_s"], tg, vg).max()), 1),
                         "n_act_max_pct": round(float(p.max()), 1)})
    return pd.DataFrame(rows, columns=SPEED_COLS)


def speed_ratio(speed: pd.DataFrame) -> float | None:
    """이벤트 대표값: 표본이 충분한 영상들의 '1 % 당 km/h' 중앙값."""
    big = speed[speed["used"] >= SPEED_SUMMARY_MIN]
    return float(big["kmh_per_pct"].median()) if len(big) else None


def run_event(event_dir: Path) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    """→ derived/ 에 표 4개.

        logger_clock.csv           레코딩마다 한 줄 (시계 상태 · 속도 · 찍힌 시각의 오차)
        logger_clock_pairs.csv     (레코딩, 영상) 쌍마다 한 줄
        logger_clock_windows.csv   맞은 창들 = 근거이자 '로거 시간 ↔ 참 시각' 지도
        speed_calibration.csv      모터 속도 ↔ GPS 속도
    """
    pairs, windows = measure_event(event_dir)
    clock = summarize(event_dir, pairs, windows)
    speed = speed_calibration(event_dir, pairs, windows)
    out = event_dir / "derived"
    out.mkdir(exist_ok=True)
    clock.to_csv(out / "logger_clock.csv", index=False, encoding="utf-8-sig")
    pairs.to_csv(out / "logger_clock_pairs.csv", index=False, encoding="utf-8-sig")
    windows.assign(true_utc=[_iso(t) for t in windows["true_s"]]).round(3).to_csv(
        out / "logger_clock_windows.csv", index=False, encoding="utf-8-sig")
    speed.to_csv(out / "speed_calibration.csv", index=False, encoding="utf-8-sig")
    return clock, pairs, windows, speed
