"""파일 올리기 — 올린 파일을 판별하고 이벤트 폴더에 넣어 변환 → 세션까지 만든다 (팀 계층, 계약 §7 · 엔진 E4).

    detect(path)                       형식 · 시작 시각 · 길이 · 차량 짐작 — 변환기마다 있는 detect 를 차례로 부른다
    ingest(paths, hints, data_root, store_root)  → job_id
        1. 받은 파일마다 판별 · sha256. 전에 올린 파일(같은 sha256)은 다시 넣지 않는다 (`_uploads/index.json`)
        2. `_uploads/<job_id>/files/` 에 복사 (이름은 ASCII 로) → 이벤트의 `raw_*/` 에 하드 링크 (같은 디스크면 공간을 더 쓰지 않는다)
        3. 이벤트에 `catalog_notes.toml` 이 없으면 hints 로 기본값을 쓴다 (`sessions = "auto"`)
        4. 그 차의 엔진 명령 순서(`_steps`)를 돈다 — 손으로 돌릴 때와 같은 함수 · 순서
        5. `session_store.build_event` → 올린 파일을 쓰는 세션 · 레코딩을 job 에 적는다
    get_job(job_id, data_root)         `_uploads/<job_id>/job.json` (단계마다 다시 쓴다 — 다른 스레드에서 진행을 볼 수 있다)

지키는 것
- 올린 파일은 믿지 않는 입력이다. 읽기만 하고, 이벤트의 원본(`raw_*`)에 있던 파일은 지우거나 덮어쓰지 않는다 — 이름이 겹치면
  (내용이 다를 때) 새 이름을 쓰거나 (이름이 레코딩을 정하는 형식이면) 거절한다
- 한 번에 하나만 돈다 (`_uploads/.lock`) — 두 작업이 같은 이벤트 폴더를 동시에 고치지 않게
- 지금은 끝날 때까지 기다린다 (동기). 서버가 다른 스레드에서 부르면 된다
- 변환기는 이벤트의 그 종류 파일을 전부 다시 변환한다 (손으로 돌릴 때와 같다). 큰 이벤트에 파일 하나를 더하면 그만큼 걸린다

hints (사이트가 사람에게서 받는 것 — 지금 `catalog_notes.toml` 에 손으로 적는 것의 일부)
    car         "LEF-26" · "LEF-25evo" (파일이 차를 말해 주면 없어도 된다)
    event_id    "<car>/<YYYY-MM-DD>_<장소>" — 있는 이벤트에 더할 때. 없으면 place_id 와 날짜로 만든다
    place_id    장소 이름 (ASCII, 예: "yeonggwang") · date "YYYY-MM-DD" (없으면 파일의 가장 이른 날짜)
    title · kind · place · venue   새 이벤트의 catalog_notes.toml [event] 에 그대로 (venue = 경기장 계측선, `_venues/<이름>.json`)
세션 이름 · 드라이버 · Run Sheet 시각(`[[timed_session]]`)은 아직 hints 로 받지 않는다 — catalog_notes.toml 에 적고 `lefdata sessions`.
"""

from __future__ import annotations

import contextlib
import datetime as dt
import hashlib
import io
import json
import os
import re
import secrets
import shutil
import time
import traceback
from pathlib import Path

from . import energymeter, evo_stm32, lef26, monolith, orion, video

UPLOADS = "_uploads"
INDEX = "index.json"
LOCK = ".lock"
LOCK_STALE_S = 6 * 3600
NOTES_NAME = "catalog_notes.toml"
JOB_RE = re.compile(r"^j\d{8}-\d{6}-[0-9a-f]{4}$")
EVENT_RE = re.compile(r"^(?P<car>[A-Za-z0-9-]+)/(?P<date>\d{4}-\d{2}-\d{2})_(?P<place>[a-z0-9][a-z0-9_-]*)$")
PLACE_RE = re.compile(r"^[a-z0-9][a-z0-9_-]*$")
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
CARS = ("LEF-25evo", "LEF-26")
HINT_KEYS = ("car", "event_id", "place_id", "date", "title", "kind", "place", "venue")

# 판별 순서 — 바이트 머리로 가리는 것부터 (둘 다 .log 인 에너지미터 · Monolith 는 머리 바이트가 다르다)
DETECTORS = (energymeter.detect, monolith.detect, video.detect, lef26.detect, evo_stm32.detect, orion.detect)
RAW_DIR = {"monolith_log": "raw_logger", "fsk_energymeter_log": "raw_energymeter", "gopro_mp4": "raw_video",
           "lef26_logger_csv": "raw_merged", "evo_stm32_csv": "raw_stm32"}       # orion_csv 는 판별만 (lefdata orion 으로 넣는다)


class UnsupportedFormat(ValueError):
    """어느 변환기의 형식도 아니다."""


class CorruptFile(ValueError):
    """형식은 알지만 읽다 깨졌다."""


class Busy(RuntimeError):
    """다른 올리기가 돌고 있다."""


def detect(path) -> dict:
    """올린 파일 하나 → {kind, car_guess, start, duration_s, ok, notes, recording_id} (계약 §7). 파일은 읽기만 한다."""
    path = Path(path)
    if not path.is_file():
        raise FileNotFoundError(f"파일이 없음: {path}")
    if path.stat().st_size == 0:
        raise UnsupportedFormat(f"{path.name}: 빈 파일 (0 바이트)")
    for fn in DETECTORS:
        try:
            found = fn(path)
        except ValueError as e:
            raise CorruptFile(str(e)) from e
        if found is not None:
            return found
    raise UnsupportedFormat(f"{path.name}: 엔진이 모르는 형식 — Monolith · 에너지미터 · GoPro · LEF-26 로거 · 25Evo STM32 · Orion CSV 가 아니다")


# ---------------------------------------------------------------- 작업 기록

def uploads_root(data_root: Path) -> Path:
    return Path(data_root) / UPLOADS


def _now() -> str:
    return dt.datetime.now().astimezone().isoformat(timespec="seconds")


def _write_json(path: Path, doc: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(doc, ensure_ascii=False, indent=1, allow_nan=False, default=str), encoding="utf-8", newline="\n")
    os.replace(tmp, path)


def get_job(job_id: str, data_root: Path) -> dict:
    """job.json 그대로 (계약 §7 의 state · progress · recordings · sessions · warnings · errors + files · steps · event_id)."""
    if not isinstance(job_id, str) or not JOB_RE.match(job_id):
        raise KeyError(f"작업 ID 가 올바르지 않음: {job_id!r}")
    path = uploads_root(data_root) / job_id / "job.json"
    if not path.exists():
        raise KeyError(f"작업 {job_id} 가 없음")
    return json.loads(path.read_text(encoding="utf-8"))


@contextlib.contextmanager
def _lock(root: Path, job_id: str):
    root.mkdir(parents=True, exist_ok=True)
    path = root / LOCK
    for _ in range(2):
        try:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            break
        except FileExistsError:
            if time.time() - path.stat().st_mtime > LOCK_STALE_S:          # 지난 작업이 죽고 남은 자물쇠
                path.unlink(missing_ok=True)
                continue
            raise Busy(f"다른 올리기가 돌고 있다 ({path.read_text(encoding='utf-8', errors='replace').strip()})")
    else:
        raise Busy("자물쇠를 잡지 못함")
    try:
        os.write(fd, f"{job_id} pid {os.getpid()} {_now()}".encode())
        os.close(fd)
        yield
    finally:
        path.unlink(missing_ok=True)


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(8 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def safe_name(name: str) -> str:
    """파일 이름 → ASCII 만 (`07_데이터` 규칙). 글자 · 숫자 · . _ - 밖은 _ 로."""
    stem = re.sub(r"[^A-Za-z0-9._-]+", "_", Path(name).name).strip("._") or "file"
    return stem[:120]


# ---------------------------------------------------------------- 이벤트 · 자리

def _hint_keys(hints) -> None:
    """hints 의 모양만 — 작업을 만들기 전에 (값이 파일과 맞는지는 판별한 뒤 _hints)."""
    if hints is not None and not isinstance(hints, dict):
        raise ValueError("hints 는 dict")
    bad = [k for k in (hints or {}) if k not in HINT_KEYS]
    if bad:
        raise ValueError(f"hints 에 모르는 키: {bad} (쓸 수 있는 것: {', '.join(HINT_KEYS)})")
    if any(v is not None and not isinstance(v, str) for v in (hints or {}).values()):
        raise ValueError("hints 의 값은 문자열이어야 한다")


def _hints(hints: dict | None, dets: list[dict]) -> dict:
    h = {k: v for k, v in (hints or {}).items() if v}
    guesses = {d["car_guess"] for d in dets if d.get("car_guess")}
    if not h.get("car"):
        if len(guesses) != 1:
            raise ValueError("hints 의 car 가 필요하다 (파일만으로는 차를 모른다)")
        h["car"] = guesses.pop()
    if h["car"] not in CARS:
        raise ValueError(f"car 는 {' · '.join(CARS)} 중 하나: {h['car']!r}")
    if h.get("event_id"):
        m = EVENT_RE.match(h["event_id"])
        if not m or m["car"] != h["car"]:
            raise ValueError(f"event_id 는 '<car>/<YYYY-MM-DD>_<장소>' (car = {h['car']}): {h['event_id']!r}")
    else:
        if not h.get("place_id") or not PLACE_RE.match(h["place_id"]):
            raise ValueError("새 이벤트면 hints 의 place_id (ASCII 소문자, 예: yeonggwang) 가 필요하다 — 있는 이벤트면 event_id")
        days = sorted(d["start"][:10] for d in dets if d.get("start"))
        day = h.get("date") or (days[0] if days else None)
        if not day:
            raise ValueError("파일에 날짜가 없다 — hints 의 date (YYYY-MM-DD) 가 필요하다")
        h["event_id"] = f"{h['car']}/{day}_{h['place_id']}"
    if h.get("date") and not DATE_RE.match(h["date"]):
        raise ValueError(f"date 는 YYYY-MM-DD: {h['date']!r}")
    if h.get("venue") and not PLACE_RE.match(h["venue"]):
        raise ValueError(f"venue 는 ASCII 소문자 이름: {h['venue']!r}")
    return h


def _ensure_notes(event_dir: Path, h: dict, job_id: str) -> bool:
    """catalog_notes.toml 이 없으면 hints 로 기본값을 쓴다 → 새로 썼나."""
    path = event_dir / NOTES_NAME
    if path.exists():
        return False
    day = EVENT_RE.match(h["event_id"])["date"]
    q = lambda s: json.dumps(s, ensure_ascii=False)                       # TOML 기본 문자열과 같은 따옴표 · 이스케이프
    lines = [f"# lefdata ingest 가 만든 기본값 ({job_id}, {_now()[:10]}). 세션 이름 · 드라이버 · Run Sheet 시각 · 없는 데이터는 손으로 더한다",
             "[event]", f"car = {q(h['car'])}", f"kind = {q(h.get('kind') or '시험 주행')}",
             f"title = {q(h.get('title') or f'올린 파일 ({day[5:]})')}", f"place = {q(h.get('place') or '')}"]
    if h.get("venue"):
        lines.append(f"venue = {q(h['venue'])}")
    lines.append('sessions = "auto"')
    event_dir.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")
    return True


def _target(event_dir: Path, kind: str, name: str, sha: str, h: dict, index: dict) -> tuple[Path | None, str]:
    """이벤트 안의 놓을 자리 → (경로 — 같은 내용의 파일이 이미 있으면 그 경로, 빈 문자열). 놓을 수 없으면 (None, 이유)."""
    raw = event_dir / RAW_DIR[kind]
    day = h.get("date") or EVENT_RE.match(h["event_id"])["date"]

    def free(make):
        for k in range(26):
            p = make(k)
            if not p.exists() or _known(p, sha, index):
                return p
        return None

    if kind == "lef26_logger_csv":                    # 레코딩 이름 = 파일 이름 앞부분 (<날짜>[_b]_rNN)
        p = free(lambda k: raw / (f"{day}_merged.csv" if k == 0 else f"{day}_{chr(97 + k)}_merged.csv"))
        return (p, "") if p else (None, "같은 날짜의 통합본이 이미 26개")
    if kind == "evo_stm32_csv":                       # 레코딩 이름 = stm32_<묶음>_logNNN — 묶음 폴더로 가른다
        group = day.replace("-", "")[2:]
        p = free(lambda k: raw / (group if k == 0 else f"{group}{chr(97 + k)}") / name)
        return (p, "") if p else (None, "같은 이름의 STM32 로그가 이미 26묶음")
    if kind == "gopro_mp4":                           # 레코딩 = 녹화 번호 — 이름을 바꾸면 조각을 잇지 못한다
        p = raw / name
        if p.exists() and not _known(p, sha, index):
            return None, f"raw_video/{name} 이 이미 있고 내용이 다르다 (다른 카메라의 같은 번호?) — 다른 이벤트로 올리거나 손으로 넣을 것"
        return p, ""
    stem, suf = Path(name).stem, Path(name).suffix    # Monolith · 에너지미터 — 레코딩 이름은 내용(시작 시각)에서 나온다
    p = free(lambda k: raw / (name if k == 0 else f"{stem}_{sha[:8]}{'' if k == 1 else k}{suf}"))
    return (p, "") if p else (None, "이름이 겹친다")


def _known(p: Path, sha: str, index: dict) -> bool:
    """그 자리에 있는 파일이 이 파일과 같은가 — 색인에 있으면 그걸로, 없으면 크기가 같을 때만 해시를 잰다."""
    for e in index.get("files", {}).values():
        if e.get("placed_abs") == str(p):
            return e.get("sha256") == sha
    return p.is_file() and _sha256(p) == sha


# ---------------------------------------------------------------- 엔진 명령 순서

def _has(event_dir: Path, sub: str, pattern: str = "*") -> bool:
    d = event_dir / sub
    return d.is_dir() and any(d.rglob(pattern))


def _metas(event_dir: Path):
    for m in sorted((event_dir / "normalized").glob("*/meta.json")):
        try:
            yield m.parent.name, json.loads(m.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue


def _steps(car: str, kinds: set[str], event_dir: Path, data_root: Path) -> list[tuple[str, object]]:
    """이번에 들어온 종류 → 돌릴 단계 (이름, 함수). 손으로 돌리는 순서와 같다 (`lefdata` 명령 이름을 적는다)."""
    from . import clocks, em_laps, events, evo_can, laps, lef26_clock, venue

    def run_laps():
        base = Path(data_root) / "_venues"
        laps.run_event(event_dir, venue.for_event(event_dir, None, base if base.is_dir() else None))

    gps_new = bool(kinds & {"monolith_log", "gopro_mp4"})
    em = _has(event_dir, "raw_energymeter", "*.log")
    out = []
    if car == "LEF-25evo":
        if "monolith_log" in kinds:
            out.append(("monolith", lambda: monolith.convert_event(event_dir)))
        if "fsk_energymeter_log" in kinds:
            out.append(("energymeter", lambda: energymeter.convert_event(event_dir)))
        if gps_new:
            out += [("laps", run_laps), ("events", lambda: events.run_event(event_dir))]
        if "monolith_log" in kinds:
            out.append(("evo-can", lambda: evo_can.convert_event(event_dir)))
        if em and _has(event_dir, "raw_logger", "*.log") and kinds & {"monolith_log", "fsk_energymeter_log"}:
            out.append(("clocks (전원 주기)", lambda: clocks.power_cycles(event_dir)))
        if _has(event_dir, "raw_stm32", "*.csv") and kinds & {"evo_stm32_csv", "monolith_log", "fsk_energymeter_log"}:
            clock = "gps" if _has(event_dir, "raw_logger", "*.log") else "em"
            out.append((f"evo-stm32 --clock {clock}", lambda: (evo_stm32.convert_event(event_dir), evo_stm32.align_event(event_dir, clock=clock))))
    elif car == "LEF-26":
        if "lef26_logger_csv" in kinds:
            out.append(("lef26", lambda: lef26.convert_event(event_dir)))
        if "gopro_mp4" in kinds:
            out.append(("gopro", lambda: video.convert_event(event_dir)))
        if "fsk_energymeter_log" in kinds:
            out.append(("energymeter", lambda: energymeter.convert_event(event_dir)))
        if gps_new:
            out += [("laps", run_laps), ("events", lambda: events.run_event(event_dir))]
        if _has(event_dir, "raw_merged", "*.csv") and _has(event_dir, "raw_video") and kinds & {"lef26_logger_csv", "gopro_mp4"}:
            out.append(("lef26-clock", lambda: lef26_clock.run_event(event_dir)))
    runs = gps_new or (event_dir / "derived" / "runs.csv").exists()          # 랩 분할이 있거나 이번에 만든다
    if em and runs and (gps_new or "fsk_energymeter_log" in kinds):
        out.append(("em-laps", lambda: em_laps.run_event(event_dir)))
    return out


# ---------------------------------------------------------------- 올리기

def ingest(paths, hints: dict | None, data_root: Path, store_root: Path) -> str:
    """올린 파일들 → 이벤트에 넣고 변환 → 세션. 돌려주는 것은 job_id (끝난 뒤 — 결과는 get_job).

    파일이 없거나 hints 의 모양이 틀리면 작업을 만들지 않고 FileNotFoundError / ValueError. 다른 작업이 돌고 있으면 Busy.
    hints 의 값이 파일과 맞지 않으면(차 · 날짜를 모름 등) 작업은 failed 로 남는다 (errors 에 이유).
    """
    if isinstance(paths, (str, Path)):
        paths = [paths]
    paths = [Path(p) for p in paths]
    if not paths:
        raise ValueError("올릴 파일이 없다")
    for p in paths:
        if not p.is_file():
            raise FileNotFoundError(f"파일이 없음: {p}")
    _hint_keys(hints)
    data_root, store_root = Path(data_root), Path(store_root)
    root = uploads_root(data_root)
    job_id = f"j{dt.datetime.now():%Y%m%d-%H%M%S}-{secrets.token_hex(2)}"
    job = {"job_id": job_id, "state": "queued", "progress": 0.0, "created": _now(), "updated": _now(), "hints": dict(hints or {}),
           "event_id": None, "files": [], "steps": [], "recordings": [], "sessions": [], "dropped_runs": [], "warnings": [], "errors": []}
    jdir = root / job_id

    def save():
        _write_json(jdir / "job.json", {**job, "updated": _now()})

    with _lock(root, job_id):
        job["state"] = "running"
        save()
        log = io.StringIO()
        try:
            _run(job, paths, hints, data_root, store_root, jdir, log, save)
        except Exception as e:                                   # 예상하지 못한 것 — 작업이 running 으로 남지 않게
            job["errors"].append(f"{type(e).__name__}: {e}")
            log.write(traceback.format_exc())
        job.update(state="failed" if job["errors"] else "done", progress=1.0)
        (jdir / "log.txt").write_text(log.getvalue(), encoding="utf-8", newline="\n")
        save()
    return job_id


def _run(job: dict, paths: list[Path], hints, data_root: Path, store_root: Path, jdir: Path, log: io.StringIO, save) -> None:
    """ingest 의 몸통 — 판별 → 놓기 → 명령 순서 → 세션. job 을 고치고 단계마다 save()."""
    from . import session_store

    dets = []
    for p in paths:                                              # 1. 판별
        f = {"name": p.name, "size_bytes": p.stat().st_size, "status": "", "kind": None, "notes": []}
        try:
            d = detect(p)
            f.update({k: d.get(k) for k in ("kind", "car_guess", "start", "duration_s", "recording_id")}, notes=list(d["notes"]))
            f["status"] = "detected" if d["ok"] else "rejected"
            if d["ok"]:
                dets.append(d)
        except UnsupportedFormat as e:
            f.update(status="rejected", code="unsupported_format", notes=[str(e)])
        except CorruptFile as e:
            f.update(status="rejected", code="corrupt_file", notes=[str(e)])
        job["files"].append(f)
    save()
    try:
        h = _hints(hints, dets)
    except ValueError as e:
        job["errors"].append(str(e))
        return
    job["event_id"] = h["event_id"]
    event_dir = data_root / h["event_id"]
    root = uploads_root(data_root)
    index_path = root / INDEX
    index = json.loads(index_path.read_text(encoding="utf-8")) if index_path.exists() else {"files": {}}

    placed, total, done_bytes = [], sum(f["size_bytes"] for f in job["files"]) or 1, 0
    for p, f in zip(paths, job["files"]):                        # 2. 복사 · 놓기
        done_bytes += f["size_bytes"]
        if f["status"] != "detected":
            continue
        if f.get("car_guess") and f["car_guess"] != h["car"]:
            f.update(status="rejected", code="car_mismatch", notes=f["notes"] + [f"{f['car_guess']} 의 파일인데 car = {h['car']}"])
            continue
        if f["kind"] not in RAW_DIR:
            f.update(status="detect_only", notes=f["notes"] + ["올리기(ingest)는 아직 — 판별만"])
            continue
        sha = _sha256(p)
        f["sha256"] = sha
        seen = index["files"].get(sha)
        if seen and (data_root / seen["placed"]).exists():
            f.update(status="duplicate", placed=seen["placed"], notes=f["notes"] + [f"전에 올린 파일 ({seen['job_id']}) — 다시 넣지 않는다"])
            continue
        target, why = _target(event_dir, f["kind"], safe_name(p.name), sha, h, index)
        if target is None:
            f.update(status="rejected", code="name_conflict", notes=f["notes"] + [why])
            continue
        stored = jdir / "files" / safe_name(p.name)
        stored.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(p, stored)
        if not target.exists():
            target.parent.mkdir(parents=True, exist_ok=True)
            try:
                os.link(stored, target)                          # 같은 디스크면 공간을 더 쓰지 않는다
            except OSError:
                shutil.copyfile(stored, target)
        rel = target.relative_to(data_root).as_posix()
        f.update(status="placed", placed=rel)
        index["files"][sha] = {"job_id": job["job_id"], "name": p.name, "kind": f["kind"], "event_id": h["event_id"], "placed": rel,
                               "placed_abs": str(target), "sha256": sha, "at": _now()}
        placed.append((f, target))
        job["progress"] = round(0.3 * done_bytes / total, 3)
        save()
    _write_json(index_path, index)

    if not placed:
        if any(f["status"] == "duplicate" for f in job["files"]):
            job["warnings"].append("새로 넣은 파일이 없다 (전에 올린 파일) — 변환 · 세션을 다시 만들지 않았다")
        else:
            job["errors"].append("넣을 수 있는 파일이 없다 (files 의 notes 참고)")
        return
    if _ensure_notes(event_dir, h, job["job_id"]):               # 3. 새 이벤트의 기본 notes
        job["warnings"].append(f"새 이벤트 {h['event_id']} — {NOTES_NAME} 를 기본값으로 만들었다 (세션 이름 · 드라이버는 손으로)")

    steps = _steps(h["car"], {f["kind"] for f, _ in placed}, event_dir, data_root)    # 4. 엔진 명령 순서
    for k, (name, fn) in enumerate(steps):
        entry = {"step": name, "state": "running"}
        job["steps"].append(entry)
        save()
        t0 = time.perf_counter()
        try:
            with contextlib.redirect_stdout(log):
                fn()
            entry["state"] = "done"
        except Exception as e:                                   # 한 단계가 깨져도 다른 장비 · 세션은 만든다 — 오류로 남긴다
            entry.update(state="failed", error=f"{type(e).__name__}: {e}")
            job["errors"].append(f"{name}: {type(e).__name__}: {e}")
            log.write(traceback.format_exc())
        entry["seconds"] = round(time.perf_counter() - t0, 1)
        job["progress"] = round(0.3 + 0.6 * (k + 1) / (len(steps) + 1), 3)
        save()

    entry = {"step": "sessions", "state": "running"}              # 5. 세션
    job["steps"].append(entry)
    save()
    t0 = time.perf_counter()
    rows, report = [], {}
    try:
        with contextlib.redirect_stdout(log):
            rows = session_store.build_event(event_dir, store_root, data_root, report=report)
        entry["state"] = "done"
    except FileNotFoundError as e:
        entry.update(state="skipped", error=str(e))
        job["warnings"].append(f"세션을 만들지 못함 — {e}. GPS 가 없는 주행은 Run Sheet 시각을 {NOTES_NAME} 의 [[timed_session]] 에 적고 lefdata sessions")
    except Exception as e:
        entry.update(state="failed", error=f"{type(e).__name__}: {e}")
        job["errors"].append(f"sessions: {type(e).__name__}: {e}")
        log.write(traceback.format_exc())
    entry["seconds"] = round(time.perf_counter() - t0, 1)

    job["dropped_runs"] = report.get("dropped_runs", [])
    if job["dropped_runs"]:
        job["warnings"].append(f"이 이벤트의 주행 {len(job['dropped_runs'])}개는 세션으로 넣지 않았다 — "
                               + " · ".join(f"{d['recording']} 주행 {d['run']}: {d['why']}" for d in job["dropped_runs"][:5])
                               + (" …" if len(job["dropped_runs"]) > 5 else "") + f" (틀렸으면 {NOTES_NAME} 의 [[session]] 으로 넣는다)")
    recs, uses = _results(event_dir, data_root, [t for _, t in placed], store_root, rows)
    job["recordings"] = sorted({r for v in recs.values() for r in v})
    job["sessions"] = sorted({s for v in uses.values() for s in v})
    for f, t in placed:
        f["recordings"], f["sessions"] = recs.get(t, []), uses.get(t, [])
        if not f["sessions"]:
            f.setdefault("code", "no_session_match")
            job["warnings"].append(f"{f['name']}: 어느 세션에도 들어가지 않았다 (no_session_match) — 주행 구간 밖이거나, 시계를 맞추지 못했거나, "
                                   f"GPS 가 없는 주행 (Run Sheet 시각을 [[timed_session]] 에)")


def _results(event_dir: Path, data_root: Path, placed: list[Path], store_root: Path, rows: list[dict]) -> tuple[dict, dict]:
    """놓은 파일마다 → (그 파일에서 나온 레코딩들, 그 파일을 쓰는 세션들)."""
    recs: dict[Path, list[str]] = {t: [] for t in placed}
    for rid, meta in _metas(event_dir):
        src = meta.get("source", {})
        for t in placed:
            if src.get("file") == t.relative_to(event_dir).as_posix() or (t.parent.name == "raw_video" and t.name in src.get("files", [])):
                recs[t].append(rid)
    uses: dict[Path, list[str]] = {t: [] for t in placed}
    for row in rows:                                    # 세션의 원본 파일 표에 있거나, 그 파일에서 나온 레코딩의 채널을 쓰면 (STM32 처럼 원본을 묶어 적는 장비)
        doc = json.loads((Path(store_root) / row["sid"] / "session.json").read_text(encoding="utf-8"))
        used = {e.get("_rel") for e in doc["files"].values() if e.get("_base") == "data"}
        sources = {(c.get("source") or {}).get("recording_id") for c in doc["channels"]}
        for t in placed:
            if t.relative_to(data_root).as_posix() in used or sources & set(recs[t]):
                uses[t].append(row["sid"])
    return recs, uses
