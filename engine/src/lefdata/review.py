"""데이터 넣기 확인 페이지 — 이벤트 하나를 넣은 결과를 사람이 눈으로 보고 고칠 곳을 고르는 HTML 한 장.

`lefdata review --event <이벤트>` → `<이벤트>/derived/review/index.html`.

읽는 것 (만들지 않는다 — 변환 · 세션 만들기가 끝난 뒤에 부른다):
- `normalized/<레코딩>/meta.json` — 레코딩마다 길이 · 장비 · 시각 · 차량 설정 판
- 세션 저장소 `<store>/<세션 ID>/session.json` · `series/*.parquet` — 그 이벤트의 세션
- `catalog_notes.toml` — 기준 시계 이름(추정인지) · 전원 켬 시각 (켠 뒤 시각만 있는 레코딩을 시간축에 놓을 때)

페이지 순서: 정해 줄 것 (추정 시각 · 미분류 종목 · 세션이 없는 레코딩 · 경고) → 날마다 시간축 (레코딩 · 세션)
→ 세션 카드 (숫자 · 미리보기 그래프) → 레코딩 표. 사용자는 이걸 보고 고칠 것을 말로 주고, 엔진이 메모를 고쳐 세션을 다시 만든다.
"""

from __future__ import annotations

import html
import json
import re
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
import pandas as pd

from . import catalog_build as cb

TZ = "Asia/Seoul"
SPARK_BINS = 160
PREVIEW = [("motor.rpm", "회전수", "rpm"), ("gps.speed_kmh", "속도", "km/h"), ("battery.current_a", "배터리 전류", "A"),
           ("battery.power_kw", "배터리 전력", "kW"), ("bms.dcl_a", "BMS 방전 한계", "A")]   # 앞에서부터 있는 것 둘
ESTIMATED = re.compile(r"추정|가정")
LONE_SHOWN = 10


@dataclass
class Rec:
    rid: str
    device: str
    duration_s: float
    start: pd.Timestamp | None            # 기준 시계 (모르면 None)
    config: dict = field(default_factory=dict)
    sessions: list[str] = field(default_factory=list)

    @property
    def end(self):
        return None if self.start is None else self.start + pd.Timedelta(seconds=self.duration_s)


@dataclass
class Sess:
    sid: str
    summary: dict
    findings: list[dict]
    previews: list[tuple[str, str, np.ndarray | None, float | None]]   # (이름, 단위, 칸마다 최댓값, 세션 최댓값)
    text: str                                                              # session.json 전체 (레코딩 이름 찾기)

    @property
    def start(self):
        return pd.Timestamp(self.summary["start"]).tz_convert(TZ)

    @property
    def end(self):
        return self.start + pd.Timedelta(seconds=float(self.summary.get("duration_s") or 0))


def _ts(x) -> pd.Timestamp | None:
    if not x:
        return None
    t = pd.Timestamp(x)
    return t.tz_localize(TZ) if t.tzinfo is None else t.tz_convert(TZ)


def read_recordings(event_dir: Path, notes: dict) -> list[Rec]:
    """레코딩마다 기준 시계로 본 시작 — 세션 만들기와 같은 길 (`catalog_build._recordings` · `_Clocks`: 장비 시계 차 · 전원 켬).

    메모에 기준 시계([event] clock)가 없으면 레코딩 자기 시각 그대로. 시계 차를 모르는 장비 · 켠 뒤 시각만 있고 전원 켬을 모르는 레코딩은 시각 없음.
    """
    clocks = cb._Clocks(notes) if (notes.get("event") or {}).get("clock") else None
    placed = {}
    for x in cb._recordings(event_dir):
        if clocks is not None and clocks.known(x):
            placed[x.rec] = (x.device, pd.Timestamp(x.t0 + clocks.offset(x), unit="s", tz="UTC").tz_convert(TZ))
        elif clocks is None and not x.boot:
            placed[x.rec] = (x.device, pd.Timestamp(x.t0, unit="s", tz="UTC").tz_convert(TZ))
        else:
            placed[x.rec] = (x.device, None)
    recs = []
    for meta_path in sorted((event_dir / "normalized").glob("*/meta.json")):
        m = json.loads(meta_path.read_text(encoding="utf-8"))
        t = m.get("time") or {}
        rid = meta_path.parent.name
        device, start = placed.get(rid, ((m.get("source") or {}).get("logger") or "", None))
        dur = t.get("duration_s")
        if dur is None and t.get("t_abs_start") and t.get("t_abs_end"):
            dur = (pd.Timestamp(t["t_abs_end"]) - pd.Timestamp(t["t_abs_start"])).total_seconds()
        recs.append(Rec(rid, str(device), float(dur or 0.0), start, m.get("config") or {}))
    return recs


def _envelope(t: np.ndarray, v: np.ndarray, dur: float) -> np.ndarray:
    out = np.full(SPARK_BINS, np.nan)
    ok = np.isfinite(v) & np.isfinite(t)
    if dur <= 0 or not ok.any():
        return out
    k = np.clip((t[ok] / dur * SPARK_BINS).astype(int), 0, SPARK_BINS - 1)
    np.fmax.at(out, k, v[ok])
    return out


def read_sessions(store: Path, event_id: str) -> list[Sess]:
    out = []
    for p in sorted(Path(store).glob("*/session.json")):
        if p.parent.name.startswith("_"):
            continue
        text = p.read_text(encoding="utf-8")
        doc = json.loads(text)
        s = doc["summary"]
        if s.get("event_id") != event_id:
            continue
        chans = {c["channel_id"]: c for c in doc.get("channels", [])}
        previews = []
        for cid, label, unit in PREVIEW:
            if cid not in chans or len(previews) == 2:
                continue
            df = pd.read_parquet(p.parent / chans[cid]["_file"])
            col = cid.split(".", 1)[1]
            if col not in df or "t_s" not in df:
                continue
            v = df[col].to_numpy(float)
            fin = v[np.isfinite(v)]
            previews.append((label, unit, _envelope(df["t_s"].to_numpy(float), v, float(s.get("duration_s") or 0)),
                             float(fin.max()) if fin.size else None))
        out.append(Sess(s["sid"], s, doc.get("findings") or [], previews, text))
    out.sort(key=lambda x: x.start)
    return out


def link(recs: list[Rec], sessions: list[Sess], notes: dict) -> None:
    """레코딩 ↔ 세션: 세션 파일에 레코딩 이름이 적혀 있거나, 메모의 시각 구간 세션이 그 레코딩이거나, 시간이 겹치면."""
    named = {}
    for ts in notes.get("timed_session") or []:
        if ts.get("recording"):
            named.setdefault(ts["recording"], set()).add(ts.get("name"))
    for r in recs:
        for s in sessions:
            hit = r.rid in s.text or s.summary.get("name") in named.get(r.rid, ())
            if not hit and r.start is not None and r.duration_s > 0:
                hit = r.start < s.end and r.end > s.start
            if hit:
                r.sessions.append(s.sid)


# ---------------------------------------------------------------- 정해 줄 것

def decisions(recs: list[Rec], sessions: list[Sess], notes: dict, multi: bool = False) -> list[dict]:
    """[{kind, title, body}] — kind: est (추정) · gray (정할 것) · warn (확인 거리)."""
    out = []
    ev = notes.get("event") or {}
    label = str(ev.get("clock_label") or "")
    if ESTIMATED.search(label):
        out.append({"kind": "est", "tag": "추정", "title": "기준 시각",
                    "body": f"{label} — 이 페이지의 시각(*)이 전부 이 값을 따름. 실제 시각 · 영상이 오면 그 값만 고치면 됨 (세션 ID 그대로)"})
    unsorted = [s for s in sessions if s.summary.get("discipline") in (None, "", "미분류")]
    if unsorted:
        out.append({"kind": "gray", "tag": "미분류", "title": f"종목 — {len(unsorted)}개 세션",
                    "body": "종목을 못 가린 세션: " + " · ".join(_short(s.sid, multi) for s in unsorted) + ". 세션마다 종목을 알려 주면 메모에 적음"})
    lone = [r for r in recs if not r.sessions and r.duration_s >= 1.0]
    empty = sum(1 for r in recs if not r.sessions and r.duration_s < 1.0)
    if lone:
        shown = " · ".join(f"{r.rid} ({_dur(r.duration_s)})" for r in lone[:LONE_SHOWN])
        more = f" 외 {len(lone) - LONE_SHOWN}개" if len(lone) > LONE_SHOWN else ""
        out.append({"kind": "gray", "tag": "세션 없음", "title": f"레코딩 {len(lone)}개",
                    "body": f"{shown}{more} — 세션에 안 들어감 (피트 · 충전 · 짧은 켬일 수 있음). 넣어야 하면 알려 주기"
                            + (f". 1 초 미만 빈 레코딩 {empty}개는 표에만" if empty else "")})
    warns: dict[str, list[tuple[str, str]]] = {}
    for s in sessions:
        for f in s.findings:
            if f.get("level") == "warn":
                warns.setdefault(f["code"], []).append((_short(s.sid, multi), f["message"]))
    for code, hits in warns.items():
        out.append({"kind": "warn", "tag": "경고", "title": f"{code} — {len(hits)}개 세션",
                    "body": f"{' · '.join(sorted({h[0] for h in hits}, key=_order))}. 예: {hits[0][1]}"})
    return out


# ---------------------------------------------------------------- 그리기

def _short(sid: str, multi: bool = False) -> str:
    """세션 ID 의 끝 (S3). 여러 날 이벤트는 날짜를 붙인다 (08-14 S3) — 날마다 S 번호가 새로 시작한다."""
    m = re.search(r"\d{4}-(\d{2}-\d{2})_(S\d+)$", sid)
    if m:
        return f"{m.group(1)} {m.group(2)}" if multi else m.group(2)
    return sid.rsplit("_", 1)[-1]


def _order(x: str):
    m = re.search(r"(\d+)$", x)
    return (x.rstrip("0123456789"), int(m.group(1)) if m else 0)


def _dur(s: float) -> str:
    return f"{s:.0f} s" if s < 120 else f"{s / 60:.1f} 분"


def _esc(x) -> str:
    return html.escape(str(x))


def _timeline(day, recs: list[Rec], sessions: list[Sess], est: bool) -> str:
    spans = [(r.start, r.end) for r in recs if r.start is not None] + [(s.start, s.end) for s in sessions]
    t0 = min(a for a, _ in spans).floor("10min")
    t1 = max(b for _, b in spans).ceil("10min")
    if t1 <= t0:
        t1 = t0 + pd.Timedelta(minutes=10)
    W, L, R = 920, 12, 12
    rows_rec = _lanes([(r.start, r.end) for r in recs if r.start is not None])
    rows_ses = _lanes([(s.start, s.end) for s in sessions])
    n_rec, n_ses = (max(rows_rec, default=-1) + 1), (max(rows_ses, default=-1) + 1)
    lane = 24
    top_rec = 26
    top_ses = top_rec + n_rec * lane + (20 if n_rec else 0)
    H = top_ses + n_ses * lane + 34
    span = (t1 - t0).total_seconds()
    X = lambda t: L + (W - L - R) * (t - t0).total_seconds() / span          # noqa: E731
    step = pd.Timedelta(minutes=10 if span <= 4 * 3600 else 60)
    out = [f'<svg viewBox="0 0 {W} {H}" role="img" aria-label="{day} 레코딩과 세션 시간축" class="timeline">']
    t = t0
    while t <= t1:
        x = X(t)
        out.append(f'<line x1="{x:.1f}" y1="18" x2="{x:.1f}" y2="{H - 26}" class="grid"/>'
                   f'<text x="{x:.1f}" y="{H - 10}" class="tick{" est" if est else ""}" text-anchor="middle">{t:%H:%M}</text>')
        t += step
    if n_rec:
        out.append(f'<text x="{L}" y="{top_rec - 8}" class="lane">레코딩</text>')
    out.append(f'<text x="{L}" y="{top_ses - 8}" class="lane">세션</text>')
    placed = [r for r in recs if r.start is not None]
    for r, k in zip(placed, rows_rec):
        x0, x1 = X(r.start), X(r.end)
        out.append(f'<rect x="{x0:.1f}" y="{top_rec + k * lane}" width="{max(x1 - x0, 3):.1f}" height="16" rx="2" '
                   f'class="rec{"" if r.sessions else " lone"}"><title>{_esc(r.rid)} · {_dur(r.duration_s)}</title></rect>')
    for s, k in zip(sessions, rows_ses):
        x0, x1 = X(s.start), X(s.end)
        y = top_ses + k * lane
        out.append(f'<rect x="{x0:.1f}" y="{y}" width="{max(x1 - x0, 3):.1f}" height="18" rx="3" class="ses{" chk" if s.summary.get("default_hidden") else ""}">'
                   f'<title>{_esc(s.sid)} · {_esc(s.summary.get("name", ""))}</title></rect>')
        wide = x1 - x0 > 34
        out.append(f'<text x="{(x0 + x1) / 2 if wide else x1 + 3:.1f}" y="{y + 13}" class="slabel{"" if wide else " out"}" '
                   f'text-anchor="{"middle" if wide else "start"}">{_short(s.sid)}</text>')
    out.append("</svg>")
    return "".join(out)


def _lanes(spans) -> list[int]:
    """겹치는 막대를 줄로 나눈다 (앞에서부터 빈 줄)."""
    ends: list[pd.Timestamp] = []
    out = []
    for a, b in spans:
        for i, e in enumerate(ends):
            if a >= e:
                ends[i] = b
                out.append(i)
                break
        else:
            ends.append(b)
            out.append(len(ends) - 1)
    return out


def _spark(vals, cls: str, top: float | None) -> str:
    if vals is None or not np.isfinite(vals).any():
        return '<div class="nodata">기록 없음</div>'
    top = top or float(np.nanmax(vals)) or 1.0
    w, h = 300, 54
    pts = [(i / (len(vals) - 1) * w, h - 2 - max(0.0, v) / top * (h - 6)) for i, v in enumerate(vals) if np.isfinite(v)]
    line = " ".join(f"{x:.1f},{y:.1f}" for x, y in pts)
    return (f'<svg viewBox="0 0 {w} {h}" class="spark {cls}" preserveAspectRatio="none" aria-hidden="true">'
            f'<polygon points="0,{h} {line} {w},{h}" class="fill"/><polyline points="{line}" class="stroke"/></svg>')


def _num(v, digits=0, unit=""):
    if v is None or (isinstance(v, float) and not np.isfinite(v)):
        return "-"
    return f"{v:,.{digits}f}<small>{unit}</small>"


def _card(s: Sess, tops: dict[str, float], est: bool, multi: bool = False) -> str:
    sm, st = s.summary, s.summary.get("stats") or {}
    disc = sm.get("discipline") or "미분류"
    pills = f'<span class="pill {"gray" if disc == "미분류" else "ok"}">{_esc(disc)}</span>'
    for f in s.findings:
        if f.get("level") == "warn":
            pills += f'<span class="pill warn" title="{_esc(f["message"])}">{_esc(f["code"])}</span>'
    nums = [("길이", _num(float(sm.get("duration_s") or 0) / 60, 1, "분")), ("랩", _num(sm.get("laps"))),
            ("베스트 랩", _num(sm.get("best_lap_s"), 2, "s")), ("최고 속도", _num(st.get("vmax_kmh"), 0, "km/h")),
            ("최대 전류", _num(st.get("i_max_a"), 0, "A")), ("순 에너지", _num(st.get("net_wh"), 0, "Wh"))]
    nums = [n for n in nums if n[1] != "-"] or nums[:1]
    charts = "".join(f'<div class="chart"><span class="clabel">{_esc(label)} <span class="peak">최대 {_num(peak, 0, unit)}</span></span>'
                     f'{_spark(env, f"c{i}", tops.get(label))}</div>'
                     for i, (label, unit, env, peak) in enumerate(s.previews))
    return (f'<article class="card"><header><h3>{_short(s.sid, multi)}</h3><span class="name">{_esc(sm.get("name", ""))}</span>'
            f'<span class="when{" est" if est else ""}">{s.start:%H:%M:%S} – {s.end:%H:%M:%S}{"*" if est else ""}</span></header>'
            f'<div class="pills">{pills}</div><dl class="nums">'
            + "".join(f"<div><dt>{k}</dt><dd>{v}</dd></div>" for k, v in nums)
            + f"</dl>{charts or '<div class=nodata>미리보기 채널 없음</div>'}</article>")


def _rec_row(r: Rec, multi: bool = False) -> str:
    c = r.config
    period = (f'{_esc(c.get("period"))} <span class="muted">({ {"date": "날짜로", "assigned": "지정", "nearest": "가장 가까운 판"}.get(c.get("basis"), c.get("basis")) })</span>'
              if c.get("period") else '<span class="muted">-</span>')
    mismatch = c.get("mismatch") or []
    return (f"<tr{' class=lone' if not r.sessions else ''}><td>{_esc(r.rid)}</td><td>{_esc(r.device)}</td>"
            f"<td class=num>{'-' if r.start is None else f'{r.start:%m-%d %H:%M:%S}'}</td><td class=num>{_dur(r.duration_s)}</td>"
            f"<td>{period}</td><td>{'<span class=warn-t>' + _esc('; '.join(mismatch)) + '</span>' if mismatch else '없음' if c.get('period') else '-'}</td>"
            f"<td>{' · '.join(_short(x, multi) for x in r.sessions) or '없음'}</td></tr>")


CSS = """
:root {
  --bg: #f3f5f4; --panel: #ffffff; --ink: #1b2320; --muted: #5a6964; --line: #d3dbd8;
  --accent: #0e6b60; --accent-soft: #d8ece8; --est: #a8660f; --est-soft: #f6ead6; --warn: #b2432a; --warn-soft: #f5e0da; --off: #b9c4c0;
  --sans: "IBM Plex Sans KR", "Apple SD Gothic Neo", "Malgun Gothic", sans-serif;
  --mono: "IBM Plex Mono", "IBM Plex Sans KR", Consolas, monospace;
}
@media (prefers-color-scheme: dark) { :root:not([data-theme="light"]) {
  --bg: #111614; --panel: #19201d; --ink: #e3ebe8; --muted: #93a39d; --line: #2b3632;
  --accent: #4db5a6; --accent-soft: #1d3632; --est: #e3a74c; --est-soft: #3a2e1b; --warn: #e2765d; --warn-soft: #3d231d; --off: #44514c; color-scheme: dark } }
:root[data-theme="dark"] {
  --bg: #111614; --panel: #19201d; --ink: #e3ebe8; --muted: #93a39d; --line: #2b3632;
  --accent: #4db5a6; --accent-soft: #1d3632; --est: #e3a74c; --est-soft: #3a2e1b; --warn: #e2765d; --warn-soft: #3d231d; --off: #44514c; color-scheme: dark }
body { background: var(--bg); color: var(--ink); font-family: var(--sans); font-size: 15px; line-height: 1.55; margin: 0;
  padding-left: max(16px, env(safe-area-inset-left, 0px)); padding-right: max(16px, env(safe-area-inset-right, 0px)); padding-block: 28px 48px; }
main { max-width: 980px; margin: 0 auto; display: grid; gap: 28px; }
h1 { font-size: 26px; line-height: 1.25; margin: 0; text-wrap: balance; }
h2 { font-size: 13px; letter-spacing: .08em; color: var(--muted); font-weight: 600; margin: 0 0 10px; }
.sub, .muted, .note { color: var(--muted); } .sub { margin: 6px 0 0; } .note { font-size: 13px; margin: 6px 0 0; }
.num, dd, .when, .tick, .mono { font-family: var(--mono); font-variant-numeric: tabular-nums; }
.decide { background: var(--panel); border: 1px solid var(--line); border-radius: 8px; padding: 18px 20px; }
.decide ol { margin: 0; padding-left: 20px; display: grid; gap: 10px; } .decide .body { display: block; color: var(--muted); font-size: 14px; }
.pill { display: inline-block; font-size: 12px; font-weight: 600; padding: 1px 8px; border-radius: 999px; margin-right: 6px; }
.pill.est { background: var(--est-soft); color: var(--est); } .pill.warn { background: var(--warn-soft); color: var(--warn); }
.pill.gray { background: var(--bg); color: var(--muted); border: 1px solid var(--line); } .pill.ok { background: var(--accent-soft); color: var(--accent); }
.est { color: var(--est); } .warn-t { color: var(--warn); }
.scroll { overflow-x: auto; }
.timeline { width: 100%; min-width: 640px; height: auto; display: block; }
.timeline .grid { stroke: var(--line); } .timeline .tick, .timeline .lane { fill: var(--muted); font-size: 11px; } .timeline .tick.est { fill: var(--est); }
.timeline .rec { fill: var(--off); } .timeline .rec.lone { fill: none; stroke: var(--warn); stroke-dasharray: 3 2; }
.timeline .ses { fill: var(--accent); } .timeline .ses.chk { fill: var(--off); } .timeline .slabel { fill: var(--panel); font-size: 11px; font-weight: 600; font-family: var(--mono); }
.timeline .slabel.out { fill: var(--muted); }
.cards { display: grid; grid-template-columns: repeat(auto-fill, minmax(290px, 1fr)); gap: 14px; }
.card { background: var(--panel); border: 1px solid var(--line); border-radius: 8px; padding: 14px 16px; display: grid; gap: 10px; min-width: 0; }
.card header { display: flex; flex-wrap: wrap; align-items: baseline; gap: 4px 10px; } .card h3 { margin: 0; font-size: 20px; }
.name { color: var(--muted); font-size: 13px; } .when { font-size: 12px; margin-left: auto; }
.nums { display: grid; grid-template-columns: repeat(3, 1fr); gap: 8px 10px; margin: 0; }
.nums dt { font-size: 11.5px; color: var(--muted); } .nums dd { margin: 0; font-size: 17px; font-weight: 500; } small { font-size: 11px; color: var(--muted); margin-left: 2px; }
.chart { display: grid; gap: 2px; } .clabel { font-size: 11.5px; color: var(--muted); } .peak { font-family: var(--mono); }
.spark { width: 100%; height: 54px; display: block; } .spark .stroke { fill: none; stroke-width: 1.4; vector-effect: non-scaling-stroke; }
.spark.c0 .stroke { stroke: var(--accent); } .spark.c0 .fill { fill: var(--accent-soft); }
.spark.c1 .stroke { stroke: var(--est); } .spark.c1 .fill { fill: var(--est-soft); }
.nodata { font-size: 12px; color: var(--muted); }
table { border-collapse: collapse; width: 100%; min-width: 760px; font-size: 13.5px; }
th, td { text-align: left; padding: 7px 8px; border-bottom: 1px solid var(--line); vertical-align: top; }
th { font-size: 12px; color: var(--muted); font-weight: 600; } td.num, th.num { text-align: right; } tr.lone td { color: var(--muted); }
"""


def render(event_dir: Path, store: Path) -> str:
    event_dir = Path(event_dir)
    notes = cb.load_notes(event_dir)
    event_id = f"{event_dir.parent.name}/{event_dir.name}"
    recs = read_recordings(event_dir, notes)
    sessions = read_sessions(store, event_id)
    link(recs, sessions, notes)
    est = bool(ESTIMATED.search(str((notes.get("event") or {}).get("clock_label") or "")))
    tops: dict[str, float] = {}
    for s in sessions:
        for label, _, _, peak in s.previews:
            if peak is not None:
                tops[label] = max(tops.get(label, 0.0), peak)
    days = sorted({s.start.date() for s in sessions} | {r.start.date() for r in recs if r.start is not None})
    multi = len({s.start.date() for s in sessions}) > 1
    items = decisions(recs, sessions, notes, multi)
    title = (sessions[0].summary.get("event_title") if sessions else None) or event_dir.name
    car = event_dir.parent.name
    periods = sorted({r.config["period"] for r in recs if r.config.get("period")})
    mism = sum(1 for r in recs if r.config.get("mismatch"))
    head_pill = ("" if not periods else '<span class="pill ok">판과 데이터 맞음</span>' if not mism
                 else f'<span class="pill warn">판과 다른 레코딩 {mism}개</span>')
    unplaced = [r for r in recs if r.start is None]
    lines = [f"<title>{_esc(car)} {_esc(event_dir.name)} 데이터 확인</title>",
             '<meta name="claude:viewport-fit-sides" content="cover">',
             '<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=IBM+Plex+Sans+KR:wght@400;500;600;700'
             '&family=IBM+Plex+Mono:wght@400;500&display=swap">',
             f"<style>{CSS}</style><main><header>",
             f"<h1>{_esc(car)} · {_esc(title)} — 넣은 데이터 확인</h1>",
             f'<p class="sub">레코딩 {len(recs)}개 · 세션 {len(sessions)}개'
             + (f" · 설정 판 {' · '.join('「' + _esc(p) + '」' for p in periods)} {head_pill}" if periods else "") + "</p></header>"]
    lines.append('<section class="decide"><h2>정해 줄 것</h2>' + (
        "<ol>" + "".join(f'<li><span class="pill {d["kind"]}">{_esc(d["tag"])}</span><b>{_esc(d["title"])}</b>'
                         f'<span class="body">{_esc(d["body"])}</span></li>' for d in items) + "</ol>"
        if items else '<p class="muted">없음 — 추정 시각 · 미분류 · 빠진 레코딩 · 경고 없음</p>') + "</section>")
    for day in days:
        rs = [r for r in recs if r.start is not None and r.start.date() == day]
        ss = [s for s in sessions if s.start.date() == day]
        lines.append(f'<section><h2>{day:%Y-%m-%d} 시간축 — 레코딩 {len(rs)} · 세션 {len(ss)}</h2>'
                     f'<div class="scroll">{_timeline(day, rs, ss, est)}</div></section>')
    lines.append('<p class="note">위 줄 = 레코딩 (점선 = 세션에 안 들어감) · 아래 줄 = 세션 (초록 = 주행 · 회색 = 점검)'
                 + (" · 시각*은 추정 기준 시계로 옮긴 값" if est else "")
                 + (f" · 시각을 몰라 시간축에 못 놓은 레코딩 {len(unplaced)}개 (아래 표)" if unplaced else "") + "</p>")
    drives = [s for s in sessions if not s.summary.get("default_hidden")]
    checks = [s for s in sessions if s.summary.get("default_hidden")]
    if drives:
        lines.append('<section><h2>세션</h2><div class="cards">' + "".join(_card(s, tops, est, multi) for s in drives) + "</div>"
                     '<p class="note">그래프 = 세션 안에서 칸마다 가장 큰 값 · 세로축은 모든 세션이 같음</p></section>')
    if checks:
        lines.append(f'<section><h2>점검 세션 {len(checks)}개 — 사이트 기본 목록에서 숨김</h2><p class="muted">'
                     + " · ".join(f'{_short(s.sid, multi)} ({s.start:%H:%M:%S}{"*" if est else ""}, {_dur(float(s.summary.get("duration_s") or 0))})'
                                  for s in checks)
                     + " — 켜기만 하고 달리지 않은 기록을 엔진이 묶은 것</p></section>")
    lines.append('<section><h2>레코딩</h2><div class="scroll"><table><thead><tr><th>레코딩</th><th>장비</th><th class="num">시작</th>'
                 '<th class="num">길이</th><th>설정 판</th><th>판과 다른 곳</th><th>세션</th></tr></thead><tbody>'
                 + "".join(_rec_row(r, multi) for r in recs) + "</tbody></table></div></section>")
    lines.append(f'<p class="note">고치는 법: 정해 줄 것에 답을 주면 엔진이 <span class="mono">{cb.NOTES_NAME}</span> 을 고치고 세션을 다시 만듦</p></main>')
    return "\n".join(lines)


def build(event_dir: Path, store: Path, out: Path | None = None) -> Path:
    out = Path(out) if out else Path(event_dir) / "derived" / "review" / "index.html"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(render(event_dir, store).encode("utf-8"))
    return out
