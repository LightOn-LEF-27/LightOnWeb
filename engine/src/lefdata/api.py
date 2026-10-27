"""분석 엔진 — 데이터 뷰어 사이트가 부르는 함수 (팀 계층).

약속(계약)은 인터페이스 문서(`분석엔진_인터페이스.md`)에 있다. 함수 이름 · 인자 · 돌려주는 모양을 바꿀 때는 그 문서를 먼저 고친다.

    engine_info()                               F1  계약 · 엔진 버전
    list_sessions(car, event_id, discipline,    F2  세션 목록 (최신순, limit · offset 으로 나눠 받는다)
                  limit, offset)
    get_session(sid)                            F3  세션 화면
    list_channels(sid)                          F4  채널 목록
    get_series(sid, channel_ids, x, ...)        F5  그래프 값 (줄여도 최소 · 최대를 남긴다)
    get_laps(sid)                               F6  랩 표
    get_track(sid, lap, color)                  F7  코스 (color = 주행선 점마다 그 채널 값)
    get_quality(sid)                            F8  위치가 있는 품질 경고
    detect · ingest · get_job                   F9 ~ F11  파일 올리기 (아직 — E4)
    open_file(file_id)                          F12 내려받을 파일

- 돌려주는 값은 전부 JSON 으로 바꿀 수 있다 (dict · list · 숫자 · 문자열 · None). NaN 은 None 이다 — 화면은 거기서 선을 끊는다
- 잘못 부르면 `EngineError` 를 던진다. `.to_json()` 이 계약 §8 의 오류 모양이다
- 엔진은 세션 저장소(`session_store`, 기본 `07_데이터/_sessions`)만 읽는다. 저장소는 `lefdata sessions` 로 만든다
- 계산은 전부 여기서 한다. 사이트는 받은 값을 그리기만 한다 (단위 · 부호 · 랩 · 시간축은 엔진이 정한 것 하나)
"""

from __future__ import annotations

import json
import re
from functools import lru_cache
from pathlib import Path

import numpy as np
import pandas as pd

from . import __version__, paths, series as ser, session_store as store

CONTRACT_VERSION = "0.4"
MAX_POINTS = 20_000
MAX_CHANNELS = 16
MAX_SMOOTH_S = 10.0
SID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")
GAP_FACTOR = 2.0            # 기록 간격 중앙값의 이 배보다 벌어진 곳은 잇지 않는다 (에너지미터의 '빠진 표본' 과 같은 기준)
FORMATS = [  # 계약 §7 — E4 에서 판별 · 변환을 붙인다
    {"kind": "monolith_log", "label": "Monolith V2 .log (25Evo)", "status": "planned"},
    {"kind": "fsk_energymeter_log", "label": "주최측 에너지미터 .log", "status": "planned"},
    {"kind": "lef26_logger_csv", "label": "LEF-26 로거 CSV", "status": "planned"},
    {"kind": "evo_stm32_csv", "label": "25Evo STM32 백업 로거 CSV", "status": "planned"},
    {"kind": "orion_csv", "label": "Orion BMS Utility CSV", "status": "planned"},
    {"kind": "gopro_mp4", "label": "GoPro 영상 (.MP4 · .LRV)", "status": "planned"},
    {"kind": "mf4", "label": "CANedge .MF4 + DBC (LEF-27)", "status": "not_started"},
]


class EngineError(Exception):
    """계약 §8 의 오류. code 는 정해진 문자열, detail 은 JSON 으로 바꿀 수 있는 값."""

    def __init__(self, code: str, message: str, detail=None):
        super().__init__(message)
        self.code, self.message, self.detail = code, message, detail

    def to_json(self) -> dict:
        return {"error": {"code": self.code, "message": self.message, "detail": self.detail}}


# ---------------------------------------------------------------- 저장소 읽기

_conf = {"root": None, "data_root": None}


def configure(store_root: Path | str | None = None, data_root: Path | str | None = None) -> None:
    """읽을 세션 저장소 · 데이터 폴더를 바꾼다 (테스트 · 사이트 서버용). None 이면 기본값."""
    _conf["root"] = Path(store_root) if store_root else None
    _conf["data_root"] = Path(data_root) if data_root else None
    _load_json.cache_clear()
    _load_frame.cache_clear()


def _root() -> Path:
    return _conf["root"] or store.DEFAULT_ROOT


def _data_root() -> Path:
    return _conf["data_root"] or paths.DATA_ROOT


@lru_cache(maxsize=64)
def _load_json(path: str, mtime: float) -> dict:
    return json.loads(Path(path).read_text(encoding="utf-8"))


@lru_cache(maxsize=32)
def _load_frame(path: str, mtime: float) -> pd.DataFrame:
    return pd.read_parquet(path)


def _json(path: Path) -> dict:
    return _load_json(str(path), path.stat().st_mtime)


def _frame(path: Path) -> pd.DataFrame:
    return _load_frame(str(path), path.stat().st_mtime)


def _session(sid) -> dict:
    if not isinstance(sid, str) or not SID.match(sid) or ".." in sid:
        raise EngineError("unknown_session", f"세션 ID 가 올바르지 않음: {sid!r}")
    path = _root() / sid / "session.json"
    if not path.exists():
        raise EngineError("unknown_session", f"세션 {sid} 가 없음")
    doc = _json(path)
    if doc.get("store_format") != store.STORE_FORMAT:
        raise EngineError("internal", f"세션 {sid} 는 다른 형식의 저장소다 — lefdata sessions 로 다시 만들 것",
                          {"store_format": doc.get("store_format"), "expected": store.STORE_FORMAT})
    return doc


def _public(d: dict) -> dict:
    """저장소 안에서만 쓰는 열(_ 로 시작)을 뺀다."""
    return {k: v for k, v in d.items() if not k.startswith("_")}


def _arr(a: np.ndarray, digits: int) -> list:
    """숫자 배열 → JSON 목록 (NaN → None)."""
    a = np.round(np.asarray(a, float), digits)
    return [None if not np.isfinite(v) else float(v) for v in a]


def _num(v, digits: int = 4):
    if v is None or (isinstance(v, (float, np.floating)) and not np.isfinite(v)):
        return None
    if isinstance(v, (bool, np.bool_)):
        return bool(v)
    if isinstance(v, (int, np.integer)):
        return int(v)
    if isinstance(v, (float, np.floating)):
        return round(float(v), digits)
    return v


# ---------------------------------------------------------------- F1 ~ F4

def engine_info() -> dict:
    """F1. 계약 · 엔진 버전, 저장소 상태, 읽을 수 있는 파일 형식."""
    index = _root() / "index.json"
    rows = _json(index)["sessions"] if index.exists() else []
    return {"contract": CONTRACT_VERSION, "engine": {"name": "lefdata", "version": __version__},
            "store": {"format": store.STORE_FORMAT, "sessions": len(rows)},
            "formats": [dict(f) for f in FORMATS],
            "functions": ["engine_info", "list_sessions", "get_session", "list_channels", "get_series", "get_laps", "get_track",
                          "get_quality", "open_file"], "not_yet": ["detect", "ingest", "get_job"]}


def list_sessions(car: str | None = None, event_id: str | None = None, discipline: str | None = None, limit: int | None = None,
                  offset: int = 0) -> list[dict]:
    """F2. 세션 목록 — 시작 시각 **최신순**. 거르기는 같은 값만. limit · offset 으로 나눠 받는다 (전체 수는 engine_info 의 store.sessions)."""
    for name, v, low in (("limit", limit, 1), ("offset", offset, 0)):
        if v is not None and (isinstance(v, bool) or not isinstance(v, (int, np.integer)) or v < low):
            raise EngineError("bad_argument", f"{name} 는 {low} 이상의 정수: {v!r}")
    index = _root() / "index.json"
    rows = _json(index)["sessions"] if index.exists() else []
    hit = [dict(r) for r in reversed(rows) if (car is None or r["car"] == car) and (event_id is None or r["event_id"] == event_id)
           and (discipline is None or r["discipline"] == discipline)]
    return hit[offset:] if limit is None else hit[offset:offset + limit]


def get_session(sid: str) -> dict:
    """F3. 세션 화면에 필요한 것 — 요약 · 메모 · 블록(사실 · 확인 · 주의 · 원본 · 결과물 · 코드 · 채널) · 없는 데이터."""
    s = _session(sid)
    return {"summary": s["summary"], "notes": s["notes"], "blocks": s["blocks"], "missing": s["missing"], "best_lap": s["best_lap"],
            "made": s["made"]}


def list_channels(sid: str) -> list[dict]:
    """F4. 채널 목록 — 이름 · 단위 · 부호 · 주기 · 어느 장비 · 계산한 값인지 · 주의."""
    return [_public(c) for c in _session(sid)["channels"]]


# ---------------------------------------------------------------- F5 그래프 값

def _channel(sid: str, spec: dict, win: tuple[float, float], smooth_s: float | None) -> tuple[np.ndarray, np.ndarray]:
    """채널 하나의 (세션 시간, 값) — 원래 주기 그대로, win 둘레만. smooth_s 를 주면 이동평균 (창 밖 값도 써서 가장자리가 고르다)."""
    frame = _frame(_root() / sid / spec["_file"])
    t, v = frame["t_s"].to_numpy(float), frame[spec["_col"]].to_numpy(float)
    margin = (smooth_s or 0.0) + 1.0
    i0 = max(int(np.searchsorted(t, win[0] - margin)) - 1, 0)
    i1 = min(int(np.searchsorted(t, win[1] + margin, side="right")) + 1, len(t))
    t, v = t[i0:i1], v[i0:i1]
    return t, (ser.smooth(t, v, smooth_s) if smooth_s else v)


def _laps(sid: str) -> pd.DataFrame:
    return _frame(_root() / sid / "laps.parquet")


def _track(sid: str) -> pd.DataFrame:
    path = _root() / sid / "track.parquet"
    if not path.exists():
        raise EngineError("bad_argument", f"세션 {sid} 에는 코스(위치) 기록이 없음")
    return _frame(path)


def _lap_row(sid: str, lap) -> pd.Series:
    laps = _laps(sid)
    if isinstance(lap, bool) or not isinstance(lap, (int, np.integer)):
        raise EngineError("bad_argument", f"lap 은 랩 번호(정수)여야 함: {lap!r}")
    hit = laps[laps["lap"] == int(lap)]
    if hit.empty:
        raise EngineError("bad_argument", f"세션 {sid} 에 랩 {lap} 이 없음", {"laps": [int(x) for x in laps["lap"]]})
    return hit.iloc[0]


def get_series(sid: str, channel_ids, x: str = "time", t0: float | None = None, t1: float | None = None, lap: int | None = None,
               max_points: int = 4000, smooth_s: float | None = None, d0: float | None = None, d1: float | None = None) -> dict:
    """F5. 그래프 값. 모든 채널이 같은 x 를 쓴다.

    x = "time"     세션 시간(s). 범위는 t0 · t1 (없으면 세션 전체, lap 을 주면 그 랩)
    x = "distance" 계측선부터 달린 거리(m). lap 이 꼭 있어야 한다. 범위는 d0 · d1 (m, 없으면 그 랩 전체)
    max_points     칸 수의 상한. 원래 표본이 더 적으면 그만큼만 만든다. 칸마다 평균(values) · 최소(y_min) · 최대(y_max)
    smooth_s       주면 그 길이(s)의 이동평균을 먼저 한다 (카메라 가속도계처럼 떨리는 값). 없으면 원래 값
    """
    s = _session(sid)
    ids = [channel_ids] if isinstance(channel_ids, str) else list(channel_ids or [])
    if not ids:
        raise EngineError("bad_argument", "channel_ids 가 비어 있음")
    if len(ids) > MAX_CHANNELS or len(set(ids)) != len(ids):
        raise EngineError("bad_argument", f"채널은 서로 다른 것으로 {MAX_CHANNELS}개까지", {"channel_ids": ids})
    specs = {c["channel_id"]: c for c in s["channels"]}
    unknown = [c for c in ids if c not in specs]
    if unknown:
        raise EngineError("unknown_channel", f"없는 채널: {', '.join(map(str, unknown))}", {"unknown": unknown})
    if x not in ("time", "distance"):
        raise EngineError("bad_argument", f"x 는 'time' 또는 'distance': {x!r}")
    if isinstance(max_points, bool) or not isinstance(max_points, (int, np.integer)) or not 2 <= max_points <= MAX_POINTS:
        raise EngineError("bad_argument", f"max_points 는 2 ~ {MAX_POINTS} 의 정수: {max_points!r}")
    if smooth_s is not None and not (isinstance(smooth_s, (int, float)) and 0 <= smooth_s <= MAX_SMOOTH_S):
        raise EngineError("bad_argument", f"smooth_s 는 0 ~ {MAX_SMOOTH_S} s: {smooth_s!r}")
    if (x == "time" and (d0 is not None or d1 is not None)) or (x == "distance" and (t0 is not None or t1 is not None)):
        raise EngineError("bad_argument", "시간축은 t0 · t1, 거리축은 d0 · d1 로 범위를 준다")
    for name, v in (("t0", t0), ("t1", t1), ("d0", d0), ("d1", d1)):
        if v is not None and (isinstance(v, bool) or not isinstance(v, (int, float)) or not np.isfinite(v)):
            raise EngineError("bad_argument", f"{name} 는 숫자: {v!r}")

    duration = float(s["summary"]["duration_s"])
    row = _lap_row(sid, lap) if lap is not None else None
    if x == "time":
        lo_t, hi_t = (float(row["t_start_s"]), float(row["t_end_s"])) if row is not None else (0.0, duration)
        lo = max(float(t0), 0.0) if t0 is not None else lo_t
        hi = min(float(t1), duration) if t1 is not None else hi_t
        to_x = None
        win = (lo, hi)
        unit, digits_x = "s", 3
    else:
        if row is None:
            raise EngineError("bad_argument", "x='distance' 는 lap 이 있어야 함 (랩마다 계측선부터의 거리)")
        tr = _track(sid)
        tt, dd = tr["t_s"].to_numpy(), tr["dist_m"].to_numpy()
        ta, tb = float(row["t_start_s"]), float(row["t_end_s"])
        base = float(np.interp(ta, tt, dd))                  # 계측선을 지날 때의 누적 거리
        length = float(np.interp(tb, tt, dd)) - base
        to_x = lambda t: np.interp(t, tt, dd) - base        # noqa: E731 — 세션 시간 → 그 랩의 거리
        lo = max(float(d0), 0.0) if d0 is not None else 0.0
        hi = min(float(d1), length) if d1 is not None else length
        win = (ta, tb)
        unit, digits_x = "m", 2
    if not hi > lo:
        raise EngineError("bad_argument", f"범위가 비어 있음: {lo} ~ {hi} {unit}")

    picked = []
    for cid in ids:
        spec = specs[cid]
        t, v = _channel(sid, spec, win, smooth_s)
        if x == "distance":                                  # 그 랩 밖의 표본은 쓰지 않는다 (양옆 하나씩은 잇기용으로 둔다)
            j0 = max(int(np.searchsorted(t, win[0])) - 1, 0)
            j1 = min(int(np.searchsorted(t, win[1], side="right")) + 1, len(t))
            t, v = t[j0:j1], v[j0:j1]
        xv = t if to_x is None else to_x(t)
        picked.append((cid, spec, xv, v, ser.time_breaks(t, GAP_FACTOR)))

    native = max(int(((xv >= lo) & (xv <= hi)).sum()) for _, _, xv, _, _ in picked)
    n = int(np.clip(native, 2, max_points))
    out_series = []
    centers = None
    for cid, spec, xv, v, brk in picked:
        r = ser.reduce(xv, v, lo, hi, n, brk)
        centers = r.x
        d = int(spec.get("digits", 3))
        out_series.append({"channel_id": cid, "unit": spec["unit"], "values": _arr(r.mean, d), "y_min": _arr(r.lo, d),
                           "y_max": _arr(r.hi, d), "samples": r.samples})
    laps = _laps(sid)
    if x == "time":
        lap_marks = [{"lap": int(l), "x_start": _num(a, 3), "x_end": _num(b, 3)} for l, a, b in
                     zip(laps["lap"], laps["t_start_s"], laps["t_end_s"]) if b > lo and a < hi]
    else:
        lap_marks = [{"lap": int(row["lap"]), "x_start": 0.0, "x_end": _num(length, 2)}]
    return {"sid": sid, "x": {"kind": x, "unit": unit, "values": _arr(centers, digits_x), "lap": int(row["lap"]) if row is not None else None,
                  "frame_id": s["summary"].get("frame_id") if x == "distance" else None},
            "series": out_series, "laps": lap_marks,
            "reduced": {"from_points": native, "to_points": n, "method": "minmax", "smooth_s": smooth_s or None}}


# ---------------------------------------------------------------- F6 ~ F8

def get_laps(sid: str) -> dict:
    """F6. 랩 표 — 열 설명과 줄. best = 최속 랩."""
    s = _session(sid)
    laps = _laps(sid)
    cols = [c for c in s["lap_columns"] if c["key"] in laps.columns]
    keys = ["lap", "t_start_s", "t_end_s"] + [c["key"] for c in cols if c["key"] not in ("lap", "t_start_s", "t_end_s")]
    rows = []
    for _, r in laps.iterrows():
        row = {k: _num(r[k]) for k in keys}
        row["lap"] = int(r["lap"])
        row["best"] = row["lap"] == s["best_lap"]
        rows.append(row)
    return {"columns": cols, "rows": rows, "best_lap": s["best_lap"],
            "notes": ["랩은 계측선 통과부터 다음 통과까지 (나가는 랩 · 들어오는 랩은 없음). 계측선은 데이터에서 고른 선 (실제 대회 계측선과 다를 수 있음)",
                      "t_start_s · t_end_s 는 세션 시간"]}


def get_track(sid: str, lap: int | str | None = None, color: str | None = None, smooth_s: float | None = None) -> dict:
    """F7. 코스. lap = 랩 번호(그 랩의 주행선, dist_m 은 계측선부터), None = 최속 랩, "all" = 세션 전체 궤적.

    color = 채널 ID 를 주면 주행선 점마다 그 채널의 값 (같은 순간의 값을 엔진이 맞춘다. 기록이 빈 곳은 None). smooth_s 는 get_series 와 같다
    """
    s = _session(sid)
    spec = None
    if color is not None:
        spec = next((c for c in s["channels"] if c["channel_id"] == color), None)
        if spec is None:
            raise EngineError("unknown_channel", f"없는 채널: {color}", {"unknown": [color]})
    if smooth_s is not None and (isinstance(smooth_s, bool) or not isinstance(smooth_s, (int, float)) or not 0 <= smooth_s <= MAX_SMOOTH_S):
        raise EngineError("bad_argument", f"smooth_s 는 0 ~ {MAX_SMOOTH_S} s: {smooth_s!r}")
    tr = _track(sid)
    t, xx, yy, dd = (tr[c].to_numpy(float) for c in ("t_s", "x_m", "y_m", "dist_m"))
    which = s["best_lap"] if lap is None else lap
    if which == "all":
        pt, lap_no = t, None
    else:
        if which is None:
            raise EngineError("bad_argument", f"세션 {sid} 에는 랩이 없음 — lap='all' 로 부를 것")
        row = _lap_row(sid, which)
        ta, tb = float(row["t_start_s"]), float(row["t_end_s"])
        pt, lap_no = np.concatenate([[ta], t[(t > ta) & (t < tb)], [tb]]), int(row["lap"])   # 계측선을 지난 순간부터 다음 순간까지
    px, py, pd_ = (np.interp(pt, t, v) for v in (xx, yy, dd))
    pd_ = pd_ - pd_[0] if lap_no is not None else pd_
    area = 0.5 * float(np.sum(px * np.roll(py, -1) - np.roll(px, -1) * py)) if len(px) > 2 else 0.0
    out = {"sid": sid, "lap": lap_no, "t_s": _arr(pt, 3), "x_m": _arr(px, 2), "y_m": _arr(py, 2), "dist_m": _arr(pd_, 2),
           "gate": s["track"]["gate"], "frame_id": s["summary"].get("frame_id"), "direction": "ccw" if area > 0 else "cw" if area < 0 else None,
           "frame": "평면 좌표 (m) — x 동쪽 · y 북쪽. 계측선(gate)도 같은 좌표"}
    if spec is not None:
        ct, cv = _channel(sid, spec, (float(pt[0]), float(pt[-1])), smooth_s)
        vals = ser.sample_at(ct, cv, pt, ser.time_breaks(ct, GAP_FACTOR))
        out["color"] = {"channel_id": color, "unit": spec["unit"], "values": _arr(vals, int(spec.get("digits", 3))),
                        "smooth_s": smooth_s or None}
    return out


def get_quality(sid: str) -> list[dict]:
    """F8. 위치(세션 시간 구간)가 있는 품질 경고. 위치 없는 주의 글은 get_session 의 블록 cautions 에 있다."""
    return [dict(f) for f in _session(sid)["findings"]]


# ---------------------------------------------------------------- F9 ~ F12

def detect(path):
    """F9. (E4 에서 만든다)"""
    raise EngineError("not_implemented", "파일 판별은 아직 없음 (엔진 E4 단계)")


def ingest(paths, hints=None):       # 인자 이름은 계약(§5)을 따른다
    """F10. (E4 에서 만든다)"""
    raise EngineError("not_implemented", "파일 올리기는 아직 없음 (엔진 E4 단계)")


def get_job(job_id):
    """F11. (E4 에서 만든다)"""
    raise EngineError("not_implemented", "파일 올리기는 아직 없음 (엔진 E4 단계)")


def open_file(file_id: str) -> dict:
    """F12. 내려받을 파일 → 서버가 읽을 경로 · 이름 · 형식. 경로는 사이트 서버 안에서만 쓰고 화면에 보이지 않는다.

    file_id 는 세션의 파일 표에 있는 것만 받는다 (ID 로 경로를 지어내지 않는다).
    """
    if not isinstance(file_id, str) or "/" not in file_id:
        raise EngineError("unknown_file", f"파일 ID 가 올바르지 않음: {file_id!r}")
    sid = file_id.split("/", 1)[0]
    s = _session(sid)
    entry = s["files"].get(file_id)
    if entry is None:
        raise EngineError("unknown_file", f"파일 {file_id} 가 없음")
    base = _root() if entry["_base"] == "store" else _data_root()
    path = (base / entry["_rel"]).resolve()
    if not path.is_file():
        raise EngineError("unknown_file", f"파일 {file_id} 의 실제 파일이 없음 (데이터 폴더가 다른 곳에 있나?)", {"name": entry["name"]})
    return {"file_id": file_id, "path": str(path), "name": entry["name"], "content_type": entry["content_type"],
            "size_bytes": path.stat().st_size}
