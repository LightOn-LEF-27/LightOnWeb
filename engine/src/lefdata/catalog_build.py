"""주행 데이터 카탈로그 — 우리 데이터 폴더에서 `catalog.Session` 을 채운다 (팀 계층).

    lefdata catalog [--event E]
        → <이벤트>/derived/catalog/<세션>/page.md · page.json · figures/ · files/     세션 페이지 (원본)
        → 07_데이터/_catalog/README.md · sessions.csv · recordings.csv · code/*.md   첫 페이지 · 세션 표 · 보유 현황 · 코드 페이지

세션 = `derived/runs.csv` 의 주행 구간 하나 (GPS 로 나눈 것, 차가 나갔다 들어올 때까지). 지금 채우는 데이터 종류:

    gps      위치 · 속도            GPS 가 있는 레코딩
    imu      차량 거동              같은 레코딩의 가속도계 · 자이로. 지금은 온보드 영상(카메라 축 → 차량 축)만
    battery  배터리 전압 · 전류     주최측 에너지미터. `em_laps` 가 GPS 와 시계를 맞춘 것만
    video    온보드 영상            raw_video/ 의 파일 목록

차량 로거(BMS · 모터 · 서스펜션)와 GPS 가 없는 주행은 아직 넣지 않았다 (나중 순서).

사람이 아는 것(세션 이름 · 드라이버 · 없는 데이터 · 주의)은 `<이벤트>/catalog_notes.toml` 에 적는다.
이 파일이 있는 이벤트만 카탈로그에 들어간다. 드라이버 실명은 적지 않는다.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import subprocess
import tomllib
from pathlib import Path

import numpy as np
import pandas as pd
import pyarrow.parquet as pq

from . import em_laps, laps as lapmod, paths
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
        raw=_raw_sources(r), code=[CodeRef("gopro", CODE_PAGES["gopro"]["title"], "영상 파일 안의 GPS 기록을 표로"),
                                   CodeRef("laps", CODE_PAGES["laps"]["title"], "주행 구간과 랩으로 나눔")],
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


def _battery_block(r: _Run, al: em_laps.Alignment, run_e: pd.Series, lap_e: pd.DataFrame, fit: pd.Series) -> DataBlock:
    me = al.meter
    m = (me.t >= float(run_e["em_t_start_s"])) & (me.t <= float(run_e["em_t_end_s"]))
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
        checks=[f"미터에는 GPS 가 없어 전력 파형으로 시계를 맞춤 — 미터 시계가 GPS 보다 {fit['em_minus_gps_s']:.2f} s 앞, "
                f"30 초 창 {int(fit['inliers'])} / {int(fit['windows'])}개가 한 직선 위 (잔차 {fit['rms_s']:.2f} s)",
                f"랩 하나씩 따로 다시 대 본 어긋남 {lag.min():+.2f} ~ {lag.max():+.2f} s"],
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


# ---------------------------------------------------------------- 이벤트 → 세션들

def build_event(event_dir: Path) -> list[Session]:
    """이벤트 폴더 → 세션들 (시작 시각 순). `catalog_notes.toml` 과 랩 분할(runs.csv · laps.csv · gate.json)이 있어야 한다."""
    event_dir = Path(event_dir).resolve()
    notes = load_notes(event_dir)
    if not notes:
        raise FileNotFoundError(f"{event_dir / NOTES_NAME} 가 없다 — 세션 이름 · 없는 데이터를 적은 파일이 있어야 카탈로그에 넣는다")
    derived = event_dir / "derived"
    runs, laps = _read(derived / "runs.csv"), _read(derived / "laps.csv")
    if runs is None or laps is None:
        raise FileNotFoundError("runs.csv · laps.csv 가 없다 — 먼저 랩 분할(lefdata laps 또는 gopro --laps)을 돌릴 것")
    gate_file = json.loads((derived / "gate.json").read_text(encoding="utf-8"))
    origin = gate_file["origin"]
    gate = {"x_m": round(float(gate_file["gate"]["x"]), 2), "y_m": round(float(gate_file["gate"]["y"]), 2),
            "heading_deg": round(float(gate_file["gate"]["heading_deg"]), 1), "half_width_m": float(gate_file.get("half_width_m", 0.0))}
    run_e, lap_e, fits = _read(derived / "run_energy.csv"), _read(derived / "lap_energy.csv"), _read(derived / "em_alignment.csv")
    aligned = {a.meter.rec: a for a in em_laps.align_event(event_dir) if a.accepted} if run_e is not None else {}
    ev = notes.get("event", {})
    named = {(s["recording"], int(s["run"])): s for s in notes.get("session", [])}
    made = {"generated": dt.date.today().isoformat(), "commit": code_version(),
            "command": f"lefdata catalog --event {event_dir.parent.name}/{event_dir.name}"}

    sessions = []
    for _, run in runs.iterrows():
        key = (run["recording_id"], int(run["run"]))
        if key not in named:                                    # 이름을 붙이지 않은 주행 구간은 넣지 않는다
            continue
        info = named[key]
        mine = laps[(laps["recording_id"] == key[0]) & (laps["run"] == key[1])]
        r = _Run(event_dir, key[0], run, mine, origin, notes)
        blocks = [_gps_block(r), _imu_block(r)]
        stats = {"distance_km": round(float(run["distance_m"]) / 1000, 2), "laps_n": int(run["full_laps"]),
                 "best_lap_s": round(float(mine["lap_time_s"].min()), 2) if len(mine) else None, "vmax_kmh": round(float(run["vmax_kmh"]), 1)}
        if run_e is not None:
            e = run_e[(run_e["recording_id"] == key[0]) & (run_e["run"] == key[1])]
            if len(e) and e.iloc[0]["em_recording"] in aligned:
                e = e.iloc[0]
                blocks.append(_battery_block(r, aligned[e["em_recording"]], e,
                                             lap_e[(lap_e["recording_id"] == key[0]) & (lap_e["run"] == key[1])],
                                             fits[fits["em_recording"] == e["em_recording"]].iloc[0]))
                stats.update(net_wh=round(float(e["net_wh"])), wh_per_km=round(float(e["wh_per_km"])), i_max_a=round(float(e["i_max_a"])))
        blocks.append(_video_block(r))
        start = pd.Timestamp(r.t0_abs, unit="s", tz="UTC").tz_convert(TZ)
        gps_cols = [c for c in r.laps.columns if c not in ("t_start_s", "t_end_s")]
        blocks[0].exports.append(Export("laps.csv", "이 세션의 랩별 값 전체 (모든 데이터 종류의 열을 한 표에)", r.laps[gps_cols].round(3), f"{len(r.laps)} 랩"))
        track = pd.DataFrame({"t_s": r.ts, "x_m": r.x, "y_m": r.y, "dist_m": r.dist})
        sessions.append(Session(
            sid="", car=ev.get("car", r.meta["car"]), day=f"{start:%Y-%m-%d}", name=info["name"], start=start, duration_s=r.t1 - r.t0,
            event_kind=ev.get("kind", ""), event_title=ev.get("title", ""), discipline=info.get("discipline", ""), place=ev.get("place", ""),
            driver=info.get("driver", ""), event_id=f"{event_dir.parent.name}/{event_dir.name}", stats=stats, laps=r.laps, track=track, gate=gate,
            frame_id=gate_file.get("frame_id") or f"{event_dir.parent.name}/{event_dir.name}",
            blocks=[b for b in blocks if b is not None],
            missing=[Missing(m["data"], m["status"], m["reason"], m.get("ask", ""), m.get("block", "")) for m in notes.get("missing", [])
                     if not m.get("sessions") or info["name"] in m["sessions"]],
            notes=list(info.get("notes", [])), made=made))
    sessions.sort(key=lambda s: s.start)
    count: dict[tuple, int] = {}
    for s in sessions:                                          # 그날의 몇 번째 세션인가
        count[(s.car, s.day)] = count.get((s.car, s.day), 0) + 1
        s.sid = f"{s.car}_{s.day}_S{count[(s.car, s.day)]}"
    for m in (m for s in sessions for m in s.missing):
        if m.status not in (SEEK, NONE):
            raise ValueError(f"{NOTES_NAME}: missing 의 status 는 '{SEEK}' 또는 '{NONE}' 이어야 한다 — {m.status!r}")
    return sessions


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
