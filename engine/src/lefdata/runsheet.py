"""일정표(Run Sheet) — 그날 주행마다 사람이 적은 R번호 · 시각 · 종목 · 드라이버 · 목적을 세션에 붙이고, 계획과 실제를 대 본다 (팀 계층).

    <이벤트>/runsheet_<YYYY-MM-DD>.csv      하루 한 파일. 두 차를 한 파일에 적어도 된다 (차 칸으로 가른다)

열 (머리줄 이름 — 한국어 또는 영어, 순서는 상관없다):
    차 car · R번호 run · 전원 켬 power_on · 출발 out · 도착 in · 종목 discipline · 드라이버 driver · 목적 purpose · 세팅 바뀜 setup · 메모 notes
    (있으면) 전원 끔 power_off · 장비 확인 devices · 사건 events · 직후 측정 after — 인쇄 양식의 칸. 세션 메모로 붙인다
시각은 폰 시계(통신망 시각), "HH:MM:SS" 또는 "HH:MM" — 날짜는 파일 이름에서.
    전원 켬 = LV(또는 HV) 를 켠 순간 · 출발 = 피트에서 바퀴가 구르기 시작한 순간 · 도착 = 피트로 돌아와 완전히 멈춘 순간 (결승선 통과는 적지 않는다)
드라이버는 A · B (driver_1 · driver_2 · 1 · 2 도 받는다). 그 밖의 글자(실명)는 넣지 않고 알린다.

짝짓기 — 세션(GPS 주행 구간 · 시각 구간)의 시작과 일정표의 출발이 MATCH_SLACK_S 안인 쌍들로 일정표 ↔ 세션 시계 차(중앙값)를 재고,
그만큼 옮긴 [출발, 도착] 과 세션 구간이 둘 중 짧은 쪽의 MATCH_SHARE 넘게 겹치면 같은 주행 (겹친 길이가 긴 짝부터 하나씩). 도착이 없으면 출발만으로 (START_ONLY_S 안).
짝이 맞으면 세션에 R번호 · 목적 · 메모를 붙이고, 종목 · 드라이버는 일정표를 따른다 — catalog_notes.toml 에 손으로 적은 세션은 그쪽이 먼저.
    데이터로 본 종목(트랙 · 가속 · 스키드패드)과 다르면 경고 runsheet_mismatch, 미분류는 일정표의 종목으로 풀린다
    그날 일정표가 있는데 짝이 없는 세션 → runsheet_unplanned (info) · 짝이 없는 일정표 줄 → 확인 보고 (기록이 없는 주행)
전원 켬 ↔ 로거가 켜진 시각(Monolith · 에너지미터 · LEF-26 로거)으로 장비마다 폰과의 시계 차를 잰다 — 시각 구간 세션의 [clocks] 에 적을 값 (적는 것은 사람이).
"""

from __future__ import annotations

import re
from pathlib import Path

import numpy as np
import pandas as pd

TZ = "Asia/Seoul"
FILE_RE = re.compile(r"^runsheet_(\d{4}-\d{2}-\d{2})\.csv$")
COLUMNS = {"차": "car", "car": "car", "r번호": "run_no", "run": "run_no", "run_no": "run_no", "전원켬": "power_on", "power_on": "power_on",
           "출발": "out", "out": "out", "도착": "in", "in": "in", "종목": "discipline", "discipline": "discipline", "드라이버": "driver",
           "driver": "driver", "목적": "purpose", "purpose": "purpose", "세팅바뀜": "setup", "setup": "setup", "메모": "notes", "notes": "notes",
           "전원끔": "power_off", "power_off": "power_off", "장비확인": "devices", "devices": "devices", "사건": "events", "events": "events",
           "직후측정": "after", "after": "after"}
NOTE_FIELDS = (("purpose", "목적"), ("setup", "세팅 바뀜"), ("devices", "장비 확인"), ("events", "사건"), ("after", "직후 측정"), ("notes", "메모"))
MATCH_SLACK_S = 300.0       # 일정표 출발 ↔ 세션 시작이 이 안인 쌍으로 시계 차를 잰다 (폰과 장비 시계가 몇 분 다를 수 있다)
MATCH_SHARE = 0.5           # 옮긴 일정표 구간과 세션 구간이 짧은 쪽의 이 비율 넘게 겹치면 같은 주행
START_ONLY_S = 60.0         # 도착을 안 적은 줄은 출발이 세션 시작과 이 안이면 같은 주행
POWER_ON_WINDOW_S = 900.0   # 전원 켬 앞뒤 이 안에 켜진 로거 레코딩을 짝으로 본다
POWER_DEVICES = {"monolith": "Monolith", "energymeter": "에너지미터", "lef26_logger": "LEF-26 차량 로거"}   # LV 와 같이 켜지는 로거
AUTO_KINDS = ("트랙", "가속", "스키드패드")
KIND_WORDS = (("가속", "가속"), ("스키드", "스키드패드"), ("트랙", "트랙"), ("내구", "트랙"), ("엔듀", "트랙"), ("오토크로스", "트랙"))   # 일정표 종목 → 데이터로 보는 종목


def kind_of(text: str) -> str | None:
    """일정표 종목이 데이터로 보는 종목(AUTO_KINDS) 중 무엇을 말하나 — '연습' · '시험 주행' 처럼 어느 쪽도 아니면 None (대 보지 않는다)."""
    return next((k for w, k in KIND_WORDS if w in str(text)), None)


def car_of(text: str, default: str) -> str | None:
    """차 칸 → 차 이름 ("26" · "LEF-26" → LEF-26, "25evo" · "25Evo" → LEF-25evo). 비면 default, 모르면 None."""
    t = str(text or "").strip().lower().replace(" ", "")
    if not t:
        return default
    if "26" in t:
        return "LEF-26"
    if "25" in t:
        return "LEF-25evo"
    if "27" in t:
        return "LEF-27"
    return None


def driver_of(text: str) -> str | None:
    """드라이버 칸 → '드라이버 A' 꼴 (실명이 저장소에 들어가지 않게). 비면 '', 받을 수 없는 글자면 None."""
    t = str(text or "").strip()
    if not t:
        return ""
    m = re.fullmatch(r"(?:드라이버\s*)?([A-Za-z])", t) or re.fullmatch(r"(?:driver[_ ]?)?([1-9])", t, re.I)
    if not m:
        return None
    g = m.group(1)
    return f"드라이버 {chr(64 + int(g)) if g.isdigit() else g.upper()}"


def _time(day: str, text) -> pd.Timestamp | None:
    t = str(text or "").strip()
    if not t or t.lower() == "nan":
        return None
    if re.fullmatch(r"\d{1,2}:\d{2}(:\d{2}(\.\d+)?)?", t):
        t = f"{day} {t}"
    try:
        ts = pd.Timestamp(t)
    except ValueError:
        raise ValueError(f"시각을 읽지 못함: {text!r}") from None
    return ts.tz_localize(TZ) if ts.tzinfo is None else ts.tz_convert(TZ)


def _read_csv(path: Path) -> pd.DataFrame:
    for enc in ("utf-8-sig", "cp949"):                  # 공유 시트에서 받은 것(UTF-8) · 엑셀로 저장한 것(CP949)
        try:
            return pd.read_csv(path, dtype=str, encoding=enc, keep_default_na=False)
        except UnicodeDecodeError:
            continue
    raise ValueError(f"{path.name}: 글자 인코딩을 읽지 못함 (UTF-8 · CP949 가 아니다)")


def read(event_dir: Path, car: str) -> tuple[list[dict], list[str]]:
    """이벤트의 일정표 → (이 차의 줄들, 문제 메모들). 줄 = {day, run_no, power_on, out, in, discipline, driver, purpose, setup, notes, file, line}."""
    rows, problems = [], []
    for path in sorted(Path(event_dir).glob("runsheet_*.csv")):
        m = FILE_RE.match(path.name)
        if not m:
            problems.append(f"{path.name}: 이름이 runsheet_<YYYY-MM-DD>.csv 가 아니라 읽지 않음")
            continue
        day = m.group(1)
        df = _read_csv(path)
        cols = {c: COLUMNS.get(re.sub(r"\s+", "", c).lower()) for c in df.columns}
        unknown = [c for c, k in cols.items() if k is None and c.strip()]
        if unknown:
            problems.append(f"{path.name}: 모르는 열 {unknown} — 쓰는 열: 차 · R번호 · 전원 켬 · 출발 · 도착 · 종목 · 드라이버 · 목적 · 세팅 바뀜 · 메모")
        df = df.rename(columns={c: k for c, k in cols.items() if k})
        for i, r in df.iterrows():
            line = i + 2                                    # 머리줄이 1
            vals = {k: str(r.get(k, "")).strip() for k in set(COLUMNS.values())}
            if not any(vals.values()):
                continue
            who = car_of(vals["car"], car)
            if who is None:
                problems.append(f"{path.name} {line}행: 차 '{vals['car']}' 를 모름")
                continue
            if who != car:
                continue
            try:
                t = {k: _time(day, vals[k]) for k in ("power_on", "out", "in", "power_off")}
            except ValueError as e:
                problems.append(f"{path.name} {line}행: {e}")
                continue
            if t["out"] is None:
                problems.append(f"{path.name} {line}행 (R{vals['run_no'] or '?'}): 출발 시각이 없어 세션과 대 보지 않음")
            elif t["in"] is not None and t["in"] <= t["out"]:
                problems.append(f"{path.name} {line}행 (R{vals['run_no'] or '?'}): 도착이 출발보다 앞 — 도착은 무시")
                t["in"] = None
            driver = driver_of(vals["driver"])
            if driver is None:
                problems.append(f"{path.name} {line}행: 드라이버 칸은 A · B 로만 (실명은 넣지 않는다) — 비워 둠")
                driver = ""
            rows.append({"day": day, "file": path.name, "line": line, "run_no": vals["run_no"], **t, "discipline": vals["discipline"],
                         "driver": driver, **{k: vals[k] for k, _ in NOTE_FIELDS}})
    return rows, problems


def _epoch(ts: pd.Timestamp) -> float:
    return ts.timestamp()


def _clock(t: pd.Timestamp | None) -> str:
    return t.strftime("%H:%M:%S") if t is not None else "-"


def apply(sessions: list, rows: list[dict], problems: list[str], recordings: list = (), explicit: set[int] = frozenset()) -> dict:
    """일정표 줄 ↔ 세션을 짝짓고 세션을 고친다 (이름 · 종목 · 드라이버 · R번호 · 목적 · 메모 · 경고). → 확인 보고.

    explicit = catalog_notes.toml 에 손으로 적은 세션들의 id() — 이름 · 종목 · 드라이버는 그쪽이 먼저 (R번호 · 목적 · 메모만 붙인다).
    recordings = 이벤트의 레코딩 (catalog_build._Rec: device · t0) — 전원 켬과 대 본다.
    """
    span = [(_epoch(s.start), _epoch(s.start) + float(s.duration_s)) for s in sessions]
    timed = [r for r in rows if r["out"] is not None]
    d = []
    for r in timed:                                     # 1. 일정표(폰) ↔ 세션 시계 차
        o = _epoch(r["out"])
        near = min(((a - o) for a, _ in span), key=abs, default=None)
        if near is not None and abs(near) <= MATCH_SLACK_S:
            d.append(near)
    offset = float(np.median(d)) if d else 0.0
    cand = []
    for i, r in enumerate(timed):                       # 2. 짝 — 겹친 길이가 긴 것부터
        o = _epoch(r["out"]) + offset
        e = _epoch(r["in"]) + offset if r["in"] is not None else None
        for j, (a, b) in enumerate(span):
            if e is None:
                if abs(a - o) <= START_ONLY_S:
                    cand.append((-START_ONLY_S + abs(a - o), i, j))
                continue
            over = min(b, e) - max(a, o)
            if over > 0 and over >= MATCH_SHARE * min(b - a, e - o):
                cand.append((-over, i, j))
    pair, used = {}, set()
    for _, i, j in sorted(cand):
        if i not in pair and j not in used:
            pair[i] = j
            used.add(j)
    matched, unmatched = [], []
    for i, r in enumerate(timed):
        if i not in pair:
            unmatched.append(r)
            continue
        s = sessions[pair[i]]
        before = s.discipline
        _annotate(s, r, id(s) in explicit)
        matched.append({**_row_text(r), "session_start": s.start.isoformat(timespec="seconds"), "session": s.name or s.discipline,
                        "discipline_data": before, "discipline": s.discipline,
                        "start_minus_out_s": round(_epoch(s.start) - _epoch(r["out"]), 1)})
    days = {r["day"] for r in rows}
    unplanned = []
    for j, s in enumerate(sessions):                    # 3. 일정표가 있는 날인데 짝이 없는 세션
        if j in used or s.day not in days:
            continue
        unplanned.append({"session_start": s.start.isoformat(timespec="seconds"), "session": s.name or s.discipline, "discipline": s.discipline})
        s.blocks[0].findings.append({"level": "info", "code": "runsheet_unplanned",
                                     "message": "그날 일정표에 없는 주행 — 일정표에 줄을 더하거나, 피트 이동이면 catalog_notes.toml 의 [[session]] skip",
                                     "t_ranges": [[0.0, round(float(s.duration_s), 3)]]})
    return {"rows": len(rows), "offset_s": round(offset, 1), "offset_pairs": len(d), "matched": matched,
            "unmatched_rows": [_row_text(r) for r in unmatched] + [_row_text(r) for r in rows if r["out"] is None],
            "unplanned_sessions": unplanned, "power_on": power_on(rows, recordings), "problems": list(problems)}


def _row_text(r: dict) -> dict:
    return {"day": r["day"], "run_no": r["run_no"], "out": _clock(r["out"]), "in": _clock(r["in"]), "discipline": r["discipline"],
            "driver": r["driver"], "where": f"{r['file']} {r['line']}행"}


def _annotate(s, r: dict, explicit: bool) -> None:
    """짝이 맞은 세션에 일정표를 붙인다."""
    s.run_no, s.purpose = r["run_no"], r["purpose"]
    if r["discipline"] and not explicit:
        if s.discipline in AUTO_KINDS and kind_of(r["discipline"]) not in (None, s.discipline):
            s.blocks[0].findings.append({"level": "warn", "code": "runsheet_mismatch",
                                         "message": f"일정표 종목은 '{r['discipline']}', 데이터로 본 종목은 '{s.discipline}' — 일정표를 따랐다. 어느 쪽이 맞는지 확인",
                                         "t_ranges": [[0.0, round(float(s.duration_s), 3)]]})
        for b in s.blocks:                              # 미분류는 일정표의 종목으로 풀린다
            b.findings[:] = [f for f in b.findings if f.get("code") != "unclassified_run"]
        s.discipline = r["discipline"]
    if r["driver"] and not (explicit and s.driver):
        s.driver = r["driver"]
    if not explicit:
        s.name = f"R{r['run_no']} {s.discipline}".strip() if r["run_no"] else ""
    head = f"일정표 R{r['run_no'] or '?'} — 출발 {_clock(r['out'])} · 도착 {_clock(r['in'])} (폰 시각"
    on_off = [f"전원 켬 {_clock(r['power_on'])}"] if r["power_on"] is not None else []
    on_off += [f"전원 끔 {_clock(r['power_off'])}"] if r.get("power_off") is not None else []
    head += (", " + " · ".join(on_off) + ")") if on_off else ")"
    s.notes = [head] + [f"{label}: {r[k]}" for k, label in NOTE_FIELDS if r.get(k)] + list(s.notes)


def power_on(rows: list[dict], recordings) -> list[dict]:
    """전원 켬(폰 시각) ↔ 그 앞뒤 POWER_ON_WINDOW_S 안에 켜진 로거 레코딩 → 장비마다 (장비 시계 − 폰) 의 중앙값 · 범위.
    레코딩은 켠 뒤 몇 초 안에 시작하므로, 차의 대부분은 장비 시계가 폰과 다른 만큼이다 (시각 구간 세션의 [clocks.<장비>] offset_s 는 이 값의 부호를 바꾼 것)."""
    out = []
    ons = [_epoch(r["power_on"]) for r in rows if r["power_on"] is not None]
    for device, label in POWER_DEVICES.items():
        starts = np.array(sorted(x.t0 for x in recordings if x.device == device))
        if not len(starts) or not ons:
            continue
        cand = sorted((abs(s - p), i, k) for i, p in enumerate(ons) for k, s in enumerate(starts) if abs(s - p) <= POWER_ON_WINDOW_S)
        d, used_on, used_rec = [], set(), set()
        for _, i, k in cand:                            # 가까운 것부터 하나씩 — 레코딩 하나는 전원 켬 하나에만
            if i not in used_on and k not in used_rec:
                used_on.add(i)
                used_rec.add(k)
                d.append(float(starts[k] - ons[i]))
        if d:
            out.append({"device": device, "label": label, "pairs": len(d), "device_minus_phone_s": round(float(np.median(d)), 1),
                        "min_s": round(min(d), 1), "max_s": round(max(d), 1)})
    return out


def write_check(event_dir: Path, check: dict) -> Path:
    """확인 보고 → <이벤트>/derived/runsheet_check.csv (사람이 표로 볼 것: 짝 · 일정표만 · 세션만 · 전원 켬 · 문제)."""
    rows = [{"kind": "짝", **m} for m in check["matched"]] + [{"kind": "일정표에만", **r} for r in check["unmatched_rows"]]
    rows += [{"kind": "세션에만", **s} for s in check["unplanned_sessions"]] + [{"kind": "전원 켬", **p} for p in check["power_on"]]
    rows += [{"kind": "문제", "note": p} for p in check["problems"]]
    out = Path(event_dir) / "derived" / "runsheet_check.csv"
    out.parent.mkdir(parents=True, exist_ok=True)
    pd.DataFrame(rows).to_csv(out, index=False, encoding="utf-8-sig")
    return out


def check_text(check: dict) -> list[str]:
    """확인 보고 → 사람이 읽을 줄들 (lefdata sessions · ingest 가 보여 준다)."""
    lines = [f"일정표 {check['rows']}줄 · 세션과 짝 {len(check['matched'])} · 일정표 ↔ 세션 시계 차 {check['offset_s']:+.1f} s ({check['offset_pairs']}쌍)"]
    lines += [f"일정표에 있는데 세션이 없음: R{r['run_no'] or '?'} {r['day']} {r['out']} ~ {r['in']} {r['discipline']} ({r['where']}) — 장비가 안 켜졌거나 GPS 가 없는 주행 "
              f"([[timed_session]] 으로 넣으려면 장비 시계 차를 적는다)" for r in check["unmatched_rows"]]
    lines += [f"일정표에 없는 주행: {s['session_start']} {s['session']}" for s in check["unplanned_sessions"]]
    lines += [f"전원 켬 ↔ {p['label']} 기록 시작: 장비 시계 − 폰 {p['device_minus_phone_s']:+.1f} s ({p['pairs']}쌍, {p['min_s']:+.1f} ~ {p['max_s']:+.1f})"
              for p in check["power_on"]]
    mism = [m for m in check["matched"] if m["discipline_data"] in AUTO_KINDS and kind_of(m["discipline"]) not in (None, m["discipline_data"])]
    lines += [f"종목이 다름: R{m['run_no']} 일정표 '{m['discipline']}' · 데이터 '{m['discipline_data']}' ({m['where']})" for m in mism]
    return lines + list(check["problems"])
