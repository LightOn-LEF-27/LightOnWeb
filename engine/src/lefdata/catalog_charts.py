"""주행 데이터 카탈로그의 그림. `catalog.Session` 만 보고 그린다 (차량 · 로거를 모른다).

데이터 종류가 달라도 그림의 형태는 다섯 가지로 같다. 가로축이 형태를 정한다.

    시간   가로 = 세션 시작부터의 시간(분)        세로 = 채널 값. 채널마다 한 칸 (3칸까지)
    랩     가로 = 랩 번호                         세로 = 랩마다 한 값
    거리   가로 = 계측선부터 달린 거리(m)          세로 = 채널 값. 최속 랩 하나
    지도   최속 랩의 주행선에 채널 값을 색으로      100 m 마다 거리 숫자 — 거리 그림의 가로축과 같은 값
    분포   가로 = 좌우 가속도, 세로 = 앞뒤 가속도   그 상태에 머문 시간 (g-g 선도)

규칙은 `report_charts` 와 같다 — 그림 안에는 데이터 · 단위 · 구간 이름만, 한 그림에 축은 하나, 글자는 Pretendard.
해석과 읽는 법은 문서 본문에 쓴다 (`Figure.how`).
"""

from __future__ import annotations

from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
from matplotlib.collections import LineCollection

from .catalog import DataBlock, Figure, Session
from .report_charts import AXIS, BLUE, DPI, INK, INK2, MUTED, SEQ_BLUE, SURFACE, W, style

MAX_PANES = 3
MAP_MARK_M = 100            # 지도에 거리 숫자를 적는 간격
GG_LIMIT_G = 2.0

HOW = {
    "time": "가로축은 세션 시작부터의 시간(분). 세로 가는 선은 계측선을 지난 때(랩 경계), 맨 위 숫자는 랩 번호.",
    "laps": "가로축은 랩 번호(세션 안 순서), 점 하나가 랩 하나. 검은 테두리는 최속 랩.",
    "trace": "최속 랩 하나. 가로축은 계측선부터 달린 거리(m) — 지도 그림에 적힌 거리 숫자와 같은 값.",
    "map": "최속 랩의 주행선. 색이 진할수록 값이 큼. 굵은 가로줄은 계측선, 화살표는 진행 방향, 숫자는 계측선부터 달린 거리(m).",
    "gg": "가로는 좌우 가속도, 세로는 앞뒤 가속도. 색이 진할수록 그 가속도 상태에 오래 머묾. 원은 0.5 g 간격.",
}


def _save(fig, out: Path) -> Path:
    fig.savefig(out, dpi=DPI, bbox_inches="tight", pad_inches=0.25)
    plt.close(fig)
    return out


def _panes(n: int, row_h: float):
    fig, axes = plt.subplots(n, 1, figsize=(W, row_h * n + 0.7), sharex=True, squeeze=False, gridspec_kw={"hspace": 0.42})
    return fig, [a[0] for a in axes]


def time_chart(s: Session, b: DataBlock, out: Path) -> Path | None:
    chans = [b.channel(k) for k in b.time_keys][:MAX_PANES]
    if not chans:
        return None
    fig, axes = _panes(len(chans), 1.9)
    end_min = s.duration_s / 60
    laps = s.laps if s.laps is not None and len(s.laps) else None
    for i, (ax, c) in enumerate(zip(axes, chans)):
        ax.plot(c.t_s / 60, c.v, color=BLUE, lw=0.7 if len(c.v) > 20000 else 1.2)
        ax.set_title(c.title, pad=20 if i == 0 and laps is not None else 8)
        ax.set_xlim(0, end_min)
        if laps is not None:
            for t in list(laps["t_start_s"]) + [laps["t_end_s"].iloc[-1]]:
                ax.axvline(t / 60, color=AXIS, lw=0.7, zorder=0)
    if laps is not None:
        for _, r in laps.iterrows():
            axes[0].text((r["t_start_s"] + r["t_end_s"]) / 120, 1.03, f"{int(r['lap'])}", transform=axes[0].get_xaxis_transform(),
                         ha="center", va="bottom", fontsize=8.5, color=INK2)
    step = next(x for x in (1, 2, 5, 10, 20) if end_min / x <= 16)
    axes[-1].set_xticks(np.arange(0, end_min + 1e-9, step))
    axes[-1].set_xlabel("세션 시작부터의 시간 (분)")
    axes[-1].spines["bottom"].set_color(AXIS)
    return _save(fig, out)


def lap_chart(s: Session, b: DataBlock, out: Path) -> Path | None:
    cols = [c for c in b.lap_columns if c.chart and s.laps is not None and c.key in s.laps.columns and s.laps[c.key].notna().any()]
    cols = cols[:MAX_PANES]
    if not cols or len(s.laps) < 2:
        return None
    fig, axes = _panes(len(cols), 2.0)
    best = s.best_lap
    for ax, c in zip(axes, cols):
        ax.plot(s.laps["lap"], s.laps[c.key], "-o", color=BLUE, ms=6.5, mec=SURFACE, mew=1.5)
        ax.plot(best["lap"], best[c.key], "o", ms=11, mfc="none", mec=INK, mew=1.3, zorder=5)
        ax.set_title(f"{c.label} ({c.unit})" if c.unit else c.label)
    axes[-1].set_xticks(list(s.laps["lap"]))
    axes[-1].set_xlim(s.laps["lap"].min() - 0.5, s.laps["lap"].max() + 0.5)
    axes[-1].set_xlabel("랩 (세션 안 순서) · 검은 테두리 = 최속 랩")
    axes[-1].spines["bottom"].set_color(AXIS)
    return _save(fig, out)


def _best_lap_track(s: Session):
    """최속 랩의 궤적 → (t_s, x, y, 계측선부터의 거리). 궤적이나 랩이 없으면 None."""
    lap = s.best_lap
    if lap is None or s.track is None or s.track.empty:
        return None
    tr = s.track
    m = ((tr["t_s"] >= lap["t_start_s"]) & (tr["t_s"] < lap["t_end_s"])).to_numpy()
    if m.sum() < 10:
        return None
    d0 = float(np.interp(lap["t_start_s"], tr["t_s"], tr["dist_m"]))
    return tr["t_s"].to_numpy()[m], tr["x_m"].to_numpy()[m], tr["y_m"].to_numpy()[m], tr["dist_m"].to_numpy()[m] - d0


def _lap_name(s: Session) -> str:
    lap = s.best_lap
    return f"{int(lap['lap'])}랩 · {lap['lap_time_s']:.2f} s"


def trace_chart(s: Session, b: DataBlock, out: Path) -> Path | None:
    chans = [b.channel(k) for k in b.trace_keys][:MAX_PANES]
    best = _best_lap_track(s)
    if not chans or best is None:
        return None
    t, _, _, dist = best
    fig, axes = _panes(len(chans), 1.9)
    for ax, c in zip(axes, chans):
        m = (c.t_s >= t[0]) & (c.t_s <= t[-1])
        ax.plot(np.interp(c.t_s[m], t, dist), c.v[m], color=BLUE, lw=1.2)
        if np.nanmin(c.v[m]) < 0 < np.nanmax(c.v[m]):
            ax.axhline(0, color=AXIS, lw=0.9, zorder=0)
        ax.set_title(c.title)
        ax.set_xlim(0, dist[-1])
    axes[-1].set_xlabel(f"계측선부터 달린 거리 (m) — {_lap_name(s)}")
    axes[-1].spines["bottom"].set_color(AXIS)
    return _save(fig, out)


def map_chart(s: Session, b: DataBlock, out: Path) -> Path | None:
    best = _best_lap_track(s)
    if b.map_key is None or best is None:
        return None
    t, x, y, dist = best
    c = b.channel(b.map_key)
    v = np.interp(t, c.t_s, c.v)
    fig, ax = plt.subplots(figsize=(W * 0.72, W * 0.72))
    pts = np.column_stack([x, y]).reshape(-1, 1, 2)
    lc = LineCollection(np.concatenate([pts[:-1], pts[1:]], axis=1), cmap=SEQ_BLUE, lw=4.5, capstyle="round")
    lc.set_array((v[:-1] + v[1:]) / 2)
    ax.add_collection(lc)
    dx, dy = x[3] - x[0], y[3] - y[0]                           # 계측선: 출발 지점에서 진행 방향에 수직으로
    nrm = np.hypot(dx, dy)
    px, py = -dy / nrm * 9, dx / nrm * 9
    ax.plot([x[0] - px, x[0] + px], [y[0] - py, y[0] + py], color=INK, lw=2.2, solid_capstyle="butt", zorder=5)
    for frac in (0.12, 0.37, 0.62, 0.87):                       # 진행 방향
        i = int(frac * (len(x) - 6))
        ax.annotate("", (x[i + 5], y[i + 5]), (x[i], y[i]), arrowprops={"arrowstyle": "-|>", "color": INK, "lw": 0, "mutation_scale": 16})
    for mark in np.arange(MAP_MARK_M, dist[-1] - MAP_MARK_M / 2, MAP_MARK_M):      # 거리 숫자: 주행선 옆(진행 방향의 왼쪽)에
        i = int(np.searchsorted(dist, mark))
        j, k = max(i - 3, 0), min(i + 3, len(x) - 1)
        nx, ny = -(y[k] - y[j]), x[k] - x[j]
        n_ = np.hypot(nx, ny) or 1.0
        ax.plot(x[i], y[i], "o", ms=3.4, color=SURFACE, mec=INK2, mew=0.8, zorder=6)
        ax.text(x[i] + 11 * nx / n_, y[i] + 11 * ny / n_, f"{mark:.0f}", ha="center", va="center", fontsize=8, color=INK2, zorder=7,
                bbox={"boxstyle": "round,pad=0.12", "fc": SURFACE, "ec": "none", "alpha": 0.85})
    ax.set_aspect("equal")
    ax.set_xlim(x.min() - 30, x.max() + 30)
    ax.set_ylim(y.min() - 30, y.max() + 22)
    x1, y0 = ax.get_xlim()[1], ax.get_ylim()[0]
    ax.plot([x1 - 54, x1 - 4], [y0 + 5, y0 + 5], color=INK2, lw=1.6, solid_capstyle="butt")       # 축척
    ax.text(x1 - 29, y0 + 8, "50 m", ha="center", va="bottom", fontsize=9, color=INK2)
    ax.axis("off")
    cb = fig.colorbar(lc, ax=ax, orientation="horizontal", fraction=0.035, pad=0.02, aspect=40)
    cb.outline.set_visible(False)
    cb.ax.tick_params(length=0, labelsize=9)
    cb.set_label(c.title, fontsize=9.5)
    ax.set_title(f"{_lap_name(s)} · {dist[-1]:.0f} m", loc="left")
    return _save(fig, out)


def gg_chart(s: Session, b: DataBlock, out: Path) -> Path | None:
    if b.gg is None or b.gg.empty:
        return None
    lim = GG_LIMIT_G
    fig, ax = plt.subplots(figsize=(W * 0.62, W * 0.62 + 0.5))
    hb = ax.hexbin(b.gg["lat_g"], b.gg["long_g"], gridsize=44, extent=(-lim, lim, -lim, lim), cmap=SEQ_BLUE, mincnt=1, linewidths=0)
    for r in (0.5, 1.0, 1.5):
        ax.add_patch(plt.Circle((0, 0), r, fill=False, color=AXIS, lw=0.8))
        ax.text(r * 0.707 + 0.03, r * 0.707 + 0.03, f"{r:g} g", fontsize=8, color=MUTED)
    ax.axhline(0, color=AXIS, lw=0.8)
    ax.axvline(0, color=AXIS, lw=0.8)
    ax.set_xlim(-lim, lim)
    ax.set_ylim(-lim, lim)
    ax.set_aspect("equal")
    ax.grid(False)
    ax.set_xticks([-2, -1, 0, 1, 2])
    ax.set_yticks([-2, -1, 0, 1, 2])
    ax.set_xlabel("좌우 가속도 (g) — 우선회 ◀ ▶ 좌선회")
    ax.set_ylabel("앞뒤 가속도 (g) — 제동 ▼ ▲ 가속")
    step_s = float(b.gg.attrs.get("step_s", 0.1))
    hb.set_array(hb.get_array() * step_s)                       # 칸의 개수 → 초
    hb.set_clim(step_s, hb.get_array().max())
    cb = fig.colorbar(hb, ax=ax, orientation="horizontal", fraction=0.045, pad=0.14, aspect=40)
    cb.outline.set_visible(False)
    cb.ax.tick_params(length=0, labelsize=9)
    cb.set_label("그 가속도 상태에 머문 시간 (s)", fontsize=9.5)
    return _save(fig, out)


FORMS = [("time", "시간에 따른 변화", time_chart), ("laps", "랩별 값", lap_chart), ("trace", "최속 랩 — 거리에 따른 변화", trace_chart),
         ("map", "최속 랩 — 코스 위", map_chart), ("gg", "가속도 분포 (g-g 선도)", gg_chart)]


def draw_session(s: Session, out_dir: Path) -> dict[str, list[Figure]]:
    """세션의 모든 블록을 그린다 → {블록 key: [Figure]}. 자료가 없는 형태는 건너뛴다."""
    out_dir.mkdir(parents=True, exist_ok=True)
    done: dict[str, list[Figure]] = {}
    with plt.rc_context(style()):
        for b in s.blocks:
            for form, name, fn in FORMS:
                path = fn(s, b, out_dir / f"{b.key}_{form}.png")
                if path is not None:
                    done.setdefault(b.key, []).append(Figure(f"{b.key}_{form}", f"{b.title} · {name}", HOW[form], path))
    return done
