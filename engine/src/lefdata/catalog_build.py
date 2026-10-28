"""주행 데이터 카탈로그 — 우리 데이터 폴더에서 `catalog.Session` 을 채운다 (팀 계층).

    lefdata catalog [--event E]
        → <이벤트>/derived/catalog/<세션>/page.md · page.json · figures/ · files/     세션 페이지 (원본)
        → 07_데이터/_catalog/README.md · sessions.csv · recordings.csv · code/*.md   첫 페이지 · 세션 표 · 보유 현황 · 코드 페이지

세션 = `derived/runs.csv` 의 주행 구간 하나 (GPS 로 나눈 것, 차가 나갔다 들어올 때까지). 지금 채우는 데이터 종류:

    gps         위치 · 속도            GPS 가 있는 레코딩
    imu         차량 거동              같은 레코딩의 가속도계 · 자이로. 지금은 온보드 영상(카메라 축 → 차량 축)만
    battery     배터리 전압 · 전류     주최측 에너지미터. `em_laps` 가 GPS 와 시계를 맞춘 것만
    video       온보드 영상            raw_video/ 의 파일 목록
    motor · bms · thermistor · suspension
                LEF-26 차량 로거        영상과 시계를 맞춘 로거 레코딩 (`lef26-clock` 의 시계 지도로 로거 시각 → 세션 시간)
    battery · bms · motor
                25Evo STM32 백업 로거   Monolith GPS 와 시계를 맞춘 레코딩 (`evo-stm32 --clock gps` 가 t_abs 를 채운 것)

GPS 가 없는 주행은 **시각 구간 세션**으로 넣는다 (`[[timed_session]]` — Run Sheet 의 시각이나 장비가 켜져 있던 동안).
GPS 가 없으니 랩 · 코스가 없고, 그 시각에 기록이 있는 장비(에너지미터 · 25Evo STM32 백업 로거 · Orion BMS · LEF-26 로거)를
**기준 시계**로 옮겨 자른다. 장비마다 시계가 다르므로 기준 시계로 옮길 값을 적어 둔다:

    [event] clock = "<시계>"                    start · end 를 적은 시계 (세션 시작 시각도 이 시계로 보인다)
    [clocks.<시계>] offset_s = <초>             그 장비 시계 + offset_s = 기준 시계. recordings = {레코딩 = 초} 로 레코딩마다 따로
    [[timed_session]] start = "…" end = "…"     또는 recording = "<레코딩>" (그 레코딩이 켜져 있던 동안)

시계 이름은 energymeter · orion · lef26_logger · monolith · gopro (25Evo STM32 는 변환기가 맞춰 둔 장비의 시계를 따른다).
두 장비가 같은 물리량을 잰 곳은 서로 대 봐서 시계가 맞는지 확인하고, 어긋나 보이면 품질 경고(clock_mismatch)를 단다.
그 구간을 GPS 레코딩(GoPro · Monolith, 시계 차를 아는 것)이 거의 다 덮으면 GPS 세션과 같은 블록 · 랩 · 코스를 낸다 —
구간을 GPS 시간으로 옮겨 GPS 세션처럼 만들고, 시각으로만 붙일 수 있는 장비는 빈 자리에 더한다. 그 주행은 자동 GPS 세션에서 뺀다.

사람이 아는 것(세션 이름 · 드라이버 · 없는 데이터 · 주의)은 `<이벤트>/catalog_notes.toml` 에 적는다.
이 파일이 있는 이벤트만 카탈로그에 들어간다. 드라이버 실명은 적지 않는다.
`[event] sessions = "auto"` 면 이름을 적지 않은 주행 구간도 들어간다 — 랩이 있으면 트랙, 가속 · 스키드패드 표에 있으면 그 종목,
나머지(피트 이동 등)는 넣지 않는다. 이름은 "트랙 1" 처럼 그날의 종목별 순서, 드라이버는 비워 둔다 (`[[session]]` 에 적은 것이 우선).
"""

from __future__ import annotations

import datetime as dt
import json
import os
import subprocess
import tomllib
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pandas as pd
import pyarrow.parquet as pq

from . import em_laps, laps as lapmod, lef26, lef26_clock, lef26_laps, paths
from .catalog import (HAVE, NONE, SEEK, WARN, Channel, CodeRef, DataBlock, Export, FileRef, LapColumn, Missing, Session, code_html,
                      size_text, to_markdown, to_notion, write_session)

G = 9.80665
TZ = "Asia/Seoul"
STEP_S = 0.05               # 차량 거동을 내는 격자 (20 Hz)
SMOOTH_S = 0.5              # 가속도 · 요레이트의 이동평균 (시험 주행 보고서와 같다)
GG_STEP_S = 0.1
STILL_S = 10.0              # 이만큼 가만히 있던 곳에서 중력 방향을 잰다
STILL_SEARCH_S = 300.0      # 세션 앞뒤 이 안에서 찾는다
STILL_STD_G = 0.03          # 세 축의 흔들림이 이보다 작아야 '가만히' 로 본다
MOVING_KMH = 10.0
REFINE_MAX_S = 4.0          # 세션 안에서 회전수 ↔ GPS 속도를 다시 대 볼 범위 (±)
REFINE_MIN_S = 0.5          # 이보다 적게 어긋났으면 옮기지 않는다 (GPS 속도 자체가 0.1 ~ 0.2 s 늦다)
REFINE_MIN_CORR = 0.9       # 옮긴 뒤의 상관이 이보다 높고
REFINE_GAIN = 0.05          #   옮기기 전보다 이만큼 좋아질 때만 옮긴다
EM_REFINE_MIN_S = 1.0       # 에너지미터 전력에 다시 맞출 때는 이보다 크게 어긋난 것만 옮긴다 — 백업 로거 시각이 1 s 단위이고,
                            #   에너지미터 쪽도 긴 레코딩에서는 구간마다 조금 벗어난다 (그보다 작은 차이를 쫓지 않는다)
ACCEL_LEAD_S = 10.0         # 가속 출발(정지)은 주행 구간 시작(10 km/h 를 넘는 순간)보다 앞이다 — 그만큼 앞까지 본다
AGREE_SMOOTH_S = 1.0        # 두 장비가 같은 물리량을 잰 것을 대 볼 때 이만큼 평균한 뒤 (1 초 간격 장비에 맞춘다)
AGREE_TOL_S = {"stm32": 1.0, "orion": 2.0, "lef26_logger": 1.0}   # 이보다 더 어긋나 보이면 경고 — 그 장비 시각의 해상도 · 평균 길이
AGREE_MIN_CORR = {"stm32": 0.8, "orion": 0.6, "lef26_logger": 0.8}   # 파형이 이보다 덜 닮았으면 어긋남을 말하지 않는다 (Orion 은 1 초 단위 평균이라 덜 닮는다)
CLOCK_SLACK_S = 3600.0      # 기준 시계로 옮길 값을 모르는 장비가 구간 앞뒤 이 안에 있으면 멈춘다 (말없이 빼지 않게)
GPS_COVER_MIN = 0.9         # 시각 구간을 GPS 레코딩이 이만큼 덮으면 그 GPS 로 랩 · 코스를 낸다 (구간은 GPS 가 있는 동안으로 줄인다)
RUN_COVER_MIN = 0.5         # GPS 주행 구간이 시각 구간 안에 이만큼 들면 그 주행은 시각 구간 세션의 것 (자동 GPS 세션으로 따로 만들지 않는다)
CAMERA_FORWARD = -1.0       # GoPro HERO5 의 a2_g 는 + 가 뒤쪽이다 (GPS 가속도와 대조해 정함)
CAMERA_YAW = 1.0            # 위 축 둘레의 각속도는 + 가 좌회전 (GPS 요레이트와 대조해 정함)
MIN_AGREE = 0.8             # 차량 축으로 바꾼 값과 GPS 에서 낸 값의 상관이 이보다 낮으면 '주의' 로 표시한다
REPO = Path(__file__).resolve().parents[2]
ROOT_NAME = "_catalog"
NOTES_NAME = "catalog_notes.toml"

SESSION_COLS = ["sid", "title", "car", "day", "start", "event_kind", "discipline", "driver", "duration_min", "distance_km",
                "laps", "best_lap_s", "have", "warn", "seek", "none"]


def _read(path: Path) -> pd.DataFrame | None:
    return pd.read_csv(path, encoding="utf-8-sig") if path.exists() else None


def _epoch_s(t_abs: pd.Series) -> np.ndarray:
    return (t_abs - pd.Timestamp("1970-01-01", tz="UTC")).dt.total_seconds().to_numpy()


def _roll(x: np.ndarray, n: int) -> np.ndarray:
    n = max(int(n), 1)
    return pd.Series(x).rolling(n, center=True, min_periods=n // 2 + 1).mean().to_numpy()


def load_notes(event_dir: Path) -> dict:
    path = event_dir / NOTES_NAME
    return tomllib.loads(path.read_text(encoding="utf-8")) if path.exists() else {}


def code_version() -> str:
    """이 페이지를 만든 코드의 커밋. 커밋하지 않은 변경이 있으면 + 를 붙인다."""
    try:
        head = subprocess.run(["git", "-C", str(REPO), "rev-parse", "--short", "HEAD"], capture_output=True, text=True, check=True).stdout.strip()
        dirty = subprocess.run(["git", "-C", str(REPO), "status", "--porcelain"], capture_output=True, text=True, check=True).stdout.strip()
        return head + ("+" if dirty else "")
    except (OSError, subprocess.CalledProcessError):
        return ""


# ---------------------------------------------------------------- 차량 거동: GPS 에서 낸 값과 카메라 축 변환

def gps_motion(t_s, x, y, grid) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    """GPS 위치 → 격자 위의 속도(m/s) · 앞뒤 가속도(g, 가속 +) · 좌우 가속도(g, 좌선회 +) · 요레이트(°/s, 좌회전 +).

    x 는 동쪽, y 는 북쪽(m). 위치를 SMOOTH_S 만큼 고른 뒤 미분한다.
    """
    step = float(grid[1] - grid[0])
    n = round(SMOOTH_S / step)
    xs, ys = _roll(np.interp(grid, t_s, x), n), _roll(np.interp(grid, t_s, y), n)
    vx, vy = np.gradient(xs, step), np.gradient(ys, step)
    ax, ay = _roll(np.gradient(vx, step), n), _roll(np.gradient(vy, step), n)
    v = np.hypot(vx, vy)
    vv = np.clip(v, 1.0, None)
    turn = vx * ay - vy * ax                                    # 왼쪽(반시계)으로 돌 때 +
    return v, (ax * vx + ay * vy) / vv / G, turn / vv / G, np.degrees(turn / vv ** 2)


def still_up(t_s: np.ndarray, acc: np.ndarray, lo: float, hi: float) -> np.ndarray | None:
    """[lo, hi] 안에서 가장 가만히 있던 STILL_S 구간의 가속도 평균 — 카메라가 본 '위' 방향. 못 찾으면 None."""
    best = None
    for a in np.arange(lo, hi - STILL_S, STILL_S / 2):
        i, j = np.searchsorted(t_s, [a, a + STILL_S])
        if j - i < 50:
            continue
        spread = float(acc[i:j].std(axis=0).max())
        if spread < STILL_STD_G and (best is None or spread < best[0]):
            best = (spread, acc[i:j].mean(axis=0))
    return None if best is None else best[1]


def camera_to_vehicle(acc: np.ndarray, gyro: np.ndarray, up: np.ndarray) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """카메라 축의 가속도(g) · 각속도(°/s) → 차량의 앞뒤 가속도(가속 +) · 좌우 가속도(좌선회 +) · 요레이트(좌회전 +).

    카메라는 앞을 보고 있지만 숙여져(또는 기울어져) 달려 있다. 서 있을 때의 가속도 평균 `up` 이 '위' 이고,
    카메라의 앞뒤 축(열 2)에서 위 성분을 뺀 것이 차의 앞뒤, 그 둘에 수직인 것이 좌우다.
    달리는 중의 차체 기울기(롤 · 피치)는 빼지 않는다 — 그만큼 중력이 섞인다.
    """
    u = up / np.linalg.norm(up)
    fwd = np.array([0.0, 0.0, 1.0]) - u[2] * u
    fwd = CAMERA_FORWARD * fwd / np.linalg.norm(fwd)
    left = np.cross(u, fwd)
    return acc @ fwd, acc @ left, CAMERA_YAW * (gyro @ u)


def _agreement(a: np.ndarray, b: np.ndarray, ok: np.ndarray) -> tuple[float, float]:
    """두 파형의 상관과 기울기(a = 기울기 × b)."""
    m = ok & np.isfinite(a) & np.isfinite(b)
    if m.sum() < 100:
        return np.nan, np.nan
    return float(np.corrcoef(a[m], b[m])[0, 1]), float(np.polyfit(b[m], a[m], 1)[0])


# ---------------------------------------------------------------- 블록

class _Run:
    """주행 구간 하나를 채우는 데 쓰는 것들 (GPS 레코딩 기준)."""

    def __init__(self, event_dir: Path, rec: str, run: pd.Series, laps: pd.DataFrame, origin: dict, notes: dict):
        self.event_dir, self.rec, self.rec_dir, self.notes = event_dir, rec, event_dir / "normalized" / rec, notes
        self.meta = json.loads((self.rec_dir / "meta.json").read_text(encoding="utf-8"))
        g = pq.read_table(self.rec_dir / "gps.parquet").to_pandas()
        g = g[g["fix"]].reset_index(drop=True)
        self.t0, self.t1 = float(run["t_start_s"]), float(run["t_end_s"])
        t = g["t_rel_ms"].to_numpy() / 1000.0
        x, y = lapmod.to_local_m(g["lat"].to_numpy(), g["lon"].to_numpy(), origin["lat0"], origin["lon0"])
        self.g_t, self.g_x, self.g_y, self.g_abs = t, x, y, _epoch_s(g["t_abs"])
        m = (t >= self.t0) & (t <= self.t1)
        self.gps = g[m].reset_index(drop=True)
        self.ts = t[m] - self.t0
        self.x, self.y = x[m], y[m]
        self.dist = np.concatenate([[0.0], np.cumsum(np.hypot(np.diff(self.x), np.diff(self.y)))])
        self.t0_abs = float(np.interp(self.t0, t, self.g_abs))
        self.laps = pd.DataFrame({"lap": laps["lap"].astype(int), "t_start_s": laps["t_start_s"] - self.t0,
                                  "t_end_s": laps["t_end_s"] - self.t0, "lap_time_s": laps["lap_time_s"],
                                  "distance_m": laps["distance_m"], "vmax_kmh": laps["vmax_kmh"], "vmin_kmh": laps["vmin_kmh"]}
                                 ).reset_index(drop=True)

    def lap_of(self, ts: np.ndarray) -> np.ndarray:
        """세션 시간 → 랩 번호 (랩 밖은 0)."""
        if self.laps.empty:
            return np.zeros(len(ts), int)
        k = np.searchsorted(self.laps["t_start_s"].to_numpy(), ts, side="right") - 1
        inside = (k >= 0) & (ts < self.laps["t_end_s"].to_numpy()[np.clip(k, 0, None)])
        return np.where(inside, self.laps["lap"].to_numpy()[np.clip(k, 0, None)], 0)

    def where(self, rel: str) -> str:
        return f"DAQ 파트 데이터 폴더 `{self.event_dir.parent.name}/{self.event_dir.name}/{rel}`"

    def extra(self, key: str) -> dict:
        return self.notes.get("blocks", {}).get(key, {})


class _Window:
    """시각 구간 하나 — 기준 시계로 [t0_abs, t1_abs] (UTC 초). 블록 함수에 _Run 대신 넘긴다. 세션 시간 = 기준 시계 − t0_abs.

    GPS 가 없으므로 랩이 없다 (빈 랩 표 — 블록 함수가 랩 열을 더해도 빈 채로 남는다).
    """

    def __init__(self, event_dir: Path, notes: dict, t0_abs: float, t1_abs: float):
        self.event_dir, self.notes, self.rec = event_dir, notes, ""
        self.t0_abs, self.t1_abs, self.t0, self.t1 = t0_abs, t1_abs, 0.0, t1_abs - t0_abs
        self.laps = pd.DataFrame({"lap": pd.Series(dtype="int64"), "t_start_s": pd.Series(dtype=float), "t_end_s": pd.Series(dtype=float),
                                  "lap_time_s": pd.Series(dtype=float)})

    def lap_of(self, ts: np.ndarray) -> np.ndarray:
        return np.zeros(len(ts), int)

    where = _Run.where
    extra = _Run.extra


def _gps_block(r: _Run) -> DataBlock:
    g = r.gps
    hz = (len(g) - 1) / max(r.ts[-1] - r.ts[0], 1e-9)
    speed = Channel("speed_kmh", "속도", "km/h", r.ts, g["speed_kmh"].to_numpy())
    table = pd.DataFrame({"t_s": r.ts.round(3), "lat": g["lat"].round(7), "lon": g["lon"].round(7), "speed_kmh": g["speed_kmh"].round(2),
                          "course_deg": g["course_deg"].round(1), "x_m": r.x.round(2), "y_m": r.y.round(2), "dist_m": r.dist.round(1),
                          "lap": r.lap_of(r.ts)})
    facts = [f"표본 {len(g):,}개 · 평균 {hz:.1f} Hz", f"최고 속도 {g['speed_kmh'].max():.1f} km/h"]
    if "dop" in g.columns:
        facts.append(f"수신 품질(DOP) 중앙값 {g['dop'].median():.1f} — 작을수록 좋음")
    lag = ["GPS 속도 값은 같은 GPS 의 위치보다 1/10 초 남짓 늦음 (GoPro). 랩 경계 · 거리는 위치로 잡은 값"] if "files" in r.meta["source"] else []
    plane = "GPS 위경도 → 평면 좌표 (m). 코스 · 계측선과 같은 좌표 — 원점은 frame_id 마다 하나"
    series = [Channel("speed_kmh", "속도", "km/h", r.ts, g["speed_kmh"].to_numpy(), cautions=lag, source=r.rec, digits=2),
              Channel("x_m", "위치 (동쪽)", "m", r.ts, r.x, sign="동쪽 +", derived=True, derivation=plane, source=r.rec, digits=2),
              Channel("y_m", "위치 (북쪽)", "m", r.ts, r.y, sign="북쪽 +", derived=True, derivation=plane, source=r.rec, digits=2)]
    return DataBlock(
        "gps", "위치 · 속도 (GPS)", HAVE, f"{r.meta['source']['logger']} · GPS {hz:.0f} Hz", facts,
        raw=_raw_sources(r), code=([CodeRef("gopro", CODE_PAGES["gopro"]["title"], "영상 파일 안의 GPS 기록을 표로")] if "files" in r.meta["source"] else [])
        + [CodeRef("laps", CODE_PAGES["laps"]["title"], "주행 구간과 랩으로 나눔")],
        outputs=[FileRef("변환된 표 (레코딩 전체)", "gps.parquet", note="lat · lon · speed_kmh · course_deg · alt_m · dop",
                         where=r.where(f"normalized/{r.rec}/"))],
        exports=[Export("gps.csv", "이 세션의 GPS (세션 시작부터의 시간 t_s · 평면 좌표 x_m 동 · y_m 북 · 달린 거리 · 랩 번호)", table,
                        f"{len(table):,} 행")],
        channels=[speed], time_keys=["speed_kmh"], trace_keys=["speed_kmh"], map_key="speed_kmh",
        lap_columns=[LapColumn("lap_time_s", "랩타임", "s", 2, chart=True), LapColumn("vmax_kmh", "최고 속도", "km/h", 1, chart=True),
                     LapColumn("vmin_kmh", "최저 속도", "km/h", 1), LapColumn("distance_m", "달린 거리", "m", 0)],
        cautions=list(r.extra("gps").get("cautions", [])), series=series)


def _raw_sources(r: _Run) -> list[FileRef]:
    """GPS · 가속도의 원본. 영상이면 영상 파일들(용량이 커서 있는 곳만), 로거면 로그 파일."""
    src = r.meta["source"]
    if "files" in src:                                           # 온보드 영상: 텔레메트리가 영상 파일 안에 있다
        folder = r.event_dir / "raw_video"
        total = sum(p.stat().st_size for p in folder.iterdir()) if folder.exists() else 0
        return [FileRef("온보드 영상 파일 (GPS · 가속도 기록이 영상 파일 안에 들어 있음)", f"raw_video/ ({len(src['files'])}개 조각)",
                        size_bytes=total, where=r.notes.get("event", {}).get("raw_where", "DAQ 파트 보관"))]
    path = r.event_dir / src["file"]
    return [FileRef("로거 원본", path.name, path, path.stat().st_size if path.exists() else 0)]


def _imu_block(r: _Run) -> DataBlock | None:
    path = r.rec_dir / "imu.parquet"
    if not path.exists() or "a2_g" not in pq.read_schema(path).names:
        return None
    imu = pq.read_table(path).to_pandas()
    t = imu["t_rel_ms"].to_numpy() / 1000.0
    near = (t >= r.t0 - STILL_SEARCH_S) & (t <= r.t1 + STILL_SEARCH_S)
    t, imu = t[near], imu[near]
    acc, gyro = imu[["a0_g", "a1_g", "a2_g"]].to_numpy(), imu[["g0_dps", "g1_dps", "g2_dps"]].to_numpy()
    up = still_up(t, acc, r.t0 - STILL_SEARCH_S, r.t0) if (t < r.t0).any() else None
    if up is None:
        up = still_up(t, acc, r.t1, r.t1 + STILL_SEARCH_S)
    if up is None:
        return None
    m = (t >= r.t0 - 2) & (t <= r.t1 + 2)
    n = round(SMOOTH_S / float(np.median(np.diff(t[m]))))
    grid = np.arange(0.0, r.t1 - r.t0, STEP_S)
    native = camera_to_vehicle(acc[m], gyro[m], up)                     # 원래 주기 (엔진이 내보낸다)
    long_g, lat_g, yaw = (np.interp(grid + r.t0, t[m], _roll(v, n)) for v in native)   # 0.5 초 평균 (페이지 · 그림 · 랩 표)

    # 독립 확인: GPS 위치에서 낸 가속도 · 요레이트와 대 본다 (같은 만큼 한 번 더 고른 뒤)
    v, g_long, g_lat, g_yaw = gps_motion(r.g_t, r.g_x, r.g_y, grid + r.t0)
    k, moving = round(SMOOTH_S / STEP_S), v * 3.6 > MOVING_KMH
    agree = {name: _agreement(_roll(a, k), b, moving) for name, a, b in
             (("앞뒤 가속도", long_g, g_long), ("좌우 가속도", lat_g, g_lat), ("요레이트", yaw, g_yaw))}
    checks = ["GPS 위치에서 낸 값과 대조 (서로 다른 센서) — " + " · ".join(f"{name} 상관 {c:.3f} (크기 비 {s:.2f})" for name, (c, s) in agree.items())]
    weak = [name for name, (c, _) in agree.items() if not c >= MIN_AGREE]

    laps = r.laps
    for col, src, how in (("long_g_max", long_g, np.nanmax), ("long_g_min", long_g, np.nanmin),
                          ("lat_left_max", lat_g, np.nanmax), ("lat_right_max", -lat_g, np.nanmax)):
        laps[col] = [float(how(src[(grid >= a) & (grid < b)])) for a, b in zip(laps["t_start_s"], laps["t_end_s"])]
    gg = pd.DataFrame({"long_g": long_g, "lat_g": lat_g}).iloc[::round(GG_STEP_S / STEP_S)].dropna()
    gg.attrs["step_s"] = GG_STEP_S
    table = pd.DataFrame({"t_s": grid.round(2), "long_g": long_g.round(4), "lat_g": lat_g.round(4), "yaw_dps": yaw.round(2),
                          "lap": r.lap_of(grid)})
    ts = t[m] - r.t0
    inside = (ts >= 0) & (ts <= r.t1 - r.t0)
    loose_at = _runs(ts[inside], imu["t_quality"].to_numpy()[m][inside] != "gps_utc", min_s=1.0)
    loose = sum(b - a for a, b in loose_at)
    cautions = ["카메라(롤후프)의 가속도계 값. 달리는 중 차체가 기우는 만큼(롤 · 피치) 중력이 섞임",
                "표와 그림의 값은 0.5 초 평균. 그보다 짧은 충격은 원본 표(200 Hz)를 볼 것"]
    if loose > 1.0:
        cautions.append(f"가속도계 시각을 GPS 에 맞추지 못한 구간이 약 {loose:.0f} s 있음 (영상 파일이 바뀌는 곳. 그 구간은 최대 2 초 늦을 수 있음)")
    if weak:
        cautions.append(f"GPS 에서 낸 값과 잘 맞지 않는 항목: {', '.join(weak)} — 부호 · 크기를 믿기 전에 확인할 것")

    # 엔진용: 원래 주기의 차량 축 값과, 위치가 있는 경고
    how = "카메라 축 → 차량 축 (차가 서 있을 때의 중력 방향을 '위' 로 삼음)"
    tilt = "카메라(롤후프)의 가속도계 값. 달리는 중 차체가 기우는 만큼(롤 · 피치) 중력이 섞임"
    series = [Channel(key, label, unit, ts[inside], v[inside], sign=sign, derived=True, derivation=how, cautions=extra, source=r.rec, digits=d)
              for (key, label, unit, sign, extra, d), v in zip(
                  (("long_g", "앞뒤 가속도", "g", "가속 +, 제동 −", [tilt], 4), ("lat_g", "좌우 가속도", "g", "좌선회 +, 우선회 −", [tilt], 4),
                   ("yaw_dps", "요레이트", "°/s", "좌회전 +", [], 2)), native)]
    findings = []
    if loose_at:
        findings.append({"level": "warn", "code": "imu_time_uncalibrated",
                         "message": f"가속도계 시각을 GPS 에 맞추지 못한 구간 {len(loose_at)}곳 · 합계 {loose:.0f} s "
                                    "(영상 파일이 바뀌는 곳. 최대 2 초 늦을 수 있음)", "t_ranges": loose_at})
    if weak:
        findings.append({"level": "warn", "code": "imu_gps_disagree", "message": f"GPS 에서 낸 값과 잘 맞지 않음: {', '.join(weak)}", "t_ranges": []})
    return DataBlock(
        "imu", "차량 거동 (가속도 · 요레이트)", WARN if weak else HAVE,
        f"{r.meta['source']['logger']} · 가속도계 · 자이로 {1 / float(np.median(np.diff(t[m]))):.0f} Hz",
        [f"앞뒤 가속도 {np.nanmin(long_g):+.2f} ~ {np.nanmax(long_g):+.2f} g (제동 − · 가속 +)",
         f"좌우 가속도 우선회 {np.nanmax(-lat_g):.2f} g · 좌선회 {np.nanmax(lat_g):.2f} g",
         f"카메라가 앞으로 {np.degrees(np.arccos(up[0] / np.linalg.norm(up))):.0f}° 기울어 달려 있어, 서 있을 때의 중력 방향으로 차량 축에 맞춤"],
        raw=_raw_sources(r), code=[CodeRef("gopro", CODE_PAGES["gopro"]["title"], "영상 파일 안의 가속도 · 자이로 기록을 표로"),
                                   CodeRef("catalog", CODE_PAGES["catalog"]["title"], "카메라 축 → 차량 축, 0.5 초 평균")],
        outputs=[FileRef("변환된 표 (레코딩 전체, 카메라 축 그대로)", "imu.parquet", note="a0_g 위아래 · a1_g 좌우 · a2_g 앞뒤 · g0 ~ g2_dps",
                         where=r.where(f"normalized/{r.rec}/"))],
        exports=[Export("imu.csv", "이 세션의 차량 거동 (20 Hz · 0.5 초 평균. long_g 가속 + · lat_g 좌선회 + · yaw_dps 좌회전 +)", table,
                        f"{len(table):,} 행")],
        channels=[Channel("long_g", "앞뒤 가속도 — 가속 +, 제동 −", "g", grid, long_g),
                  Channel("lat_g", "좌우 가속도 — 좌선회 +, 우선회 −", "g", grid, lat_g),
                  Channel("yaw_dps", "요레이트 — 좌회전 +", "°/s", grid, yaw)],
        time_keys=["long_g", "lat_g"], trace_keys=["long_g", "lat_g"],
        lap_columns=[LapColumn("long_g_max", "가속 최대", "g", 2), LapColumn("long_g_min", "제동 최대", "g", 2),
                     LapColumn("lat_left_max", "좌선회 최대", "g", 2), LapColumn("lat_right_max", "우선회 최대", "g", 2)],
        gg=gg, checks=checks, cautions=cautions + list(r.extra("imu").get("cautions", [])), series=series, findings=findings)


def _runs(t: np.ndarray, flag: np.ndarray, min_s: float = 0.0) -> list[list[float]]:
    """flag 가 이어지는 구간들 [[시작, 끝]] (세션 시간). min_s 보다 짧은 것은 뺀다."""
    if not flag.any():
        return []
    edge = np.diff(np.concatenate([[0], flag.astype(int), [0]]))
    starts, ends = np.flatnonzero(edge == 1), np.flatnonzero(edge == -1) - 1
    return [[round(float(t[a]), 3), round(float(t[b]), 3)] for a, b in zip(starts, ends) if t[b] - t[a] >= min_s]


def _em_mask(r: _Run, al: em_laps.Alignment, run_e: pd.Series) -> np.ndarray:
    """세션 구간에 든 에너지미터 표본 — 미터 시간 경계(반올림해 둔 것)와 세션 시간 둘 다로 자른다."""
    me = al.meter
    ts = al.to_gps(me.t) - r.t0_abs
    return (me.t >= float(run_e["em_t_start_s"])) & (me.t <= float(run_e["em_t_end_s"])) & (ts >= 0) & (ts <= r.t1 - r.t0)


def _em_ref(r: _Run, al: em_laps.Alignment, run_e: pd.Series) -> dict:
    """에너지미터 전력을 세션 시간으로 — 다른 장비(STM32 BMS 전력)와 대 볼 파형."""
    m = _em_mask(r, al, run_e)
    return {"t": al.to_gps(al.meter.t[m]) - r.t0_abs, "kw": al.meter.kw[m]}


def _battery_block(r: _Run, al: em_laps.Alignment, run_e: pd.Series, lap_e: pd.DataFrame, fit: pd.Series) -> DataBlock:
    me = al.meter
    m = _em_mask(r, al, run_e)
    ts = al.to_gps(me.t[m]) - r.t0_abs
    meta = json.loads((r.event_dir / "normalized" / me.rec / "meta.json").read_text(encoding="utf-8"))
    raw = r.event_dir / meta["source"]["file"]
    for col in ("net_wh", "regen_wh", "p_mean_kw", "i_max_a", "v_min_v"):
        r.laps[col] = r.laps["lap"].map(lap_e.set_index("lap")[col])
    table = pd.DataFrame({"t_s": ts.round(3), "voltage_v": me.volt[m].round(1), "current_a": me.amp[m].round(1),
                          "power_kw": me.kw[m].round(3), "lap": r.lap_of(ts)})
    lag = lap_e["em_lag_s"]
    step = np.diff(me.t[m])
    holes = float(step[step > 2 * np.median(step)].sum())
    cautions = list(r.extra("battery").get("cautions", []))
    if holes > 0.5:
        cautions.insert(0, f"표본이 빠진 곳이 있음 — 기록 간격의 두 배 넘게 빈 틈이 합계 {holes:.1f} s (세션의 {100 * holes / (r.t1 - r.t0):.1f} %). "
                           "에너지를 직접 적분할 때는 표본 개수가 아니라 시간 열(t_s)로 적분할 것")
    rec = me.rec
    series = [Channel("current_a", "배터리 전류", "A", ts, me.amp[m], sign="방전 +, 배터리로 돌아오면 −", source=rec, digits=2),
              Channel("voltage_v", "팩 전압", "V", ts, me.volt[m], source=rec, digits=2),
              Channel("power_kw", "전력", "kW", ts, me.kw[m], sign="방전 +", derived=True, derivation="전압 × 전류", source=rec, digits=3),
              Channel("power_500ms_kw", "전력 500 ms 평균", "kW", ts, me.p500_kw[m], sign="방전 +", derived=True,
                      derivation="전압 × 전류의 500 ms 이동평균 (대회 출력 한도 판정과 같은 방식)", source=rec, digits=3)]
    hole_at = [[round(float(ts[i]), 3), round(float(ts[i + 1]), 3)] for i in np.flatnonzero(step > 2 * np.median(step))]   # 맞닿은 틈도 따로 (사이에 표본 하나가 있다)
    findings = [{"level": "warn", "code": "em_dropped_samples", "t_ranges": hole_at,
                 "message": f"기록 간격의 두 배 넘게 빈 틈 {len(hole_at)}곳 · 합계 {holes:.1f} s — 그 사이에는 값이 없다 (확대하면 그래프가 끊긴다)"}] if hole_at else []
    return DataBlock(
        "battery", "배터리 전압 · 전류", HAVE, f"{meta['source']['logger']} · {1000 / meta['time']['median_dt_ms']:.0f} Hz",
        [f"표본 {int(m.sum()):,}개", f"방전 {run_e['discharge_wh']:,.0f} Wh · 배터리로 돌아온 양 {run_e['regen_wh']:,.0f} Wh · 순 {run_e['net_wh']:,.0f} Wh",
         f"거리당 {run_e['wh_per_km']:.0f} Wh/km (순 에너지 ÷ GPS 거리)",
         f"전류 최대 {run_e['i_max_a']:.0f} A · 500 ms 평균 전력 최대 {run_e['p500_max_kw']:.1f} kW (대회 한도 80 kW)",
         f"팩 전압 {run_e['v_min_v']:.0f} ~ {run_e['v_max_v']:.0f} V"],
        raw=[FileRef("에너지미터 원본 로그 (바이너리, 레코딩 전체)", raw.name, raw, raw.stat().st_size, f"{meta['counts']['records']:,} 표본 · 체크섬 실패 {meta['counts']['checksum_fail']}")],
        code=[CodeRef("energymeter", CODE_PAGES["energymeter"]["title"], "원본 로그를 전압 · 전류 표로"),
              CodeRef("em_laps", CODE_PAGES["em_laps"]["title"], "미터 시계를 GPS 에 맞추고 랩별 에너지를 냄")],
        outputs=[FileRef("변환된 표 (레코딩 전체)", "energy.parquet", note="voltage_v · current_a · lv_voltage_v · temp_c · valid",
                         where=r.where(f"normalized/{me.rec}/"))],
        exports=[Export("battery.csv", "이 세션의 전압 · 전류 · 전력 (시간 t_s 는 GPS 에 맞춘 세션 시간 · 랩 번호)", table, f"{len(table):,} 행")],
        channels=[Channel("current_a", "배터리 전류", "A", ts, me.amp[m]), Channel("voltage_v", "팩 전압", "V", ts, me.volt[m]),
                  Channel("power_kw", "전력", "kW", ts, me.kw[m])],
        time_keys=["current_a", "voltage_v", "power_kw"], trace_keys=["current_a", "power_kw"], map_key="power_kw",
        lap_columns=[LapColumn("net_wh", "순 에너지", "Wh", 0, chart=True), LapColumn("regen_wh", "돌아온 에너지", "Wh", 1),
                     LapColumn("p_mean_kw", "평균 전력", "kW", 1), LapColumn("i_max_a", "전류 최대", "A", 0, chart=True),
                     LapColumn("v_min_v", "팩 전압 최저", "V", 0)],
        checks=[f"미터 시계를 같은 전원 스위치로 켜지고 꺼진 시각에 맞춤 (lefdata clocks) — 미터 시계가 GPS 보다 {fit['em_minus_gps_s']:.2f} s 앞. "
                "주행이 짧아 전력 파형으로 맞출 창이 모자랐다" if fit.get("method", "wave") == "power_cycle" else
                f"미터에는 GPS 가 없어 전력 파형으로 시계를 맞춤 — 미터 시계가 GPS 보다 {fit['em_minus_gps_s']:.2f} s 앞, "
                f"30 초 창 {int(fit['inliers'])} / {int(fit['windows'])}개가 한 직선 위 (잔차 {fit['rms_s']:.2f} s)",
                f"랩 하나씩 따로 다시 대 본 어긋남 {lag.min():+.2f} ~ {lag.max():+.2f} s" if lag.notna().any() else
                f"이 주행 구간만 따로 다시 대 본 어긋남 {run_e['em_lag_s']:+.2f} s (상관 {run_e['em_corr']:.2f})"],
        cautions=cautions, series=series, findings=findings)


def _video_block(r: _Run) -> DataBlock | None:
    folder = r.event_dir / "raw_video"
    if "files" not in r.meta["source"] or not folder.exists():
        return None
    where = r.notes.get("event", {}).get("raw_where", "DAQ 파트 보관")
    files = sorted(folder.iterdir())
    extra = r.extra("video")
    return DataBlock("video", "온보드 영상", HAVE, extra.get("device", r.meta["source"]["logger"]),
                     [f"파일 {len(files)}개 · 합계 {size_text(sum(p.stat().st_size for p in files))} (레코딩 전체. 이 세션은 그 일부)",
                      f"있는 곳: {where}"],
                     raw=[FileRef("영상 조각" if p.suffix.upper() == ".MP4" else "저해상도 사본 (같은 GPS · 가속도 기록이 들어 있음)", p.name,
                                  p, size_bytes=p.stat().st_size) for p in files],
                     cautions=list(extra.get("cautions", [])))


# ---------------------------------------------------------------- LEF-26 차량 로거 (영상 GPS 시간축에 맞춰 붙인다)

class _Loggers:
    """이벤트의 로거 레코딩 — 영상(GPS 레코딩)마다 짝이 맞은 것을 시계 지도와 함께 한 번만 읽는다."""

    def __init__(self, event_dir: Path):
        d = event_dir / "derived"
        self.norm = event_dir / "normalized"
        self.ok = (d / "logger_clock_windows.csv").exists() and (d / "logger_clock_pairs.csv").exists()
        if self.ok:
            self.windows = pd.read_csv(d / "logger_clock_windows.csv", encoding="utf-8-sig")
            pairs = pd.read_csv(d / "logger_clock_pairs.csv", encoding="utf-8-sig")
            self.pairs = pairs[pairs["accepted"]]
        self.cache: dict[str, lef26_laps.Logger | None] = {}

    def for_video(self, video: str) -> list[lef26_laps.Logger]:
        if not self.ok:
            return []
        out = []
        for rid in self.pairs.loc[self.pairs["video"] == video, "recording_id"]:
            if rid not in self.cache:
                cm = lef26_clock.clock_map(self.windows, self.pairs, rid)
                self.cache[rid] = None if cm is None else lef26_laps.read_logger(
                    self.norm / rid, cm, self.windows.loc[self.windows["recording_id"] == rid, "true_s"].to_numpy())
            if self.cache[rid] is not None:
                out.append(self.cache[rid])
        return out


def _gather(r: _Run, logs: list, pick) -> tuple[np.ndarray, np.ndarray, list[str]]:
    """로거마다 pick(log) → (로거 경과시간, 값) 을 세션 시간으로 옮겨 이어 붙인다 → (세션 시간, 값, 쓴 레코딩)."""
    ts, vs, used = [], [], []
    for log in logs:
        t_rel, v = pick(log)
        if t_rel is None or not len(t_rel):
            continue
        t = log.cm.to_true(np.asarray(t_rel, float)) - r.t0_abs
        m = (t >= 0) & (t <= r.t1 - r.t0) & np.isfinite(np.asarray(v, float))
        if m.any():
            ts.append(t[m])
            vs.append(np.asarray(v, float)[m])
            used.append(log.rec)
    if not ts:
        return np.zeros(0), np.zeros(0), []
    t, v = np.concatenate(ts), np.concatenate(vs)
    o = np.argsort(t, kind="stable")
    return t[o], v[o], used


def _motor_signal(name: str):
    def pick(log):
        df = log.motor.get(name)
        if df is None or not len(df):
            return None, None
        fresh = lef26_laps._fresh(df["raw"].to_numpy())             # 같은 CAN 프레임이 다시 찍힌 표본은 뺀다
        return df["t_s"].to_numpy()[fresh], df["value"].to_numpy()[fresh]
    return pick


def _column(df_name: str, col: str, keep=None):
    def pick(log):
        df = getattr(log, df_name)
        if df is None or not len(df) or col not in df.columns:
            return None, None
        d = df if keep is None else df[keep(df)]
        return d["t_s"].to_numpy(), d[col].to_numpy()
    return pick


def _lap_stat(r: _Run, key: str, t: np.ndarray, v: np.ndarray, how) -> None:
    out = []
    for a, b in zip(r.laps["t_start_s"], r.laps["t_end_s"]):
        m = (t >= a) & (t < b)
        out.append(float(how(v[m])) if m.any() else np.nan)
    r.laps[key] = out


LOGGER_BY_VIDEO = "로거 시계를 영상 GPS 에 맞춤 — 로거 요레이트 ↔ 카메라 자이로 파형 (lefdata lef26-clock)."


def _logger_blocks(r, logs: list, how: str = LOGGER_BY_VIDEO) -> list[DataBlock]:
    """LEF-26 로거 블록 (모터 · BMS · 셀 온도 · 서스펜션). 로거마다 시계 지도(cm)가 로거 경과시간 → 기준 시각. 기록이 없으면 빈 목록.

    how = 로거 시계를 어떻게 맞췄나 (블록 '확인' 글의 앞부분).
    """
    if not logs:
        return []
    imu_t, _, used = _gather(r, logs, lambda log: (log.imu_t, np.ones(len(log.imu_t))))
    if not used:
        return []
    dur = r.t1 - r.t0
    gaps = [[round(float(a), 3), round(float(b), 3)] for a, b in zip(np.concatenate([[0.0], imu_t]), np.concatenate([imu_t, [dur]])) if b - a > 2.0]
    cover = 1 - sum(b - a for a, b in gaps) / dur
    clock = f"{how} 로거 레코딩 {', '.join(used)} · 이 세션의 {cover * 100:.0f} % 를 기록"
    where = r.where("normalized/<로거 레코딩>/")
    code = [CodeRef("catalog", CODE_PAGES["catalog"]["title"], "로거 시각 → 세션 시간, 세션 구간만")]
    raw = [FileRef("로거 통합본 CSV (그날 전체, 용량이 커서 있는 곳만)", "raw_merged/<날짜>_merged.csv", where=r.notes.get("event", {}).get("raw_where", "DAQ 파트 보관"))]
    sparse = "CAN 신호는 띄엄띄엄 들어와 있다 — 같은 값이 다시 찍힌 표본은 뺐고, 최댓값은 '기록된 것 중' 이다 (실제 순간 최대는 더 클 수 있다)"
    findings = [{"level": "warn", "code": "logger_not_recording", "t_ranges": gaps,
                 "message": f"로거가 기록하지 않은 구간 {len(gaps)}곳 · 합계 {sum(b - a for a, b in gaps):.0f} s (2 s 넘게 빈 곳)"}] if gaps else []
    blocks = []

    # 모터 · 인버터 (BAMOCAR 0x181)
    sig = {k: _gather(r, logs, _motor_signal(k))[:2] for k in ("n_act_pct", "iq_act_a", "iq_cmd_a", "vdc_v", "t_igbt_c", "t_motor_c")}
    series = []
    spec = (("n_act_pct", "rpm", "모터 회전수", "rpm", lambda v: v / 100 * lef26.N_MAX_RPM, "전진할 때 − (컨트롤러가 보는 회전 방향 그대로)", 0,
             [f"컨트롤러 설정값 N-100 % = {lef26.N_MAX_RPM:.0f} rpm 기준으로 바꾼 값", sparse]),
            ("iq_act_a", "iq_a", "토크 전류", "A", None, "", 1, ["모터 상전류(교류)의 토크 성분 — 배터리 전류가 아니다", sparse]),
            ("iq_cmd_a", "iq_cmd_a", "토크 전류 지령", "A", None, "", 1, [sparse]),
            ("vdc_v", "vdc_v", "DC 버스 전압", "V", None, "", 1, [sparse]),
            ("t_igbt_c", "inverter_c", "인버터(IGBT) 온도", "°C", None, "", 1, ["NTC 값을 컨트롤러 제조사 표로 바꾼 값", sparse]),
            ("t_motor_c", "motor_c", "모터 온도", "°C", None, "", 1, [sparse]))
    for src, key, label, unit, conv, sign, d, cautions in spec:
        t, v = sig[src]
        if len(t):
            series.append(Channel(key, label, unit, t, conv(v) if conv else v, sign=sign, derived=conv is not None,
                                  derivation="n_act_pct ÷ 100 × 설정 최대 회전수" if conv else "", cautions=cautions, source=", ".join(used), digits=d))
    if series:
        keys = {c.key for c in series}
        cols = []
        if "inverter_c" in keys:
            _lap_stat(r, "inverter_t_max_c", *sig["t_igbt_c"], np.max)
            cols.append(LapColumn("inverter_t_max_c", "인버터 최고 온도", "°C", 1, chart=True))
        if "motor_c" in keys:
            _lap_stat(r, "motor_t_max_c", *sig["t_motor_c"], np.max)
            cols.append(LapColumn("motor_t_max_c", "모터 최고 온도", "°C", 1))
        if "iq_a" in keys:
            _lap_stat(r, "iq_peak_a", sig["iq_act_a"][0], np.abs(sig["iq_act_a"][1]), np.max)
            cols.append(LapColumn("iq_peak_a", "토크 전류 최대", "A", 0))
        facts = [f"{c.label} {np.nanmin(c.v):.0f} ~ {np.nanmax(c.v):.0f} {c.unit} (표본 {len(c.v):,}개)" for c in series if c.key in ("inverter_c", "motor_c", "iq_a")]
        blocks.append(DataBlock("motor", "모터 · 인버터 (BAMOCAR)", HAVE, "LEF-26 차량 로거 — CAN 0x181 (컨트롤러가 보내는 8개 값)", facts,
                                raw=raw, code=code, outputs=[FileRef("변환된 표 (로거 레코딩 전체)", "motor.parquet", note="signal · value · raw", where=where)],
                                channels=series, time_keys=[k for k in ("rpm", "iq_a", "inverter_c") if k in keys],
                                trace_keys=[k for k in ("iq_a",) if k in keys], map_key="iq_a" if "iq_a" in keys else None, lap_columns=cols,
                                checks=[clock], cautions=[sparse], series=series, findings=findings))

    # BMS (0x6B1) — 셀 전압 · SOC
    bms = {k: _gather(r, logs, _column("bms", k))[:2] for k in ("cell_max_v", "cell_min_v", "soc_pct", "temp_c")}
    series = [Channel(k, label, unit, *bms[k], sign=sign, cautions=[note] if note else [], source=", ".join(used), digits=d)
              for k, label, unit, sign, note, d in (("cell_max_v", "셀 최고 전압", "V", "", "", 4), ("cell_min_v", "셀 최저 전압", "V", "", "", 4),
                                                    ("soc_pct", "SOC", "%", "", "BMS 가 보고한 값", 1),
                                                    ("temp_c", "BMS 온도", "°C", "", "BMS 가 보고한 온도 (셀 하나하나는 셀 온도 블록)", 0))
              if len(bms[k][0])]
    if series:
        _lap_stat(r, "cell_v_min", *bms["cell_min_v"], np.min)
        sv = bms["soc_pct"][1]
        blocks.append(DataBlock("bms", "배터리 상태 (BMS)", HAVE, "LEF-26 차량 로거 — Orion BMS CAN 0x6B1",
                                [f"셀 전압 {np.nanmin(bms['cell_min_v'][1]):.3f} ~ {np.nanmax(bms['cell_max_v'][1]):.3f} V"] +
                                ([f"SOC {sv[0]:.0f} → {sv[-1]:.0f} %"] if len(sv) else []),
                                raw=raw, code=code, outputs=[FileRef("변환된 표 (로거 레코딩 전체)", "bms.parquet", where=where)],
                                channels=series, time_keys=[c.key for c in series if c.key in ("cell_min_v", "soc_pct")],
                                lap_columns=[LapColumn("cell_v_min", "셀 최저 전압", "V", 3)], checks=[clock],
                                cautions=["BMS 프레임은 체크섬이 약해, 셀 전압이 2.0 ~ 4.3 V 밖인 줄과 valid 가 아닌 줄은 뺐다",
                                          "팩 전류는 기록되지 않았다 (BMS 가 보내는 프레임이 한 종류뿐)"], series=series))

    # 셀 온도 (Orion 써미스터 확장 모듈)
    has = lambda col: (lambda df: df[col].notna())                                   # noqa: E731
    th = {k: _gather(r, logs, _column("therm", k, has(k)))[:2] for k in ("highest_c", "lowest_c", "average_c")}
    series = [Channel(k, label, "°C", *th[k], source=", ".join(used), digits=1)
              for k, label in (("highest_c", "셀 최고 온도"), ("lowest_c", "셀 최저 온도"), ("average_c", "셀 평균 온도")) if len(th[k][0])]
    if series:
        _lap_stat(r, "cell_t_max_c", *th["highest_c"], np.max)
        blocks.append(DataBlock("thermistor", "배터리 셀 온도", HAVE, "LEF-26 차량 로거 — Orion 써미스터 확장 모듈 (확장 ID 프레임 · 0x80)",
                                [f"셀 최고 온도 {np.nanmax(th['highest_c'][1]):.0f} °C" if len(th["highest_c"][0]) else "셀 최고 온도 없음"],
                                raw=raw, code=code, outputs=[FileRef("변환된 표 (로거 레코딩 전체)", "thermistor.parquet", where=where)],
                                channels=series, time_keys=[c.key for c in series if c.key in ("highest_c", "lowest_c")],
                                lap_columns=[LapColumn("cell_t_max_c", "셀 최고 온도", "°C", 0, chart=True)], checks=[clock],
                                cautions=["모듈이 보고한 최고 · 최저 · 평균 (써미스터 32개 중). 1 °C 단위"], series=series))

    # 서스펜션 (리어 리니어 센서)
    lin = {k: _gather(r, logs, _column("lin", k))[:2] for k in ("lin_rl_mm", "lin_rr_mm")}
    series = [Channel(k.replace("lin_", ""), label, "mm", *lin[k], sign="압축하면 줄어든다", source=", ".join(used), digits=1)
              for k, label in (("lin_rl_mm", "리어 왼쪽 스트로크"), ("lin_rr_mm", "리어 오른쪽 스트로크")) if len(lin[k][0])]
    if series:
        clip = {c.key: float(np.mean(c.v >= lef26_laps.LIN_CLIP_MM)) for c in series}
        warn = [f"{'왼쪽' if k == 'rl_mm' else '오른쪽'} 표본의 {s * 100:.0f} % 가 센서 상한(75 mm)에 붙어 있다 — 그 구간은 실제 위치를 모른다"
                for k, s in clip.items() if s > 0.05]
        blocks.append(DataBlock("suspension", "서스펜션 (리어)", WARN if warn else HAVE, "LEF-26 차량 로거 — 리니어 센서 RL · RR (0 ~ 75 mm)",
                                [f"{c.label} {np.nanpercentile(c.v, 1):.1f} ~ {np.nanpercentile(c.v, 99):.1f} mm (1 ~ 99 %)" for c in series],
                                raw=raw, code=code, outputs=[FileRef("변환된 표 (로거 레코딩 전체)", "lin.parquet", where=where)],
                                channels=series, time_keys=[c.key for c in series], trace_keys=[c.key for c in series], checks=[clock],
                                cautions=warn + ["오프셋을 빼지 않은 원래 거리 (서 있을 때의 값이 0 이 아니다)"], series=series))
    return blocks


def _binned(t: np.ndarray, v: np.ndarray, grid: np.ndarray, step: float) -> np.ndarray:
    """표본을 격자 칸([grid, grid + step))마다 평균 — 표본이 없는 칸은 NaN (기록이 빈 곳을 잇지 않는다)."""
    t, v = np.asarray(t, float), np.asarray(v, float)
    out = np.full(len(grid), np.nan)
    if not len(grid):
        return out
    k = np.floor((t - grid[0]) / step).astype(int)
    ok = (k >= 0) & (k < len(grid)) & np.isfinite(v)
    s = np.bincount(k[ok], weights=v[ok], minlength=len(grid))
    n = np.bincount(k[ok], minlength=len(grid))
    out[n > 0] = s[n > 0] / n[n > 0]
    return out


def _logger_imu_block(r, logs: list, how: str) -> DataBlock | None:
    """LEF-26 로거 보드의 가속도계 · 자이로 → 차량 거동 (영상이 없는 세션용). 로거 축: X 횡(우회전 +) · Y 종(가속 +) · 요레이트 좌회전 +."""
    got = {"long_g": _gather(r, logs, lambda log: (log.imu_t, log.long_g)),
           "lat_g": _gather(r, logs, lambda log: (log.imu_t, log.lat_left_g)),
           "yaw_dps": _gather(r, logs, lambda log: (log.imu_t, log.yaw_dps) if log.yaw_dps is not None else (None, None))}
    t, _, used = got["long_g"]
    if len(t) < 100:
        return None
    grid = np.arange(0.0, r.t1 - r.t0, STEP_S)
    n = round(SMOOTH_S / STEP_S)
    long_g, lat_g = (_roll(_binned(*got[k][:2], grid, STEP_S), n) for k in ("long_g", "lat_g"))
    if not (np.isfinite(long_g).any() and np.isfinite(lat_g).any()):
        return None
    src = ", ".join(used)
    tilt = "차량 로거 보드의 가속도계 값. 보드가 기운 만큼(장착 · 롤 · 피치) 중력이 섞임 — 영점을 빼지 않았다"
    series = [Channel("long_g", "앞뒤 가속도", "g", *got["long_g"][:2], sign="가속 +, 제동 −", cautions=[tilt], source=src, digits=4),
              Channel("lat_g", "좌우 가속도", "g", *got["lat_g"][:2], sign="좌선회 +, 우선회 −", derived=True,
                      derivation="로거 X 축(우회전 +)의 부호를 뒤집음", cautions=[tilt], source=src, digits=4)]
    if len(got["yaw_dps"][0]):
        series.append(Channel("yaw_dps", "요레이트", "°/s", *got["yaw_dps"][:2], sign="좌회전 +", source=src, digits=2))
    gg = pd.DataFrame({"long_g": long_g, "lat_g": lat_g}).iloc[::round(GG_STEP_S / STEP_S)].dropna()
    gg.attrs["step_s"] = GG_STEP_S
    hz = (len(t) - 1) / max(float(t[-1] - t[0]), 1e-9)
    return DataBlock(
        "imu", "차량 거동 (가속도 · 요레이트)", HAVE, f"LEF-26 차량 로거 — 가속도계 · 자이로 · 평균 {hz:.0f} Hz",
        [f"앞뒤 가속도 {np.nanmin(long_g):+.2f} ~ {np.nanmax(long_g):+.2f} g (제동 − · 가속 +)",
         f"좌우 가속도 우선회 {np.nanmax(-lat_g):.2f} g · 좌선회 {np.nanmax(lat_g):.2f} g"],
        code=[CodeRef("catalog", CODE_PAGES["catalog"]["title"], "로거 시각 → 세션 시간, 세션 구간만")],
        outputs=[FileRef("변환된 표 (로거 레코딩 전체)", "imu.parquet", note="ax_lat_g · ay_long_g · az_g · yaw_dps · …",
                         where=r.where("normalized/<로거 레코딩>/"))],
        channels=series, time_keys=["long_g", "lat_g"], gg=gg, checks=[f"{how} 로거 레코딩 {src}"],
        cautions=[tilt, "위 사실 줄의 최대 · 최소는 0.5 초 평균값. 그보다 짧은 충격은 원래 주기 값(그래프)으로 볼 것"]
        + list(r.extra("imu").get("cautions", [])), series=series)


# ---------------------------------------------------------------- 25Evo STM32 백업 로거 (Monolith GPS 시계에 맞춘 것)

class _Stm32:
    """이벤트의 STM32 레코딩 중 시계를 맞춘 것 (derived/stm32_alignment.csv 의 applied).

    clock = gps 면 t_abs 가 GPS 로거 시계 (그 GPS 레코딩과 짝, gps_ok), em 이면 에너지미터 시계 (그 미터 레코딩과 짝, em_ok).
    """

    def __init__(self, event_dir: Path):
        self.norm = event_dir / "normalized"
        a = _read(event_dir / "derived" / "stm32_alignment.csv")
        self.table = a[a["applied"].astype(bool)] if a is not None else None

    def for_gps(self, gps_rec: str, em_rec: str | None = None) -> pd.DataFrame:
        if self.table is None:
            return pd.DataFrame()
        t = self.table
        clock = t["clock"] if "clock" in t else pd.Series("gps", index=t.index)
        on_gps = (clock == "gps") & t["gps_ok"].astype(bool) & (t["gps_recording"] == gps_rec)
        on_em = (clock == "em") & t["em_ok"].astype(bool) & (t["em_recording"] == em_rec) if em_rec else False
        return t[on_gps | on_em]


def _stm32_frame(r, rec: str, name: str, offset_s: float = 0.0) -> pd.DataFrame | None:
    """STM32 레코딩의 표 하나를 세션 구간만 — offset_s = 로거 시각(t_abs)에 더하면 기준 시계가 되는 초."""
    path = r.event_dir / "normalized" / rec / f"{name}.parquet"
    if not path.exists():
        return None
    df = pq.read_table(path).to_pandas()
    df = df[df["t_abs"].notna()]
    if df.empty:
        return None
    df = df.assign(t_s=_epoch_s(df["t_abs"]) + offset_s - r.t0_abs)
    return df[(df["t_s"] >= 0) & (df["t_s"] <= r.t1 - r.t0)]


def best_lag(t, v, t_ref, v_ref, max_s: float = REFINE_MAX_S, step: float = 0.1) -> tuple[float, float, float]:
    """v(t) 가 v_ref(t_ref) 와 가장 잘 겹치려면 t 에 더할 시간 → (어긋남 s, 그대로의 상관, 옮긴 뒤의 상관). 표본이 모자라면 (0, nan, nan)."""
    t, v, t_ref, v_ref = (np.asarray(a, float) for a in (t, v, t_ref, v_ref))
    ok = np.isfinite(v)
    if ok.sum() < 30 or len(t_ref) < 30:
        return 0.0, np.nan, np.nan
    t, v = t[ok], v[ok]
    corr = {}
    for k in np.round(np.arange(-max_s, max_s + 1e-9, step), 3):
        inside = (t + k >= t_ref[0]) & (t + k <= t_ref[-1])
        if inside.sum() >= 30:
            corr[k] = float(np.corrcoef(v[inside], np.interp(t[inside] + k, t_ref, v_ref))[0, 1])
    if not corr or 0.0 not in corr:
        return 0.0, np.nan, np.nan
    k = max(corr, key=lambda key: corr[key])
    return float(k), corr[0.0], corr[k]


def _stm32_blocks(r: _Run, stm: _Stm32, em_ref: dict | None = None, al: em_laps.Alignment | None = None) -> list[DataBlock]:
    """25Evo STM32 백업 로거 (GPS 세션). em_ref = 같은 구간의 에너지미터 전력 (있으면 배터리는 그쪽), al = 그 미터를 GPS 에 맞춘 것.

    Monolith GPS 에 시계를 맞춘 레코딩은 그대로, 에너지미터에 맞춘 레코딩(대회)은 al 로 GPS 시계까지 한 번 더 옮긴다.
    """
    rows = stm.for_gps(r.rec, al.meter.rec if al is not None else None)
    if rows.empty:
        return []
    offs, fits, on_em = {}, [], False
    for _, x in rows.iterrows():
        if x.get("clock", "gps") == "em":
            mid = float(al.to_em(r.t0_abs + (r.t1 - r.t0) / 2))                 # 세션 가운데의 미터 경과시간
            offs[x["recording_id"]] = al.a + (al.f - 1) * mid                    # 미터 시계 → GPS 시계
            fits.append(f"{x['recording_id']} 시계 속도 {x['em_f']:.4f} (창 {int(x['em_windows'])}개)")
            on_em = True
        else:
            offs[x["recording_id"]] = 0.0
            fits.append(f"{x['recording_id']} 시계 속도 {x['gps_f']:.4f} (창 {int(x['gps_windows'])}개)")
    if on_em:
        clock = (f"로거 시계를 에너지미터에 맞춤 — DC 버스 전압 ↔ 미터 전압 파형으로 시작 시각과 시계 속도를 같이 구함 (lefdata evo-stm32), "
                 f"그 미터를 GPS 에 맞춘 값으로 한 번 더 옮김. {' · '.join(fits)}")
    else:
        clock = f"로거 시계를 Monolith GPS 에 맞춤 — 모터 회전수 ↔ GPS 속도 파형으로 시작 시각과 시계 속도를 같이 구함 (lefdata evo-stm32 --clock gps). {' · '.join(fits)}"
    return _stm32_make(r, offs, clock, refine=True, skip=("battery",) if em_ref is not None else (), ref=em_ref)


def _stm32_make(r, recs: dict[str, float], clock: str, refine: bool = False, skip: tuple = (), ref: dict | None = None) -> list[DataBlock]:
    """25Evo STM32 백업 로거 → battery (팩 전압 · 전류 · 전력) · bms (셀 · 온도 · SOC) · motor (온도 · 회전수).

    recs = {레코딩: 로거 시각에 더하면 기준 시계가 되는 초}. clock = 시계를 어떻게 맞췄나 (블록 '확인' 글).
    refine = 세션 안에서 GPS 속도와 한 번 더 대 본다 (GPS 세션만). skip = 만들지 않을 블록 (다른 장비가 더 촘촘한 값을 줄 때).
    ref = 같은 구간의 에너지미터 전력 {t, kw}. refine 이면 회전수 ↔ GPS 속도 대신 BMS 전력 ↔ 에너지미터 전력(같은 물리량,
    에너지미터는 GPS 위치에 맞춰 둔 것)으로 다시 맞추고, 아니면 대 본 결과만 첫 블록의 '확인' 에 적는다 (어긋나 보이면 clock_mismatch).
    """
    parts = {name: [d for d in (_stm32_frame(r, rec, name, off) for rec, off in recs.items()) if d is not None and len(d)]
             for name in ("bms", "motor")}
    bms = pd.concat(parts["bms"]).sort_values("t_s") if parts["bms"] else None
    mot = pd.concat(parts["motor"]).sort_values("t_s") if parts["motor"] else None
    if bms is None and mot is None:
        return []
    src = ", ".join(recs)
    unsure = ""
    # 레코딩 전체를 직선 하나로 맞춘 시계는 긴 레코딩의 한 구간에서 몇 초 벗어날 수 있다 → 이 세션 안에서 한 번 더 대 본다
    by_speed = refine and mot is not None and "rpm" in mot and mot["rpm"].notna().sum() >= 30
    if refine and ref is not None and bms is not None and bms["power_kw"].notna().sum() >= 30:
        lag, c0, c1 = best_lag(*_smoothed(bms["t_s"], bms["power_kw"]), *_smoothed(ref["t"], ref["kw"]))
        also = ""
        if by_speed:
            k, _, c = best_lag(mot["t_s"].to_numpy(), np.abs(mot["rpm"].to_numpy(float)), r.ts, r.gps["speed_kmh"].to_numpy(float))
            also = f" (회전수 ↔ GPS 속도로 재면 {k:+.1f} s, 상관 {c:.2f})" if np.isfinite(c) else ""
        how = "BMS 전력 ↔ 에너지미터 전력(같은 물리량)"
        if abs(lag) >= EM_REFINE_MIN_S and c1 >= REFINE_MIN_CORR:
            dur = r.t1 - r.t0
            shift = lambda d: d.assign(t_s=d["t_s"] + lag).pipe(lambda d: d[(d["t_s"] >= 0) & (d["t_s"] <= dur)])   # noqa: E731
            bms = shift(bms)
            mot = shift(mot) if mot is not None else None
            clock += f" · 이 세션 안에서 {how} 을 다시 대 보니 {lag:+.1f} s 어긋나 있어 옮겼다 (상관 {c0:.2f} → {c1:.2f}){also}"
        elif np.isfinite(c1):
            clock += f" · 이 세션 안에서 {how} 을 다시 대 본 어긋남 {lag:+.1f} s (상관 {c1:.2f}) — 옮기지 않음{also}"
            if abs(lag) >= EM_REFINE_MIN_S:
                unsure = (f"백업 로거 시계가 에너지미터와 {lag:+.1f} s 어긋났을 수 있다 — 다시 대 본 상관이 {c1:.2f} 라 옮길 만큼 확실하지 않아 그대로 둠. "
                          "순간 값의 시각(가속 출발 · 제동 시작)은 믿지 말 것")
    elif by_speed:
        lag, c0, c1 = best_lag(mot["t_s"].to_numpy(), np.abs(mot["rpm"].to_numpy(float)), r.ts, r.gps["speed_kmh"].to_numpy(float))
        if abs(lag) >= REFINE_MIN_S and c1 >= REFINE_MIN_CORR and c1 - c0 >= REFINE_GAIN:
            dur = r.t1 - r.t0
            shift = lambda d: d.assign(t_s=d["t_s"] + lag).pipe(lambda d: d[(d["t_s"] >= 0) & (d["t_s"] <= dur)])   # noqa: E731
            bms = shift(bms) if bms is not None else None
            mot = shift(mot)
            clock += (f" · 이 세션 안에서 회전수 ↔ GPS 속도를 다시 대 보니 {lag:+.1f} s 어긋나 있어 옮겼다 (상관 {c0:.2f} → {c1:.2f}). "
                      "레코딩 전체를 한 직선으로 맞춘 시계가 이 구간에서 벗어난 것")
        elif np.isfinite(c1):
            clock += f" · 이 세션 안에서 다시 대 본 어긋남 {lag:+.1f} s (상관 {c1:.2f}) — 옮기지 않음"
            if abs(lag) >= REFINE_MIN_S:
                unsure = (f"로거 시계가 GPS 와 {lag:+.1f} s 어긋났을 수 있다 — 세션 안에서 다시 대 본 상관이 {c1:.2f} 라 옮길 만큼 확실하지 않아 그대로 둠. "
                          "순간 값의 시각(가속 출발 · 제동 시작)은 믿지 말 것")
    raw = [FileRef("STM32 백업 로거 CSV", f"raw_stm32/ ({len(recs)}개 파일)", where=r.notes.get("event", {}).get("raw_where", "DAQ 파트 보관"))]
    where = r.where("normalized/<STM32 레코딩>/")
    code = [CodeRef("catalog", CODE_PAGES["catalog"]["title"], "로거 시각 → 세션 시간, 세션 구간만")]
    slow = "행이 초당 2 ~ 10 개라 짧은 변화는 뭉개진다. 로거 시계는 2 % 가까이 느려 변환기가 바로잡은 값"
    extra = [unsure] if unsure else []
    findings = [{"level": "warn", "code": "logger_clock_uncertain", "t_ranges": [], "message": unsure}] if unsure else []
    blocks = []
    if bms is not None:
        t = bms["t_s"].to_numpy()
        series = [Channel("current_a", "배터리 전류", "A", t, bms["current_a"].to_numpy(), sign="방전 +", source=src, digits=1,
                          cautions=["BMS 가 보낸 값의 아랫자리만 남아 있어 윗자리를 다른 열(전력 ÷ 전압)로 되살린 값", slow]),
                  Channel("voltage_v", "팩 전압", "V", t, bms["pack_v"].to_numpy(), source=src, digits=1,
                          cautions=["아랫자리만 남아 있어 윗자리를 DC 버스 전압으로 되살린 값", slow]),
                  Channel("power_kw", "전력", "kW", t, bms["power_kw"].to_numpy(), sign="방전 +", source=src, digits=1,
                          cautions=["BMS 가 보낸 팩 전력 (0.1 kW 단위)", slow])]
        if "battery" not in skip:
            _lap_stat(r, "p_mean_kw", t, bms["power_kw"].to_numpy(), np.nanmean)
            energy = []
            for a, b in zip(r.laps["t_start_s"], r.laps["t_end_s"]):          # 랩 에너지 = 전력을 시간으로 적분 (표본 수가 아니라 시간 열로)
                m = (t >= a) & (t < b)
                energy.append(float(np.trapezoid(bms["power_kw"].to_numpy()[m], t[m]) / 3.6) if m.sum() > 1 else np.nan)
            r.laps["net_wh"] = energy
            blocks.append(DataBlock("battery", "배터리 전압 · 전류", HAVE, "25Evo STM32 백업 로거 — BMS 값 (팩 전압 · 전류 · 전력)",
                                    [f"표본 {len(t):,}개", f"전력 최대 {np.nanmax(bms['power_kw']):.1f} kW · 전류 최대 {np.nanmax(bms['current_a']):.0f} A",
                                     f"팩 전압 {np.nanmin(bms['pack_v']):.0f} ~ {np.nanmax(bms['pack_v']):.0f} V"],
                                    raw=raw, code=code, outputs=[FileRef("변환된 표 (레코딩 전체)", "bms.parquet", note="pack_v · current_a · power_kw", where=where)],
                                    channels=series, time_keys=["current_a", "voltage_v", "power_kw"], trace_keys=["current_a", "power_kw"], map_key="power_kw",
                                    lap_columns=[LapColumn("net_wh", "순 에너지", "Wh", 0, chart=True), LapColumn("p_mean_kw", "평균 전력", "kW", 1)],
                                    checks=[clock], cautions=extra + ["주최측 에너지미터보다 거칠다 (해상도 · 표본 수). 에너지미터 기록이 있는 날은 그쪽이 정확하다"],
                                    series=series, findings=findings))
        cell = [Channel(k, label, unit, t, bms[k].to_numpy(), cautions=[slow], source=src, digits=d)
                for k, label, unit, d in (("cell_min_v", "셀 최저 전압", "V", 4), ("temp_max_c", "셀 최고 온도", "°C", 0),
                                          ("temp_min_c", "셀 최저 온도", "°C", 0), ("soc_pct", "SOC", "%", 1)) if k in bms and bms[k].notna().any()]
        if cell and "bms" not in skip:
            cols = []
            if any(c.key == "temp_max_c" for c in cell):
                _lap_stat(r, "cell_t_max_c", t, bms["temp_max_c"].to_numpy(), np.nanmax)
                cols.append(LapColumn("cell_t_max_c", "셀 최고 온도", "°C", 0, chart=True))
            if any(c.key == "cell_min_v" for c in cell):
                _lap_stat(r, "cell_v_min", t, bms["cell_min_v"].to_numpy(), np.nanmin)
                cols.append(LapColumn("cell_v_min", "셀 최저 전압", "V", 3))
            blocks.append(DataBlock("bms", "배터리 상태 (BMS)", HAVE, "25Evo STM32 백업 로거 — BMS 값 (셀 전압 · 온도 · SOC)",
                                    [f"{c.label} {np.nanmin(c.v):.4g} ~ {np.nanmax(c.v):.4g} {c.unit}" for c in cell],
                                    raw=raw, code=code, outputs=[FileRef("변환된 표 (레코딩 전체)", "bms.parquet", where=where)],
                                    channels=cell, time_keys=[c.key for c in cell if c.key in ("temp_max_c", "soc_pct")], lap_columns=cols,
                                    checks=[clock], cautions=extra + ["BMS 가 보고한 셀 최고 · 최저 (셀 하나하나의 값은 없다)"], series=cell))
    if mot is not None and "motor" not in skip:
        t = mot["t_s"].to_numpy()
        series = [Channel(k, label, unit, t, mot[k].to_numpy(), cautions=extra + [slow], source=src, digits=d)
                  for k, label, unit, d, extra in (("t_igbt_c", "인버터(IGBT) 온도", "°C", 1, ["NTC 값을 컨트롤러 제조사 표로 바꾼 값 (새 형식 로그만 있다)"]),
                                                   ("t_motor_c", "모터 온도", "°C", 1, []), ("vdc_v", "DC 버스 전압", "V", 1, []),
                                                   ("rpm", "회전수 (로거 환산)", "rpm", 0, ["로거가 상수 기준으로 환산한 값 — 실제 모터 rpm · 감속비를 말하지 못한다"]))
                  if k in mot and mot[k].notna().any()]
        if series:
            cols = []
            if any(c.key == "t_igbt_c" for c in series):
                _lap_stat(r, "inverter_t_max_c", t, mot["t_igbt_c"].to_numpy(), np.nanmax)
                cols.append(LapColumn("inverter_t_max_c", "인버터 최고 온도", "°C", 1, chart=True))
            if any(c.key == "t_motor_c" for c in series):
                _lap_stat(r, "motor_t_max_c", t, mot["t_motor_c"].to_numpy(), np.nanmax)
                cols.append(LapColumn("motor_t_max_c", "모터 최고 온도", "°C", 1))
            blocks.append(DataBlock("motor", "모터 · 인버터", HAVE, "25Evo STM32 백업 로거 — 모터 컨트롤러 값",
                                    [f"{c.label} {np.nanmin(c.v):.0f} ~ {np.nanmax(c.v):.0f} {c.unit}" for c in series if c.unit == "°C"],
                                    raw=raw, code=code, outputs=[FileRef("변환된 표 (레코딩 전체)", "motor.parquet", where=where)],
                                    channels=series, time_keys=[c.key for c in series if c.key in ("t_igbt_c", "t_motor_c")], lap_columns=cols,
                                    checks=[clock], cautions=extra, series=series))
    if findings and "battery" in skip and blocks:                   # 경고는 배터리 블록에 달았다 — 그것을 만들지 않았으면 처음 블록에
        blocks[0].findings = blocks[0].findings + findings
    if ref is not None and bms is not None and blocks and not refine:      # GPS 세션은 위에서 에너지미터에 맞췄다
        _add_check(blocks[0], _agree(bms["t_s"], bms["power_kw"], ref["t"], ref["kw"], "stm32", "STM32 백업 로거의 BMS 전력을 에너지미터 전력과 대 봄"))
    return blocks


def _auto_sessions(runs: pd.DataFrame, derived: Path) -> dict[tuple, dict]:
    """이름을 적지 않은 주행 구간 → 자동 이름 (랩이 있으면 트랙, 가속 · 스키드패드 표에 있으면 그 종목). 나머지는 넣지 않는다."""
    accel, skid = _read(derived / "accel_runs.csv"), _read(derived / "skidpad_laps.csv")
    if accel is not None and "direction" in accel:
        accel = accel[accel["direction"] == "forward"]                  # 반대 방향(돌아오는 길)은 가속 시도가 아니다
    found = {}
    for _, run in runs.iterrows():
        rec, n, a, b = run["recording_id"], int(run["run"]), float(run["t_start_s"]), float(run["t_end_s"])
        if int(run.get("full_laps", 0) or 0) > 0:
            kind = "트랙"
        elif accel is not None and ((accel["recording_id"] == rec) & (accel["t_start_s"] >= a - ACCEL_LEAD_S) & (accel["t_start_s"] <= b)).any():
            kind = "가속"
        elif skid is not None and ((skid["recording_id"] == rec) & (skid["run"] == n)).any():
            kind = "스키드패드"
        else:
            continue
        found[(rec, n)] = {"recording": rec, "run": n, "discipline": kind, "auto": True}
    return found


def _segment_notes(info: dict, derived: Path, r: _Run) -> list[str]:
    """가속 · 스키드패드 세션에는 종목 결과를 한 줄씩 (derived/accel_runs.csv · skidpad_laps.csv 의 값)."""
    out = []
    if info.get("discipline") == "가속":
        a = _read(derived / "accel_runs.csv")
        rows = a[(a["recording_id"] == r.rec) & (a["t_start_s"] >= r.t0 - ACCEL_LEAD_S) & (a["t_start_s"] <= r.t1)] if a is not None else pd.DataFrame()
        rows = rows[rows["direction"] == "forward"] if "direction" in rows else rows
        for _, x in rows.iterrows():
            out.append(f"가속 75 m {x['t75_s']:.3f} s (75 m 지점 {x['v_at_75_kmh']:.0f} km/h)")
    if info.get("discipline") == "스키드패드":
        s = _read(derived / "skidpad_laps.csv")
        runs = info["run"] if isinstance(info.get("run"), list) else [info.get("run")]
        rows = s[(s["recording_id"] == r.rec) & s["run"].isin(runs)] if s is not None else pd.DataFrame()
        if len(rows):
            for turn, g in rows.groupby("turn"):
                out.append(f"스키드패드 {dict(left='좌', right='우').get(turn, turn)}회전 — 원 {len(g)}바퀴 · 최속 {g['lap_time_s'].min():.2f} s · 반지름 {g['circle_r_m'].median():.1f} m")
    return out


# ---------------------------------------------------------------- 시각 구간 세션 (GPS 없이 — Run Sheet 시각 · 장비가 켜져 있던 동안)

DEVICES = (("FSK 에너지미터", "energymeter"), ("Orion BMS Connect", "orion"), ("자작 STM32", "stm32"),
           ("LEF-26 STM32F446RE", "lef26_logger"), ("Monolith", "monolith"), ("GoPro", "gopro"))     # 변환기가 meta 에 적는 장비 이름 → 장비
STM32_CLOCK = {"에너지미터": "energymeter", "Monolith": "monolith"}     # 25Evo STM32 시각을 어느 장비에 맞췄나 (meta time.alignment.clock_of)
CLOCK_LABEL = {"energymeter": "에너지미터", "orion": "Orion BMS Connect", "lef26_logger": "LEF-26 차량 로거", "monolith": "Monolith",
               "gopro": "GoPro GPS"}
TIMED_DEVICES = ("energymeter", "stm32", "orion", "lef26_logger")     # 시각 구간 세션에 붙이는 장비
GPS_DEVICES = ("monolith", "gopro")                                   # 아직 붙이지 않는다 (겹치면 메모로 알린다)
ORION_CHANNELS = (("soc_pct", "SOC", "%", 1), ("cell_min_v", "셀 최저 전압", "V", 3), ("cell_max_v", "셀 최고 전압", "V", 3),
                  ("temp_max_c", "셀 최고 온도", "°C", 0), ("temp_min_c", "셀 최저 온도", "°C", 0), ("dcl_a", "방전 전류 한계 (DCL)", "A", 0))
DCL_REASONS = (("dcl_limited_soc", "SOC"), ("dcl_limited_temp", "온도"), ("dcl_limited_cell_v", "셀 전압"),
               ("dcl_limited_pack_v", "팩 전압"), ("dcl_limited_cell_r", "셀 저항"))


@dataclass
class _Rec:
    """변환된 레코딩 하나 — 장비, 그 t_abs 가 어느 시계인가, 그 시계로 본 시작 · 끝 (UTC 초).

    anchor = 시계를 맞춰 둔 다른 레코딩 (25Evo STM32 → 그 에너지미터 · Monolith 레코딩). 그 레코딩의 시계 차를 따로 적었으면 따른다.
    """
    rec: str
    dir: Path
    meta: dict
    device: str
    clock: str
    t0: float
    t1: float
    anchor: str = ""


def _recordings(event_dir: Path) -> list[_Rec]:
    """이벤트의 변환된 레코딩 중 절대 시각이 있는 것 (25Evo STM32 는 다른 장비에 시계를 맞춘 것만)."""
    out = []
    for path in sorted((event_dir / "normalized").glob("*/meta.json")):
        meta = json.loads(path.read_text(encoding="utf-8"))
        logger = meta.get("source", {}).get("logger", "")
        device = next((d for key, d in DEVICES if logger.startswith(key)), None)
        tm = meta.get("time", {})
        if device is None or not tm.get("t_abs_start"):
            continue
        t0 = pd.Timestamp(tm["t_abs_start"]).timestamp()
        span = tm.get("duration_s")
        if span is None and tm.get("t_abs_end"):
            span = pd.Timestamp(tm["t_abs_end"]).timestamp() - t0
        al = tm.get("alignment", {}) if device == "stm32" else {}
        clock = STM32_CLOCK.get(al.get("clock_of", ""), "") if device == "stm32" else device
        if span is None or not clock:
            continue
        out.append(_Rec(path.parent.name, path.parent, meta, device, clock, t0, t0 + float(span),
                        al.get("em_recording") or al.get("gps_recording") or ""))
    return out


class _Clocks:
    """기준 시계([event] clock — start · end 를 적은 시계)와, 장비 시계를 그리로 옮길 때 더할 초([clocks.<시계>])."""

    def __init__(self, notes: dict):
        ev = notes.get("event", {})
        self.ref = ev.get("clock", "")
        if not self.ref:
            raise ValueError(f"{NOTES_NAME}: 시각 구간 세션에는 [event] clock (start · end 를 적은 시계) 이 있어야 한다")
        self.label = ev.get("clock_label") or CLOCK_LABEL.get(self.ref, self.ref)
        self.table = notes.get("clocks", {})

    def _own(self, x: _Rec) -> float | None:
        """레코딩마다 따로 적은 값 (그 레코딩, 아니면 시계를 맞춰 둔 레코딩의 것)."""
        per = self.table.get(x.clock, {}).get("recordings", {})
        return next((float(per[k]) for k in (x.rec, x.anchor) if k and k in per), None)

    def known(self, x: _Rec) -> bool:
        return x.clock == self.ref or "offset_s" in self.table.get(x.clock, {}) or self._own(x) is not None

    def offset(self, x: _Rec) -> float:
        own = self._own(x)
        if own is not None:
            return own
        return 0.0 if x.clock == self.ref else float(self.table[x.clock]["offset_s"])

    def text(self, x: _Rec) -> str:
        """확인 글 — 이 레코딩의 시각을 기준 시계로 어떻게 옮겼나."""
        name, off = CLOCK_LABEL.get(x.clock, x.clock), self.offset(x)
        if x.clock == self.ref and off == 0.0:
            return f"{name} 시계가 기준 시계"
        basis = self.table.get(x.clock, {}).get("basis", "")
        return f"{name} 시계 {off:+.1f} s = 기준 시계({self.label})" + (f" — {basis}" if basis else "")


def _when(v) -> float:
    """적어 둔 시각 → UTC 초. 시간대가 없으면 KST."""
    ts = pd.Timestamp(v)
    return (ts.tz_localize(TZ) if ts.tzinfo is None else ts).timestamp()


def _window(e: dict, by_id: dict[str, _Rec], clocks: _Clocks) -> tuple[float, float]:
    """[[timed_session]] 한 줄 → 기준 시계로 (시작, 끝) UTC 초. start · end, 또는 recording (그 레코딩이 켜져 있던 동안)."""
    if "recording" in e:
        x = by_id.get(e["recording"])
        if x is None:
            raise ValueError(f"{NOTES_NAME}: 레코딩 {e['recording']} 이 없다 (normalized/ 에 절대 시각이 있는 레코딩이어야 한다)")
        if not clocks.known(x):
            raise ValueError(f"{NOTES_NAME}: {CLOCK_LABEL.get(x.clock, x.clock)} 시계를 기준 시계로 옮길 값이 없다 — [clocks.{x.clock}] offset_s")
        t0, t1 = x.t0 + clocks.offset(x), x.t1 + clocks.offset(x)
    elif "start" in e and "end" in e:
        t0, t1 = _when(e["start"]), _when(e["end"])
    else:
        raise ValueError(f"{NOTES_NAME}: [[timed_session]] 에는 start · end 나 recording 이 있어야 한다 — {e}")
    if not t1 > t0:
        raise ValueError(f"{NOTES_NAME}: 시각 구간의 끝이 시작보다 앞이다 — {e}")
    return t0, t1


def _em_window_block(w: _Window, parts: list[tuple[_Rec, float]], clock: str) -> tuple[DataBlock | None, dict, dict | None]:
    """주최측 에너지미터 (시각 구간) → battery 블록 · 세션 요약 값 · 다른 장비와 대 볼 파형. parts = [(레코딩, 기준 시계로 더할 초)]."""
    dur = w.t1 - w.t0
    got = []
    for x, off in parts:
        me = em_laps.read_meter(x.dir)
        base = me.start + off - w.t0_abs                                # 미터 경과시간 + base = 세션 시간
        r0, r1 = max(float(me.t[0]), -base), min(float(me.t[-1]), dur - base)
        e = em_laps.segment_energy(me, r0, r1) if r1 > r0 else {}
        if e:
            m = (me.t >= r0) & (me.t <= r1)
            got.append((x, me, m, me.t[m] + base, e))
    if not got:
        return None, {}, None
    ts = np.concatenate([g[3] for g in got])
    volt, amp, kw, p500 = (np.concatenate([getattr(g[1], k)[g[2]] for g in got]) for k in ("volt", "amp", "kw", "p500_kw"))
    dis, reg = (sum(g[4][k] for g in got) for k in ("discharge_wh", "regen_wh"))
    hole_at, holes = [], 0.0
    for g in got:
        step = np.diff(g[3])
        big = np.flatnonzero(step > 2 * np.median(step))
        holes += float(step[big].sum())
        hole_at += [[round(float(g[3][i]), 3), round(float(g[3][i + 1]), 3)] for i in big]
    srcs = ", ".join(g[0].rec for g in got)
    meta = got[0][0].meta
    cautions = list(w.extra("battery").get("cautions", []))
    if holes > 0.5:
        cautions.insert(0, f"표본이 빠진 곳이 있음 — 기록 간격의 두 배 넘게 빈 틈이 합계 {holes:.1f} s (세션의 {100 * holes / dur:.1f} %). "
                           "에너지를 직접 적분할 때는 표본 개수가 아니라 시간 열(t_s)로 적분할 것")
    findings = [{"level": "warn", "code": "em_dropped_samples", "t_ranges": hole_at,
                 "message": f"기록 간격의 두 배 넘게 빈 틈 {len(hole_at)}곳 · 합계 {holes:.1f} s — 그 사이에는 값이 없다 (확대하면 그래프가 끊긴다)"}] if hole_at else []
    series = [Channel("current_a", "배터리 전류", "A", ts, amp, sign="방전 +, 배터리로 돌아오면 −", source=srcs, digits=2),
              Channel("voltage_v", "팩 전압", "V", ts, volt, source=srcs, digits=2),
              Channel("power_kw", "전력", "kW", ts, kw, sign="방전 +", derived=True, derivation="전압 × 전류", source=srcs, digits=3),
              Channel("power_500ms_kw", "전력 500 ms 평균", "kW", ts, p500, sign="방전 +", derived=True,
                      derivation="전압 × 전류의 500 ms 이동평균 (대회 출력 한도 판정과 같은 방식)", source=srcs, digits=3)]
    p500_max = float(np.nanmax(p500)) if np.isfinite(p500).any() else np.nan
    hv = volt[volt > lef26_laps.HV_ON_V]
    volts = f"팩 전압 {hv.min():.0f} ~ {hv.max():.0f} V (고전압이 켜진 동안)" if len(hv) else "고전압이 켜진 적 없음"
    table = pd.DataFrame({"t_s": ts.round(3), "voltage_v": volt.round(1), "current_a": amp.round(1), "power_kw": kw.round(3)})
    raws = []
    for g in got:
        path = w.event_dir / g[0].meta["source"]["file"]
        counts = g[0].meta.get("counts", {})
        raws.append(FileRef("에너지미터 원본 로그 (바이너리, 레코딩 전체)", path.name, path, path.stat().st_size if path.exists() else 0,
                            f"{counts.get('records', 0):,} 표본 · 체크섬 실패 {counts.get('checksum_fail', 0)}"))
    block = DataBlock(
        "battery", "배터리 전압 · 전류", HAVE, f"{meta['source']['logger']} · {1000 / meta['time']['median_dt_ms']:.0f} Hz",
        [f"표본 {len(ts):,}개" + (f" · 레코딩 {len(got)}개" if len(got) > 1 else ""),
         f"방전 {dis:,.0f} Wh · 배터리로 돌아온 양 {reg:,.0f} Wh · 순 {dis - reg:,.0f} Wh",
         f"전류 최대 {np.max(amp):.0f} A · 500 ms 평균 전력 최대 {p500_max:.1f} kW (대회 한도 80 kW)",
         volts],
        raw=raws, code=[CodeRef("energymeter", CODE_PAGES["energymeter"]["title"], "원본 로그를 전압 · 전류 표로"),
                        CodeRef("catalog", CODE_PAGES["catalog"]["title"], "미터 시각 → 세션 시간, 세션 구간만")],
        outputs=[FileRef("변환된 표 (레코딩 전체)", "energy.parquet", note="voltage_v · current_a · lv_voltage_v · temp_c · valid",
                         where=w.where("normalized/<에너지미터 레코딩>/"))],
        exports=[Export("battery.csv", "이 세션의 전압 · 전류 · 전력 (시간 t_s 는 기준 시계로 옮긴 세션 시간)", table, f"{len(table):,} 행")],
        channels=series[:3], time_keys=["current_a", "voltage_v", "power_kw"], map_key="power_kw",
        checks=[clock], cautions=cautions, series=series, findings=findings)
    return block, {"net_wh": round(dis - reg), "i_max_a": round(float(np.max(amp)))}, {"t": ts, "amp": amp, "volt": volt, "kw": kw}


def _orion_block(w: _Window, parts: list[tuple[_Rec, float]], clock: str) -> tuple[DataBlock | None, dict | None]:
    """Orion BMS Connect 로그 (시각 구간) → bms 블록 · 다른 장비와 대 볼 팩 전류."""
    dur = w.t1 - w.t0
    frames = []
    for x, off in parts:
        d = pq.read_table(x.dir / "bms.parquet").to_pandas()
        d = d[d["t_abs"].notna()]
        d = d.assign(t_s=_epoch_s(d["t_abs"]) + off - w.t0_abs)
        frames.append(d[(d["t_s"] >= 0) & (d["t_s"] <= dur)])
    d = pd.concat(frames).sort_values("t_s") if frames else pd.DataFrame()
    if len(d) < 2:
        return None, None
    if "soc_pct" in d:
        d["soc_pct"] = d["soc_pct"].where(d["soc_pct"] > 0)            # 켠 직후의 0 은 값이 아니다
    t = d["t_s"].to_numpy()
    src = ", ".join(x.rec for x, _ in parts)
    every = "Connect 가 1 초 단위 시각으로 남긴 값 (약 1 초 간격)"
    series = [Channel(k, label, unit, t, d[k].to_numpy(float), cautions=[every], source=src, digits=dg)
              for k, label, unit, dg in ORION_CHANNELS if k in d and d[k].notna().any()]
    if not series:
        return None, None
    facts = []
    soc = d["soc_pct"].dropna() if "soc_pct" in d else pd.Series(dtype=float)
    if len(soc):
        facts.append(f"SOC {soc.iloc[0]:.1f} → {soc.iloc[-1]:.1f} %")
    if {"cell_min_v", "cell_max_v"} <= set(d.columns):
        facts.append(f"셀 전압 {d['cell_min_v'].min():.3f} ~ {d['cell_max_v'].max():.3f} V")
    hot = d["temp_max_c"].dropna() if "temp_max_c" in d else pd.Series(dtype=float)
    if len(hot):
        facts.append(f"셀 최고 온도 {hot.iloc[0]:.0f} → {hot.iloc[-1]:.0f} °C (가장 높을 때 {hot.max():.0f} °C)")
    if "dcl_a" in d and d["dcl_a"].notna().any():
        facts.append(f"방전 전류 한계(DCL) {d['dcl_a'].min():.0f} ~ {d['dcl_a'].max():.0f} A")
    if any(col in d for col, _ in DCL_REASONS):
        why = [name for col, name in DCL_REASONS if col in d and d[col].fillna(0).astype(bool).any()]
        facts.append(f"BMS 가 방전 전류 한계를 줄인 표시: {', '.join(why) if why else '없음'}")
    if "errors_present" in d:
        facts.append(f"BMS 오류 표시: {'있음' if d['errors_present'].fillna(0).astype(bool).any() else '없음'}")
    keys = {c.key for c in series}
    where = w.notes.get("event", {}).get("raw_where", "DAQ 파트 보관")
    block = DataBlock(
        "bms", "배터리 상태 (BMS)", HAVE, f"{parts[0][0].meta['source']['logger']} · 약 1 Hz", facts,
        raw=[FileRef("Orion BMS Connect 로그 (CSV 로 바꾼 것)", x.meta["source"].get("file", x.rec), where=where) for x, _ in parts],
        code=[CodeRef("catalog", CODE_PAGES["catalog"]["title"], "BMS 시각 → 세션 시간, 세션 구간만")],
        outputs=[FileRef("변환된 표 (레코딩 전체)", "bms.parquet", note="팩 전류 · 전압 · SOC · 셀 전압 · 온도 · DCL · 제한 표시",
                         where=w.where("normalized/<Orion 레코딩>/"))],
        channels=series, time_keys=[k for k in ("soc_pct", "temp_max_c", "cell_min_v") if k in keys], checks=[clock],
        cautions=[every + ". 전류 · 전압은 그 사이의 평균이라 순간 값보다 작다", "켠 직후의 SOC 0 은 값이 아니라서 비웠다"]
        + list(w.extra("bms").get("cautions", [])), series=series)
    ref = {"t": t, "amp": d["current_a"].to_numpy(float)} if "current_a" in d else None
    return block, ref


def _smoothed(t, v, step: float = 0.1) -> tuple[np.ndarray, np.ndarray]:
    """표본 → step 격자의 점마다 앞뒤 AGREE_SMOOTH_S / 2 안 표본의 평균 (표본이 없는 점은 뺀다).

    100 Hz 장비도 초당 몇 개뿐인 장비도 같은 방식으로 고른다.
    """
    t, v = np.asarray(t, float), np.asarray(v, float)
    ok = np.isfinite(t) & np.isfinite(v)
    t, v = t[ok], v[ok]
    if len(t) < 2:
        return np.zeros(0), np.zeros(0)
    grid = np.arange(t[0], t[-1], step)
    cs = np.concatenate([[0.0], np.cumsum(v)])
    i0, i1 = np.searchsorted(t, grid - AGREE_SMOOTH_S / 2), np.searchsorted(t, grid + AGREE_SMOOTH_S / 2)
    n = i1 - i0
    return grid[n > 0], (cs[i1] - cs[i0])[n > 0] / n[n > 0]


def _agree(t, v, t_ref, v_ref, device: str, what: str) -> tuple[str, dict | None]:
    """두 장비가 같은 물리량을 잰 파형 → (확인 글, 경고 또는 None). 1 초 평균으로 고른 뒤 가장 잘 겹치는 어긋남을 잰다.

    어긋남 + = 이 장비의 시각에 그만큼 더해야 기준 장비와 겹친다.
    """
    tol_s = AGREE_TOL_S[device]
    a, av = _smoothed(t, v)
    b, bv = _smoothed(t_ref, v_ref)
    lag, _, c1 = best_lag(a, av, b, bv)
    if not np.isfinite(c1) or c1 < AGREE_MIN_CORR[device]:
        return f"{what}: 파형이 덜 닮아 어긋남을 재지 못함" + (f" (상관 {c1:.2f})" if np.isfinite(c1) else ""), None
    text = f"{what}: 가장 잘 겹치는 어긋남 {lag:+.1f} s (상관 {c1:.2f})"
    if abs(lag) <= tol_s:
        return text + f" — 허용 {tol_s:g} s 안", None
    return text + f" — 허용 {tol_s:g} s 를 넘는다", {
        "level": "warn", "code": "clock_mismatch", "t_ranges": [],
        "message": f"{what} — {lag:+.1f} s 어긋나 보인다. 장비 시계를 기준 시계로 옮기는 값이 틀렸을 수 있다 (순간 값의 시각을 믿지 말 것)"}


def _add_check(b: DataBlock, said: tuple[str, dict | None]) -> None:
    b.checks.append(said[0])
    if said[1]:
        b.findings.append(said[1])


def _window_blocks(w: _Window, recs: list[_Rec], clocks: _Clocks, strict: bool = True) -> tuple[list[DataBlock], dict]:
    """시각 구간에 기록이 있는 장비 → 블록들 · 세션 요약 값.

    같은 것을 두 장비가 주면 더 촘촘한 쪽을 쓴다 (배터리: 에너지미터 > STM32, BMS: Orion > STM32). 에너지미터가 있으면
    다른 장비의 같은 물리량(전력 · 전류 · 전압)을 그것과 대 봐서 시계가 맞는지 블록의 '확인' 에 적는다.
    """
    hit = []
    for x in recs:
        if x.device not in TIMED_DEVICES:
            continue
        if not clocks.known(x):
            if strict and x.t0 < w.t1_abs + CLOCK_SLACK_S and x.t1 > w.t0_abs - CLOCK_SLACK_S:
                raise ValueError(f"{NOTES_NAME}: {CLOCK_LABEL.get(x.clock, x.clock)} 시계를 기준 시계로 옮길 값이 없다 — "
                                 f"[clocks.{x.clock}] offset_s 를 적을 것 (레코딩 {x.rec})")
            continue
        off = clocks.offset(x)
        if x.t0 + off < w.t1_abs and x.t1 + off > w.t0_abs:
            hit.append((x, off))
    of = lambda dev: [(x, off) for x, off in hit if x.device == dev]                    # noqa: E731
    said = lambda part: " · ".join(dict.fromkeys(clocks.text(x) for x, _ in part))      # noqa: E731
    stats = {"distance_km": None, "laps_n": None, "best_lap_s": None, "vmax_kmh": None}
    blocks = []
    em, em_ref = None, None
    if of("energymeter"):
        em, st, em_ref = _em_window_block(w, of("energymeter"), f"미터 시각을 기준 시계로 옮김 — {said(of('energymeter'))}")
        if em is not None:
            blocks.append(em)
            stats.update(st)
    orion, orion_ref = _orion_block(w, of("orion"), f"Connect 시각을 기준 시계로 옮김 — {said(of('orion'))}") if of("orion") else (None, None)
    part = of("stm32")
    if part:
        skip = tuple(k for k, b in (("battery", em), ("bms", orion)) if b is not None)
        made = _stm32_make(w, {x.rec: off for x, off in part},
                           f"백업 로거 시각(lefdata evo-stm32 가 다른 장비에 맞춰 둔 것)을 기준 시계로 옮김 — {said(part)}", skip=skip, ref=em_ref)
        bat = next((b for b in made if b.key == "battery"), None)
        if bat is not None and em is None:
            p, i = (next(c for c in bat.series if c.key == k) for k in ("power_kw", "current_a"))
            ok = np.isfinite(p.v)
            stats.update(net_wh=round(float(np.trapezoid(p.v[ok], p.t_s[ok]) / 3.6)), i_max_a=round(float(np.nanmax(i.v))))
        blocks += made
    if orion is not None:
        if em_ref is not None and orion_ref is not None:
            _add_check(orion, _agree(orion_ref["t"], orion_ref["amp"], em_ref["t"], em_ref["amp"], "orion",
                                     "Orion 팩 전류를 에너지미터 전류와 대 봄"))
        blocks.append(orion)
    part = of("lef26_logger")
    if part:
        logs = [lef26_laps.read_logger(x.dir, lef26_clock.ClockMap(np.array([0.0, x.t1 - x.t0]), np.array([x.t0 + off, x.t1 + off]), 1.0, 1.0),
                                       np.zeros(0)) for x, off in part]
        how = f"로거 시각(변환기가 바로잡은 RTC 시각 t_abs)을 기준 시계로 옮김 — {said(part)}."
        made = _logger_blocks(w, logs, how)
        imu = _logger_imu_block(w, logs, how)
        made += [imu] if imu is not None else []
        mixed = [x.rec for x, _ in part if x.meta.get("time", {}).get("clock", {}).get("mode") == "mixed"]
        for b in made if mixed else []:
            b.cautions.insert(0, f"시계 속도를 바로잡지 못한 로거 레코딩이 섞여 있다 ({', '.join(mixed)}) — 그 부분의 시각은 몇 초씩 밀려 있을 수 있다")
        mot = next((b for b in made if b.key == "motor"), None)
        vdc = next((c for c in mot.series if c.key == "vdc_v"), None) if mot is not None else None
        if em_ref is not None and vdc is not None:
            _add_check(mot, _agree(vdc.t_s, vdc.v, em_ref["t"], em_ref["volt"], "lef26_logger",
                                   "로거의 DC 버스 전압을 에너지미터 전압과 대 봄"))
        blocks += made
    return blocks, stats


def timed_sessions(event_dir: Path, notes: dict, made: dict | None = None) -> list[Session]:
    """`[[timed_session]]` → 시각 구간 세션들 (sid · 자동 이름은 build_event 가 붙인다).

    그 시각에 기록이 있는 장비의 블록이 붙는다. GPS 레코딩이 구간을 거의 다 덮으면 랩 · 코스도 있다 (GPS 세션과 같은 블록).
    """
    event_dir = Path(event_dir).resolve()
    ctx = _GpsCtx(event_dir, notes, made or {}) if _GpsCtx.available(event_dir) else None
    return _timed(event_dir, notes, made or {}, ctx)[0]


def _gps_window(ctx: "_GpsCtx", x: _Rec, t0: float, t1: float, off: float):
    """시각 구간(기준 시계) → 그 GPS 레코딩의 주행 구간 한 줄 · 그 안의 랩 · 덮은 주행 번호 · 줄인 구간(기준 시계). 덜 덮으면 None."""
    g = pq.read_table(x.dir / "gps.parquet", columns=["t_rel_ms", "t_abs", "fix", "lat", "lon", "speed_kmh"]).to_pandas()
    g = g[g["fix"] & g["t_abs"].notna()]
    if len(g) < 2:
        return None
    t, ab = g["t_rel_ms"].to_numpy() / 1000.0, _epoch_s(g["t_abs"])
    a0, a1 = max(t0 - off, float(ab[0])), min(t1 - off, float(ab[-1]))
    if a1 - a0 < GPS_COVER_MIN * (t1 - t0):
        return None
    r0, r1 = (float(v) for v in np.interp([a0, a1], ab, t))
    m = (t >= r0) & (t <= r1)
    px, py = lapmod.to_local_m(g["lat"].to_numpy()[m], g["lon"].to_numpy()[m], ctx.origin["lat0"], ctx.origin["lon0"])
    lp = ctx.laps[(ctx.laps["recording_id"] == x.rec) & (ctx.laps["t_start_s"] >= r0 - 1e-6) & (ctx.laps["t_end_s"] <= r1 + 1e-6)]
    lp = lp.sort_values("t_start_s").assign(lap=lambda d: np.arange(1, len(d) + 1))      # 주행 구간이 여러 개여도 겹치지 않게 다시 센다
    runs = ctx.runs[ctx.runs["recording_id"] == x.rec]
    inside = (np.minimum(runs["t_end_s"], r1) - np.maximum(runs["t_start_s"], r0)) / (runs["t_end_s"] - runs["t_start_s"]).clip(lower=1e-9)
    covered = [int(n) for n in runs.loc[inside >= RUN_COVER_MIN, "run"]]
    row = pd.Series({"recording_id": x.rec, "t_start_s": r0, "t_end_s": r1, "full_laps": len(lp),
                     "distance_m": float(np.hypot(np.diff(px), np.diff(py)).sum()), "vmax_kmh": float(g["speed_kmh"].to_numpy()[m].max())})
    return row, lp, covered, (a0 + off, a1 + off)


def _window_energy(ctx: "_GpsCtx", rec: str, row: pd.Series, lp: pd.DataFrame) -> tuple[pd.Series | None, pd.DataFrame | None]:
    """GPS 로 옮긴 시각 구간의 에너지미터 값 — em_laps 가 그 GPS 레코딩에 맞춘 미터가 있으면 (주행 구간 표와 같은 열)."""
    als = [al for (_, tr), al in ctx.aligned.items() if tr == rec]
    if not als:
        return None, None
    seg = em_laps.summarize_segment(als, als[0].track, float(row["t_start_s"]), float(row["t_end_s"]), float(row["distance_m"]))
    if seg.get("cover_share", 0.0) < em_laps.MIN_COVER or (seg.get("em_recording"), rec) not in ctx.aligned:
        return None, None
    laps = [{"lap": int(x["lap"]), **em_laps.summarize_segment(als, als[0].track, float(x["t_start_s"]), float(x["t_end_s"]), float(x["distance_m"]))}
            for _, x in lp.iterrows()]
    lap_e = pd.DataFrame(laps).reindex(columns=["lap"] + em_laps.JOIN_COLS + em_laps.VALUE_COLS)
    return pd.Series(seg), lap_e


def _timed(event_dir: Path, notes: dict, made: dict, ctx: "_GpsCtx | None") -> tuple[list[Session], set[tuple[str, int]]]:
    """→ (시각 구간 세션들, 그 세션들이 가져간 GPS 주행 구간 {(레코딩, 주행)})."""
    entries = notes.get("timed_session", [])
    if not entries:
        return [], set()
    ev = notes.get("event", {})
    clocks = _Clocks(notes)
    recs = _recordings(event_dir)
    by_id = {x.rec: x for x in recs}
    day_notes = ev.get("day_notes", {})
    split = set(ctx.runs["recording_id"]) if ctx is not None else set()          # 랩 분할을 한 GPS 레코딩
    taken: list[tuple[float, float, str]] = []
    out, covered = [], set()
    for e in entries:
        label = e.get("name") or str(e.get("start") or e.get("recording", "?"))
        if not (e.get("name") or e.get("discipline")):
            raise ValueError(f"{NOTES_NAME}: [[timed_session]] 에는 name 이나 discipline 이 있어야 한다 — {e}")
        t0, t1 = _window(e, by_id, clocks)
        clash = next((name for a, b, name in taken if t0 < b and a < t1), None)
        if clash:
            raise ValueError(f"{NOTES_NAME}: 시각 구간 '{label}' 이 '{clash}' 와 겹친다")
        taken.append((t0, t1, label))
        near = [(x, clocks.offset(x)) for x in recs if x.device in GPS_DEVICES and x.rec in split and clocks.known(x)]
        hit = max(((x, off, min(t1, x.t1 + off) - max(t0, x.t0 + off)) for x, off in near), key=lambda h: h[2], default=None)
        got = _gps_window(ctx, hit[0], t0, t1, hit[1]) if hit is not None and hit[2] >= GPS_COVER_MIN * (t1 - t0) else None
        if got is not None:
            x, off = hit[0], hit[1]
            row, lp, runs, (c0, c1) = got
            covered |= {(x.rec, n) for n in runs}
            run_e, lap_e = _window_energy(ctx, x.rec, row, lp)
            start = pd.Timestamp(c0, unit="s", tz="UTC").tz_convert(TZ)
            end = start + pd.Timedelta(seconds=c1 - c0)
            head = [f"시각으로 나눈 세션 — {clocks.label} 기준 {start:%H:%M:%S} ~ {end:%H:%M:%S}. 이 구간을 덮는 GPS 기록({x.rec})으로 랩 · 코스를 냈다"
                    + (f" (GPS 기록이 있는 동안으로 {(t1 - t0) - (c1 - c0):.0f} s 줄였다)" if (t1 - t0) - (c1 - c0) > 0.5 else "")]
            s = _gps_session(ctx, x.rec, row, lp, {**e, "run": runs}, run_e, lap_e, head=head, split_by="time")
            more, st = _window_blocks(_Window(event_dir, notes, c0, c1), recs, clocks, strict=False)     # 시각으로만 붙는 장비는 빈 자리에
            have = {b.key for b in s.blocks}
            s.blocks += [b for b in more if b.key not in have]
            for k in ("net_wh", "i_max_a"):
                if s.stats.get(k) is None and st.get(k) is not None:
                    s.stats[k] = st[k]
            out.append(s)
            continue
        w = _Window(event_dir, notes, t0, t1)
        blocks, stats = _window_blocks(w, recs, clocks)
        if not blocks and not e.get("allow_empty"):
            raise ValueError(f"{NOTES_NAME}: 시각 구간 '{label}' 에 기록이 남은 장비가 없다 — 시각 · 시계 차를 확인할 것 "
                             "(정말 없으면 allow_empty = true)")
        start = pd.Timestamp(t0, unit="s", tz="UTC").tz_convert(TZ)
        end = start + pd.Timedelta(seconds=t1 - t0)
        gps = [x.rec for x in recs if x.device in GPS_DEVICES
               and x.t0 + (clocks.offset(x) if clocks.known(x) else 0.0) < t1 and x.t1 + (clocks.offset(x) if clocks.known(x) else 0.0) > t0]
        head = f"시각으로 나눈 세션 — {clocks.label} 기준 {start:%H:%M:%S} ~ {end:%H:%M:%S}. "
        lines = [head + (f"이 시각의 GPS 기록({', '.join(gps)})이 구간을 덮지 않아(또는 시계 차 · 랩 분할이 없어) 랩 · 코스가 없다" if gps
                         else "GPS 기록이 없어 랩 · 코스가 없다")]
        lines += [] if blocks else ["이 시각에 기록이 남은 장비가 없다"]
        out.append(Session(
            sid="", car=ev.get("car") or next((x.meta.get("car", "") for x in recs if x.meta.get("car")), ""), day=f"{start:%Y-%m-%d}",
            name=e.get("name", ""), start=start, duration_s=t1 - t0, event_kind=ev.get("kind", ""), event_title=ev.get("title", ""),
            discipline=e.get("discipline", ""), place=ev.get("place", ""), driver=e.get("driver", ""),
            event_id=f"{event_dir.parent.name}/{event_dir.name}", stats=stats, laps=w.laps, blocks=blocks,
            missing=[Missing(m["data"], m["status"], m["reason"], m.get("ask", ""), m.get("block", "")) for m in notes.get("missing", [])
                     if not m.get("sessions") or e.get("name") in m["sessions"]],
            notes=list(e.get("notes", [])) + lines + list(day_notes.get(f"{start:%Y-%m-%d}", [])), made=made, split_by="time"))
    return out, covered

# ---------------------------------------------------------------- 이벤트 → 세션들

def build_event(event_dir: Path) -> list[Session]:
    """이벤트 폴더 → 세션들 (시작 시각 순). `catalog_notes.toml` 이 있어야 한다.

    GPS 주행 구간 세션(`[[session]]` · `sessions = "auto"`)은 랩 분할(runs.csv · laps.csv · gate.json)이, 시각 구간 세션(`[[timed_session]]`)은
    기준 시계와 장비 시계 차(`[event] clock` · `[clocks]`)가 있어야 한다.
    """
    event_dir = Path(event_dir).resolve()
    notes = load_notes(event_dir)
    if not notes:
        raise FileNotFoundError(f"{event_dir / NOTES_NAME} 가 없다 — 세션 이름 · 없는 데이터를 적은 파일이 있어야 카탈로그에 넣는다")
    ev = notes.get("event", {})
    made = {"generated": dt.date.today().isoformat(), "commit": code_version(),
            "command": f"lefdata catalog --event {event_dir.parent.name}/{event_dir.name}"}
    ctx = _GpsCtx(event_dir, notes, made) if _GpsCtx.available(event_dir) else None
    sessions, covered = _timed(event_dir, notes, made, ctx)
    if notes.get("session") or ev.get("sessions") == "auto":
        if ctx is None:
            raise FileNotFoundError("runs.csv · laps.csv · gate.json 이 없다 — 먼저 랩 분할(lefdata laps 또는 gopro --laps)을 돌릴 것")
        sessions = _gps_sessions(ctx, covered) + sessions
    sessions.sort(key=lambda s: s.start)
    auto: dict[tuple, int] = {}
    for s in sessions:                                          # 자동 이름: 그날 종목별 순서 ("트랙 2")
        if not s.name:
            auto[(s.day, s.discipline)] = auto.get((s.day, s.discipline), 0) + 1
            s.name = f"{s.discipline} {auto[(s.day, s.discipline)]}"
    count: dict[tuple, int] = {}
    for s in sessions:                                          # 그날의 몇 번째 세션인가
        count[(s.car, s.day)] = count.get((s.car, s.day), 0) + 1
        s.sid = f"{s.car}_{s.day}_S{count[(s.car, s.day)]}"
    for m in (m for s in sessions for m in s.missing):
        if m.status not in (SEEK, NONE):
            raise ValueError(f"{NOTES_NAME}: missing 의 status 는 '{SEEK}' 또는 '{NONE}' 이어야 한다 — {m.status!r}")
    return sessions


class _GpsCtx:
    """GPS 로 세션을 만드는 데 쓰는 이벤트 공통 것들 — 랩 분할 · 계측선 · 에너지미터 맞춤 · 로거 · 기준 시계."""

    def __init__(self, event_dir: Path, notes: dict, made: dict):
        d = event_dir / "derived"
        self.event_dir, self.notes, self.made, self.derived = event_dir, notes, made, d
        if not self.available(event_dir):
            raise FileNotFoundError("runs.csv · laps.csv · gate.json 이 없다 — 먼저 랩 분할(lefdata laps 또는 gopro --laps)을 돌릴 것")
        self.runs, laps = _read(d / "runs.csv"), _read(d / "laps.csv")
        if "run" not in laps:                                  # 완주 랩이 하나도 없는 이벤트 (가속 · 스키드패드뿐)
            laps = pd.DataFrame(columns=["recording_id", "run", "lap", "t_start_s", "t_end_s", "lap_time_s", "distance_m", "vmax_kmh", "vmin_kmh"])
        self.laps = laps
        self.gate_file = json.loads((d / "gate.json").read_text(encoding="utf-8"))
        self.origin = self.gate_file["origin"]
        g = self.gate_file["gate"]
        self.gate = {"x_m": round(float(g["x"]), 2), "y_m": round(float(g["y"]), 2), "heading_deg": round(float(g["heading_deg"]), 1),
                     "half_width_m": float(self.gate_file.get("half_width_m", 0.0))}
        self.run_e, self.lap_e, self.fits = _read(d / "run_energy.csv"), _read(d / "lap_energy.csv"), _read(d / "em_alignment.csv")
        self.aligned = {(a.meter.rec, a.track.rec): a for a in em_laps.align_event(event_dir) if a.accepted} if self.run_e is not None else {}
        self.cycles = _read(d / "power_cycles.csv")            # 같은 전원 스위치 — 켜지고 꺼진 시각으로 따로 잰 시계 차 (있으면)
        self.ev = notes.get("event", {})
        self.loggers, self.stm = _Loggers(event_dir), _Stm32(event_dir)
        self.day_notes = self.ev.get("day_notes", {})
        self.clocks = _Clocks(notes) if self.ev.get("clock") else None        # 기준 시계가 있는 이벤트 — 시작 시각을 그 시계로 보인다
        self.gps_recs = {x.rec: x for x in _recordings(event_dir) if x.device in GPS_DEVICES} if self.clocks else {}

    @staticmethod
    def available(event_dir: Path) -> bool:
        return all((Path(event_dir) / "derived" / n).exists() for n in ("runs.csv", "laps.csv", "gate.json"))


def _gps_sessions(ctx: _GpsCtx, skip: set[tuple[str, int]] = frozenset()) -> list[Session]:
    """GPS 주행 구간(runs.csv) 하나 = 세션 하나. 이름을 적은 것(`[[session]]`)과, `sessions = "auto"` 면 종목을 알아본 것.

    skip = 시각 구간 세션이 가져간 주행 구간 (자동으로는 만들지 않고, `[[session]]` 에도 적었으면 멈춘다).
    """
    named = {(s["recording"], int(s["run"])): s for s in ctx.notes.get("session", [])}
    twice = sorted(k for k in named if k in skip)
    if twice:
        raise ValueError(f"{NOTES_NAME}: 같은 주행을 [[session]] 과 [[timed_session]] 에 두 번 적었다 — {twice}")
    if ctx.ev.get("sessions") == "auto":
        named = {**_auto_sessions(ctx.runs, ctx.derived), **named}
    sessions = []
    for _, run in ctx.runs.iterrows():
        key = (run["recording_id"], int(run["run"]))
        if key not in named or key in skip:                     # 이름을 붙이지 않은 주행 구간은 넣지 않는다
            continue
        mine = ctx.laps[(ctx.laps["recording_id"] == key[0]) & (ctx.laps["run"] == key[1])]
        e = ctx.run_e[(ctx.run_e["recording_id"] == key[0]) & (ctx.run_e["run"] == key[1])] if ctx.run_e is not None else pd.DataFrame()
        e = e.iloc[0] if len(e) and (e.iloc[0]["em_recording"], key[0]) in ctx.aligned and e.iloc[0]["cover_share"] >= em_laps.MIN_COVER else None
        lap_e = ctx.lap_e[(ctx.lap_e["recording_id"] == key[0]) & (ctx.lap_e["run"] == key[1])] if e is not None else None
        sessions.append(_gps_session(ctx, key[0], run, mine, named[key], e, lap_e))
    return sessions


def _gps_session(ctx: _GpsCtx, rec: str, run: pd.Series, mine: pd.DataFrame, info: dict, e: pd.Series | None, lap_e: pd.DataFrame | None,
                 head: list[str] = (), split_by: str = "gps") -> Session:
    """GPS 레코딩의 한 구간(run 의 t_start_s ~ t_end_s) → 세션. e · lap_e = 그 구간 · 랩의 에너지미터 값 (em_laps 의 열), 없으면 None."""
    r = _Run(ctx.event_dir, rec, run, mine, ctx.origin, ctx.notes)
    stats = {"distance_km": round(float(run["distance_m"]) / 1000, 2), "laps_n": int(run["full_laps"]),
             "best_lap_s": round(float(mine["lap_time_s"].min()), 2) if len(mine) else None, "vmax_kmh": round(float(run["vmax_kmh"]), 1)}
    al = ctx.aligned[(e["em_recording"], rec)] if e is not None else None
    blocks = [_gps_block(r), _imu_block(r)] + _logger_blocks(r, ctx.loggers.for_video(r.rec)) + _stm32_blocks(r, ctx.stm, _em_ref(r, al, e) if al else None, al)
    if al is not None:
        fit = ctx.fits[(ctx.fits["em_recording"] == e["em_recording"]) & (ctx.fits["gps_recording"] == rec)].iloc[0]
        em = _battery_block(r, al, e, lap_e, fit)
        pc = ctx.cycles[(ctx.cycles["energymeter"] == e["em_recording"]) & (ctx.cycles["monolith"] == rec)] if ctx.cycles is not None else pd.DataFrame()
        if len(pc) and fit.get("method", "wave") != "power_cycle":
            x = float(pc.iloc[0]["em_minus_monolith_s"])
            em.checks.append(f"같은 전원 스위치로 켜지고 꺼진 시각으로 따로 잰 시계 차 {x:.1f} s (lefdata clocks) — 파형으로 맞춘 값과 "
                             f"{float(fit['em_minus_gps_s']) - x:+.2f} s 차이")
        blocks.append(em)
        stats.update(net_wh=round(float(e["net_wh"])), wh_per_km=round(float(e["wh_per_km"])), i_max_a=round(float(e["i_max_a"])))
    blocks.append(_video_block(r))
    start = pd.Timestamp(r.t0_abs, unit="s", tz="UTC").tz_convert(TZ)
    shown = []
    x = ctx.gps_recs.get(r.rec)
    if x is not None and ctx.clocks.known(x) and ctx.clocks.offset(x):
        start += pd.Timedelta(seconds=ctx.clocks.offset(x))
        shown = [f"시작 시각은 이벤트의 기준 시계로 옮겨 보인다 — {ctx.clocks.text(x)}. 세션 안의 시간축(t_s)은 GPS 로거 그대로"]
    gps_cols = [c for c in r.laps.columns if c not in ("t_start_s", "t_end_s")]
    blocks[0].exports.append(Export("laps.csv", "이 세션의 랩별 값 전체 (모든 데이터 종류의 열을 한 표에)", r.laps[gps_cols].round(3), f"{len(r.laps)} 랩"))
    track = pd.DataFrame({"t_s": r.ts, "x_m": r.x, "y_m": r.y, "dist_m": r.dist})
    ev, event_dir = ctx.ev, ctx.event_dir
    return Session(
        sid="", car=ev.get("car", r.meta["car"]), day=f"{start:%Y-%m-%d}", name=info.get("name", ""), start=start, duration_s=r.t1 - r.t0,
        event_kind=ev.get("kind", ""), event_title=ev.get("title", ""), discipline=info.get("discipline", ""), place=ev.get("place", ""),
        driver=info.get("driver", ""), event_id=f"{event_dir.parent.name}/{event_dir.name}", stats=stats, laps=r.laps, track=track, gate=ctx.gate,
        frame_id=ctx.gate_file.get("frame_id") or f"{event_dir.parent.name}/{event_dir.name}",
        blocks=[b for b in blocks if b is not None],
        missing=[Missing(m["data"], m["status"], m["reason"], m.get("ask", ""), m.get("block", "")) for m in ctx.notes.get("missing", [])
                 if not m.get("sessions") or info.get("name") in m["sessions"]],
        notes=list(info.get("notes", [])) + list(head) + _segment_notes(info, ctx.derived, r) + shown + list(ctx.day_notes.get(f"{start:%Y-%m-%d}", [])),
        made=ctx.made, split_by=split_by)

def write_event(event_dir: Path) -> list[dict]:
    """이벤트의 세션 페이지를 전부 쓴다 → 세션 표의 줄들. <이벤트>/derived/catalog/<세션>/"""
    event_dir = event_dir.resolve()
    out = event_dir / "derived" / "catalog"
    code_dir = paths.DATA_ROOT.resolve() / ROOT_NAME / "code"
    rows = []
    for s in build_event(event_dir):
        row = write_session(s, out / s.sid, code_dir)
        row["dir"] = os.path.relpath(out / s.sid, paths.DATA_ROOT.resolve()).replace("\\", "/")
        rows.append(row)
    (out / "index.json").write_text(json.dumps(rows, ensure_ascii=False, indent=1), encoding="utf-8", newline="\n")
    return rows


# ---------------------------------------------------------------- 코드 페이지

CODE_PAGES = {
    "energymeter": {
        "title": "에너지미터 로그 → 전압 · 전류 표 (energymeter)", "files": ["src/lefdata/energymeter.py"],
        "command": "lefdata energymeter --event <이벤트 폴더>",
        "what": ["주최측 에너지미터(FSK-EEM)가 남긴 바이너리 로그(.log)를 읽어 전압 · 전류 표로 바꿈",
                 "파일 구조: 머리 32 바이트(장치 번호 · 시작 시각) 뒤에 16 바이트 레코드가 10 ms 마다 하나씩 (HV 전압 · HV 전류 · LV 전압 · 장치 온도)",
                 "레코드마다 체크섬을 확인하고, 틀린 레코드는 지우지 않고 valid 열에 표시"],
        "inputs": "raw_energymeter/*.log (전원을 켤 때마다 파일 하나)",
        "outputs": "normalized/<레코딩>/energy.parquet — t_rel_ms · t_abs · voltage_v · current_a · lv_voltage_v · temp_c · valid",
        "checks": ["파일 형식은 장비 설계자가 공개한 소스(luftaquila/fsk-energymeter)와 대조",
                   "가진 로그 전부를 이 정의로 읽어 체크섬 실패 0 확인"],
        "cautions": ["미터의 절대 시각은 사람이 맞춘 시계. 다른 장비보다 수 분 앞서 있을 수 있음"],
        "tests": ["tests/test_fsk_evo_golden.py", "tests/test_em_laps.py"]},
    "gopro": {
        "title": "온보드 영상 → GPS · 가속도 표 (gopro · video)", "files": ["src/lefdata/gopro.py", "src/lefdata/video.py"],
        "command": "lefdata gopro --event <이벤트 폴더> --laps",
        "what": ["GoPro 영상 파일 안에는 화면과 별도로 텔레메트리 기록(GPMF)이 들어 있음: GPS 약 18 Hz · 가속도계와 자이로 약 200 Hz",
                 "영상을 풀지 않고 그 기록만 읽어 표로 바꿈. 4 GB 마다 쪼개진 파일(GOPR → GP01 → GP02 …)은 한 레코딩으로 이어 붙임",
                 "위성을 충분히 잡지 못한 구간(3D fix 아님)은 버림",
                 "가속도계 시각이 GPS 보다 0 · 1 · 2 초 늦게 들어 있어(파일마다 다름) 재서 당김"],
        "inputs": "raw_video/*.MP4 · *.LRV (저해상도 사본에도 같은 기록이 있음)",
        "outputs": "normalized/<레코딩>/gps.parquet (lat · lon · speed_kmh · course_deg · alt_m · dop) · imu.parquet (a0 ~ a2_g · g0 ~ g2_dps, 카메라 축)",
        "checks": ["영상마다 차량 로거의 요레이트와 파형을 대 봐서 짝이 맞는지 확인"],
        "cautions": ["GPS 속도 값은 같은 GPS 의 위치보다 1/10 초 남짓 늦음",
                     "가속도 축은 카메라 기준. 차량 축으로 바꾸는 것은 세션 페이지를 만드는 코드가 함"],
        "tests": ["tests/test_gopro_time.py", "tests/test_lef26_video_golden.py"]},
    "laps": {
        "title": "GPS → 주행 구간 · 랩 (laps)", "files": ["src/lefdata/laps.py"],
        "command": "lefdata laps --event <이벤트 폴더>   (영상은 lefdata gopro --laps)",
        "what": ["속도 10 km/h 이상이 이어지는 구간을 주행 구간으로 나눔 (10 초보다 짧은 정지는 잇고, 20 초보다 짧은 구간은 버림)",
                 "가장 긴 주행에서 차가 늘 같은 방향으로 지나는 곳을 계측선으로 자동 선정",
                 "계측선을 지날 때마다 랩을 끊음. 통과 시각은 앞뒤 표본 사이를 보간"],
        "inputs": "normalized/<레코딩>/gps.parquet",
        "outputs": "derived/runs.csv (주행 구간) · laps.csv (랩) · gate.json (계측선)",
        "checks": ["대회 측 리포트의 랩 수 · 주행 시간을 독립적으로 재현하는지 확인"],
        "cautions": ["계측선은 데이터에서 고른 선이라 실제 대회 계측선의 위치와 다를 수 있음"],
        "tests": ["tests/test_laps_golden.py"]},
    "em_laps": {
        "title": "에너지미터 ↔ GPS 시계 맞추기 · 랩별 에너지 (em_laps)", "files": ["src/lefdata/em_laps.py"],
        "command": "lefdata em-laps --event <이벤트 폴더>",
        "what": ["에너지미터에는 GPS 가 없어 랩이 어디서 끊기는지 모름",
                 "미터의 전력(전압 × 전류)과 GPS 위치에서 낸 '차를 미는 일률' 이 같은 모양으로 오르내리는 것을 이용해 두 장비의 시계를 맞춤",
                 "30 초 창마다 가장 잘 겹치는 어긋남을 구하고 창들이 한 직선에 놓이는지 확인 → 시계 차와 시계 속도를 같이 구함",
                 "맞춘 뒤 랩 · 주행 구간마다 에너지(방전 · 돌아온 양 · 순) · 전력 · 전류 · 전압을 냄"],
        "inputs": "normalized/<에너지미터 레코딩>/energy.parquet · <GPS 레코딩>/gps.parquet · derived/runs.csv · laps.csv",
        "outputs": "derived/em_alignment.csv (시계) · lap_energy.csv (랩별) · run_energy.csv (주행 구간별)",
        "checks": ["답을 아는 데이터로 확인 — 전원이 꺼진 순간으로 짝지은 시계 차와 이 코드의 결과가 0.1 s 안에서 같은지",
                   "카메라 가속도계로 다시 대 봐도 같은 시계 차가 나오는지"],
        "cautions": ["미터가 구간의 98 % 이상을 기록하지 않은 랩에는 값을 내지 않음"],
        "tests": ["tests/test_em_laps.py"]},
    "catalog": {
        "title": "세션 페이지 만들기 (catalog)", "files": ["src/lefdata/catalog.py", "src/lefdata/catalog_charts.py", "src/lefdata/catalog_build.py"],
        "command": "lefdata catalog --event <이벤트 폴더>",
        "what": ["변환된 표와 랩 분할 결과를 읽어 세션마다 페이지를 만듦 (그림 · 표 · 세션 구간만 자른 CSV)",
                 "그림의 형태는 데이터 종류와 상관없이 다섯 가지 — 시간 · 랩 · 거리 · 지도 · 분포",
                 "카메라 가속도계는 카메라 축 기준이라, 차가 서 있을 때의 중력 방향을 '위' 로 삼아 차량의 앞뒤 · 좌우 축으로 바꿈"],
        "inputs": "normalized/ · derived/ · <이벤트>/catalog_notes.toml (세션 이름 · 없는 데이터처럼 사람이 아는 것)",
        "outputs": "<이벤트>/derived/catalog/<세션>/page.md · figures/ · files/",
        "checks": ["차량 축으로 바꾼 가속도 · 요레이트를 GPS 위치에서 낸 값과 세션마다 대조 (세션 페이지의 '검증' 항목)"],
        "cautions": ["페이지는 코드가 만든 것. 노션에서 손으로 고친 내용은 다음 생성 때 사라짐"],
        "tests": ["tests/test_catalog.py"]},
}


def code_blocks(key: str) -> list[dict]:
    """코드 페이지 하나의 블록 목록: 무엇을 하는가 → 입력 · 출력 · 실행 → 검증 · 주의 → 코드 전문."""
    c = CODE_PAGES[key]
    out: list[dict] = [{"t": "h", "level": 2, "text": "무엇을 하는가"}, {"t": "ul", "items": c["what"]},
                       {"t": "h", "level": 2, "text": "입력 · 출력 · 실행"},
                       {"t": "kv", "rows": [("입력", c["inputs"]), ("출력", c["outputs"]), ("실행", f"`{c['command']}`"),
                                            ("테스트", " · ".join(c["tests"])), ("코드 버전", code_version())]},
                       {"t": "h", "level": 2, "text": "검증 · 주의"},
                       {"t": "ul", "items": [f"검증: {x}" for x in c["checks"]] + [f"주의: {x}" for x in c["cautions"]]}]
    for rel in c["files"]:
        text = (REPO / rel).read_text(encoding="utf-8").replace("\r\n", "\n")
        out += [{"t": "h", "level": 2, "text": f"코드 — {rel} ({len(text.splitlines())} 줄)"},
                {"t": "code", "lang": "python", "text": text, "file": rel, "html": f"code/html/{Path(rel).stem}.html"}]
    return out


# ---------------------------------------------------------------- 보유 현황 · 첫 페이지

def inventory(data_root: Path) -> pd.DataFrame:
    """변환된 레코딩 전부 — 차량 · 이벤트 · 장비 · 시작 시각 · 길이 · 들어 있는 표. (세션으로 묶기 전의 재료)"""
    rows = []
    for meta_path in sorted(data_root.glob("*/*/normalized/*/meta.json")):
        m = json.loads(meta_path.read_text(encoding="utf-8"))
        t = m.get("time", {})
        span = t.get("duration_s")
        if span is None and t.get("t_abs_start") and t.get("t_abs_end"):       # 길이를 따로 적지 않는 장비: 시작 · 끝 시각에서
            span = (pd.Timestamp(t["t_abs_end"]) - pd.Timestamp(t["t_abs_start"])).total_seconds()
        rows.append({"car": m.get("car", meta_path.parts[-5]), "event": meta_path.parts[-4], "device": m.get("source", {}).get("logger", ""),
                     "recording_id": m.get("recording_id", meta_path.parent.name), "start": t.get("t_abs_start") or "",
                     "duration_s": span, "tables": " ".join(sorted(p.stem for p in meta_path.parent.glob("*.parquet")))})
    return pd.DataFrame(rows)


def inventory_summary(inv: pd.DataFrame) -> pd.DataFrame:
    """차량 · 이벤트 · 장비마다 레코딩 수와 기록 시간."""
    g = inv.groupby(["car", "event", "device"], sort=True)
    out = g.agg(recordings=("recording_id", "size"), timed=("duration_s", lambda s: int(s.notna().sum())),
                hours=("duration_s", lambda s: round(s.fillna(0).sum() / 3600, 1)),
                first=("start", lambda s: min((x[:10] for x in s if x), default="")),
                last=("start", lambda s: max((x[:10] for x in s if x), default=""))).reset_index()
    return out


FORMS_TABLE = [["시간", "세션 시작부터의 시간 (분)", "채널 값. 채널마다 한 칸", "세션 전체의 흐름, 랩 경계"],
               ["랩", "랩 번호", "랩마다 한 값", "랩이 지나며 어떻게 변했나"],
               ["거리", "계측선부터 달린 거리 (m)", "채널 값. 최속 랩 하나", "코스의 어느 지점에서 무슨 일이 있었나"],
               ["지도", "(코스 모양)", "주행선에 채널 값을 색으로", "거리 그림의 숫자가 코스의 어디인가"],
               ["분포", "좌우 가속도 (g)", "앞뒤 가속도 (g)", "타이어를 어디까지 썼나"]]


def root_blocks(rows: list[dict], inv: pd.DataFrame) -> list[dict]:
    """첫 페이지: 안내 → 세션 표 → 읽는 법 → 코드 → 보유 현황."""
    table = [[r["title"], r["event_kind"], r["discipline"], r["driver"], f"{r['duration_min']:.1f}", f"{r['distance_km']:.2f}",
              str(r["laps"]), f"{r['best_lap_s']:.2f}" if r["best_lap_s"] else "–", " · ".join(r["have"] + r["warn"]),
              " · ".join(r["seek"]) or "–"] for r in rows]
    summary = inventory_summary(inv)
    return [
        {"t": "p", "text": "라이트온 차량의 과거 주행 데이터를 세션(차가 나갔다 들어올 때까지) 단위로 모은 곳입니다. "
                           "세션마다 어떤 데이터가 있는지, 원본 파일과 그것을 바꾼 코드, 결과 파일, 같은 형태로 그린 그래프가 한 페이지에 있습니다."},
        {"t": "ul", "items": ["아래 표에서 세션을 누르면 그 세션의 페이지가 열립니다",
                              "'찾는 데이터' 칸에 적힌 것은 저희에게 파일이 없는 데이터입니다. 가지고 계신 분은 DAQ 파트로 알려 주세요",
                              "페이지는 코드가 데이터에서 자동으로 만듭니다. 틀린 곳을 보시면 페이지를 고치지 마시고 DAQ 파트에 알려 주세요 (다음 생성 때 반영됩니다)",
                              "팀 내부 자료입니다. 팀 밖으로 공유하지 말아 주세요 (대회 영상의 저작권은 학회에 있습니다)"]},
        {"t": "h", "level": 2, "text": "세션"},
        {"t": "table", "head": ["세션", "행사", "종목", "드라이버", "주행 (분)", "거리 (km)", "랩", "최속 랩 (s)", "있는 데이터", "찾는 데이터"], "rows": table},
        {"t": "h", "level": 2, "text": "세션 페이지 읽는 법"},
        {"t": "ul", "items": ["맨 위 표: 언제 · 어디서 · 얼마나 달렸는지",
                              "데이터 현황: 이 세션에 있는 데이터와 없는 데이터. 상태는 있음 · 있음 (주의) · 찾는 중 · 기록 안 됨",
                              "데이터 종류마다 한 절: 원본 → 코드 → 결과물, 그래프, 랩별 값, 검증 · 주의 순서",
                              "결과물의 CSV 는 그 세션 구간만 잘라 낸 표입니다. 엑셀에서 바로 열립니다 (시간 t_s 는 세션 시작부터의 초)"]},
        {"t": "p", "text": "그래프는 데이터 종류가 달라도 형태가 같습니다. 가로축이 형태를 정합니다."},
        {"t": "table", "head": ["형태", "가로축", "세로축", "보는 것"], "rows": FORMS_TABLE},
        {"t": "h", "level": 2, "text": "변환 · 분석 코드"},
        {"t": "p", "text": "세션 페이지의 '변환 · 분석 코드' 는 아래 페이지를 가리킵니다. 코드 하나가 여러 세션에 쓰입니다."},
        {"t": "codes", "title": "코드 페이지", "codes": [{"key": k, "title": c["title"], "role": c["what"][0]} for k, c in CODE_PAGES.items()]},
        {"t": "h", "level": 2, "text": "보유 현황 — 변환이 끝난 레코딩"},
        {"t": "p", "text": f"변환이 끝난 레코딩은 {len(inv):,}개입니다 (레코딩 = 장비의 전원을 한 번 켰다 끌 때까지의 기록). "
                           f"세션 페이지로 만든 것은 지금 {len(rows)}개 세션이고, 나머지는 차례로 추가합니다. "
                           "기록 시간은 길이를 아는 레코딩만 더한 값입니다 (괄호 안은 그 개수)."},
        {"t": "table", "head": ["차량", "이벤트", "장비", "레코딩", "기록 시간 (h)", "기간"],
         "rows": [[r["car"], r["event"], r["device"], str(r["recordings"]),
                   f"{r['hours']:.1f}" + ("" if r["timed"] == r["recordings"] else f" ({r['timed']}개)"),
                   r["first"] if r["first"] == r["last"] else f"{r['first']} ~ {r['last'][5:]}" if r["first"] else "시각 없음"]
                  for _, r in summary.iterrows()]},
    ]


def write_root(data_root: Path) -> dict:
    """이벤트마다 써 둔 index.json 을 모아 첫 페이지 · 세션 표 · 보유 현황 · 코드 페이지를 쓴다."""
    root = data_root / ROOT_NAME
    (root / "code" / "html").mkdir(parents=True, exist_ok=True)
    rows = [r for p in sorted(data_root.glob("*/*/derived/catalog/index.json")) for r in json.loads(p.read_text(encoding="utf-8"))]
    rows.sort(key=lambda r: r["start"])
    inv = inventory(data_root)
    inv.to_csv(root / "recordings.csv", index=False, encoding="utf-8-sig", lineterminator="\n")
    flat = pd.DataFrame([{**r, **{k: " | ".join(r[k]) for k in ("have", "warn", "seek", "none")}} for r in rows])
    flat.reindex(columns=SESSION_COLS + ["dir"]).to_csv(root / "sessions.csv", index=False, encoding="utf-8-sig", lineterminator="\n")
    blocks = root_blocks(rows, inv)
    title = "LightOn 주행 데이터"
    (root / "README.md").write_text(to_markdown(title, blocks, "code"), encoding="utf-8", newline="\n")
    pages = {"README": {"title": title, "blocks": blocks}}
    for key, c in CODE_PAGES.items():
        cb = code_blocks(key)
        (root / "code" / f"{key}.md").write_text(to_markdown(c["title"], cb), encoding="utf-8", newline="\n")
        for b in (b for b in cb if b["t"] == "code"):
            (root / b["html"]).write_text(code_html(b["file"], b["text"]), encoding="utf-8", newline="\n")
        pages[f"code/{key}"] = {"title": c["title"], "blocks": cb,
                                "uploads": [u for b in cb if b["t"] == "code" for u in (b["html"], b["file"])]}
    (root / "pages.json").write_text(json.dumps(pages, ensure_ascii=False, indent=1), encoding="utf-8", newline="\n")
    return {"sessions": rows, "recordings": len(inv), "root": root}


def write_notion(data_root: Path, mapping: dict) -> list[Path]:
    """노션에 올릴 마크다운을 쓴다 (사본). mapping = {"pages": {코드 key: 주소}, "uploads": {세션 폴더: {파일: 업로드 ID}}}.

    → _catalog/notion/<이름>.md. 그림 · 파일은 업로드 ID 로, 코드 링크는 페이지 주소로 바뀐다.
    """
    root = data_root / ROOT_NAME
    out = root / "notion"
    out.mkdir(parents=True, exist_ok=True)
    pages = {k.removeprefix("code/"): v for k, v in mapping.get("pages", {}).items()}
    done = []
    for name, page in json.loads((root / "pages.json").read_text(encoding="utf-8")).items():
        blocks = [b for b in page["blocks"] if not (name == "README" and b["t"] == "table" and b["head"][0] == "세션")]
        path = out / f"{name.replace('/', '_')}.md"
        path.write_text(to_notion(blocks, mapping.get("uploads", {}).get(name, {}), pages), encoding="utf-8", newline="\n")
        done.append(path)
    for index in sorted(data_root.glob("*/*/derived/catalog/index.json")):
        for row in json.loads(index.read_text(encoding="utf-8")):
            page = json.loads((data_root / row["dir"] / "page.json").read_text(encoding="utf-8"))
            path = out / f"{row['sid']}.md"
            path.write_text(to_notion(page["blocks"], mapping.get("uploads", {}).get(row["sid"], {}), pages), encoding="utf-8", newline="\n")
            done.append(path)
    return done
