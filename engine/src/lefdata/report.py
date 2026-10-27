"""시험 주행 일일 보고서 — 자료 구조와 HTML 조립.

차량·로거가 달라도 같은 모양의 보고서가 나오게, 보고서가 받는 자료를 `DayReport` 하나로 고정한다.
차량별 코드(예: `lef26_report.build_day`)는 자기 데이터를 읽어 `DayReport` 를 채우기만 하고,
그림(`report_charts`)과 문서(`render_html`)는 `DayReport` 만 본다. LEF-27 은 `build_day` 만 새로 쓰면 된다.

    build_day(event_dir, day)  →  DayReport  →  write_report(...)  →  index.html + figures/*.png

채워지지 않은 항목은 그 절이 빠지거나 "기록 없음" 으로 나온다 (없는 것을 지어내지 않는다).

사람이 적는 내용(시험 목적 · 드라이버 · 세팅 · 판단 · 조치)은 데이터에서 나오지 않으므로
`<이벤트>/report_notes/<날짜>.toml` 에 따로 둔다. Run Sheet 에 적을 내용과 같다.

문체: 사내 시험 보고서처럼 개조식(명사형 종결)으로 쓴다. 사실 · 판단 · 조치를 한 문장에 섞지 않는다.
"""

from __future__ import annotations

import base64
import html
import shutil
import subprocess
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

import pandas as pd

OK, WARN, BAD, INFO = "정상", "주의", "결함", "참고"
STATUS_ICON = {OK: "●", WARN: "▲", BAD: "■", INFO: "–"}
STATUS_CLASS = {OK: "ok", WARN: "warn", BAD: "bad", INFO: "info"}


@dataclass
class Session:
    """주행 구간 하나 (차가 나갔다 들어올 때까지)."""
    key: str                          # "S1"
    kind: str                         # "gps" = 랩 기록 있음 · "logger" = 위치 기록 없음
    start: pd.Timestamp
    end: pd.Timestamp
    recording: str                    # 로거 레코딩
    video: str | None = None
    driver: str | None = None
    stats: dict = field(default_factory=dict)      # 구간 요약 (열 이름은 lef26_laps 의 값 열과 같다)
    laps: pd.DataFrame | None = None               # 랩마다 한 줄
    best_lap: pd.DataFrame | None = None           # 최속 랩의 궤적: dist_m, x_m, y_m, speed_kmh, long_g, lat_g
    best_lap_no: int | None = None

    @property
    def duration_s(self) -> float:
        return (self.end - self.start).total_seconds()


@dataclass
class DayReport:
    car: str
    day: str                          # "2026-08-13"
    place: str = ""
    sessions: list[Session] = field(default_factory=list)
    recordings: pd.DataFrame | None = None         # lane, name, start, end — 그날 장비가 기록한 구간
    series: dict[str, pd.DataFrame] = field(default_factory=dict)   # 이름 → (t, v) 1 초 간격
    thermistors: dict[str, pd.DataFrame] = field(default_factory=dict)   # 구간 이름 → (id, below_hot_c, n)
    gg: dict[str, pd.DataFrame] = field(default_factory=dict)       # 구간 → (long_g, lat_g)
    susp: dict[str, pd.DataFrame] = field(default_factory=dict)     # 구간 → (rl_mm, rr_mm)
    coverage: pd.DataFrame | None = None           # 구간 × 신호: 1 초 칸 중 표본이 있던 비율
    quality: list[dict] = field(default_factory=list)   # item, status, value, note
    events: list[dict] = field(default_factory=list)    # time, session, what, … — 주행 중 일어난 특이 사항
    facts: list[str] = field(default_factory=list)      # 데이터에서 바로 나온 사실 (개조식)
    kpis: list[dict] = field(default_factory=list)      # label, value, unit, note
    notes: dict = field(default_factory=dict)           # report_notes/<날짜>.toml
    sources: list[str] = field(default_factory=list)
    methods: list[str] = field(default_factory=list)

    def gps_sessions(self) -> list[Session]:
        return [s for s in self.sessions if s.kind == "gps" and s.laps is not None and len(s.laps)]


def load_notes(event_dir: Path, day: str) -> dict:
    """사람이 적은 내용. 파일이 없으면 빈 dict — 보고서에는 "기록 없음" 으로 나온다."""
    path = event_dir / "report_notes" / f"{day}.toml"
    return tomllib.loads(path.read_text(encoding="utf-8")) if path.exists() else {}


# ---------------------------------------------------------------- HTML

CSS = """
:root{--sans:Pretendard,"Apple SD Gothic Neo","Malgun Gothic",sans-serif;
  --page:#f9f9f7;--surface:#fcfcfb;--ink:#0b0b0b;--ink2:#52514e;--muted:#898781;--line:#e1e0d9;--rule:#c3c2b7;
  --accent:#2a78d6;--ok:#0ca30c;--warn:#fab219;--bad:#d03b3b}
*{box-sizing:border-box}
html{background:var(--page)}
body{margin:0 auto;max-width:1040px;padding:40px 32px 80px;font-family:var(--sans);color:var(--ink);font-size:15px;line-height:1.65;
  word-break:keep-all}
h1{font-size:28px;line-height:1.3;margin:0 0 6px;font-weight:700;letter-spacing:-0.01em}
h2{font-size:20px;margin:56px 0 14px;padding-top:18px;border-top:2px solid var(--ink);font-weight:700}
h3{font-size:16px;margin:30px 0 8px;font-weight:700}
p{margin:8px 0}
.doc-meta{color:var(--ink2);font-size:13.5px;margin:0 0 22px}
.doc-meta span+span::before{content:"·";margin:0 8px;color:var(--muted)}
table{border-collapse:collapse;width:100%;font-size:13px;margin:10px 0 6px}
th,td{padding:6px 8px;border-bottom:1px solid var(--line);text-align:left;vertical-align:top;white-space:nowrap}
th{font-weight:600;color:var(--ink2);border-bottom:1px solid var(--rule);vertical-align:bottom;line-height:1.3}
th small{display:block;font-weight:400;font-size:11.5px;color:var(--muted)}
td.num,th.num{text-align:right;font-variant-numeric:tabular-nums}
td.wrap,th.wrap{white-space:normal}
tr.best td{font-weight:700;background:#eef4fc}
table.kv th{width:140px}
table.kv td{white-space:normal}
.scroll{overflow-x:auto}
.kpis{display:grid;grid-template-columns:repeat(4,1fr);gap:12px;margin:18px 0 8px}
.kpi{background:var(--surface);border:1px solid var(--line);border-radius:8px;padding:14px 16px}
.kpi .label{font-size:12.5px;color:var(--ink2)}
.kpi .value{font-size:26px;font-weight:600;line-height:1.25;margin-top:2px}
.kpi .value small{font-size:14px;font-weight:500;color:var(--ink2);margin-left:3px}
.kpi .note{font-size:12px;color:var(--muted);margin-top:2px}
ul{margin:6px 0 10px;padding-left:20px}
li{margin:3px 0}
.fja{display:grid;grid-template-columns:1fr 1fr 1fr;gap:14px;margin:14px 0}
.fja>div{background:var(--surface);border:1px solid var(--line);border-radius:8px;padding:12px 16px}
.fja h4{margin:0 0 6px;font-size:13px;color:var(--ink2);font-weight:600}
.fja ul{margin:0;padding-left:18px;font-size:14px}
figure{margin:18px 0 22px}
figure img{width:100%;height:auto;display:block;border:1px solid var(--line);border-radius:8px;background:var(--surface)}
figure.narrow img{max-width:620px;margin:0 auto}
figcaption{font-size:13px;color:var(--ink2);margin-top:8px}
figcaption b{color:var(--ink)}
.status{font-weight:600}
.status i{font-style:normal;margin-right:5px}
.status.ok i{color:var(--ok)}.status.warn i{color:#c98500}.status.bad i{color:var(--bad)}.status.info i{color:var(--muted)}
.none{color:var(--muted);font-size:13px}
footer{margin-top:60px;padding-top:14px;border-top:1px solid var(--line);font-size:12.5px;color:var(--muted)}
@media (max-width:760px){body{padding:24px 16px 60px}.kpis{grid-template-columns:repeat(2,1fr)}.fja{grid-template-columns:1fr}}
@media print{html{background:#fff}body{max-width:none;padding:0;font-size:10.5pt}.kpis{grid-template-columns:repeat(4,1fr)}.fja{grid-template-columns:1fr 1fr 1fr}.fja ul{font-size:9.5pt}.kpi .value{font-size:18pt}h2{break-after:avoid;margin-top:28px}h3{break-after:avoid}
  figure,tr,.kpi,.fja>div{break-inside:avoid}figure img{border:0}.scroll{overflow:visible}table{font-size:8.5pt}th,td{padding:4px 5px}}
@page{size:A4;margin:15mm 13mm}
"""

E = html.escape
NUM, TEXT, WRAP = "num", "", "wrap"


def _fmt(v, nd=1, dash="–") -> str:
    """숫자 → 문자열. 없는 값은 줄표."""
    if v is None or v is pd.NA or (isinstance(v, float) and pd.isna(v)):
        return dash
    if isinstance(v, str):
        return E(v)
    return f"{float(v):,.{nd}f}"


def _hm(ts, seconds=False) -> str:
    return "–" if ts is None or pd.isna(ts) else pd.Timestamp(ts).strftime("%H:%M:%S" if seconds else "%H:%M")


def _table(head: list[tuple[str, str]], rows: list[list[str]], row_class=None, cls="") -> str:
    """head = [("이름|단위", NUM / TEXT / WRAP)]. rows 는 이미 이스케이프된 칸들."""
    def th(label, kind):
        name, _, unit = label.partition("|")
        return f'<th class="{kind}">{E(name)}' + (f"<small>{E(unit)}</small>" if unit else "") + "</th>"

    body = []
    for i, r in enumerate(rows):
        c = f' class="{row_class(i)}"' if row_class and row_class(i) else ""
        body.append(f"<tr{c}>" + "".join(f'<td class="{head[j][1]}">{v}</td>' for j, v in enumerate(r)) + "</tr>")
    return (f'<div class="scroll"><table class="{cls}"><thead><tr>{"".join(th(l, k) for l, k in head)}</tr></thead>'
            f'<tbody>{"".join(body)}</tbody></table></div>')


def _ul(items) -> str:
    items = [i for i in (items or []) if i]
    return "<ul>" + "".join(f"<li>{E(str(i))}</li>" for i in items) + "</ul>" if items else '<p class="none">기록 없음</p>'


class _Figures:
    """그림을 문서에 넣으면서 번호를 매긴다 (없는 그림은 건너뛰고 번호도 쓰지 않는다)."""

    def __init__(self, files: dict[str, Path], embed: bool):
        self.files, self.embed, self.n = files, embed, 0

    def __call__(self, key: str, title: str, how: str, narrow: bool = False) -> str:
        """how = 읽는 법. 그림 안에는 해석을 넣지 않고 여기 적는다."""
        if key not in self.files:
            return ""
        self.n += 1
        path = self.files[key]
        src = ("data:image/png;base64," + base64.b64encode(path.read_bytes()).decode()) if self.embed else f"figures/{path.name}"
        return (f'<figure{" class=narrow" if narrow else ""}><img src="{src}" alt="{E(title)}">'
                f"<figcaption><b>그림 {self.n}. {E(title)}</b> — {E(how)}</figcaption></figure>")


LAP_COLS = [  # (열, "머리글|단위", 소수 자리)
    ("lap", "랩", 0), ("lap_time_s", "랩타임|s", 2), ("vmax_kmh", "최고속도|km/h", 1),
    ("soc_start_pct", "SOC 시작|%", 1), ("soc_end_pct", "SOC 끝|%", 1), ("cell_min_v", "최저 셀|V", 3),
    ("vdc_min_v", "팩 전압 최저|V", 1), ("iq_peak_a", "전류 최대|A", 0), ("batt_high_c", "배터리 최고|℃", 0),
    ("t_motor_max_c", "모터|℃", 0), ("long_g_max", "가속|g", 2), ("long_g_min", "제동|g", 2),
    ("lat_g_left_max", "좌선회|g", 2), ("lat_g_right_max", "우선회|g", 2),
]
RUN_COLS = [
    ("duration_s", "주행 시간|s", 0), ("laps_n", "완주 랩", 0), ("turns", "회전수|바퀴", 1), ("best_lap_s", "최속 랩|s", 2),
    ("distance_km", "거리|km", 1), ("vmax_kmh", "최고속도|km/h", 1), ("long_g_max", "가속 최대|g", 2),
    ("long_g_min", "제동 최대|g", 2), ("lat_g_left_max", "좌선회 최대|g", 2), ("lat_g_right_max", "우선회 최대|g", 2),
]
BATT_COLS = [
    ("soc_start_pct", "SOC 시작|%", 1), ("soc_end_pct", "SOC 끝|%", 1), ("cell_min_v", "최저 셀|V", 3),
    ("batt_high_c", "배터리 최고|℃", 0), ("batt_low_c", "배터리 최저|℃", 0), ("batt_spread_c", "팩 내 편차|℃", 0),
]
DRIVE_COLS = [
    ("iq_cmd_peak_a", "전류 지령 최대|A", 0), ("iq_peak_a", "전류 최대|A", 0), ("iq_mean_a", "전류 평균|A", 0),
    ("vdc_max_v", "팩 전압 최고|V", 1), ("vdc_min_v", "팩 전압 최저|V", 1), ("t_motor_max_c", "모터|℃", 0), ("t_igbt_max_c", "인버터|℃", 1),
]


def _session_table(day: DayReport, cols, with_kind: bool = False) -> str:
    cols = [c for c in cols if any(pd.notna(s.stats.get(c[0])) for s in day.sessions)]
    head = [("구간", TEXT), ("시각", TEXT)] + ([("기록", TEXT)] if with_kind else []) + [(h, NUM) for _, h, _ in cols]
    rows = []
    for s in day.sessions:
        kind = ["랩 기록" if s.kind == "gps" else "로거 단독"] if with_kind else []
        rows.append([E(s.key), f"{_hm(s.start)} ~ {_hm(s.end)}"] + kind + [_fmt(s.stats.get(c), nd) for c, _, nd in cols])
    return _table(head, rows)


def _lap_table(s: Session) -> str:
    cols = [c for c in LAP_COLS if c[0] in s.laps.columns and s.laps[c[0]].notna().any()]
    best = s.laps["lap_time_s"].idxmin()
    idx = list(s.laps.index)
    rows = [[_fmt(r[c], nd) for c, _, nd in cols] for _, r in s.laps.iterrows()]
    return _table([(h, NUM) for _, h, _ in cols], rows, row_class=lambda i: "best" if idx[i] == best else "")


def _obs(notes: dict, key: str) -> str:
    """절마다 사람이 적은 관찰 (report_notes 의 [observations])."""
    items = notes.get("observations", {}).get(key)
    return "<h3>관찰</h3>" + _ul(items) if items else ""


def _events(day: DayReport) -> str:
    if not day.events:
        return ""
    rows = [[_hm(e["time"], seconds=True), E(e["session"]), E(e["what"]), _fmt(e.get("speed_kmh"), 0), _fmt(e.get("soc_pct"), 1),
             _fmt(e.get("cell_min_v"), 2), _fmt(e.get("vdc_min_v"), 0)] for e in day.events]
    return ("<h3>특이 사항</h3>" + _table([("시각", TEXT), ("구간", TEXT), ("내용", TEXT), ("차속|km/h", NUM), ("SOC|%", NUM),
                                        ("직전 30 초 최저 셀|V", NUM), ("직전 30 초 팩 전압 최저|V", NUM)], rows))


def _kv(pairs: list[tuple[str, str]]) -> str:
    return '<table class="kv"><tbody>' + "".join(
        f"<tr><th>{E(k)}</th><td>{E(v) if v else '<span class=none>기록 없음</span>'}</td></tr>" for k, v in pairs) + "</tbody></table>"


def render_html(day: DayReport, figs: dict[str, Path], embed: bool = True) -> str:
    """DayReport + 그림 파일들 → HTML 문서 한 장. embed = 그림을 문서 안에 넣는다 (파일 하나로 전달 가능)."""
    n = day.notes
    meta, cond, summary = n.get("meta", {}), n.get("conditions", {}), n.get("summary", {})
    title = meta.get("title") or f"{day.car} 시험 주행 결과 보고 — {day.day}"
    fig = _Figures(figs, embed)
    sec = iter(range(1, 50))
    h2 = lambda name: f"<h2>{next(sec)}. {E(name)}</h2>"
    head_bits = [f"시험일 {day.day}", day.place, f"작성 {meta['author']}" if meta.get("author") else "",
                 meta.get("written", ""), meta.get("version", "")]
    out = ["<!doctype html><html lang=ko><head><meta charset=utf-8><meta name=viewport content='width=device-width,initial-scale=1'>"
           f"<title>{E(title)}</title>"
           '<link rel="stylesheet" href="https://cdn.jsdelivr.net/gh/orioncactus/pretendard@v1.3.9/dist/web/static/pretendard.min.css">'
           f"<style>{CSS}</style></head><body>",
           f"<h1>{E(title)}</h1>",
           '<p class="doc-meta">' + "".join(f"<span>{E(x)}</span>" for x in head_bits if x) + "</p>"]

    # ---- 요약
    out.append(h2("요약"))
    if day.kpis:
        out.append('<div class="kpis">' + "".join(
            f'<div class="kpi"><div class="label">{E(k["label"])}</div><div class="value">{E(str(k["value"]))}'
            f'<small>{E(k.get("unit", ""))}</small></div><div class="note">{E(k.get("note", ""))}</div></div>'
            for k in day.kpis) + "</div>")
    out.append('<div class="fja">'
               f'<div><h4>확인된 사실</h4>{_ul(summary.get("facts") or day.facts[:6])}</div>'
               f'<div><h4>판단</h4>{_ul(summary.get("judgments"))}</div>'
               f'<div><h4>조치 필요 (제안)</h4>{_ul(summary.get("actions"))}</div></div>')

    # ---- 시험 개요
    out.append(h2("시험 개요"))
    out.append(_kv([("시험 목적", meta.get("purpose", "")), ("장소 · 코스", meta.get("course", "")),
                    ("차량 세팅", cond.get("setup", "")), ("타이어", cond.get("tires", "")),
                    ("날씨 · 노면", cond.get("weather", "")), ("드라이버", cond.get("drivers", "")),
                    ("계측 장비", cond.get("equipment", ""))]))
    out.append(fig("timeline", "하루 일정", "가로축은 시각(KST). 위에서부터 로거가 기록한 구간, 온보드 영상이 있는 구간, "
                   "차가 실제로 달린 구간. 주행 구간 위의 S1 ~ 은 아래 표의 구간 번호."))
    out.append("<h3>주행 구간</h3>" + _session_table(day, RUN_COLS, with_kind=True))
    out.append('<p class="none">랩 기록 = 위치(GPS) 기록이 있어 랩타임을 낸 구간 · 로거 단독 = 위치 기록 없이 로거만 남은 구간. '
               "회전수는 요레이트를 누적해 센 값(한 바퀴가 약 1 랩). 가속도는 0.5 초 평균의 최댓값.</p>")
    out.append(_events(day))

    # ---- 랩 기록
    gps = day.gps_sessions()
    if gps:
        out.append(h2("랩 기록"))
        out.append(fig("lap_times", "랩타임 추이", "가로축은 구간 안에서의 랩 순서, 세로축은 랩타임(낮을수록 빠름). 선 하나가 주행 구간 하나."))
        for s in gps:
            out.append(f"<h3>{E(s.key)} — {_hm(s.start)} ~ {_hm(s.end)} · 완주 {len(s.laps)}랩 · 최속 {_fmt(s.stats.get('best_lap_s'), 2)} s"
                       + (f" · {E(s.driver)}" if s.driver else "") + "</h3>")
            out.append(_lap_table(s))
        out.append('<p class="none">굵은 줄 = 구간 최속 랩. 가속도는 0.5 초 평균의 최댓값(센서 장착 기울기 보정 후). '
                   "전류 · 전압 · 배터리 값은 기록된 표본 중의 최대 · 최소.</p>")
        out.append("<h3>최속 랩 상세</h3>")
        out.append(fig("track_map", "최속 랩 주행선과 속도", "색이 진할수록 빠른 구간. 화살표는 진행 방향, 굵은 가로줄은 계측선(랩 시작 · 끝).",
                       narrow=True))
        out.append(fig("speed_trace", "구간별 최속 랩의 속도", "가로축은 계측선부터 달린 거리. 같은 지점에서 선이 위에 있을수록 그 지점을 빠르게 지난 랩."))
        out.append(fig("g_trace", "최속 랩의 가속도", "위는 앞뒤 방향(+ 가속, − 제동), 아래는 좌우 방향(+ 좌선회, − 우선회). "
                       "가로축은 앞 그림과 같은 거리."))
        out.append(_obs(n, "laps"))

    # ---- 차량 거동
    if day.gg or day.susp:
        out.append(h2("차량 거동"))
        out.append(fig("gg", "가속도 분포 (g-g 선도)", "가로는 좌우 가속도, 세로는 앞뒤 가속도. 색이 진할수록 그 가속도 상태에 오래 머묾. "
                       "색칠된 범위가 바깥 원에 가까울수록 타이어를 한계 가까이 쓴 것."))
        out.append(fig("susp", "리어 서스펜션 스트로크 분포", "가로축은 스트로크(mm, 압축하면 값이 작아짐), 세로축은 그 값에 머문 시간의 비율. "
                       "75 mm 는 센서 상한."))
        out.append(_obs(n, "dynamics"))

    # ---- 배터리
    if any(k in day.series for k in ("soc", "cell_min", "batt_high")):
        out.append(h2("배터리"))
        out.append(_session_table(day, BATT_COLS))
        out.append(fig("soc", "SOC 와 셀 전압", "위는 BMS 가 보고한 SOC, 아래는 가장 높은 셀과 가장 낮은 셀의 전압. 회색 띠는 주행 구간. "
                       "주행 중 낮은 셀 전압이 아래로 내려가는 폭이 부하에 의한 전압 강하."))
        out.append(fig("batt_temp", "배터리 온도", "써미스터 중 가장 뜨거운 곳 · 평균 · 가장 차가운 곳. 선 사이가 벌어질수록 팩 안 온도가 고르지 않음. "
                       "붉은 가로선은 규정 한계."))
        out.append(fig("thermistors", "써미스터별 온도 분포", "점 하나가 써미스터 하나. 세로축은 같은 순간 팩에서 가장 뜨거운 곳보다 몇 ℃ 낮았는지(구간 중앙값). "
                       "0 에 가까울수록 뜨거운 자리, 아래로 갈수록 차가운 자리."))
        out.append(_obs(n, "battery"))

    # ---- 구동계
    if any(k in day.series for k in ("vdc_med", "t_motor")):
        out.append(h2("구동계"))
        out.append(_session_table(day, DRIVE_COLS))
        out.append('<p class="none">전류 평균은 표본이 있던 1 초 칸들의 평균. 기록률이 낮아 참고값.</p>')
        out.append(fig("pack_voltage", "팩 전압", "모터 컨트롤러가 본 직류 전압. 5 초마다의 중앙값과 최저값. 두 선의 간격이 큰 구간이 부하가 크게 걸린 구간."))
        out.append(fig("drive_temp", "모터 · 인버터 온도", "모터 컨트롤러가 보고한 값. 회색 띠는 주행 구간."))
        out.append(_obs(n, "drivetrain"))

    # ---- 데이터 품질
    out.append(h2("데이터 품질"))
    if day.quality:
        rows = [[E(q["item"]), f'<span class="status {STATUS_CLASS[q["status"]]}"><i>{STATUS_ICON[q["status"]]}</i>{E(q["status"])}</span>',
                 E(str(q.get("value", ""))), E(q.get("note", ""))] for q in day.quality]
        out.append(_table([("항목", TEXT), ("판정", TEXT), ("값", TEXT), ("비고", WRAP)], rows))
    if day.coverage is not None and len(day.coverage):
        cov = day.coverage
        rows = [[E(str(i))] + [_fmt(v * 100 if pd.notna(v) else None, 0) for v in r] for i, r in zip(cov.index, cov.to_numpy())]
        out.append("<h3>신호별 기록률</h3>" + _table([("구간", TEXT)] + [(f"{c}|%", NUM) for c in cov.columns], rows))
        out.append('<p class="none">주행 중 1 초 칸 가운데 새 표본이 하나라도 있던 칸의 비율. 100 이면 매초 값이 있었다는 뜻. '
                   "낮을수록 그 신호의 최대 · 최소가 실제 피크를 놓쳤을 수 있음.</p>")
    out.append(_obs(n, "quality"))

    # ---- 조치 사항
    out.append(h2("조치 사항 (제안)"))
    actions = n.get("actions", [])
    if actions:
        rows = [[str(i), E(a.get("item", "")), E(a.get("owner", "")), E(a.get("due", "")), E(a.get("basis", ""))]
                for i, a in enumerate(actions, 1)]
        out.append(_table([("번호", NUM), ("조치", WRAP), ("담당", TEXT), ("시점", TEXT), ("근거", WRAP)], rows))
    else:
        out.append('<p class="none">기록 없음</p>')

    # ---- 부록
    out.append("<h2>부록. 산출 방법 · 자료</h2>")
    out.append("<h3>산출 방법</h3>" + _ul(day.methods))
    out.append("<h3>사용 자료</h3>" + _ul(day.sources))
    if day.facts:
        out.append("<h3>데이터에서 자동 산출한 수치</h3>" + _ul(day.facts))
    out.append(f"<footer>{E(meta.get('footer', ''))}</footer></body></html>")
    return "\n".join(x for x in out if x)


def write_report(day: DayReport, out_dir: Path) -> Path:
    """→ out_dir/index.html (그림 포함, 파일 하나로 전달 가능) + out_dir/figures/*.png"""
    from . import report_charts               # matplotlib 은 그릴 때만 불러온다

    out_dir.mkdir(parents=True, exist_ok=True)
    figs = report_charts.draw_all(day, out_dir / "figures")
    path = out_dir / "index.html"
    path.write_text(render_html(day, figs, embed=True), encoding="utf-8")
    return path


BROWSERS = ("chrome", "msedge", r"C:\Program Files\Google\Chrome\Application\chrome.exe",
            r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe", "chromium", "google-chrome")


def write_pdf(html_path: Path) -> Path | None:
    """index.html → report.pdf (A4). Chrome · Edge 의 인쇄 기능을 쓴다. 브라우저를 못 찾으면 None.

    글꼴(Pretendard)을 CDN 에서 받을 시간을 준다 — 안 주면 PDF 에 대체 글꼴이 섞인다.
    """
    exe = next((b for b in BROWSERS if shutil.which(b) or Path(b).exists()), None)
    if exe is None:
        return None
    pdf = html_path.with_name("report.pdf")
    subprocess.run([shutil.which(exe) or exe, "--headless=new", "--disable-gpu", "--no-pdf-header-footer",
                    "--virtual-time-budget=10000", f"--print-to-pdf={pdf}", html_path.resolve().as_uri()],
                   check=False, capture_output=True, timeout=120)
    return pdf if pdf.exists() else None
