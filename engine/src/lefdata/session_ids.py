"""안 바뀌는 세션 ID — 한 번 준 `sid` 는 저장소를 다시 만들어도 같은 주행이면 그대로 (팀 계층, 계약 §3 v0.9).

등록부는 세션 저장소 안의 `_ids.json` 하나다 (`_frames/` 처럼 한 번 정하면 고정 — 저장소를 통째로 지우면 같이 사라지니 지우지 않는다).
등록부가 없는 저장소에 처음 쓸 때는 그 저장소에 이미 있는 세션(`*/session.json`)을 첫 목록으로 삼는다.

    같은 주행     같은 이벤트(`event_id`) · 같은 차에서 시간 구간(`start` ~ `start + duration_s`, 이벤트의 기준 시계)이 겹치고, 겹친 길이가
                  둘 중 짧은 세션의 SAME_MIN_SHARE 이상. 짝은 겹친 길이가 긴 것부터 하나씩 (옛 세션 하나 ↔ 새 세션 하나).
                  그래서 쪼개지면 옛 구간을 가장 많이 가진 조각이, 합쳐지면 가장 많이 들어간 옛 세션이 sid 를 갖는다 (같으면 앞의 것).
                  장비 · 나눈 방법(`split_by`)은 보지 않는다 — Run Sheet 시각 세션에 나중에 GPS 가 붙어도 같은 sid
    새 세션       그 차 · 그날 번호 중 가장 큰 것 + 1 (없어진 번호도 다시 쓰지 않는다). 시간상 앞이어도 뒤 번호가 될 수 있다
    없어진 세션   짝이 없는 옛 세션. status = retired. why = replaced (새 세션 하나가 덮음 — 합쳐짐 · 경계 이동) · split (둘 이상) · removed (덮는 세션 없음).
                  replaced_by = 그 구간의 NEXT_MIN_SHARE 이상을 덮는 새 세션들. removed 세션의 구간이 다시 나타나면 같은 sid 로 되살린다

그날의 순서(화면용, 바뀌어도 된다)는 sid 가 아니라 세션 목록의 `day_order` 다 (`session_store.write_index`).
"""

from __future__ import annotations

import datetime as dt
import json
import re
from pathlib import Path

import pandas as pd

FILE = "_ids.json"
FORMAT = 1
SAME_MIN_SHARE = 0.5        # 짧은 쪽의 이만큼 겹치면 같은 주행 (가장 짧은 세션 20 여 초 · 세션 사이 10 초 남짓 — 시계가 몇 초 옮겨져도 남는다)
NEXT_MIN_SHARE = 0.1        # 없어진 세션 구간의 이만큼 이상을 덮는 새 세션을 뒤를 잇는 세션으로 적는다
SID_RE = re.compile(r"^(?P<car>.+)_(?P<day>\d{4}-\d{2}-\d{2})_S(?P<n>[1-9][0-9]*)$")
ACTIVE, RETIRED = "active", "retired"


def make_sid(car: str, day: str, n: int) -> str:
    return f"{car}_{day}_S{n}"


def _epoch(start) -> float:
    return pd.Timestamp(start).timestamp()


def _span(e: dict) -> tuple[float, float]:
    t0 = _epoch(e["start"])
    return t0, t0 + max(float(e["duration_s"] or 0.0), 0.0)


def _overlap(a: tuple[float, float], b: tuple[float, float]) -> float:
    return max(0.0, min(a[1], b[1]) - max(a[0], b[0]))


def _share(a: tuple[float, float], b: tuple[float, float]) -> float:
    """겹친 길이 ÷ 둘 중 짧은 쪽의 길이."""
    short = min(a[1] - a[0], b[1] - b[0])
    return _overlap(a, b) / short if short > 0 else 0.0


def _today() -> str:
    return dt.date.today().isoformat()


def read(root: Path) -> dict | None:
    """등록부 → dict (없으면 None). 읽기만 한다 (API 용)."""
    path = Path(root) / FILE
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else None


def load(root: Path) -> dict:
    """등록부를 읽는다. 없으면 저장소에 이미 있는 세션으로 첫 목록을 만든다 (아직 쓰지는 않는다 — save)."""
    reg = read(root)
    if reg is not None:
        if reg.get("format") != FORMAT:
            raise ValueError(f"{Path(root) / FILE}: 등록부 형식 {reg.get('format')!r} — 이 엔진은 {FORMAT}")
        return reg
    reg = {"format": FORMAT, "sessions": {}}
    for p in sorted(Path(root).glob("*/session.json")):
        row = json.loads(p.read_text(encoding="utf-8")).get("summary", {})
        sid = row.get("sid", "")
        if SID_RE.match(sid) and p.parent.name == sid:
            reg["sessions"][sid] = {"event_id": row["event_id"], "car": row["car"], "start": row["start"], "duration_s": row["duration_s"],
                                    "title": row.get("title", ""), "status": ACTIVE, "first_built": _today(), "last_built": _today(),
                                    "seeded": True}
    return reg


def save(root: Path, reg: dict) -> None:
    path = Path(root) / FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    reg["sessions"] = dict(sorted(reg["sessions"].items(), key=lambda kv: (kv[1]["start"], kv[0])))
    path.write_text(json.dumps(reg, ensure_ascii=False, indent=1, allow_nan=False), encoding="utf-8", newline="\n")


def _next_number(reg: dict, car: str, day: str) -> int:
    used = [int(m["n"]) for sid in reg["sessions"] if (m := SID_RE.match(sid)) and m["car"] == car and m["day"] == day]
    return max(used, default=0) + 1


def _pairs(old: list[str], new: list[dict], ents: dict, taken_new: set[int]) -> list[tuple[str, int]]:
    """겹친 길이가 긴 것부터 하나씩 짝 (옛 sid, 새 세션 번호). 같은 차끼리만."""
    cand = []
    for sid in old:
        e = ents[sid]
        for i, n in enumerate(new):
            if i in taken_new or n["car"] != e["car"]:
                continue
            a, b = _span(e), n["_span"]
            if _share(a, b) >= SAME_MIN_SHARE:
                cand.append((-round(_overlap(a, b), 3), -_share(a, b), e["start"], n["start"], sid, i))
    cand.sort()
    used_old, out = set(), []
    for *_, sid, i in cand:
        if sid in used_old or i in taken_new:
            continue
        used_old.add(sid)
        taken_new.add(i)
        out.append((sid, i))
    return out


def assign(reg: dict, event_id: str, sessions: list[dict]) -> tuple[list[str], list[str]]:
    """이벤트 하나를 새로 만든 세션들(car · day · start · duration_s · title) → (세션마다 sid, 이번에 없어진 sid). reg 를 고친다.

    다른 이벤트의 세션은 건드리지 않는다 (그 이벤트를 다시 만들지 않았으니 없어진 것이 아니다).
    """
    ents, today = reg["sessions"], _today()
    new = [{**s, "start": pd.Timestamp(s["start"]).isoformat(timespec="milliseconds"), "_span": None} for s in sessions]
    for n in new:
        n["_span"] = _span(n)
    mine = [sid for sid, e in ents.items() if e["event_id"] == event_id]
    live = [sid for sid in mine if ents[sid]["status"] == ACTIVE]
    gone = [sid for sid in mine if ents[sid]["status"] == RETIRED and ents[sid].get("why") == "removed"]
    taken: set[int] = set()
    sids: list[str | None] = [None] * len(new)
    for sid, i in _pairs(live, new, ents, taken) + _pairs(gone, new, ents, taken):
        sids[i] = sid
    for i in sorted((i for i in range(len(new)) if sids[i] is None), key=lambda i: (new[i]["start"], i)):
        sids[i] = make_sid(new[i]["car"], new[i]["day"], _next_number(reg, new[i]["car"], new[i]["day"]))
        ents[sids[i]] = {"first_built": today}
    for sid, n in zip(sids, new):
        e = ents[sid]
        for k in ("why", "replaced_by", "retired_at", "seeded"):
            e.pop(k, None)
        e.update({"event_id": event_id, "car": n["car"], "start": n["start"], "duration_s": round(float(n["duration_s"]), 3),
                  "title": n.get("title", ""), "status": ACTIVE, "last_built": today})
        e.setdefault("first_built", today)
    retired = [sid for sid in live if sid not in sids]
    for sid in retired:
        e = ents[sid]
        span = _span(e)
        share = [(_overlap(span, n["_span"]) / max(span[1] - span[0], 1e-9), n["start"], s) for s, n in zip(sids, new)]
        nxt = [s for f, _, s in sorted(share, key=lambda x: (x[1], x[2])) if f >= NEXT_MIN_SHARE]
        e.update({"status": RETIRED, "retired_at": today, "replaced_by": nxt,
                  "why": "removed" if not nxt else "replaced" if len(nxt) == 1 else "split"})
    return sids, retired


def resolve(reg: dict | None, sid: str) -> list[str]:
    """없어진 sid → 지금 있는 세션들 (뒤를 이은 세션이 또 없어졌으면 끝까지 따라간다). 있는 sid 면 [sid]."""
    if not reg or sid not in reg["sessions"]:
        return []
    out, seen, todo = [], set(), [sid]
    while todo:
        s = todo.pop(0)
        if s in seen or s not in reg["sessions"]:
            continue
        seen.add(s)
        e = reg["sessions"][s]
        if e["status"] == ACTIVE:
            out.append(s)
        else:
            todo += e.get("replaced_by", [])
    return out
