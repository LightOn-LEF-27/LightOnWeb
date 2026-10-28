"""GPS 로 나눈 랩 · 주행 구간에 주최측 에너지미터의 전압 · 전류를 붙인다 → 랩별 에너지.

에너지미터(FSK-EEM)는 GPS 가 없고 자기 시계(사람이 맞춘 RTC)로만 시각을 적는다. 랩은 GPS(로거 또는 온보드 영상)로
나눈다. 두 장비를 같은 시간축에 놓으려면 둘이 같이 잰 것이 있어야 하는데 같은 물리량은 없다. 대신

    에너지미터 전력  P = V·I      ≈      차를 미는 일률  (m·a + 주행저항)·v      ← GPS

가 같은 모양으로 오르내린다는 것을 쓴다 (가속 페달을 밟으면 전력이 오르고 차가 빨라진다). 짧은 창마다 가장 닮은
위치를 찾고 그 짝들에 직선을 맞추는 것은 `sync` 그대로다 → 시계 차(offset)와 시계 속도(rate)가 같이 나온다.

    GPS 시각 = 미터 레코딩 시작(미터 시계) + a + f × 미터 경과시간

GPS 쪽 일률은 **위치에서 낸 속도**로 만든다 — 수신기가 주는 속도 열이 아니다. 랩은 위치(계측선 통과)로 끊으므로
시계도 위치의 시간축에 맞춰야 하는데, GoPro 의 속도 열은 위치보다 1/10 초 남짓 늦다.

먼저 `lefdata energymeter` 와 랩 분할(`lefdata laps` 또는 `gopro --laps`)이 끝나 있어야 한다.

GPS 로거가 같은 팩 전류를 CAN 으로 받아 두었으면(25Evo Monolith 의 Orion `0x447` — `lefdata evo-can` 이 푼 `orion_can.parquet`)
그 전류에 미터 전류를 직접 맞춘다 — 같은 물리량이라 가장 정확하다 (`method = can_current`, 레코딩 앞 · 뒤 절반에서 따로 재 시계 속도도).
그것도 파형도 안 되면(주행이 짧아 맞출 창이 모자람) 같은 전원 스위치에 물린 GPS 로거와 켜지고 꺼진 시각으로 잰 시계 차
(`derived/power_cycles.csv`, `lefdata clocks`)를 쓴다 — `method = power_cycle`, 시계 속도는 1 로 둔다. 구간마다 다시 대 보는
검증(`em_lag_s`)은 어느 방법이든 한다.
결과는 해석이므로 derived/ 에 둔다.

    em_alignment.csv   에너지미터 레코딩 ↔ GPS 레코딩: 시계 차 · 시계 속도 · 맞은 창 수 · 상관
    lap_energy.csv     laps.csv 의 랩마다 에너지 · 전력 · 전류 · 전압
    run_energy.csv     runs.csv 의 주행 구간마다 같은 값 (피트 출입 랩까지)

앞쪽 열은 원래 표(laps.csv · runs.csv)의 열 그대로라 그 표와 합칠 수 있고, `em_recording` · `em_t_start_s` · `em_t_end_s` 로
normalized/ 의 energy 표에서 같은 구간을 잘라낼 수 있다.

붙였는지 믿을 수 있나 — 구간마다 같이 적는다
    cover_share    구간 중 에너지미터가 기록하고 있던 비율. MIN_COVER 에 못 미치면 값을 비운다
    em_lag_s       그 구간만 따로 다시 대 봤을 때 남는 어긋남(초). 전체에 맞춘 직선과 독립인 검증 — 0 근처여야 한다.
                   양수 = 그 구간의 전력이 직선이 말한 것보다 늦게 있다
    em_corr        그때의 상관

값을 읽을 때
    - 에너지는 방전(discharge)과 회생(regen)을 따로 적분한다. net = 방전 − 회생
    - `p500_max_kw` 는 500 ms 이동평균의 최댓값이다 (출력 80 kW 한도를 판정하는 방식, 차량기술규정 제68조)
    - 구간 경계는 GPS 위치로 잡은 것이라 미터 시간으로 ±0.1 s 쯤 불확실하다. 계측선이 가속 구간에 있으면 한 랩의
      에너지가 수 Wh 까지 이웃 랩으로 넘어갈 수 있다 (합계에는 영향이 없다)
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pandas as pd
import pyarrow.parquet as pq

from . import laps, lef26_clock as lc, sync

STEP_S = 0.05               # 두 파형을 놓는 격자
POS_SMOOTH_S = 0.5          # 위치를 이만큼 이동평균한 뒤 미분해 속도를 낸다
SMOOTH_S = 1.0              # 가속도 · 전력의 이동평균. 노면 진동이 아니라 페달을 밟고 떼는 것을 본다
RESIST_MS2 = 0.3            # 주행저항을 가속도로 친 어림값. 모양만 맞추므로 정밀할 필요가 없다 (0 ~ 0.5 에서 결과가 같다)
GPS_GAP_S = 1.0             # GPS 표본이 이만큼 비면 그 사이는 모르는 것으로 둔다
SEARCH_S = 600.0            # 두 장비의 시계가 이 안에서 어긋나 있다고 본다
COARSE_WIN_S, COARSE_HOP_S, COARSE_TOL_S = 60.0, 20.0, 0.5
FINE_WIN_S, FINE_HOP_S, FINE_TOL_S, FINE_SLACK_S = 30.0, 10.0, 0.3, 3.0
MIN_STD_KW = 2.0            # 전력이 이만큼도 안 변하는 창(정지 · 정속)은 어디에나 맞으므로 건너뛴다
MIN_WINDOWS = 10            # 직선 위에 모인 창이 이보다 적으면 맞췄다고 하지 않는다
MIN_INLIER_SHARE = 0.6
MIN_CORR = 0.7
MIN_WH = 50.0               # 이만큼도 쓰지 않은 레코딩은 주행이 아니다
MIN_COVER = 0.98            # 에너지미터가 구간의 이만큼을 기록하지 않았으면 값을 내지 않는다
P500_S = 0.5
COAST_A = 5.0               # 전류의 크기가 이보다 작으면 페달을 뗀 것으로 본다
LAP_LAGS_S = np.arange(-1.0, 1.0001, 0.02)
CAN_SPAN_S = 3.0            # CAN 전류에 맞출 때 짐작한 시계 차(전원 주기 · 파형) 둘레 이만큼을 훑는다
CAN_STEP_S = 0.01
CAN_MIN_CORR = 0.95         # 같은 물리량이라 이보다 덜 닮으면 맞추지 않는다

ALIGN_COLS = ["em_recording", "gps_recording", "method", "accepted", "windows", "inliers", "corr_median", "em_minus_gps_s", "f",
              "rms_s", "span_s", "gps_t_abs_at_em_start", "discharge_wh", "regen_wh"]
JOIN_COLS = ["t_abs_start", "em_recording", "em_t_start_s", "em_t_end_s", "cover_share", "em_lag_s", "em_corr"]
VALUE_COLS = ["discharge_wh", "regen_wh", "net_wh", "wh_per_km", "p_mean_kw", "p_max_kw", "p500_max_kw",
              "i_max_a", "i_min_a", "i_rms_a", "v_min_v", "v_max_v", "coast_share"]
LAP_KEEP = ["recording_id", "label", "run", "lap", "lap_time_s", "distance_m", "vmax_kmh"]
RUN_KEEP = ["recording_id", "label", "run", "duration_s", "distance_m", "vmax_kmh", "full_laps"]


def _smooth(x: np.ndarray, seconds: float) -> np.ndarray:
    """가운데 맞춘 이동평균. 빈 칸(NaN)은 건너뛰고, 창의 절반도 못 채우면 NaN."""
    n = max(int(round(seconds / STEP_S)), 1)
    return pd.Series(x).rolling(n, center=True, min_periods=n // 2 + 1).mean().to_numpy()


# ---------------------------------------------------------------- 두 장비의 파형

@dataclass
class Meter:
    """에너지미터 레코딩 하나. 시각은 레코딩 경과시간(초), start 는 미터 시계로 본 시작(UTC 초)."""
    rec: str
    start: float
    t: np.ndarray
    volt: np.ndarray
    amp: np.ndarray
    kw: np.ndarray
    p500_kw: np.ndarray         # 500 ms 이동평균 전력
    cum_dis_wh: np.ndarray      # 처음부터 그 표본까지 방전한 에너지
    cum_reg_wh: np.ndarray      # 회생으로 돌려받은 에너지 (양수)
    k0: int                     # 격자: 칸 k0 + i 의 가운데 시각이 (k0 + i + 0.5) × STEP_S
    grid_kw: np.ndarray


@dataclass
class Track:
    """GPS 레코딩 하나: 경과시간 ↔ GPS 시각(UTC 초), 그리고 위치에서 낸 '차를 미는 일률' 의 모양."""
    rec: str
    t_rel: np.ndarray
    t_abs: np.ndarray
    k0: int                     # 격자의 첫 칸 번호 (GPS 시각 기준)
    drive: np.ndarray

    def to_abs(self, t_rel_s) -> np.ndarray:
        return np.interp(np.asarray(t_rel_s, float), self.t_rel, self.t_abs)


def drive_power(t_s, lat, lon) -> tuple[int, np.ndarray]:
    """GPS 위치 → 격자 위의 '차를 미는 일률' 모양 (첫 칸 번호, 값). 단위는 W/kg 꼴이지만 크기는 쓰지 않는다.

    v × (a + 주행저항 어림) 에서 음수(제동)는 0 으로 자른다 — 제동은 대부분 마찰 브레이크가 해서 전력에 보이지 않는다.
    칸의 값은 칸 가운데 시각의 값이라 `sync.grid` 로 만든 칸 평균과 같은 시간축에 놓인다.
    """
    t_s = np.asarray(t_s, float)
    x, y = laps.to_local_m(lat, lon, float(np.mean(lat)), float(np.mean(lon)))
    k0 = int(np.ceil(t_s[0] / STEP_S))
    at = (k0 + np.arange(max(int(np.floor(t_s[-1] / STEP_S)) - k0, 0)) + 0.5) * STEP_S
    xs, ys = _smooth(np.interp(at, t_s, x), POS_SMOOTH_S), _smooth(np.interp(at, t_s, y), POS_SMOOTH_S)
    v = np.hypot(np.gradient(xs, STEP_S), np.gradient(ys, STEP_S))
    drive = np.clip(v * (_smooth(np.gradient(v, STEP_S), SMOOTH_S) + RESIST_MS2), 0.0, None)
    i = np.clip(np.searchsorted(t_s, at), 1, len(t_s) - 1)
    drive[(t_s[i] - t_s[i - 1]) > GPS_GAP_S] = np.nan            # 끊긴 사이를 직선으로 이은 값은 가짜다
    return k0, drive


def make_meter(rec: str, start: float, t_s, volt, amp) -> Meter:
    t, volt, amp = np.asarray(t_s, float), np.asarray(volt, float), np.asarray(amp, float)
    kw = volt * amp / 1000.0
    seg = (kw[1:] + kw[:-1]) / 2 * np.diff(t) / 3.6                        # kW × s → Wh
    cum = lambda part: np.concatenate([[0.0], np.cumsum(part)])
    n500 = max(int(round(P500_S / max(float(np.median(np.diff(t))), 1e-6))), 1) if len(t) > 1 else 1
    k0, g = sync.grid(t, kw, STEP_S)
    return Meter(rec, start, t, volt, amp, kw, pd.Series(kw).rolling(n500, min_periods=n500).mean().to_numpy(),
                 cum(np.clip(seg, 0, None)), cum(np.clip(-seg, 0, None)), k0, _smooth(g, SMOOTH_S))


def read_meter(rec_dir: Path) -> Meter:
    e = pq.read_table(rec_dir / "energy.parquet", columns=["t_rel_ms", "t_abs", "valid", "voltage_v", "current_a"]).to_pandas()
    start = float(lc._epoch_s(e["t_abs"].iloc[[0]])[0]) - e["t_rel_ms"].iloc[0] / 1000.0
    e = e[e["valid"]]
    return make_meter(rec_dir.name, start, e["t_rel_ms"].to_numpy() / 1000.0, e["voltage_v"].to_numpy(), e["current_a"].to_numpy())


def read_track(rec_dir: Path) -> Track:
    g = pq.read_table(rec_dir / "gps.parquet", columns=["t_rel_ms", "t_abs", "fix", "lat", "lon"]).to_pandas()
    g = g[g["fix"] & g["t_abs"].notna()]
    t_abs = lc._epoch_s(g["t_abs"])
    return Track(rec_dir.name, g["t_rel_ms"].to_numpy() / 1000.0, t_abs, *drive_power(t_abs, g["lat"].to_numpy(), g["lon"].to_numpy()))


# ---------------------------------------------------------------- 시계 맞추기

@dataclass
class Alignment:
    """GPS 시각 = meter.start + a + f × 미터 경과시간."""
    meter: Meter
    track: Track
    a: float
    f: float
    windows: int
    inliers: int
    corr_median: float
    rms_s: float
    span_s: float
    t_mid: float                # 맞은 창들의 가운데 (미터 경과시간)
    method: str = "wave"        # wave = 전력 파형 · can_current = GPS 로거가 CAN 으로 받은 팩 전류 · power_cycle = 같은 전원 스위치로 잰 시계 차

    @property
    def accepted(self) -> bool:
        if self.method in ("power_cycle", "can_current"):
            return True
        return (self.inliers >= MIN_WINDOWS and self.inliers >= MIN_INLIER_SHARE * self.windows
                and self.corr_median >= MIN_CORR)

    @property
    def em_minus_gps_s(self) -> float:
        """미터 시계가 GPS 보다 앞선 정도 (맞은 구간의 가운데에서)."""
        return self.t_mid - (self.a + self.f * self.t_mid)

    def to_gps(self, t_em_s) -> np.ndarray:
        return self.meter.start + self.a + self.f * np.asarray(t_em_s, float)

    def to_em(self, t_gps_abs) -> np.ndarray:
        return (np.asarray(t_gps_abs, float) - self.meter.start - self.a) / self.f


def align(meter: Meter, track: Track, search_s: float = SEARCH_S) -> Alignment | None:
    """전력 ↔ 차를 미는 일률을 창별로 대어 시계 차와 시계 속도를 구한다. 닮은 곳이 없으면 None.

    두 단계다. 넓은 창(60 s)으로 ±search_s 를 훑어 대략의 직선을 잡고 — 같은 코스를 도는 랩끼리는 닮았으므로
    틀린 짝이 섞이지만 한 직선에 모이는 것은 맞는 짝뿐이다 — 그 직선 둘레 ±3 s 를 좁은 창(30 s)으로 다시 잰다.
    """
    sig_k0 = track.k0 - int(round(meter.start / STEP_S))          # GPS 격자를 '미터 시작 = 0' 인 시간축으로
    same = lambda t: np.asarray(t, float)
    w = sync.match_windows(meter.k0, meter.grid_kw, sig_k0, track.drive, step=STEP_S, win_s=COARSE_WIN_S,
                           hop_s=COARSE_HOP_S, min_std=MIN_STD_KW, expect=same,
                           slack=lambda t: np.full(len(t), search_s))
    fit = sync.fit_clock(w["ref_s"], w["sig_s"], tol_s=COARSE_TOL_S)
    if fit is None:
        return None
    a, f = fit["a"], fit["f"]
    w = sync.match_windows(meter.k0, meter.grid_kw, sig_k0, track.drive, step=STEP_S, win_s=FINE_WIN_S,
                           hop_s=FINE_HOP_S, min_std=MIN_STD_KW, expect=lambda t: a + f * np.asarray(t, float),
                           slack=lambda t: np.full(len(t), FINE_SLACK_S))
    fit = sync.fit_clock(w["ref_s"], w["sig_s"], slopes=np.arange(f - 0.002, f + 0.0020001, 0.00005), tol_s=FINE_TOL_S)
    if fit is None:
        return None
    inl = fit["inliers"]
    # 격자를 미터 시작에 맞추며 반올림한 만큼(STEP_S 의 절반 이하)을 되돌린다
    shift = int(round(meter.start / STEP_S)) * STEP_S - meter.start
    return Alignment(meter, track, fit["a"] + shift, fit["f"], len(w), fit["n"], float(w["corr"][inl].median()),
                     fit["rms_s"], fit["span_s"], float(w["ref_s"][inl].mean()))


def segment_lag(al: Alignment, t0_abs: float, t1_abs: float) -> tuple[float | None, float | None]:
    """구간 하나만 따로 다시 대 본다 → (남는 어긋남 초, 상관). 잴 파형이 없으면 (None, None)."""
    tr, me = al.track, al.meter
    i0 = max(int(np.ceil(t0_abs / STEP_S)) - tr.k0, 0)
    i1 = min(int(np.floor(t1_abs / STEP_S)) - tr.k0, len(tr.drive))
    if i1 - i0 < 40:
        return None, None
    d = tr.drive[i0:i1]
    t_em = al.to_em((tr.k0 + np.arange(i0, i1) + 0.5) * STEP_S)
    at = (me.k0 + np.arange(len(me.grid_kw)) + 0.5) * STEP_S
    best = (None, None)
    for lag in LAP_LAGS_S:
        p = np.interp(t_em + lag, at, me.grid_kw, left=np.nan, right=np.nan)
        ok = np.isfinite(p) & np.isfinite(d)
        if ok.sum() < 40 or p[ok].std() < 1e-6 or d[ok].std() < 1e-9:
            continue
        c = float(np.corrcoef(p[ok], d[ok])[0, 1])
        if best[1] is None or c > best[1]:
            best = (round(float(lag), 2), round(c, 3))
    return best


# ---------------------------------------------------------------- 구간 하나를 요약한다

def segment_energy(me: Meter, r0: float, r1: float, distance_m: float | None = None) -> dict:
    """미터 경과시간 [r0, r1) 의 에너지 · 전력 · 전류 · 전압. 경계의 에너지는 표본 사이를 보간한다."""
    m = (me.t >= r0) & (me.t < r1)
    if m.sum() < 2:
        return {}
    dis = float(np.diff(np.interp([r0, r1], me.t, me.cum_dis_wh))[0])
    reg = float(np.diff(np.interp([r0, r1], me.t, me.cum_reg_wh))[0])
    amp, volt, p500 = me.amp[m], me.volt[m], me.p500_kw[m]
    out = {"discharge_wh": round(dis, 1), "regen_wh": round(reg, 1), "net_wh": round(dis - reg, 1),
           "p_mean_kw": round((dis - reg) * 3.6 / (r1 - r0), 2), "p_max_kw": round(float(me.kw[m].max()), 1),
           "p500_max_kw": round(float(np.nanmax(p500)), 1) if np.isfinite(p500).any() else None,
           "i_max_a": round(float(amp.max()), 1), "i_min_a": round(float(amp.min()), 1),
           "i_rms_a": round(float(np.sqrt(np.mean(amp ** 2))), 1),
           "v_min_v": round(float(volt.min()), 1), "v_max_v": round(float(volt.max()), 1),
           "coast_share": round(float((np.abs(amp) < COAST_A).mean()), 3)}
    if distance_m:
        out["wh_per_km"] = round((dis - reg) / (distance_m / 1000.0), 1)
    return out


def summarize_segment(aligned: list[Alignment], track: Track, t0_rel: float, t1_rel: float,
                      distance_m: float | None = None) -> dict:
    """GPS 경과시간으로 준 구간 → 그 구간을 가장 많이 덮는 에너지미터 레코딩의 값."""
    t0, t1 = (float(x) for x in track.to_abs([t0_rel, t1_rel]))
    row = {"t_abs_start": lc._iso(t0), "cover_share": 0.0}
    best = None
    for al in aligned:
        r0, r1 = (float(x) for x in al.to_em([t0, t1]))
        cover = max(0.0, min(r1, float(al.meter.t[-1])) - max(r0, float(al.meter.t[0]))) / (r1 - r0) if r1 > r0 else 0.0
        if cover > row["cover_share"]:
            row["cover_share"], best = round(cover, 3), (al, r0, r1)
    if best is None:
        return row
    al, r0, r1 = best
    row.update({"em_recording": al.meter.rec, "em_t_start_s": round(r0, 3), "em_t_end_s": round(r1, 3)})
    if row["cover_share"] < MIN_COVER:
        return row
    lag, corr = segment_lag(al, t0, t1)
    row.update({"em_lag_s": lag, "em_corr": corr, **segment_energy(al.meter, r0, r1, distance_m)})
    return row


# ---------------------------------------------------------------- 이벤트 전체

def driven_meters(norm: Path) -> list[Path]:
    """달린 기록이 있는 에너지미터 레코딩 (HV 가 켜졌고 MIN_WH 넘게 쓴 것)."""
    index = norm / "recordings_energymeter.csv"
    if not index.exists():
        raise FileNotFoundError("recordings_energymeter.csv 가 없다 — 먼저 lefdata energymeter 를 돌릴 것")
    rows = pd.read_csv(index, encoding="utf-8-sig")
    return [norm / r for r in rows.loc[rows["hv_on"] & (rows["discharge_wh"] >= MIN_WH), "recording_id"]]


def from_power_cycle(meter: Meter, track: Track, em_minus_gps_s: float) -> Alignment:
    """같은 전원 스위치로 켜지고 꺼진 두 장비 — 시계 차 하나로 맞춘다 (시계 속도 1, 둘 다 크리스털 시계)."""
    return Alignment(meter, track, -float(em_minus_gps_s), 1.0, 0, 0, np.nan, np.nan, 0.0, float(np.mean(meter.t)), "power_cycle")


def _can_lag(t_can, i_can, t_me, i_me, guess: float) -> tuple[float, float]:
    """CAN 팩 전류(GPS 로거 시계)와 미터 전류(미터 시계)가 가장 잘 겹치는 '미터 시계 − GPS 시계' (guess 둘레) → (값, 상관)."""
    best = (np.nan, -1.0)
    for d in np.arange(guess - CAN_SPAN_S, guess + CAN_SPAN_S + 1e-9, CAN_STEP_S):
        m = np.interp(t_can + d, t_me, i_me, left=np.nan, right=np.nan)
        ok = np.isfinite(m)
        if ok.sum() < 100 or np.std(m[ok]) < 1e-6:
            continue
        c = float(np.corrcoef(i_can[ok], m[ok])[0, 1])
        if c > best[1]:
            best = (float(d), c)
    return best


def from_can_current(meter: Meter, track: Track, rec_dir: Path, guess: float) -> Alignment | None:
    """GPS 로거가 CAN 으로 받은 팩 전류(`orion_can.parquet`)에 미터 전류를 맞춘다 — 레코딩 앞 · 뒤 절반에서 따로 재 시계 속도까지. 덜 닮으면 None."""
    path = rec_dir / "orion_can.parquet"
    if not path.exists():
        return None
    c = pd.read_parquet(path, columns=["t_abs", "current_a", "checksum_ok"])
    c = c[c["checksum_ok"] & c["t_abs"].notna()]
    t_c, i_c = lc._epoch_s(c["t_abs"]), c["current_a"].to_numpy(float)
    t_me = meter.start + meter.t
    inside = (t_c + guess >= t_me[0]) & (t_c + guess <= t_me[-1])
    if inside.sum() < 200:
        return None
    t_c, i_c = t_c[inside], i_c[inside]
    half = len(t_c) // 2
    fits = [_can_lag(t_c[s], i_c[s], t_me, meter.amp, guess) for s in (slice(0, half), slice(half, None))]
    if min(f[1] for f in fits) < CAN_MIN_CORR:
        return None
    x = [float(np.median(t_c[s])) + d - meter.start for s, (d, _) in zip((slice(0, half), slice(half, None)), fits)]   # 미터 경과시간
    d = [f[0] for f in fits]
    f = 1.0 - (d[1] - d[0]) / (x[1] - x[0]) if x[1] > x[0] else 1.0       # 미터 시계 − GPS 시계 = x (1 − f) − a
    a = x[0] * (1.0 - f) - d[0]
    return Alignment(meter, track, a, f, 2, 2, min(fi[1] for fi in fits), np.nan, float(x[1] - x[0]), float(np.mean(x)), "can_current")


def _power_cycles(out: Path) -> dict[tuple[str, str], float]:
    """derived/power_cycles.csv → {(에너지미터 레코딩, GPS 레코딩): 미터 시계 − GPS 로거 시계 (s)}. 없으면 빈 것."""
    path = out / "power_cycles.csv"
    if not path.exists():
        return {}
    rows = pd.read_csv(path, encoding="utf-8-sig")
    return {(r["energymeter"], r["monolith"]): float(r["em_minus_monolith_s"]) for _, r in rows.iterrows()}


def align_event(event_dir: Path) -> list[Alignment]:
    """랩이 있는 GPS 레코딩마다 시계상 겹칠 수 있는 에너지미터 레코딩을 전부 대 본다 (맞지 않은 것도 돌려준다).

    GPS 로거가 팩 전류를 CAN 으로 받았으면 그것에 맞춘 것 하나만 (가장 정확하다). 아니면 파형으로 맞춘 것, 그것이 안 되고
    같은 전원 주기(power_cycles.csv)에 있으면 그 시계 차로 맞춘 것을 더한다.
    """
    norm, out = event_dir / "normalized", event_dir / "derived"
    runs = pd.read_csv(out / "runs.csv", encoding="utf-8-sig")
    meters = [read_meter(p) for p in driven_meters(norm)]
    cycles = _power_cycles(out)
    found = []
    for rid in runs["recording_id"].unique():
        track = read_track(norm / rid)
        for me in meters:
            if me.start + me.t[-1] < track.t_abs[0] - SEARCH_S or me.start + me.t[0] > track.t_abs[-1] + SEARCH_S:
                continue
            guess, al = cycles.get((me.rec, rid)), None
            if guess is None:
                al = align(me, track)
                guess = al.em_minus_gps_s if al is not None and al.accepted else None
            can = from_can_current(me, track, norm / rid, guess) if guess is not None else None
            if can is not None:
                found.append(can)
                continue
            al = al if al is not None else align(me, track)
            if al is not None:
                found.append(al)
            if (al is None or not al.accepted) and (me.rec, rid) in cycles:
                found.append(from_power_cycle(me, track, cycles[(me.rec, rid)]))
    return found


def alignment_table(found: list[Alignment]) -> pd.DataFrame:
    rows = [{"em_recording": al.meter.rec, "gps_recording": al.track.rec, "method": al.method, "accepted": al.accepted, "windows": al.windows,
             "inliers": al.inliers, "corr_median": round(al.corr_median, 3), "em_minus_gps_s": round(al.em_minus_gps_s, 2),
             "f": round(al.f, 5), "rms_s": round(al.rms_s, 3), "span_s": round(al.span_s, 1),
             "gps_t_abs_at_em_start": lc._iso(float(al.to_gps(0.0))),
             "discharge_wh": round(float(al.meter.cum_dis_wh[-1]), 1), "regen_wh": round(float(al.meter.cum_reg_wh[-1]), 1)}
            for al in found]
    return pd.DataFrame(rows, columns=ALIGN_COLS)


def run_event(event_dir: Path) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    """→ (시계 맞춘 결과, 랩별 에너지, 주행 구간별 에너지). derived/ 에 CSV 로도 쓴다."""
    out = event_dir / "derived"
    for need in ("runs.csv", "laps.csv"):
        if not (out / need).exists():
            raise FileNotFoundError(f"{need} 가 없다 — 먼저 랩 분할(lefdata laps 또는 gopro --laps)을 돌릴 것")
    found = align_event(event_dir)
    table = alignment_table(found)
    table.to_csv(out / "em_alignment.csv", index=False, encoding="utf-8-sig")
    good = [al for al in found if al.accepted]

    def attach(src: str, keep: list[str]) -> pd.DataFrame:
        seg = pd.read_csv(out / src, encoding="utf-8-sig")
        rows = []
        for _, s in seg.iterrows():
            mine = [al for al in good if al.track.rec == s["recording_id"]]
            if not mine:
                continue
            rows.append({**{c: s[c] for c in keep},
                         **summarize_segment(mine, mine[0].track, float(s["t_start_s"]), float(s["t_end_s"]), float(s["distance_m"]))})
        return pd.DataFrame(rows).reindex(columns=keep + JOIN_COLS + VALUE_COLS)

    lap_df, run_df = attach("laps.csv", LAP_KEEP), attach("runs.csv", RUN_KEEP)
    lap_df.to_csv(out / "lap_energy.csv", index=False, encoding="utf-8-sig")
    run_df.to_csv(out / "run_energy.csv", index=False, encoding="utf-8-sig")
    return table, lap_df, run_df
