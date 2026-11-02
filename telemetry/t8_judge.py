"""T8 휴대전화망 실시간 시험 판정 — Monolith 가 서버로 보낸 기록을 재서 합격 여부를 낸다.

    t8_judge.py fetch --date YYYY-MM-DD --out <폴더>      서버 DB 에서 그날 기록을 읽어 온다 (SELECT 만)
    t8_judge.py judge --dir <폴더> [--sd <normalized>] [--runs <runs.csv>] [--venue <venue.json>]

시계: 장치 시각은 부팅 뒤 ms 이고 그 기준(boot_epoch)은 초 단위 · 동기화 전 값이라 믿지 않는다.
`<이름>/d` 메시지에 보낸 순간의 장치 시각이 들어 있으므로, 세션마다 "받은 시각 − 보낸 시각" 의
아래쪽 경계(바닥)를 빼서 시계 차이와 시계 속도 차이를 없앤다. 바닥 자체(가장 빠른 전달 시간)는
재지 못하므로 FLOOR_S 를 더한다 [가정].

SD 와는 같은 부팅의 장치 시각을 그대로 쓰므로 시계 맞추기가 필요 없다.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pandas as pd

# 합격 기준 (트랙데이 준비안 T8)
AGE_MEDIAN_S = 1.0
AGE_P95_S = 3.0
OUTAGE_S = 10.0
OUTAGES_PER_LAP = 1

FLOOR_S = 0.10          # 가장 빠른 전달 시간 [가정]
FLOOR_BIN_S = 60.0      # 바닥을 잡는 구간 길이
SAME_SPOT_M = 50.0      # 끊김이 같은 자리로 묶이는 거리
MIN_SESSION_MSGS = 20   # 이보다 짧은 세션은 바닥을 못 잡는다
MAX_CLOCK_RATE = 100e-6 # 장치 · 서버 시계 속도 차이의 상한 (수정 발진기)


# ---------------------------------------------------------------- 서버에서 읽기

QUERIES = {
    "messages": """select extract(epoch from received_at) as recv_s, topic, payload_length,
        encode(substring(payload from 1 for 4), 'hex') as head
        from raw_messages where {when} order by id""",
    "can_frames": """select extract(epoch from received_at) as recv_s, session_id, device_ts_ms, can_id
        from can_frames where {when} order by id""",
    "gps": """select session_id, device_ts_ms, latitude, longitude, speed
        from samples where kind = 'gps' and {when} order by id""",
    "sessions": """select id, boot_epoch, extract(epoch from started_at) as started_s,
        extract(epoch from last_seen_at) as last_seen_s from sessions where {when_sessions} order by id""",
}


def fetch(date: str, out: Path, target: str, key: str, tz: str = "Asia/Seoul") -> None:
    """읽기 전용 트랜잭션으로 그날 기록을 CSV 로 받는다. 서버에 아무것도 쓰지 않는다."""
    out.mkdir(parents=True, exist_ok=True)
    start = f"'{date} 00:00'::timestamp at time zone '{tz}'"
    end = f"('{date} 00:00'::timestamp + interval '1 day') at time zone '{tz}'"
    when = f"received_at >= {start} and received_at < {end}"
    when_s = f"started_at >= {start} and started_at < {end}"
    remote = ("docker exec -i -e PGOPTIONS='-c default_transaction_read_only=on' "
              "monolith-v2-postgres-1 psql -U monolith -d monolith -v ON_ERROR_STOP=1")
    for name, sql in QUERIES.items():
        sql = " ".join(sql.format(when=when, when_sessions=when_s).split())
        with open(out / f"{name}.csv", "wb") as f:
            subprocess.run(
                ["ssh", "-i", key, "-o", "BatchMode=yes", target, remote],
                input=f"copy ({sql}) to stdout csv header;\n".encode(), stdout=f, check=True)
        print(f"{name}: {sum(1 for _ in open(out / f'{name}.csv', encoding='utf-8')) - 1}줄")


# ---------------------------------------------------------------- 계산 (입력은 표, 파일을 모른다)

def ddmm_to_deg(v):
    """서버 DB 좌표(ddmm.mmmm / 100, 예 37.3311 = 37° 33.11′) → 십진도."""
    v = np.asarray(v, float)
    d = np.floor(v)
    return d + (v - d) * 100.0 / 60.0


def assign_sessions(messages: pd.DataFrame, sessions: pd.DataFrame) -> pd.Series:
    """받은 시각으로 세션을 붙인다: 그 시각 전에 시작한 가장 늦은 세션."""
    s = sessions.sort_values("started_s")
    k = np.searchsorted(s["started_s"].to_numpy(), messages["recv_s"].to_numpy() + 1.0, side="right") - 1
    ids = s["id"].to_numpy()
    return pd.Series(np.where(k >= 0, ids[np.clip(k, 0, None)], -1), index=messages.index)


def device_ms_of(head: pd.Series) -> np.ndarray:
    """`<이름>/d` 첫 4바이트 (little endian u32) = 보낸 순간의 장치 ms."""
    return np.array([int.from_bytes(bytes.fromhex(h), "little") for h in head], dtype=np.int64)


@dataclass
class Floor:
    """받은 시각 ↔ 장치 시각. offset_s(recv) = 받은 시각 − 장치 시각 의 아래쪽 경계."""
    slope: float
    intercept: float

    def offset_s(self, recv_s):
        return self.slope * np.asarray(recv_s, float) + self.intercept

    def device_ms(self, recv_s):
        return (np.asarray(recv_s, float) - self.offset_s(recv_s)) * 1000.0


def fit_floor(recv_s: np.ndarray, device_ms: np.ndarray) -> Floor:
    """구간별 최솟값에 직선을 맞추고 전체가 그 위에 오도록 내린다 (시계 속도 차이까지 없앤다)."""
    transit = recv_s - device_ms / 1000.0
    t0 = recv_s[0]
    bins = ((recv_s - t0) // FLOOR_BIN_S).astype(int)
    g = pd.DataFrame({"b": bins, "t": recv_s, "x": transit})
    g = g.loc[g.groupby("b")["x"].idxmin()]
    slope = 0.0
    if len(g) >= 3:
        slope = float(np.polyfit(g["t"].to_numpy() - t0, g["x"].to_numpy(), 1)[0])
        # 길게 이어진 혼잡이 시계 속도 차이로 흡수되지 않게 물리적으로 가능한 만큼만 받는다
        slope = float(np.clip(slope, -MAX_CLOCK_RATE, MAX_CLOCK_RATE))
    intercept = float(np.min(transit - slope * (recv_s - t0)))
    return Floor(slope, intercept - slope * t0)


def outages(recv_s: np.ndarray, floor: Floor, span_ms: tuple[float, float] | None = None,
            limit_s: float = OUTAGE_S) -> pd.DataFrame:
    """메시지가 limit_s 넘게 안 들어온 구간. 장치 시각으로 돌려준다.

    span_ms(SD 의 기록 범위)를 주면 마지막 메시지 뒤의 빈 구간도 끊김으로 센다.
    첫 메시지 전은 끊김이 아니라 부팅 뒤 접속 시간이다 (따로 잰다).
    """
    r = np.sort(recv_s)
    dev = floor.device_ms(r)
    rows = [(dev[i], dev[i + 1]) for i in np.flatnonzero(np.diff(r) > limit_s)]
    if span_ms is not None:
        if span_ms[1] - dev[-1] > limit_s * 1000:
            rows.append((dev[-1], span_ms[1]))
    d = pd.DataFrame(rows, columns=["start_ms", "end_ms"], dtype=float)
    d["duration_s"] = (d["end_ms"] - d["start_ms"]) / 1000.0
    return d


def throttle(ts_ms: np.ndarray, can_id: np.ndarray, intv_ms: float) -> pd.DataFrame:
    """펌웨어가 ID 마다 거르는 규칙을 SD 프레임에 그대로 돌려 "보냈어야 할" 프레임을 낸다."""
    order = np.argsort(ts_ms, kind="stable")
    ts, ids = ts_ms[order], can_id[order]
    keep_ts, keep_id = [], []
    for i in np.unique(ids):
        last = -np.inf
        for v in ts[ids == i]:
            if v - last >= intv_ms:
                keep_ts.append(v)
                keep_id.append(i)
                last = v
    return pd.DataFrame({"device_ts_ms": keep_ts, "can_id": keep_id})


def in_windows(ms: np.ndarray, windows: list[tuple[float, float]] | None) -> np.ndarray:
    if windows is None:
        return np.ones(len(ms), bool)
    m = np.zeros(len(ms), bool)
    for a, b in windows:
        m |= (ms >= a) & (ms <= b)
    return m


def watched_s(windows: list[tuple[float, float]] | None, span: tuple[float, float]) -> float:
    if windows is None:
        return (span[1] - span[0]) / 1000.0
    return sum(max(0.0, min(b, span[1]) - max(a, span[0])) for a, b in windows) / 1000.0


def per_lap(out: pd.DataFrame, crossings_ms: np.ndarray) -> np.ndarray:
    """바퀴(계측선 사이)마다 겹친 끊김 수."""
    laps = np.zeros(max(len(crossings_ms) - 1, 0), int)
    for i in range(len(laps)):
        a, b = crossings_ms[i], crossings_ms[i + 1]
        laps[i] = int(((out["start_ms"] < b) & (out["end_ms"] > a)).sum())
    return laps


def spots(lat: np.ndarray, lon: np.ndarray, labels: list, radius_m: float = SAME_SPOT_M) -> list[dict]:
    """끊김 위치를 거리로 묶는다. labels 는 그 끊김이 난 바퀴 (없으면 None)."""
    out: list[dict] = []
    for la, lo, lb in zip(lat, lon, labels):
        if not (np.isfinite(la) and np.isfinite(lo)):
            continue
        for s in out:
            dy = (la - s["lat"]) * 111_320.0
            dx = (lo - s["lon"]) * 111_320.0 * np.cos(np.radians(s["lat"]))
            if np.hypot(dx, dy) <= radius_m:
                s["count"] += 1
                if lb is not None:
                    s["laps"].add(lb)
                break
        else:
            out.append({"lat": float(la), "lon": float(lo), "count": 1, "laps": {lb} if lb is not None else set()})
    for s in out:
        s["laps"] = sorted(s["laps"])
    return sorted(out, key=lambda s: -s["count"])


def judge_session(msgs: pd.DataFrame, frames: pd.DataFrame, *, sd_frames: pd.DataFrame | None = None,
                  sd_span_ms: tuple[float, float] | None = None, windows: list[tuple[float, float]] | None = None,
                  crossings_ms: np.ndarray | None = None, pos: pd.DataFrame | None = None,
                  floor_s: float = FLOOR_S) -> dict | None:
    """세션 하나의 숫자. msgs 는 recv_s · topic · head, frames 는 recv_s · device_ts_ms · can_id."""
    d = msgs[msgs["topic"].str.endswith("/d")].sort_values("recv_s")
    if len(d) < MIN_SESSION_MSGS:
        return None
    recv, dev = d["recv_s"].to_numpy(float), device_ms_of(d["head"])
    keep = np.r_[True, np.diff(dev) > 0]        # 장치 시각이 뒤로 가면 순서가 뒤집힌 메시지다
    recv, dev = recv[keep], dev[keep]
    floor = fit_floor(recv, dev)
    loop_ms = float(np.median(np.diff(dev)))
    intv_ms = np.floor(loop_ms / 10.0) * 10.0   # 보내기 주기 = 설정값 + 보내는 데 든 시간. 거르는 건 설정값으로 한다

    r: dict = {"loop_ms": loop_ms, "intv_ms": intv_ms, "d_msgs": int(len(d)), "floor_slope_ppm": floor.slope * 1e6}

    # 전달 시간 (보낸 순간 → 서버)
    transit = recv - floor.offset_s(recv) - dev / 1000.0 + floor_s
    m = in_windows(dev, windows)
    if m.any():
        r["transit_p50_s"], r["transit_p95_s"] = map(float, np.percentile(transit[m], [50, 95]))

    # 값의 나이 (CAN 프레임이 찍힌 순간 → 서버). 판정은 이 값으로 한다
    if len(frames):
        fr = frames["recv_s"].to_numpy(float)
        ft = frames["device_ts_ms"].to_numpy(float)
        age = fr - floor.offset_s(fr) - ft / 1000.0 + floor_s
        fm = in_windows(ft, windows)
        if fm.any():
            r["age_n"] = int(fm.sum())
            r["age_p50_s"], r["age_p95_s"], r["age_max_s"] = map(float, np.percentile(age[fm], [50, 95, 100]))

    # 끊김
    span = sd_span_ms if sd_span_ms is not None else (float(dev[0]), float(dev[-1]))
    if sd_span_ms is not None:
        r["connect_s"] = max(0.0, (float(dev[0]) - sd_span_ms[0]) / 1000.0)
    # 장치가 주기적으로 보내는 토픽만 본다. 전원을 끈 뒤 브로커가 내는 last will 이나
    # 다시 접속할 때의 boot · ver 를 세면 없던 끊김이 생기거나 있던 끊김이 가려진다
    live = msgs[msgs["topic"].str.endswith(("/d", "/d/can"))]
    out = outages(live["recv_s"].to_numpy(float), floor, sd_span_ms)
    if windows is not None and len(out):
        out = out[[any(a < x.end_ms and x.start_ms < b for a, b in windows) for x in out.itertuples()]]
    r["outages"] = int(len(out))
    r["outage_longest_s"] = float(out["duration_s"].max()) if len(out) else 0.0
    r["outage_total_s"] = float(out["duration_s"].sum())
    r["watched_s"] = watched_s(windows, span)

    # 바퀴마다, 자리마다
    lap_of = [None] * len(out)
    if crossings_ms is not None and len(crossings_ms) >= 2:
        r["laps"] = int(len(crossings_ms) - 1)
        r["outages_per_lap_max"] = int(per_lap(out, crossings_ms).max())
        lap_of = [int(np.searchsorted(crossings_ms, s, side="right")) for s in out["start_ms"]]
    if pos is not None and len(pos) and len(out):
        p = pos.sort_values("device_ts_ms")
        k = np.clip(np.searchsorted(p["device_ts_ms"].to_numpy(), out["start_ms"].to_numpy()), 0, len(p) - 1)
        r["spots"] = spots(p["lat"].to_numpy()[k], p["lon"].to_numpy()[k], lap_of)

    # 손실 (SD 에 있는 프레임에 펌웨어 규칙을 돌린 것 대비)
    if sd_frames is not None and len(sd_frames):
        want = throttle(sd_frames["device_ts_ms"].to_numpy(float), sd_frames["can_id"].to_numpy(float), intv_ms)
        want = want[in_windows(want["device_ts_ms"].to_numpy(float), windows)]
        got = frames[in_windows(frames["device_ts_ms"].to_numpy(float), windows)]
        r["expected_frames"], r["received_frames"] = int(len(want)), int(len(got))
        if len(want):
            r["delivered_ratio"] = len(got) / len(want)
        if len(got):
            sd_keys = set(zip(sd_frames["device_ts_ms"].astype(np.int64), sd_frames["can_id"].astype(np.int64)))
            r["in_sd_ratio"] = float(np.mean([k in sd_keys for k in zip(got["device_ts_ms"].astype(np.int64),
                                                                       got["can_id"].astype(np.int64))]))
    return r


def verdict(r: dict) -> dict:
    """합격 기준에 대기. 값이 없으면 None (판정 못 함)."""
    v: dict = {"age_median": None, "age_p95": None, "outages": None}
    if "age_p50_s" in r:
        v["age_median"] = r["age_p50_s"] <= AGE_MEDIAN_S
        v["age_p95"] = r["age_p95_s"] <= AGE_P95_S
    if "outages_per_lap_max" in r:
        v["outages"] = r["outages_per_lap_max"] <= OUTAGES_PER_LAP
    return v


# ---------------------------------------------------------------- 파일 → 표

def find_sd(norm: Path, boot_epoch: float) -> Path | None:
    """같은 부팅의 SD 레코딩 폴더를 찾는다 (BOOT 시각이 같다)."""
    for meta in sorted(norm.glob("*/meta.json")):
        m = json.loads(meta.read_text(encoding="utf-8"))
        if "Monolith" not in str(m.get("source", {}).get("logger")):
            continue
        if abs(pd.Timestamp(m["time"]["t_abs_start"]).timestamp() - boot_epoch) < 1.5:
            if "boot_ts_ms" not in m["time"]:
                print(f"{meta.parent.name}: 옛 변환본이라 SD 와 맞대지 못합니다 (lefdata monolith 로 다시 변환)", file=sys.stderr)
                return None
            return meta.parent
    return None


def sd_tables(rec: Path) -> dict:
    m = json.loads((rec / "meta.json").read_text(encoding="utf-8"))
    boot = float(m["time"]["boot_ts_ms"])
    f = pd.read_parquet(rec / "frames.parquet", columns=["t_rel_ms", "kind", "can_id"])
    c = f[f["kind"] == "CAN"]
    g = pd.read_parquet(rec / "gps.parquet")
    g = g[g["valid"] & g["fix"]]
    return {
        "recording": str(m["recording_id"]),
        "boot_ms": boot,
        "span": (float(f["t_rel_ms"].min()) + boot, float(f["t_rel_ms"].max()) + boot),
        "frames": pd.DataFrame({"device_ts_ms": c["t_rel_ms"].to_numpy(float) + boot,
                                "can_id": c["can_id"].to_numpy(float)}),
        "pos": pd.DataFrame({"device_ts_ms": g["t_rel_ms"].to_numpy(float) + boot, "lat": g["lat"].to_numpy(float),
                             "lon": g["lon"].to_numpy(float), "course": g["course_deg"].to_numpy(float)}),
    }


def crossings(pos: pd.DataFrame, venue: dict) -> np.ndarray:
    """계측선을 넘은 장치 시각 (ms). 랩 나누기는 엔진 것을 쓴다."""
    from lefdata.laps import Gate, gate_crossings, to_local_m
    o, g = venue["origin"], venue["gate"]
    x, y = to_local_m(pos["lat"].to_numpy(), pos["lon"].to_numpy(), o["lat0"], o["lon0"])
    gate = Gate(g["x"], g["y"], g["heading_deg"])
    t = pos["device_ts_ms"].to_numpy() / 1000.0
    return np.asarray(gate_crossings(t, x, y, pos["course"].to_numpy(), gate)) * 1000.0


def judge_dir(d: Path, norm: Path | None, runs_csv: Path | None, venue_json: Path | None, floor_s: float) -> dict:
    messages = pd.read_csv(d / "messages.csv", dtype={"head": str}).fillna({"head": ""})
    can = pd.read_csv(d / "can_frames.csv")
    gps = pd.read_csv(d / "gps.csv")
    sessions = pd.read_csv(d / "sessions.csv")
    messages["session_id"] = assign_sessions(messages, sessions)
    runs = pd.read_csv(runs_csv) if runs_csv else None
    venue = json.loads(venue_json.read_text(encoding="utf-8")) if venue_json else None

    out: dict = {"floor_s_assumed": floor_s, "sessions": []}
    for s in sessions.itertuples():
        msgs = messages[messages["session_id"] == s.id]
        frames = can[can["session_id"] == s.id]
        rec = find_sd(norm, s.boot_epoch) if norm and pd.notna(s.boot_epoch) else None
        sd = sd_tables(rec) if rec is not None else None
        windows = cross = None
        if sd is not None:
            if runs is not None:
                w = runs[runs["recording_id"] == sd["recording"]]
                windows = [(a * 1000 + sd["boot_ms"], b * 1000 + sd["boot_ms"])
                           for a, b in zip(w["t_start_s"], w["t_end_s"])] or None
            if venue is not None and len(sd["pos"]):
                cross = crossings(sd["pos"], venue)
            pos = sd["pos"]
        else:
            g = gps[gps["session_id"] == s.id]
            pos = pd.DataFrame({"device_ts_ms": g["device_ts_ms"].to_numpy(float),
                                "lat": ddmm_to_deg(g["latitude"]), "lon": ddmm_to_deg(g["longitude"])})
        r = judge_session(msgs, frames, sd_frames=sd["frames"] if sd else None, sd_span_ms=sd["span"] if sd else None,
                          windows=windows, crossings_ms=cross, pos=pos, floor_s=floor_s)
        if r is None:
            continue
        r.update(session=int(s.id), recording=sd["recording"] if sd else None, driving_only=windows is not None)
        r["verdict"] = verdict(r)
        out["sessions"].append(r)
    return out


# ---------------------------------------------------------------- 출력

def mark(x) -> str:
    return {True: "합격", False: "불합격", None: "판정 못 함"}[x]


def report(res: dict) -> str:
    L = [f"바닥 가정 {res['floor_s_assumed']:.2f} s (가장 빠른 전달 시간은 못 잰다)"]
    for r in res["sessions"]:
        L += ["", f"세션 {r['session']}  {r['recording'] or '(SD 없음)'}  "
                  f"{'주행 구간만' if r['driving_only'] else '전체'} {r['watched_s']:.0f} s · 보내기 주기 {r['loop_ms']:.0f} ms"
                  + (f" · 부팅 뒤 접속까지 {r['connect_s']:.0f} s" if "connect_s" in r else "")]
        v = r["verdict"]
        if "age_p50_s" in r:
            L.append(f"  값의 나이   중앙 {r['age_p50_s']:.2f} s (≤ {AGE_MEDIAN_S:g}) {mark(v['age_median'])} · "
                     f"95 % {r['age_p95_s']:.2f} s (≤ {AGE_P95_S:g}) {mark(v['age_p95'])} · "
                     f"최대 {r['age_max_s']:.1f} s · {r['age_n']}프레임")
        else:
            L.append("  값의 나이   판정 못 함 (구간 안 CAN 프레임 없음)")
        if "transit_p50_s" in r:
            L.append(f"  전달 시간   중앙 {r['transit_p50_s']:.2f} s · 95 % {r['transit_p95_s']:.2f} s (보낸 순간 → 서버, 묶음 대기 뺌)")
        L.append(f"  끊김 > {OUTAGE_S:g} s  {r['outages']}번 · 가장 긴 {r['outage_longest_s']:.1f} s · 합 {r['outage_total_s']:.1f} s")
        if "laps" in r:
            L.append(f"  랩당 끊김   최대 {r['outages_per_lap_max']}번 / {r['laps']}랩 (≤ {OUTAGES_PER_LAP}) {mark(v['outages'])}")
        else:
            L.append("  랩당 끊김   판정 못 함 (계측선을 두 번 넘은 랩 없음)")
        for s in r.get("spots", []):
            lap = f" · 랩 {s['laps']}" if s["laps"] else ""
            L.append(f"  끊긴 자리   {s['lat']:.5f}, {s['lon']:.5f} — {s['count']}번{lap}")
        if "delivered_ratio" in r:
            L.append(f"  전달률      {r['received_frames']} / {r['expected_frames']} = {r['delivered_ratio'] * 100:.1f} % "
                     f"(SD 에 펌웨어 규칙을 돌린 수 대비) · 받은 프레임 중 SD 에 있는 것 "
                     f"{r.get('in_sd_ratio', float('nan')) * 100:.1f} %")
    return "\n".join(L)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    f = sub.add_parser("fetch")
    f.add_argument("--date", required=True)
    f.add_argument("--out", type=Path, required=True)
    f.add_argument("--target", default=os.environ.get("T8_SSH_TARGET"), help="ssh 대상 (환경 변수 T8_SSH_TARGET)")
    f.add_argument("--key", default=os.environ.get("T8_SSH_KEY"), help="ssh 키 경로 (환경 변수 T8_SSH_KEY)")
    j = sub.add_parser("judge")
    j.add_argument("--dir", type=Path, required=True)
    j.add_argument("--sd", type=Path, help="그 이벤트의 normalized 폴더")
    j.add_argument("--runs", type=Path, help="derived/runs.csv — 주면 주행 구간만 판정")
    j.add_argument("--venue", type=Path, help="_venues/<이름>.json — 주면 랩당 끊김 판정")
    j.add_argument("--floor", type=float, default=FLOOR_S)
    a = ap.parse_args(argv)
    if a.cmd == "fetch":
        if not a.target or not a.key:
            ap.error("--target · --key (또는 T8_SSH_TARGET · T8_SSH_KEY) 가 필요합니다")
        fetch(a.date, a.out, a.target, a.key)
        return 0
    res = judge_dir(a.dir, a.sd, a.runs, a.venue, a.floor)
    print(report(res))
    (a.dir / "t8_result.json").write_text(json.dumps(res, ensure_ascii=False, indent=1), encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
