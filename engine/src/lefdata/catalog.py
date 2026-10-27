"""주행 데이터 카탈로그 — 세션 하나를 한 페이지로 (자료 구조와 문서 조립).

팀원이 과거 주행 데이터를 찾아보고 가져갈 수 있게, 세션(차가 나갔다 들어올 때까지)마다
"어떤 데이터가 있고 · 원본은 무엇이고 · 어떤 코드로 바꿨고 · 결과물은 무엇인지" 를 같은 양식으로 적는다.

    catalog_build.build_event(event_dir)  →  [Session]  →  write_session(...)
        → <세션>/page.md (원본 문서) · page.json (블록 목록) · figures/*.png · files/*.csv

이 모듈과 그림(`catalog_charts`)은 **차량 · 폴더 구조를 모른다** — `Session` 만 본다. 차량별 코드는 `catalog_build` 에 있다.
LEF-27 은 `Session` 을 채우는 함수만 새로 쓰면 같은 페이지가 나온다.

페이지는 블록 목록으로 한 번 만들고, 로컬 md 와 노션용 마크다운을 둘 다 그 목록에서 찍는다.
**로컬 md 가 원본이고 노션은 사본이다.** 손으로 고친 노션 페이지는 다음 생성 때 덮인다.

데이터 종류(`DataBlock`)마다 그림의 형태가 같다 — 시간 · 랩 · 거리 · 지도 · 분포 (`catalog_charts`).
채워지지 않은 것은 그 절이 빠진다 (없는 것을 지어내지 않는다).
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
import pandas as pd

HAVE, WARN, SEEK, NONE = "있음", "있음 (주의)", "찾는 중", "기록 안 됨"
NOTION_FILE_TYPES = (".csv", ".png", ".html", ".pdf", ".zip", ".json", ".txt")   # 노션이 그대로 받는 형식 (쓰는 것만)
ATTACH_MAX_BYTES = 5 * 1024 * 1024     # 이보다 큰 파일은 페이지에 붙이지 않고 있는 곳만 적는다 (노션 무료 요금제의 파일 한도)


@dataclass
class FileRef:
    """원본 · 결과물 파일 하나. path 가 있고 작으면 페이지에 붙이고, 아니면 where(있는 곳)만 적는다."""
    label: str                        # "에너지미터 원본 로그"
    name: str                         # 파일명
    path: Path | None = None
    size_bytes: int = 0
    note: str = ""                    # "100 Hz · 88,517 표본"
    where: str = ""

    @property
    def attached(self) -> bool:
        return self.path is not None and 0 < self.size_bytes <= ATTACH_MAX_BYTES


@dataclass
class Export:
    """세션 구간만 잘라 낸 표. write_session 이 files/<name> 으로 쓰고 결과물 목록에 넣는다."""
    name: str                         # "battery.csv"
    label: str
    table: pd.DataFrame
    note: str = ""


@dataclass
class CodeRef:
    """변환 · 분석 코드 하나 (코드 페이지 하나). 여러 세션이 같은 페이지를 가리킨다."""
    key: str                          # "energymeter"
    title: str                        # "에너지미터 로그 → 표"
    role: str = ""                    # 이 데이터에서 한 일 한 줄


@dataclass
class Channel:
    """시간에 따라 변하는 값 하나. t_s 는 세션 시작부터의 초.

    sign 이하는 분석 엔진(`lefdata.api`)이 채널 목록에 내보내는 설명이다. 페이지 · 그림은 쓰지 않는다.
    """
    key: str
    label: str
    unit: str
    t_s: np.ndarray
    v: np.ndarray
    sign: str = ""                    # "가속 +, 제동 −" — 부호의 뜻
    derived: bool = False             # 장비가 이 이름으로 기록하지 않고 계산해 만든 값인가
    derivation: str = ""              # 어떻게 만들었나 한 줄
    cautions: list[str] = field(default_factory=list)
    source: str = ""                  # 레코딩 ID
    digits: int = 3                   # 값을 내보낼 때의 소수 자릿수

    @property
    def title(self) -> str:
        return f"{self.label} ({self.unit})" if self.unit else self.label


@dataclass
class LapColumn:
    """랩 표의 열 하나 (Session.laps 의 열 이름). chart = 랩 그림에도 그린다."""
    key: str
    label: str
    unit: str = ""
    digits: int = 1
    chart: bool = False


@dataclass
class DataBlock:
    """데이터 종류 하나 — 예: 배터리 전압 · 전류."""
    key: str
    title: str
    status: str = HAVE
    device: str = ""                                  # 장비와 주기: "주최측 에너지미터 · 100 Hz"
    facts: list[str] = field(default_factory=list)    # 데이터에서 바로 나온 사실 (개조식)
    raw: list[FileRef] = field(default_factory=list)
    code: list[CodeRef] = field(default_factory=list)
    outputs: list[FileRef] = field(default_factory=list)
    exports: list[Export] = field(default_factory=list)
    channels: list[Channel] = field(default_factory=list)
    time_keys: list[str] = field(default_factory=list)     # 시간 그림에 그릴 채널 (3개까지)
    trace_keys: list[str] = field(default_factory=list)    # 거리 그림 (최속 랩)
    map_key: str | None = None                             # 지도에 색으로 입힐 채널
    lap_columns: list[LapColumn] = field(default_factory=list)
    gg: pd.DataFrame | None = None                         # long_g, lat_g — 분포 그림
    checks: list[str] = field(default_factory=list)        # 독립된 측정으로 확인한 것
    cautions: list[str] = field(default_factory=list)
    series: list[Channel] = field(default_factory=list)    # 엔진이 내보낼 원래 주기의 값 (비면 channels). 그림용 channels 는 고른 값일 수 있다
    findings: list[dict] = field(default_factory=list)     # 위치가 있는 품질 경고 {level, code, message, t_ranges} — t_ranges 는 세션 시간

    def channel(self, key: str) -> Channel:
        return next(c for c in self.channels if c.key == key)


@dataclass
class Missing:
    """이 세션에 없는 데이터. status = SEEK(가진 사람이 있으면 받는다) 또는 NONE(애초에 기록되지 않았다)."""
    data: str
    status: str
    reason: str
    ask: str = ""
    block: str = ""                   # 들어왔다면 어느 데이터 종류(DataBlock.key)가 되나 — "bms" · "motor" … (모르면 빈 값)


@dataclass
class Figure:
    key: str
    title: str
    how: str                          # 읽는 법 — 그림 안에는 해석을 넣지 않고 여기 적는다
    path: Path


@dataclass
class Session:
    sid: str                          # "LEF-26_2026-08-30_S1" — 다시 만들어도 같은 세션을 가리키는 이름
    car: str
    day: str                          # "2026-08-30"
    name: str                         # "내구 1"
    start: pd.Timestamp
    duration_s: float
    event_kind: str = ""              # "대회" · "테스트위크"
    event_title: str = ""
    discipline: str = ""              # "내구" · "가속" …
    place: str = ""
    driver: str = ""
    event_id: str = ""                # "LEF-26/2026-08-30_yeonggwang"
    stats: dict = field(default_factory=dict)       # distance_km, laps_n, best_lap_s, vmax_kmh, net_wh, wh_per_km, i_max_a
    laps: pd.DataFrame | None = None                # lap, t_start_s, t_end_s (세션 시간), lap_time_s + 블록별 열
    track: pd.DataFrame | None = None               # t_s, x_m, y_m, dist_m — 세션 전체 궤적
    gate: dict | None = None                        # 계측선: x_m, y_m (track 과 같은 평면), heading_deg (북 = 0, 시계 방향), half_width_m
    frame_id: str = ""                              # 평면 좌표 원점 · 계측선의 이름. 같으면 지도 · 랩 거리를 겹칠 수 있다
    blocks: list[DataBlock] = field(default_factory=list)
    missing: list[Missing] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    made: dict = field(default_factory=dict)        # generated, commit, command

    @property
    def title(self) -> str:
        return f"{self.car} · {self.day[5:]} · {self.name}"

    @property
    def best_lap(self) -> pd.Series | None:
        if self.laps is None or self.laps.empty:
            return None
        return self.laps.loc[self.laps["lap_time_s"].idxmin()]

    def index_row(self) -> dict:
        """세션 표(노션 데이터베이스)의 한 줄."""
        s = self.stats
        return {"sid": self.sid, "title": self.title, "car": self.car, "start": self.start.round("ms").isoformat(), "day": self.day,
                "event_kind": self.event_kind, "discipline": self.discipline, "driver": self.driver,
                "duration_min": round(self.duration_s / 60, 1), "distance_km": s.get("distance_km"), "laps": s.get("laps_n"),
                "best_lap_s": s.get("best_lap_s"), "have": [b.title for b in self.blocks if b.status == HAVE],
                "warn": [b.title for b in self.blocks if b.status == WARN],
                "seek": [m.data for m in self.missing if m.status == SEEK],
                "none": [m.data for m in self.missing if m.status == NONE]}


# ---------------------------------------------------------------- 블록 목록

def _num(v, digits=1) -> str:
    if v is None or (isinstance(v, float) and np.isnan(v)):
        return "–"
    return f"{float(v):,.{digits}f}"


def size_text(n: int) -> str:
    return f"{n / 1e9:.1f} GB" if n >= 1e9 else f"{n / 1e6:.1f} MB" if n >= 1e5 else f"{max(n / 1e3, 0.1):.0f} KB"


def _summary(s: Session) -> list[tuple[str, str]]:
    st = s.stats
    end = s.start + pd.Timedelta(seconds=s.duration_s)
    rows = [("차량", s.car), ("날짜 · 시각", f"{s.day} {s.start:%H:%M:%S} ~ {end:%H:%M:%S} (KST)"),
            ("행사", " · ".join(x for x in (s.event_kind, s.event_title) if x)), ("종목", s.discipline), ("장소", s.place),
            ("드라이버", s.driver), ("주행 시간", f"{s.duration_s / 60:.1f} 분 ({s.duration_s:.0f} s)")]
    for key, label, unit, digits in (("distance_km", "거리", "km", 2), ("laps_n", "완주 랩", "랩", 0), ("best_lap_s", "최속 랩", "s", 2),
                                     ("vmax_kmh", "최고 속도", "km/h", 1), ("net_wh", "순 에너지 (방전 − 회생)", "Wh", 0),
                                     ("wh_per_km", "거리당 에너지", "Wh/km", 0), ("i_max_a", "전류 최대", "A", 0)):
        if st.get(key) is not None:
            rows.append((label, f"{_num(st[key], digits)} {unit}"))
    return [(k, v) for k, v in rows if v]


def _lap_table(s: Session, b: DataBlock) -> tuple[list[str], list[list[str]]] | None:
    cols = [c for c in b.lap_columns if c.key != "lap_time_s" and s.laps is not None and c.key in s.laps.columns and s.laps[c.key].notna().any()]
    if not cols:
        return None
    best = s.best_lap["lap"]
    head = ["랩", "랩타임 (s)"] + [f"{c.label} ({c.unit})" if c.unit else c.label for c in cols]
    rows = []
    for _, r in s.laps.iterrows():
        mark = " ★" if r["lap"] == best else ""
        rows.append([f"{int(r['lap'])}{mark}", _num(r["lap_time_s"], 2)] + [_num(r[c.key], c.digits) for c in cols])
    return head, rows


def page_blocks(s: Session, figures: dict[str, list[Figure]], files_dir: Path | None = None) -> list[dict]:
    """Session → 문서 블록 목록. figures = {블록 key: 그 블록의 그림들}."""
    out: list[dict] = [{"t": "kv", "rows": _summary(s)}]
    if s.notes:
        out.append({"t": "ul", "items": s.notes})

    out.append({"t": "h", "level": 2, "text": "데이터 현황"})
    rows = [[b.title, b.status, b.device] for b in s.blocks] + [[m.data, m.status, m.reason] for m in s.missing]
    out.append({"t": "table", "head": ["데이터", "상태", "장비 · 주기 / 이유"], "rows": rows})
    out.append({"t": "p", "text": f"{SEEK} = 우리에게 파일이 없음. 가진 분이 있으면 받고 싶은 데이터 · {NONE} = 그 주행에서 애초에 기록되지 않은 데이터."})

    n_fig = 0
    for i, b in enumerate(s.blocks, 1):
        out.append({"t": "h", "level": 2, "text": f"{i}. {b.title}"})
        if b.device:
            out.append({"t": "p", "text": f"장비: {b.device}"})
        if b.facts:
            out.append({"t": "ul", "items": b.facts})
        out.append({"t": "h", "level": 3, "text": "원본 → 코드 → 결과물"})
        if b.raw:
            out.append({"t": "files", "title": "원본", "files": [_file(f, files_dir) for f in b.raw]})
        if b.code:
            out.append({"t": "codes", "title": "변환 · 분석 코드", "codes": [{"key": c.key, "title": c.title, "role": c.role} for c in b.code]})
        if b.outputs:
            out.append({"t": "files", "title": "결과물", "files": [_file(f, files_dir) for f in b.outputs]})
        figs = figures.get(b.key, [])
        if figs:
            out.append({"t": "h", "level": 3, "text": "그래프"})
        for f in figs:
            n_fig += 1
            out.append({"t": "image", "file": f"figures/{f.path.name}", "caption": f"그림 {n_fig}. {f.title} — {f.how}"})
        table = _lap_table(s, b)
        if table:
            out.append({"t": "h", "level": 3, "text": "랩별 값"})
            out.append({"t": "table", "head": table[0], "rows": table[1]})
            out.append({"t": "p", "text": "★ = 이 세션의 최속 랩. 랩은 계측선 통과부터 다음 통과까지 (나가는 랩 · 들어오는 랩은 표에 없음)."})
        if b.checks or b.cautions:
            out.append({"t": "h", "level": 3, "text": "검증 · 주의"})
            out.append({"t": "ul", "items": [f"검증: {c}" for c in b.checks] + [f"주의: {c}" for c in b.cautions]})

    seek = [m for m in s.missing if m.status == SEEK]
    if seek:
        out.append({"t": "h", "level": 2, "text": "찾는 데이터"})
        out.append({"t": "p", "text": "아래 데이터는 우리에게 파일이 없음. 가지고 있거나 어디 있는지 아는 분은 DAQ 파트로 알려 주세요."})
        out.append({"t": "table", "head": ["데이터", "없는 이유", "이런 분을 찾습니다"], "rows": [[m.data, m.reason, m.ask or "–"] for m in seek]})

    out.append({"t": "h", "level": 2, "text": "이 페이지에 대해"})
    made = s.made
    out.append({"t": "ul", "items": [x for x in (
        "코드가 데이터에서 자동으로 만든 페이지. 손으로 고친 내용은 다음 생성 때 사라짐 — 고칠 곳이 있으면 DAQ 파트에 알려 주세요",
        "시간축은 세션 시작(차가 움직이기 시작한 때)부터의 시간. 랩은 계측선 통과 기준",
        f"생성: {made['generated']}" if made.get("generated") else "",
        f"코드 버전: {made['commit']}" if made.get("commit") else "",
        f"다시 만드는 명령: `{made['command']}`" if made.get("command") else "",
        f"세션 ID: `{s.sid}`") if x]})
    return out


def _file(f: FileRef, files_dir: Path | None) -> dict:
    rel = ""
    if f.attached and files_dir is not None:
        rel = f"files/{f.name}" if f.path.parent == files_dir else os.path.relpath(f.path, files_dir.parent).replace("\\", "/")
    return {"label": f.label, "name": f.name, "size": size_text(f.size_bytes) if f.size_bytes else "", "note": f.note,
            "where": f.where, "file": rel, "path": str(f.path) if f.attached else ""}


# ---------------------------------------------------------------- 로컬 md (원본)

def to_markdown(title: str, blocks: list[dict], code_dir: str = "") -> str:
    """블록 목록 → 로컬 md. code_dir = 코드 페이지 폴더까지의 상대 경로."""
    out = [f"# {title}", ""]
    for b in blocks:
        t = b["t"]
        if t == "h":
            out += [f"{'#' * b['level']} {b['text']}", ""]
        elif t == "p":
            out += [b["text"], ""]
        elif t == "ul":
            out += [f"- {x}" for x in b["items"]] + [""]
        elif t == "kv":
            out += ["| | |", "|---|---|"] + [f"| {k} | {v} |" for k, v in b["rows"]] + [""]
        elif t == "table":
            out += ["| " + " | ".join(b["head"]) + " |", "|" + "---|" * len(b["head"])]
            out += ["| " + " | ".join(str(c).replace("|", "/") for c in r) + " |" for r in b["rows"]] + [""]
        elif t == "image":
            out += [f"![{b['caption']}]({b['file']})", "", f"*{b['caption']}*", ""]
        elif t == "files":
            out.append(f"**{b['title']}**")
            out += ["- " + _file_line(f, lambda f: f"[{f['name']}]({f['file']})" if f["file"] else f"`{f['name']}`") for f in b["files"]]
            out.append("")
        elif t == "codes":
            out.append(f"**{b['title']}**")
            out += [f"- [{c['title']}]({code_dir}/{c['key']}.md)" + (f" — {c['role']}" if c["role"] else "") for c in b["codes"]] + [""]
        elif t == "code":
            out += [f"```{b['lang']}", b["text"].rstrip(), "```", ""]
    return "\n".join(out).rstrip() + "\n"


def _file_line(f: dict, name) -> str:
    bits = [x for x in (f["label"], f["size"], f["note"]) if x]
    tail = f" · 있는 곳: {f['where']}" if f["where"] else ""
    return f"{name(f)} — " + " · ".join(bits) + tail


# ---------------------------------------------------------------- 노션용 마크다운 (사본)

_ESCAPE = str.maketrans({c: "\\" + c for c in "~$[]<>{}|^"})


def notion_text(text: str) -> str:
    """노션 마크다운에서 뜻이 있는 글자를 막는다. 굵게(**) 와 코드(`) 는 의도한 것이라 그대로 두고, 코드 안은 건드리지 않는다."""
    parts = str(text).split("`")
    return "`".join(p if i % 2 else p.translate(_ESCAPE) for i, p in enumerate(parts))


def to_notion(blocks: list[dict], uploads: dict[str, str] | None = None, pages: dict[str, str] | None = None) -> str:
    """블록 목록 → 노션용 마크다운. uploads = {파일 상대 경로: 업로드 ID}, pages = {코드 key: 노션 페이지 주소}.

    짝이 없는 그림 · 파일 · 코드는 빼지 않고 글로 남긴다 (무엇이 빠졌는지 페이지에서 보이게).
    """
    uploads, pages, x = uploads or {}, pages or {}, notion_text
    out: list[str] = []
    for b in blocks:
        t = b["t"]
        if t == "h":
            out.append(f"{'#' * b['level']} {x(b['text'])}")
        elif t == "p":
            out.append(x(b["text"]))
        elif t == "ul":
            out += [f"- {x(i)}" for i in b["items"]]
        elif t in ("kv", "table"):
            head = [] if t == "kv" else [b["head"]]
            attrs = 'header-column="true"' if t == "kv" else 'header-row="true"'
            out.append(f"<table {attrs}>")
            for r in head + [list(r) for r in b["rows"]]:
                out.append("\t<tr>\n" + "\n".join(f"\t\t<td>{x(c)}</td>" for c in r) + "\n\t</tr>")
            out.append("</table>")
        elif t == "image":
            up = uploads.get(b["file"])
            out.append(f'<image src="file-upload://{up}">{x(b["caption"])}</image>' if up else x(f"(그림 없음: {b['caption']})"))
        elif t == "files":
            out.append(f"**{x(b['title'])}**")
            for f in b["files"]:
                up = uploads.get(f["file"]) if f["file"] else None
                line = _file_line(f, lambda f: f"`{f['name']}`")
                if up and not f["name"].lower().endswith(NOTION_FILE_TYPES):
                    line += " · zip 으로 묶어 붙임"            # 노션이 받지 않는 형식(.log 등)은 묶어서 올린다
                out.append(f'<file src="file-upload://{up}">{x(line)}</file>' if up else f"- {x(line)}")
        elif t == "codes":
            out.append(f"**{x(b['title'])}**")
            for c in b["codes"]:
                link = f'<mention-page url="{pages[c["key"]]}">{x(c["title"])}</mention-page>' if c["key"] in pages else x(c["title"])
                out.append(f"- {link}" + (f" — {x(c['role'])}" if c["role"] else ""))
        elif t == "code":
            view, src = uploads.get(b.get("html", "")), uploads.get(b.get("file", ""))
            if view:                                            # 올려 둔 HTML 을 페이지 안에 끼워 보여 준다 (코드를 다시 옮겨 적지 않는다)
                out.append(f'<embed src="file-upload://{view}">{x(b["file"])}</embed>')
            else:                                               # 코드 블록 안은 글자 그대로 (막지 않는다)
                out += [f"```{b['lang']}", b["text"].rstrip(), "```"]
            if src:
                out.append(f'<file src="file-upload://{src}">{x(b["file"])} (원본 파일)</file>')
    return "\n".join(out) + "\n"


# ---------------------------------------------------------------- 코드 보기 (HTML)

CODE_CSS = """
:root{--code:"IBM Plex Mono",Pretendard,Consolas,monospace;--surface:#fcfcfb;--ink:#0b0b0b;--muted:#898781;--line:#e1e0d9}
html,body{margin:0;background:var(--surface);color:var(--ink)}
.wrap{display:flex;font:12.5px/1.55 var(--code);padding:12px 0}
pre{margin:0;font:inherit;white-space:pre;tab-size:4}
.n{color:var(--muted);text-align:right;padding:0 12px 0 14px;border-right:1px solid var(--line);user-select:none}
.src{padding:0 16px;overflow-x:auto;flex:1}
.c{color:#6b6a65}.s{color:#0c7a4d}.k{color:#2a78d6;font-weight:600}
"""


def code_html(title: str, text: str) -> str:
    """파이썬 소스 → 줄 번호가 붙은 HTML 한 장 (주석 · 문자열 · 예약어만 색을 달리한다). 노션 페이지에 끼워 보여 주는 용도."""
    import html
    import io
    import keyword
    import tokenize

    lines = text.splitlines(keepends=True)
    offs = np.cumsum([0] + [len(x) for x in lines])
    spans = []
    try:
        for tok in tokenize.generate_tokens(io.StringIO(text).readline):
            cls = ("c" if tok.type == tokenize.COMMENT else "s" if tok.type == tokenize.STRING
                   else "k" if tok.type == tokenize.NAME and keyword.iskeyword(tok.string) else None)
            if cls:
                spans.append((int(offs[tok.start[0] - 1]) + tok.start[1], int(offs[tok.end[0] - 1]) + tok.end[1], cls))
    except (tokenize.TokenError, IndentationError):
        spans = []                                              # 색을 못 입혀도 코드는 그대로 보여 준다
    out, pos = [], 0
    for a, b, cls in spans:
        out += [html.escape(text[pos:a]), f'<span class="{cls}">{html.escape(text[a:b])}</span>']
        pos = b
    out.append(html.escape(text[pos:]))
    numbers = chr(10).join(str(i) for i in range(1, len(lines) + 1))
    return ("<!doctype html><html lang=ko><head><meta charset=utf-8>"
            '<link rel="stylesheet" href="https://cdn.jsdelivr.net/gh/orioncactus/pretendard@v1.3.9/dist/web/static/pretendard.min.css">'
            f"<title>{html.escape(title)}</title><style>{CODE_CSS}</style></head><body><div class=wrap>"
            f'<pre class="n">{numbers}</pre><pre class="src">{"".join(out)}</pre></div></body></html>')


# ---------------------------------------------------------------- 쓰기

def write_session(s: Session, out_dir: Path, code_dir: Path | None = None) -> dict:
    """세션 하나를 out_dir 에 쓴다 → 세션 표의 한 줄 (index_row + 올릴 파일 목록).

    out_dir/page.md · page.json · figures/*.png · files/*.csv
    """
    from . import catalog_charts                  # matplotlib 은 그릴 때만 불러온다

    files_dir = out_dir / "files"
    files_dir.mkdir(parents=True, exist_ok=True)
    for b in s.blocks:
        for e in b.exports:
            path = files_dir / e.name
            e.table.to_csv(path, index=False, encoding="utf-8-sig", lineterminator="\n")
            b.outputs.insert(0, FileRef(e.label, e.name, path, path.stat().st_size, e.note))
        b.exports = []
    figures = catalog_charts.draw_session(s, out_dir / "figures")
    blocks = page_blocks(s, figures, files_dir)
    rel_code = os.path.relpath(code_dir, out_dir).replace("\\", "/") if code_dir else "code"
    (out_dir / "page.md").write_text(to_markdown(s.title, blocks, rel_code), encoding="utf-8", newline="\n")
    row = s.index_row()
    row["uploads"] = sorted({b["file"] for b in blocks if b["t"] == "image"}
                            | {f["file"] for b in blocks if b["t"] == "files" for f in b["files"] if f["file"]})
    (out_dir / "page.json").write_text(json.dumps({"title": s.title, "row": row, "blocks": blocks}, ensure_ascii=False, indent=1),
                                       encoding="utf-8", newline="\n")
    return row
