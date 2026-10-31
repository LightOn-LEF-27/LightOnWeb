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
    detect(path) · ingest(paths, hints)         F9 · F10  올린 파일 판별 · 넣기 → 변환 → 세션 (E4)
    get_job(job_id)                             F11 올리기 작업의 상태 · 결과
    open_file(file_id)                          F12 내려받을 파일
    get_sectors(sid, mini_m)                    F13 구간 · 미니 구간 시간 (기준 거리 위의 경계, frame_id 마다 고정)
    get_delta(sid, lap, ref_sid, ref_lap, ...)  F14 두 랩의 시간 차 (같은 frame 이면 다른 세션 랩끼리도)
    resolve_session(sid)                        F15 sid 풀기 — 지금 있는 세션인가, 없어졌으면 이어지는 세션 (sid 는 같은 주행이면 안 바뀐다)

- 돌려주는 값은 전부 JSON 으로 바꿀 수 있다 (dict · list · 숫자 · 문자열 · None). NaN 은 None 이다 — 화면은 거기서 선을 끊는다
- 잘못 부르면 `EngineError` 를 던진다. `.to_json()` 이 계약 §8 의 오류 모양이다
- 엔진은 세션 저장소(`session_store`, 기본 `07_데이터/_sessions`)만 읽는다. 저장소는 `lefdata sessions` 로 만든다
- 계산은 전부 여기서 한다. 사이트는 받은 값을 그리기만 한다 (단위 · 부호 · 랩 · 시간축은 엔진이 정한 것 하나)
- 구간 · 시간 차는 기준 거리(`ref_m`) 위에서 잰다 — frame(기준선 · 구간 경계)은 `lefdata sessions` 가 정해 둔 것 (`lefdata.sectors`)
"""

from __future__ import annotations

import json
import re
from functools import lru_cache
from pathlib import Path

import numpy as np
import pandas as pd

from . import __version__, ingest as ing, paths, sectors, series as ser, session_ids as ids, session_store as store

CONTRACT_VERSION = "0.15"
MAX_POINTS = 20_000
MAX_CHANNELS = 16
MAX_SMOOTH_S = 10.0
SID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")
GAP_FACTOR = 2.0            # 기록 간격 중앙값의 이 배보다 벌어진 곳은 잇지 않는다 (에너지미터의 '빠진 표본' 과 같은 기준)
MINI_SIZES = (10, 25, 50)   # 미니 구간 크기 (m) — 사이트가 고른다
FORMATS = [  # 계약 §7 — status: ingest = 판별 · 올리기 둘 다, detect = 판별만, not_started = 아직 (v0.10, E4)
    {"kind": "monolith_log", "label": "Monolith V2 .log (25Evo)", "status": "ingest"},
    {"kind": "fsk_energymeter_log", "label": "주최측 에너지미터 .log", "status": "ingest"},
    {"kind": "lef26_logger_csv", "label": "LEF-26 로거 통합본 CSV (SD 의 data.csv)", "status": "ingest"},
    {"kind": "evo_stm32_csv", "label": "25Evo STM32 백업 로거 CSV", "status": "ingest"},
    {"kind": "orion_csv", "label": "Orion BMS Utility CSV", "status": "detect"},
    {"kind": "gopro_mp4", "label": "GoPro 영상 (.MP4 · .LRV)", "status": "ingest"},
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
    _load_reference.cache_clear()
    _lap_ref_cached.cache_clear()


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


def _registry() -> dict | None:
    path = _root() / ids.FILE
    return _json(path) if path.exists() else None


def _retired(sid: str) -> EngineError | None:
    """등록부에 없어진 세션으로 적힌 sid → session_retired 오류 (뒤를 이은 지금 세션들을 detail 에)."""
    reg = _registry()
    e = (reg or {}).get("sessions", {}).get(sid)
    if not e or e.get("status") != ids.RETIRED:
        return None
    now = ids.resolve(reg, sid)
    return EngineError("session_retired", f"세션 {sid} 는 없어졌다 ({e.get('why')}) — 이어지는 세션: {', '.join(now) or '없음'}",
                       {"sid": sid, "why": e.get("why"), "retired_at": e.get("retired_at"), "replaced_by": list(e.get("replaced_by", [])),
                        "current": now})


def _session(sid) -> dict:
    if not isinstance(sid, str) or not SID.match(sid) or ".." in sid:
        raise EngineError("unknown_session", f"세션 ID 가 올바르지 않음: {sid!r}")
    path = _root() / sid / "session.json"
    if not path.exists():
        raise _retired(sid) or EngineError("unknown_session", f"세션 {sid} 가 없음")
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
    reg = _registry()
    retired = sum(e.get("status") == ids.RETIRED for e in reg["sessions"].values()) if reg else 0
    return {"contract": CONTRACT_VERSION, "engine": {"name": "lefdata", "version": __version__},
            "store": {"format": store.STORE_FORMAT, "sessions": len(rows), "retired_sids": retired},
            "formats": [dict(f) for f in FORMATS],
            "functions": ["engine_info", "list_sessions", "get_session", "list_channels", "get_series", "get_laps", "get_track",
                          "get_quality", "open_file", "get_sectors", "get_delta", "resolve_session", "detect", "ingest", "get_job"], "not_yet": []}


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
    return {"summary": {**s["summary"], "day_order": _index_row(sid).get("day_order")}, "notes": s["notes"], "blocks": s["blocks"],
            "missing": s["missing"], "best_lap": s["best_lap"], "made": s["made"]}


def _index_row(sid: str) -> dict:
    index = _root() / "index.json"
    return next((r for r in (_json(index)["sessions"] if index.exists() else []) if r["sid"] == sid), {})


def resolve_session(sid: str) -> dict:
    """F15. sid 풀기 — 지금 있는 세션이면 그대로, 없어진 세션이면 이유 · 뒤를 이은 세션 · 지금 있는 세션 (링크 · 북마크 넘기기용)."""
    if not isinstance(sid, str) or not SID.match(sid) or ".." in sid:
        raise EngineError("unknown_session", f"세션 ID 가 올바르지 않음: {sid!r}")
    if (_root() / sid / "session.json").exists():
        return {"sid": sid, "status": ids.ACTIVE, "why": None, "retired_at": None, "replaced_by": [], "current": [sid]}
    err = _retired(sid)
    if err is None:
        raise EngineError("unknown_session", f"세션 {sid} 가 없음")
    return {"status": ids.RETIRED, **err.detail}


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


def _frame_doc(s: dict, need: bool = False) -> tuple[dict, Path] | None:
    """세션의 frame (기준선 · 구간 경계) → (내용, 파일). 랩이 없는 세션은 None.

    랩이 있는데 frame 파일이 없으면(저장소를 옛 코드로 만든 경우) need 일 때 오류, 아니면 None.
    """
    sid = s["summary"]["sid"]
    if not s["track"]["has_track"] or _laps(sid).empty:
        return None
    key = store.frame_key(s["summary"])
    path = store.frame_path(_root(), key)
    if not path.exists():
        if need:
            raise EngineError("internal", f"세션 {sid} 의 frame(기준선 · 구간 경계) 파일이 없음 — lefdata sessions 로 다시 만들 것", {"frame": key})
        return None
    return _json(path), path


@lru_cache(maxsize=16)
def _load_reference(path: str, mtime: float) -> sectors.Reference:
    line = _load_json(path, mtime)["line"]
    return sectors.Reference(np.asarray(line["x_m"], float), np.asarray(line["y_m"], float))


def _reference(path: Path) -> sectors.Reference:
    return _load_reference(str(path), path.stat().st_mtime)


def _sector_edges(doc: dict, length: float) -> np.ndarray:
    e = np.array([doc["sectors"][0]["ref_m0"]] + [c["ref_m1"] for c in doc["sectors"]], float)
    e[0], e[-1] = 0.0, length                       # 끝은 기준선 길이 그대로 (파일의 값은 반올림한 것)
    return e


@lru_cache(maxsize=512)
def _lap_ref_cached(sid: str, lap: int, track_key: tuple, frame_key: tuple) -> tuple[np.ndarray, np.ndarray]:
    row = _lap_row(sid, lap)
    tr = _load_frame(*track_key)
    pt, px, py = sectors.lap_points(tr["t_s"].to_numpy(float), tr["x_m"].to_numpy(float), tr["y_m"].to_numpy(float),
                                    float(row["t_start_s"]), float(row["t_end_s"]))
    sv = sectors.project(_load_reference(*frame_key), px, py)
    pt.setflags(write=False)
    sv.setflags(write=False)
    return pt, sv


def _lap_ref(sid: str, lap: int, frame_path: Path) -> tuple[np.ndarray, np.ndarray]:
    """랩 하나의 점 시각(get_track 의 점과 같다)과 점마다 기준 거리. 저장소 · frame 파일이 바뀌면 다시 계산한다."""
    tp = _root() / sid / "track.parquet"
    return _lap_ref_cached(sid, int(lap), (str(tp), tp.stat().st_mtime), (str(frame_path), frame_path.stat().st_mtime))


def _split_laps(sid: str, frame_path: Path, edges: np.ndarray) -> tuple[list[int], np.ndarray]:
    """세션의 랩마다 경계 사이 시간 → (랩 번호들, 표 [랩 × 구간])."""
    nums = [int(v) for v in _laps(sid)["lap"]]
    rows = [sectors.split(*_lap_ref(sid, n, frame_path), edges) for n in nums]
    return nums, np.array(rows, float).reshape(len(nums), len(edges) - 1)


def _mini_size(mini_m) -> int:
    if isinstance(mini_m, bool) or not isinstance(mini_m, (int, float)) or mini_m not in MINI_SIZES:
        raise EngineError("bad_argument", f"mini_m 은 {' · '.join(map(str, MINI_SIZES))} m 중 하나: {mini_m!r}")
    return int(mini_m)


def _max_points(max_points) -> int:
    if isinstance(max_points, bool) or not isinstance(max_points, (int, np.integer)) or not 2 <= max_points <= MAX_POINTS:
        raise EngineError("bad_argument", f"max_points 는 2 ~ {MAX_POINTS} 의 정수: {max_points!r}")
    return int(max_points)


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
    x = "ref_distance"  기준 거리(m) — 구간 · 시간 차(F13 · F14)와 같은 축. lap 이 꼭 있어야 한다. 범위는 d0 · d1
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
    if x not in ("time", "distance", "ref_distance"):
        raise EngineError("bad_argument", f"x 는 'time' · 'distance' · 'ref_distance' 중 하나: {x!r}")
    _max_points(max_points)
    if smooth_s is not None and not (isinstance(smooth_s, (int, float)) and 0 <= smooth_s <= MAX_SMOOTH_S):
        raise EngineError("bad_argument", f"smooth_s 는 0 ~ {MAX_SMOOTH_S} s: {smooth_s!r}")
    if (x == "time" and (d0 is not None or d1 is not None)) or (x != "time" and (t0 is not None or t1 is not None)):
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
            raise EngineError("bad_argument", f"x={x!r} 는 lap 이 있어야 함 (랩마다 계측선부터의 거리)")
        ta, tb = float(row["t_start_s"]), float(row["t_end_s"])
        if x == "distance":
            tr = _track(sid)
            tt, dd = tr["t_s"].to_numpy(), tr["dist_m"].to_numpy()
            base = float(np.interp(ta, tt, dd))              # 계측선을 지날 때의 누적 거리
            length = float(np.interp(tb, tt, dd)) - base
            to_x = lambda t: np.interp(t, tt, dd) - base    # noqa: E731 — 세션 시간 → 그 랩의 거리
        else:
            fr = _frame_doc(s, need=True)
            if fr is None:
                raise EngineError("bad_argument", f"세션 {sid} 에는 코스(위치) 기록이 없음")
            pt, sv = _lap_ref(sid, int(row["lap"]), fr[1])
            length = float(sv[-1])
            to_x = lambda t: np.interp(t, pt, sv)           # noqa: E731 — 세션 시간 → 기준 거리
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
        if x != "time":                                      # 그 랩 밖의 표본은 쓰지 않는다 (양옆 하나씩은 잇기용으로 둔다)
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
                  "frame_id": s["summary"].get("frame_id") if x != "time" else None},
            "series": out_series, "laps": lap_marks,
            "reduced": {"from_points": native, "to_points": n, "method": "minmax", "smooth_s": smooth_s or None}}


# ---------------------------------------------------------------- F6 ~ F8

def get_laps(sid: str) -> dict:
    """F6. 랩 표 — 열 설명과 줄. best = 최속 랩."""
    s = _session(sid)
    laps = _laps(sid)
    cols = [c for c in s["lap_columns"] if c["key"] in laps.columns]
    keys = ["lap", "t_start_s", "t_end_s"] + [c["key"] for c in cols if c["key"] not in ("lap", "t_start_s", "t_end_s")]
    notes = ["랩은 계측선 통과부터 다음 통과까지 (나가는 랩 · 들어오는 랩은 없음). 계측선은 데이터에서 고른 선 (실제 대회 계측선과 다를 수 있음)",
             "t_start_s · t_end_s 는 세션 시간"]
    fr = _frame_doc(s) if len(laps) else None
    table, summary = None, None
    if fr is not None:
        doc, path = fr
        nums, table = _split_laps(sid, path, _sector_edges(doc, _reference(path).length))
        k = table.shape[1]
        skeys = [f"s{i + 1}_s" for i in range(k)]
        at = next((i + 1 for i, c in enumerate(cols) if c["key"] == "lap_time_s"), 0)     # 랩타임 바로 뒤에
        cols = cols[:at] + [{"key": key, "label": f"S{i + 1}", "unit": "s", "digits": 2, "block": "gps", "chart": False}
                            for i, key in enumerate(skeys)] + cols[at:]
        best_i = np.argmin(table, axis=0)
        summary = {"best": {key: {"value": _num(table[best_i[i], i], 3), "lap": nums[best_i[i]]} for i, key in enumerate(skeys)},
                   "mean": {key: _num(float(table[:, i].mean()), 3) for i, key in enumerate(skeys)}, "n_laps": len(nums),
                   "theoretical_best_s": _num(float(table[best_i, np.arange(k)].sum()), 3)}
        notes.append(f"구간 S1 ~ S{k} 은 기준 거리 위의 경계 (get_sectors 와 같다). 평균은 완주 랩 {len(nums)}개, 이론 최속 = 구간마다 최속의 합")
    elif len(laps) and s["track"]["has_track"]:
        notes.append("구간 시간 없음 — frame(기준선 · 구간 경계) 파일이 없다. lefdata sessions 로 다시 만들 것")
    rows = []
    for j, (_, r) in enumerate(laps.iterrows()):
        row = {k: _num(r[k]) for k in keys}
        row["lap"] = int(r["lap"])
        row["best"] = row["lap"] == s["best_lap"]
        if table is not None:
            row.update({f"s{i + 1}_s": _num(table[j, i], 3) for i in range(table.shape[1])})
            row["best_sectors"] = [i + 1 for i in range(table.shape[1]) if best_i[i] == j]
        rows.append(row)
    return {"columns": cols, "rows": rows, "best_lap": s["best_lap"], "sectors": summary, "notes": notes}


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
        pt = sectors.lap_points(t, xx, yy, float(row["t_start_s"]), float(row["t_end_s"]))[0]    # 계측선을 지난 순간부터 다음 순간까지
        lap_no = int(row["lap"])
    px, py, pd_ = (np.interp(pt, t, v) for v in (xx, yy, dd))
    pd_ = pd_ - pd_[0] if lap_no is not None else pd_
    area = 0.5 * float(np.sum(px * np.roll(py, -1) - np.roll(px, -1) * py)) if len(px) > 2 else 0.0
    out = {"sid": sid, "lap": lap_no, "t_s": _arr(pt, 3), "x_m": _arr(px, 2), "y_m": _arr(py, 2), "dist_m": _arr(pd_, 2),
           "gate": s["track"]["gate"], "frame_id": s["summary"].get("frame_id"), "direction": "ccw" if area > 0 else "cw" if area < 0 else None,
           "frame": "평면 좌표 (m) — x 동쪽 · y 북쪽. 계측선(gate)도 같은 좌표", "ref_m": None}
    fr = _frame_doc(s) if lap_no is not None else None
    if fr is not None:
        out["ref_m"] = _arr(_lap_ref(sid, lap_no, fr[1])[1], 2)
    if spec is not None:
        ct, cv = _channel(sid, spec, (float(pt[0]), float(pt[-1])), smooth_s)
        levels = spec.get("levels")
        vals = ser.sample_at(ct, cv, pt, ser.time_breaks(ct, GAP_FACTOR), nearest=bool(levels))   # 정해진 값만 갖는 채널은 잇지 않는다
        out["color"] = {"channel_id": color, "unit": spec["unit"], "values": _arr(vals, int(spec.get("digits", 3))),
                        "smooth_s": smooth_s or None, **({"levels": levels} if levels else {})}
    return out


def get_quality(sid: str) -> list[dict]:
    """F8. 위치(세션 시간 구간)가 있는 품질 경고. 위치 없는 주의 글은 get_session 의 블록 cautions 에 있다."""
    return [dict(f) for f in _session(sid)["findings"]]


# ---------------------------------------------------------------- F13 · F14 구간 · 시간 차

def get_sectors(sid: str, mini_m: int = 25) -> dict:
    """F13. 구간 · 미니 구간 — 경계(기준 거리, frame_id 마다 고정)와 랩마다 시간, 세션의 최속 · 평균 (완주 랩만).

    mini_m = 10 · 25 · 50 (m). 랩이 없는 세션은 빈 목록.
    """
    s = _session(sid)
    size = _mini_size(mini_m)
    fr = _frame_doc(s, need=True)
    if fr is None:
        return {"sid": sid, "frame_id": s["summary"].get("frame_id"), "ref": None, "sectors": [],
                "mini": {"mini_m": size, "edges_ref_m": [], "count": 0}, "laps": [], "best": None, "mean": None}
    doc, path = fr
    length = _reference(path).length
    sec_edges, mini_edges = _sector_edges(doc, length), sectors.mini_edges(length, size)
    nums, sec = _split_laps(sid, path, sec_edges)
    _, mini = _split_laps(sid, path, mini_edges)
    sb, mb = np.argmin(sec, axis=0), np.argmin(mini, axis=0)
    cols = np.arange(sec.shape[1]), np.arange(mini.shape[1])
    boundaries = [{**c, "ref_m0": _num(sec_edges[i], 2), "ref_m1": _num(sec_edges[i + 1], 2)} for i, c in enumerate(doc["sectors"])]
    return {"sid": sid, "frame_id": s["summary"].get("frame_id"),
            "ref": {"ref_len_m": _num(length, 2), "method": doc["method"], "source": doc["source"], "fixed_at": doc["fixed_at"],
                    "reference_lap": {"sid": doc["reference"]["sid"], "lap": doc["reference"]["lap"]}},
            "sectors": boundaries,
            "mini": {"mini_m": size, "edges_ref_m": _arr(mini_edges, 2), "count": int(len(mini_edges) - 1)},
            "laps": [{"lap": n, "sector_s": _arr(sec[j], 3), "mini_s": _arr(mini[j], 3)} for j, n in enumerate(nums)],
            "best": {"sector_s": _arr(sec[sb, cols[0]], 3), "sector_lap": [nums[i] for i in sb],
                     "mini_s": _arr(mini[mb, cols[1]], 3), "mini_lap": [nums[i] for i in mb]},
            "mean": {"sector_s": _arr(sec.mean(axis=0), 3), "mini_s": _arr(mini.mean(axis=0), 3), "n_laps": len(nums)}}


def get_delta(sid: str, lap: int, ref_sid: str, ref_lap: int, mini_m: int = 25, max_points: int = 4000) -> dict:
    """F14. 두 랩의 시간 차 — (sid, lap) 이 (ref_sid, ref_lap) 보다 기준 거리마다 얼마나 늦나 (+ 면 늦다).

    delta_s     기준 거리 x 마다 누적 차 (각자 랩 시작부터). 끝 값 = 랩타임 차 = total_s
    mini        미니 구간마다 차 (누적 아님)
    on_track    get_track(sid, lap) 의 점마다 누적 차 — 트랙맵 색
    두 세션의 frame 이 다르면 frame_mismatch.
    """
    s, r = _session(sid), _session(ref_sid)
    size = _mini_size(mini_m)
    n_max = _max_points(max_points)
    row, rrow = _lap_row(sid, lap), _lap_row(ref_sid, ref_lap)
    ka, kb = store.frame_key(s["summary"]), store.frame_key(r["summary"])
    if ka != kb:
        raise EngineError("frame_mismatch", f"두 랩의 frame 이 다름 (계측선 · 좌표가 다르다): {ka} ≠ {kb}",
                          {"frame_id": [s["summary"].get("frame_id"), r["summary"].get("frame_id")]})
    fr = _frame_doc(s, need=True)
    if fr is None:
        raise EngineError("bad_argument", f"세션 {sid} 에는 코스(위치) 기록이 없음")
    path = fr[1]
    length = _reference(path).length
    pa, sa = _lap_ref(sid, int(row["lap"]), path)
    pb, sb = _lap_ref(ref_sid, int(rrow["lap"]), path)
    grid = np.linspace(0.0, length, int(min(n_max, max(2, int(np.floor(length)) + 1))))     # 1 m 간격, max_points 이하
    edges = sectors.mini_edges(length, size)
    mini = sectors.split(pa, sa, edges) - sectors.split(pb, sb, edges)
    return {"lap": {"sid": sid, "lap": int(row["lap"])}, "ref": {"sid": ref_sid, "lap": int(rrow["lap"])},
            "frame_id": s["summary"].get("frame_id"),
            "x": {"kind": "ref_distance", "unit": "m", "values": _arr(grid, 2)},
            "delta_s": _arr(sectors.delta(pa, sa, pb, sb, grid), 3),
            "mini": {"mini_m": size, "edges_ref_m": _arr(edges, 2), "delta_s": _arr(mini, 3)},
            "on_track": {"values": _arr(sectors.delta_at_points(pa, sa, pb, sb), 3)},
            "total_s": _num(float((pa[-1] - pa[0]) - (pb[-1] - pb[0])), 3),
            "lap_time_s": {"lap": _num(float(pa[-1] - pa[0]), 3), "ref": _num(float(pb[-1] - pb[0]), 3)}}


# ---------------------------------------------------------------- F9 ~ F12

def detect(path) -> dict:
    """F9. 올린 파일이 무엇인가 — 형식 · 차량 짐작 · 시작 시각 · 길이 · 메모 (변환 전 미리보기, 파일은 읽기만)."""
    if not isinstance(path, (str, Path)):
        raise EngineError("bad_argument", f"path 는 파일 경로: {path!r}")
    try:
        return ing.detect(path)
    except FileNotFoundError as e:
        raise EngineError("bad_argument", str(e))
    except ing.UnsupportedFormat as e:
        raise EngineError("unsupported_format", str(e))
    except ing.CorruptFile as e:
        raise EngineError("corrupt_file", str(e))


def ingest(paths, hints=None) -> str:       # 인자 이름은 계약(§5)을 따른다
    """F10. 올린 파일들 → 이벤트에 넣고 변환 → 세션 → job_id. 끝날 때까지 기다린다 (서버는 다른 스레드에서 부르고 get_job 으로 진행을 본다).

    파일 · hints 의 모양이 틀리면 bad_argument, 다른 올리기가 돌고 있으면 busy. 그 밖의 결과(거절된 파일 · 실패한 단계)는 get_job 에.
    """
    if isinstance(paths, (str, Path)):
        paths = [paths]
    if not isinstance(paths, (list, tuple)) or not all(isinstance(p, (str, Path)) for p in paths):
        raise EngineError("bad_argument", "paths 는 파일 경로의 목록")
    try:
        job_id = ing.ingest(paths, hints, _data_root(), _root())
    except (FileNotFoundError, ValueError) as e:
        raise EngineError("bad_argument", str(e))
    except ing.Busy as e:
        raise EngineError("busy", str(e))
    _load_json.cache_clear()                    # 저장소가 바뀌었다 (파일 시각으로도 알지만 같은 초 안이면 놓칠 수 있다)
    return job_id


def get_job(job_id: str) -> dict:
    """F11. 올리기 작업의 상태 · 결과 — state · progress · recordings · sessions · warnings · errors + files · steps · event_id."""
    try:
        return ing.get_job(job_id, _data_root())
    except KeyError as e:
        raise EngineError("unknown_job", str(e.args[0]))


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
