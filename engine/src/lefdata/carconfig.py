"""차량 설정을 날짜별 판으로 — `<데이터 폴더>/_cars/<차>.toml`.

같은 차라도 로거 펌웨어 · 컨트롤러가 보내는 레지스터 목록 · BMS 프레임 모양 같은 설정이 날짜에 따라 바뀐다.
엔진은 데이터 모양만 보고 고르지 않고, 레코딩의 날짜로 판을 골라 그 판대로 읽는다 (모양 보기는 '설정과 데이터가 맞나' 검사로 남긴다).
새 판을 더해도 옛 데이터는 옛 판으로 읽히므로, 다시 변환해도 값이 바뀌지 않는다.

    car = "LEF-26"

    [[period]]                      # 판 하나 = from 부터 다음 판 앞까지
    name = "2026-08 펌웨어"
    from = 2026-08-01               # 날짜, 또는 날짜 시각 (시간대가 없으면 KST)
    source = "근거"
    logger.format = "config_on"     # 칸 이름은 차마다 (변환기가 읽는다)

    [[period]]
    name = "2026-11 새 펌웨어"
    from = 2026-11-12
    logger.format = "log_on"        # 바뀐 칸만 적는다 — 나머지는 앞 판을 물려받는다

레코딩마다 판을 따로 정할 수도 있다 — `<차>_assign.toml` 의 `[recordings]` 표 (레코딩 → 판 이름). 날짜보다 먼저.
첫 판보다 앞선 날짜는 가장 가까운 판(첫 판)으로 읽고, 고른 근거를 `nearest` 로 남긴다 (경고로 보인다).
설정 파일이 없는 차는 None — 변환기는 예전처럼 데이터 모양으로 고른다.
"""

from __future__ import annotations

import copy
import datetime as dt
import hashlib
import json
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

NAME = "_cars"
KST = dt.timezone(dt.timedelta(hours=9))
BASES = ("date", "assigned", "nearest")


@dataclass(frozen=True)
class Period:
    name: str
    start: dt.datetime
    source: str
    own: dict                  # 이 판에 적은 칸만
    values: dict               # 앞 판을 물려받은 전체 설정 (점으로 이은 이름 → 값)
    hash: str                  # values 를 줄인 값 — 판을 고치면 바뀐다 (다시 변환할 레코딩을 찾을 때)


@dataclass(frozen=True)
class Choice:
    """한 레코딩에 고른 판과 그 근거 (date = 날짜로 · assigned = 지정 표로 · nearest = 날짜가 첫 판보다 앞이라 가장 가까운 판)."""
    car: str
    period: Period
    basis: str

    def get(self, key: str, default=None):
        return self.period.values.get(key, default)

    def record(self) -> dict:
        """meta.json 에 남길 것."""
        return {"car": self.car, "period": self.period.name, "basis": self.basis, "hash": self.period.hash,
                "from": self.period.start.isoformat(), "source": self.period.source}


@dataclass
class Book:
    """한 차의 판들 (시작 순) · 레코딩 지정 표."""
    car: str
    path: Path
    periods: list[Period]
    assigned: dict[str, str] = field(default_factory=dict)

    def period(self, name: str) -> Period:
        got = next((p for p in self.periods if p.name == name), None)
        if got is None:
            raise ValueError(f"{self.path.name}: 판 '{name}' 이 없다 — 있는 판: {', '.join(p.name for p in self.periods)}")
        return got

    def choose(self, when, recording: str | None = None) -> Choice:
        """레코딩 → 판. 지정 표 > 날짜 (그 날짜 · 시각 이전에 시작한 마지막 판) > 첫 판보다 앞이면 첫 판 (nearest)."""
        if recording and recording in self.assigned:
            return Choice(self.car, self.period(self.assigned[recording]), "assigned")
        t = _moment(when)
        before = [p for p in self.periods if p.start <= t]
        return Choice(self.car, before[-1], "date") if before else Choice(self.car, self.periods[0], "nearest")


def _moment(v) -> dt.datetime:
    """날짜 · 날짜 시각 · 그 글자 → KST 시각 (날짜만이면 그날 0 시)."""
    if isinstance(v, str):
        v = dt.datetime.fromisoformat(v) if "T" in v or " " in v.strip() else dt.date.fromisoformat(v)
    if isinstance(v, dt.datetime):
        return v if v.tzinfo else v.replace(tzinfo=KST)
    if isinstance(v, dt.date):
        return dt.datetime(v.year, v.month, v.day, tzinfo=KST)
    raise ValueError(f"판의 시작은 날짜나 날짜 시각이어야 한다: {v!r}")


def _flat(d: dict, prefix: str = "") -> dict:
    """{"bms": {"profile": "x"}} → {"bms.profile": "x"} (TOML 의 점 이름을 다시 펼친다)."""
    out = {}
    for k, v in d.items():
        key = f"{prefix}{k}"
        if isinstance(v, dict):
            out.update(_flat(v, key + "."))
        else:
            out[key] = v
    return out


def _hash(values: dict) -> str:
    return hashlib.sha1(json.dumps(values, sort_keys=True, ensure_ascii=False, default=str).encode("utf-8")).hexdigest()[:12]


def root_for(path: Path) -> Path | None:
    """원본 · 이벤트 폴더에서 위로 올라가며 `_cars` 폴더를 찾는다 (`<데이터>/<차>/<이벤트>/raw_*/<파일>` 꼴 — 네 단계까지)."""
    path = Path(path).resolve()
    for p in [path, *path.parents][:5]:
        if (p / NAME).is_dir():
            return p / NAME
    return None


def load(car: str, root: Path | None) -> Book | None:
    """`<root>/<차>.toml` (+ `<차>_assign.toml`) → 판들. 파일이 없으면 None."""
    if root is None or not (Path(root) / f"{car}.toml").exists():
        return None
    path = Path(root) / f"{car}.toml"
    doc = tomllib.loads(path.read_text(encoding="utf-8"))
    if doc.get("car") not in (None, car):
        raise ValueError(f"{path.name}: car = '{doc.get('car')}' 인데 {car} 로 읽으려 함")
    raw = doc.get("period", [])
    if not raw:
        raise ValueError(f"{path.name}: [[period]] 가 하나도 없다")
    items = []
    for p in raw:
        if "name" not in p or "from" not in p:
            raise ValueError(f"{path.name}: [[period]] 마다 name 과 from 이 있어야 한다 — {p}")
        own = _flat({k: v for k, v in p.items() if k not in ("name", "from", "source")})
        items.append((_moment(p["from"]), p["name"], p.get("source", ""), own))
    items.sort(key=lambda x: x[0])
    names = [x[1] for x in items]
    if len(set(names)) != len(names):
        raise ValueError(f"{path.name}: 판 이름이 겹친다 — {names}")
    starts = [x[0] for x in items]
    if len(set(starts)) != len(starts):
        raise ValueError(f"{path.name}: 시작이 같은 판이 둘 있다")
    periods, values = [], {}
    for start, name, source, own in items:
        values = {**copy.deepcopy(values), **own}
        periods.append(Period(name, start, source, own, values, _hash(values)))
    assigned = {}
    apath = Path(root) / f"{car}_assign.toml"
    if apath.exists():
        assigned = dict(tomllib.loads(apath.read_text(encoding="utf-8")).get("recordings", {}))
    book = Book(car, path, periods, assigned)
    for rec, name in assigned.items():
        book.period(name)                                   # 없는 판을 지정했으면 여기서 멈춘다
    return book


def assign(root: Path, car: str, recording: str, period: str | None) -> Path:
    """레코딩 하나의 판을 지정 표에 적는다 (period = None 이면 지정을 지운다 — 날짜로 고르게). → 지정 표 경로.

    사이트의 '데이터 넣기 → 편집' 화면이 부를 길 (판 고르기). 판 이름은 그 차의 설정 파일에 있어야 한다.
    """
    book = load(car, root)
    if book is None:
        raise ValueError(f"{car} 의 설정 파일이 없다 ({Path(root) / f'{car}.toml'})")
    if period is not None:
        book.period(period)
    table = dict(book.assigned)
    if period is None:
        table.pop(recording, None)
    else:
        table[recording] = period
    lines = [f"# {car} — 레코딩마다 고른 판 (날짜보다 먼저). lefdata config --assign 이나 사이트 편집 화면이 쓴다", "", "[recordings]"]
    lines += [f"{json.dumps(k, ensure_ascii=False)} = {json.dumps(v, ensure_ascii=False)}" for k, v in sorted(table.items())]
    out = Path(root) / f"{car}_assign.toml"
    out.write_bytes(("\n".join(lines) + "\n").encode("utf-8"))
    return out


SESSION_TIME = {"LEF-25evo": ("Monolith",)}   # 변환이 아니라 세션을 만들 때 판을 읽는 장비 (meta source.logger 의 앞부분)


def status(data_root: Path, car: str) -> list[dict]:
    """그 차의 레코딩마다 — 변환할 때 쓴 판 · 지금 고르면 나오는 판 · 다시 변환이 필요한가 (판 이름이나 내용이 바뀜).

    `<data_root>/<차>/<이벤트>/normalized/<레코딩>/meta.json` 을 본다. 판을 읽는 변환기의 레코딩(meta 에 config 가 있음)과,
    세션에서 판을 읽는 장비(SESSION_TIME — 다시 변환은 필요 없고 세션만 다시)만. 판과 상관없는 장비(영상 등)는 뺀다.
    """
    book = load(car, Path(data_root) / NAME)
    if book is None:
        raise ValueError(f"{car} 의 설정 파일이 없다 ({Path(data_root) / NAME / f'{car}.toml'})")
    rows = []
    for meta_path in sorted((Path(data_root) / car).glob("*/normalized/*/meta.json")):
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        rec = meta_path.parent.name
        at_session = str((meta.get("source") or {}).get("logger", "")).startswith(SESSION_TIME.get(car, ())) if SESSION_TIME.get(car) else False
        if "config" not in meta and not at_session:
            continue
        start = (meta.get("time") or {}).get("t_abs_start")
        try:
            when = start if start else dt.date.fromisoformat(rec[:10])
        except ValueError:
            continue
        now = book.choose(when, rec)
        used = meta.get("config") or {}
        rows.append({"event": meta_path.parents[2].name, "recording": rec, "used_period": used.get("period"), "used_hash": used.get("hash"),
                     "period": now.period.name, "basis": now.basis, "hash": now.period.hash,
                     "mismatch": len(used.get("mismatch") or []), "at_session": at_session,
                     "reconvert": False if at_session else used.get("hash") != now.period.hash})
    return rows
