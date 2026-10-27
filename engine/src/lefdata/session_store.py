"""세션 저장소 — 분석 엔진(`lefdata.api`)이 읽는 것을 미리 만들어 둔다 (팀 계층).

    lefdata sessions [--event E] [--root R]
        → <저장소>/<세션 ID>/session.json      요약 · 데이터 현황 · 블록 · 없는 데이터 · 채널 목록 · 품질 경고 · 파일 표
                            laps.parquet       랩 표
                            track.parquet      궤적 (t_s · x_m · y_m · dist_m)
                            series/*.parquet   채널 값 — 원래 주기 그대로 (t_s + 채널 열)
                            files/*.csv        세션 구간만 자른 표 (내려받기용)
        → <저장소>/index.json                  세션 목록
        → <저장소>/_frames/<frame>.json         기준선 · 구간 경계 (frame_id 마다 하나, 한 번 정하면 고정 — --reframe 로만 다시)

부를 때마다 원본 표를 다시 읽고 장비 시계를 다시 맞추면 느리다 (세션 하나에 수 초). 그래서 한 번 만들어 두고 API 는 이 폴더만 읽는다.
사이트에 필요한 데이터는 이 폴더와, 내려받기용 원본 파일(데이터 폴더 기준 상대 경로로 적어 둔다)뿐이다.

세션을 채우는 것은 `catalog_build.build_event` 다 (차량 · 폴더를 아는 코드는 거기 한 곳). 여기는 그 `Session` 을 파일로 옮긴다.
저장소 위치 기본값은 `07_데이터/_sessions` — 환경 변수 `LEF_SESSION_ROOT` 로 바꾼다.

엔진이 계산해 더하는 블록: `drive` (주행 상태 추정 — 배터리 전력 + GPS 속도, `lefdata.drive`). 페달 신호가 생기면 그쪽으로 바꾼다.
frame 은 같은 `frame_id` 의 세션 전부에서 기준 랩을 하나 고른 것이다 (`lefdata.sectors`). 세션이 더 들어와도 바뀌지 않아야
구간 시간 · 시간 차가 날마다 달라지지 않는다. frame_id 가 없는 세션은 그 세션만의 frame 을 갖는다 (다른 세션과 비교하지 않는다).
"""

from __future__ import annotations

import datetime as dt
import json
import os
import re
from pathlib import Path

import numpy as np
import pandas as pd

from . import catalog_build, drive, paths, sectors, series as ser
from .catalog import HAVE, NONE, SEEK, WARN, Channel, DataBlock, FileRef, Session

STORE_FORMAT = 1
DEFAULT_ROOT = Path(os.environ.get("LEF_SESSION_ROOT", paths.DATA_ROOT / "_sessions"))
STATUS_CODE = {HAVE: "have", WARN: "warn", SEEK: "seek", NONE: "none"}
DRIVER_LABEL = re.compile(r"^드라이버 [A-Z]$")          # 실명이 저장소에 들어가지 않게 — 이 모양만 받는다
FRAMES = "_frames"
FRAME_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")
N_SECTORS = 3
CONTENT_TYPES = {".csv": "text/csv", ".log": "application/octet-stream", ".mp4": "video/mp4", ".lrv": "application/octet-stream",
                 ".parquet": "application/vnd.apache.parquet", ".json": "application/json", ".zip": "application/zip"}


def _iso(ts: pd.Timestamp) -> str:
    return ts.round("ms").isoformat(timespec="milliseconds")


def _num(v, digits: int = 3):
    if v is None:
        return None
    v = float(v)
    return None if not np.isfinite(v) else round(v, digits)


def _rel(path: Path, base: Path) -> str:
    return os.path.relpath(Path(path).resolve(), base.resolve()).replace("\\", "/")


def summary(s: Session, blocks: list[DataBlock] | None = None) -> dict:
    """세션 목록의 한 줄 (계약 §6-1). blocks 를 주면 그것으로 (엔진이 더한 블록 포함)."""
    st = s.stats
    status = [{"block": b.key, "title": b.title, "status": STATUS_CODE[b.status], "detail": b.device}
              for b in (s.blocks if blocks is None else blocks)]
    status += [{"block": m.block or None, "title": m.data, "status": STATUS_CODE[m.status], "detail": m.reason} for m in s.missing]
    blocks = {code: [d["block"] for d in status if d["status"] == code and d["block"]] for code in ("have", "warn", "seek", "none")}
    return {"sid": s.sid, "title": s.title, "name": s.name, "car": s.car, "event_id": s.event_id, "event_kind": s.event_kind,
            "event_title": s.event_title, "discipline": s.discipline, "place": s.place, "driver_label": s.driver,
            "start": _iso(s.start), "duration_s": _num(s.duration_s, 2), "distance_km": st.get("distance_km"), "laps": st.get("laps_n"),
            "best_lap_s": st.get("best_lap_s"), "stats": {k: v for k, v in st.items() if k not in ("distance_km", "laps_n", "best_lap_s")},
            "frame_id": s.frame_id or None, "blocks": blocks, "data_status": status}


def _channel_specs(b: DataBlock) -> list[tuple[Channel, dict]]:
    out = []
    for c in (b.series or b.channels):
        t = np.asarray(c.t_s, float)
        dt_med = ser.median_dt(t)
        out.append((c, {"channel_id": f"{b.key}.{c.key}", "block": b.key, "label": c.label, "unit": c.unit, "sign": c.sign,
                        "rate_hz": _num(1 / dt_med, 1) if np.isfinite(dt_med) else None, "samples": int(len(t)),
                        "source": {"recording_id": c.source or None, "device": b.device},
                        "derived": c.derived, "derivation": c.derivation,
                        "t_range": [_num(t[0], 3), _num(t[-1], 3)] if len(t) else [None, None],
                        "cautions": list(c.cautions), "digits": c.digits,
                        **({"levels": [dict(lv) for lv in c.levels]} if c.levels else {})}))
    return out


def _write_series(b: DataBlock, specs: list[tuple[Channel, dict]], out: Path) -> None:
    """같은 시간축을 쓰는 채널끼리 한 파일에 (t_s + 채널 열). 원래 주기 그대로."""
    groups: list[list[tuple[Channel, dict]]] = []
    for c, spec in specs:
        for g in groups:
            if len(g[0][0].t_s) == len(c.t_s) and np.array_equal(g[0][0].t_s, c.t_s):
                g.append((c, spec))
                break
        else:
            groups.append([(c, spec)])
    (out / "series").mkdir(parents=True, exist_ok=True)
    for k, g in enumerate(groups):
        name = f"{b.key}.parquet" if len(groups) == 1 else f"{b.key}_{k + 1}.parquet"
        frame = pd.DataFrame({"t_s": np.asarray(g[0][0].t_s, float)})
        for c, spec in g:
            frame[c.key] = np.asarray(c.v, float).astype("float32")
            spec["_file"], spec["_col"] = f"series/{name}", c.key
        frame.to_parquet(out / "series" / name, index=False)


def _file_entry(sid: str, kind: str, n: int, f: FileRef, data_root: Path) -> tuple[str | None, dict]:
    """원본 파일 하나 → (file_id, 목록에 보일 정보). 경로를 모르면(폴더 · 있는 곳만 아는 것) file_id 는 None."""
    show = {"label": f.label, "name": f.name, "size_bytes": int(f.size_bytes) or None, "note": f.note, "where": f.where}
    if f.path is None or not Path(f.path).is_file():
        return None, show
    return f"{sid}/{kind}/{n}", {**show, "_base": "data", "_rel": _rel(f.path, data_root),
                                  "content_type": CONTENT_TYPES.get(Path(f.path).suffix.lower(), "application/octet-stream")}


def _find(s: Session, block: str, key: str) -> Channel | None:
    b = next((b for b in s.blocks if b.key == block), None)
    return None if b is None else next((c for c in (b.series or b.channels) if c.key == key), None)


def drive_block(s: Session, dt_s: float = 0.1) -> DataBlock | None:
    """주행 상태 추정 블록 — 배터리 전력(battery.power_kw)과 GPS 속도(gps.speed_kmh)가 둘 다 있을 때만."""
    power, speed = _find(s, "battery", "power_kw"), _find(s, "gps", "speed_kmh")
    if power is None or speed is None:
        return None
    t, st = drive.estimate(power.t_s, power.v, speed.t_s, speed.v, dt_s)
    if not np.isfinite(st).any():
        return None
    sh = drive.shares(st)
    ch = Channel("state_est", "주행 상태 (추정)", "", t, st, sign="+1 가속 · 0 타력 · −1 제동", derived=True,
                 derivation=(f"배터리 전력 > {drive.POWER_ON_KW:g} kW 이면 가속, 아니고 GPS 속도가 {-drive.BRAKE_G:g} g 보다 빠르게 줄면 제동, "
                             f"나머지 타력 (전력 {drive.POWER_SMOOTH_S:g} s · 속도 {drive.SPEED_SMOOTH_S:g} s 이동평균)"),
                 cautions=["추정 — 가속페달 · 브레이크 압력 신호가 아니다",
                           "가속페달 · 브레이크를 같이 밟은 순간은 가속으로, 아주 가벼운 제동은 타력으로 나온다",
                           "상태가 바뀌는 순간은 0.1 ~ 0.2 s 흐리다 (전력 · 속도를 잰 장비의 시각 차)",
                           f"서 있을 때({drive.STOP_KMH:g} km/h 미만)는 값이 없다",
                           "그래프에서 칸으로 줄인 값(평균)은 그 칸의 가속 비율 − 제동 비율이다"],
                 digits=0, levels=[dict(lv) for lv in drive.LEVELS])
    return DataBlock("drive", "주행 상태 (추정)", WARN, f"엔진 계산 — 배터리 전력 + GPS 속도 변화율 · {1 / dt_s:g} Hz",
                     facts=[f"달린 시간 중 가속 {sh[1] * 100:.0f} % · 타력 {sh[0] * 100:.0f} % · 제동 {sh[-1] * 100:.0f} %"],
                     series=[ch], channels=[ch], time_keys=["state_est"], map_key="state_est",
                     cautions=["가속페달 · 브레이크 압력이 기록되지 않아 전력 · 속도로 추정한 값이다. 센서가 붙으면 페달 신호로 바꾼다"])


def write_session(s: Session, root: Path, data_root: Path | None = None) -> dict:
    """세션 하나 → <root>/<sid>/. 돌려주는 것은 세션 목록의 한 줄."""
    data_root = Path(data_root or paths.DATA_ROOT)
    if s.driver and not DRIVER_LABEL.match(s.driver):
        raise ValueError(f"{s.sid}: 드라이버는 '드라이버 A' 같은 표시만 쓴다 (실명 금지) — {s.driver!r}")
    out = Path(root) / s.sid
    (out / "files").mkdir(parents=True, exist_ok=True)
    files: dict[str, dict] = {}
    channels: list[dict] = []
    blocks: list[dict] = []
    findings: list[dict] = []
    lap_columns: dict[str, dict] = {}
    n_raw = 0
    all_blocks = list(s.blocks) + [b for b in (drive_block(s),) if b is not None]
    for b in all_blocks:
        specs = _channel_specs(b)
        _write_series(b, specs, out)
        channels += [spec for _, spec in specs]
        raw = []
        for f in b.raw:
            n_raw += 1
            fid, show = _file_entry(s.sid, "raw", n_raw, f, data_root)
            if fid:
                files[fid] = show
            raw.append({"file_id": fid, **{k: v for k, v in show.items() if not k.startswith("_")}})
        exports = []
        for e in b.exports:
            path = out / "files" / e.name
            e.table.to_csv(path, index=False, encoding="utf-8-sig", lineterminator="\n")
            fid = f"{s.sid}/files/{e.name}"
            files[fid] = {"label": e.label, "name": e.name, "size_bytes": path.stat().st_size, "note": e.note,
                          "_base": "store", "_rel": f"{s.sid}/files/{e.name}", "content_type": "text/csv"}
            exports.append({"file_id": fid, "name": e.name, "label": e.label, "note": e.note, "rows": int(len(e.table)),
                            "size_bytes": path.stat().st_size})
        keys = {c.key for c, _ in specs}
        views = {"time": [f"{b.key}.{k}" for k in b.time_keys if k in keys], "distance": [f"{b.key}.{k}" for k in b.trace_keys if k in keys],
                 "map": f"{b.key}.{b.map_key}" if b.map_key in keys else None, "gg": b.gg is not None}
        blocks.append({"block": b.key, "title": b.title, "status": STATUS_CODE[b.status], "device": b.device, "facts": list(b.facts),
                       "checks": list(b.checks), "cautions": list(b.cautions), "raw": raw,
                       "outputs": [{"label": f.label, "name": f.name, "note": f.note, "where": f.where} for f in b.outputs],
                       "exports": exports, "code": [{"key": c.key, "title": c.title, "role": c.role} for c in b.code],
                       "channels": [spec["channel_id"] for _, spec in specs], "default_views": views})
        findings += [{"block": b.key, **f} for f in b.findings]
        for c in b.lap_columns:
            lap_columns.setdefault(c.key, {"key": c.key, "label": c.label, "unit": c.unit, "digits": c.digits, "block": b.key, "chart": c.chart})

    laps = s.laps if s.laps is not None else pd.DataFrame(columns=["lap", "t_start_s", "t_end_s", "lap_time_s"])
    laps.to_parquet(out / "laps.parquet", index=False)
    if s.track is not None:
        s.track[["t_s", "x_m", "y_m", "dist_m"]].to_parquet(out / "track.parquet", index=False)
    best = s.best_lap
    row = summary(s, all_blocks)
    doc = {"store_format": STORE_FORMAT, "summary": row, "notes": list(s.notes), "blocks": blocks,
           "missing": [{"block": m.block or None, "data": m.data, "status": STATUS_CODE[m.status], "reason": m.reason, "ask": m.ask}
                       for m in s.missing],
           "channels": channels, "lap_columns": list(lap_columns.values()),
           "best_lap": int(best["lap"]) if best is not None else None,
           "track": {"has_track": s.track is not None, "gate": s.gate},
           "findings": findings, "files": files,
           "made": {"built_at": dt.datetime.now().astimezone().isoformat(timespec="seconds"), "commit": catalog_build.code_version(),
                    "command": f"lefdata sessions --event {s.event_id}"}}
    (out / "session.json").write_text(json.dumps(doc, ensure_ascii=False, indent=1, allow_nan=False), encoding="utf-8", newline="\n")
    return row


def write_index(root: Path) -> list[dict]:
    """저장소의 세션 전부 → index.json (시작 시각 순)."""
    root = Path(root)
    rows = [json.loads(p.read_text(encoding="utf-8"))["summary"] for p in sorted(root.glob("*/session.json"))]
    rows.sort(key=lambda r: r["start"])
    (root / "index.json").write_text(json.dumps({"store_format": STORE_FORMAT, "sessions": rows}, ensure_ascii=False, indent=1,
                                                allow_nan=False), encoding="utf-8", newline="\n")
    return rows


def frame_key(row: dict) -> str:
    """세션 요약 한 줄 → 그 세션의 frame 이름 (frame_id, 없으면 세션 ID — 그 세션만의 frame)."""
    return row.get("frame_id") or row["sid"]


def frame_path(root: Path, key: str) -> Path:
    name = key.replace("/", "__")
    if not FRAME_NAME.match(name) or ".." in name:
        raise ValueError(f"frame 이름이 올바르지 않음: {key!r}")
    return Path(root) / FRAMES / f"{name}.json"


def _stored_laps(root: Path, sid: str) -> list[dict]:
    """저장소의 세션 하나 → 랩마다 {key, length_m, time_s, x, y} (계측선에서 계측선까지의 점)."""
    tp = root / sid / "track.parquet"
    if not tp.exists():
        return []
    tr = pd.read_parquet(tp)
    t, x, y = (tr[c].to_numpy(float) for c in ("t_s", "x_m", "y_m"))
    out = []
    for _, r in pd.read_parquet(root / sid / "laps.parquet").iterrows():
        ta, tb = float(r["t_start_s"]), float(r["t_end_s"])
        _, px, py = sectors.lap_points(t, x, y, ta, tb)
        out.append({"key": (sid, int(r["lap"])), "length_m": float(sectors.path_length(px, py)[-1]), "time_s": tb - ta, "x": px, "y": py})
    return out


def write_frames(root: Path, reframe: bool = False, keys: set[str] | None = None) -> list[str]:
    """저장소의 frame 마다 기준선 · 구간 경계를 정해 `_frames/` 에 쓴다 → 새로 쓴 frame 이름들.

    이미 있는 frame 은 그대로 둔다 (reframe = True 면 다시 정한다 — keys 를 주면 그 frame 만). 기준 랩은 그 frame 의 세션 전부의 랩 중에서
    `sectors.pick_reference` 로 고르고, 구간은 기준 길이를 N_SECTORS 등분한다 (source = auto).
    """
    root = Path(root)
    rows = [json.loads(p.read_text(encoding="utf-8"))["summary"] for p in sorted(root.glob("*/session.json"))]
    groups: dict[str, list[dict]] = {}
    for r in rows:
        groups.setdefault(frame_key(r), []).append(r)
    written = []
    for key, members in groups.items():
        path = frame_path(root, key)
        if path.exists() and not (reframe and (keys is None or key in keys)):
            continue
        laps = [lap for r in members for lap in _stored_laps(root, r["sid"])]
        if not laps:
            continue
        pick = sectors.pick_reference(laps)
        ref_lap = next(lap for lap in laps if lap["key"] == pick)
        lx, ly = np.round(ref_lap["x"], 2), np.round(ref_lap["y"], 2)
        ref = sectors.Reference(lx, ly)
        edges = sectors.equal_edges(ref.length, N_SECTORS)
        bx, by, bh = ref.at(edges[:-1])
        doc = {"frame_key": key, "frame_id": members[0].get("frame_id") or None, "method": "projection", "source": "auto",
               "window_m": sectors.WINDOW_M, "ref_len_m": round(ref.length, 2),
               "reference": {"sid": pick[0], "lap": pick[1], "lap_time_s": round(ref_lap["time_s"], 3), "laps_considered": len(laps),
                             "rule": f"길이가 중앙값의 {sectors.LENGTH_TOL * 100:g} % 안인 랩 중 가장 빠른 랩"},
               "sectors": [{"sector": k + 1, "ref_m0": round(float(edges[k]), 2), "ref_m1": round(float(edges[k + 1]), 2),
                            "x_m": round(float(bx[k]), 2), "y_m": round(float(by[k]), 2), "heading_deg": round(float(bh[k]), 1)}
                           for k in range(N_SECTORS)],
               "line": {"x_m": [float(v) for v in lx], "y_m": [float(v) for v in ly]},
               "fixed_at": dt.datetime.now().astimezone().isoformat(timespec="seconds"), "commit": catalog_build.code_version()}
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(doc, ensure_ascii=False, allow_nan=False), encoding="utf-8", newline="\n")
        written.append(key)
    return written


def build_event(event_dir: Path, root: Path | None = None, data_root: Path | None = None, reframe: bool = False) -> list[dict]:
    """이벤트의 세션을 전부 만들어 저장소에 쓴다 → 세션 목록의 줄들. index.json · frame 도 쓴다 (있는 frame 은 그대로)."""
    root = Path(root or DEFAULT_ROOT)
    rows = [write_session(s, root, data_root) for s in catalog_build.build_event(event_dir)]
    write_index(root)
    write_frames(root, reframe, {frame_key(r) for r in rows})
    return rows
