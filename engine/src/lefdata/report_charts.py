"""시험 주행 보고서의 그림. `report.DayReport` 만 보고 그린다 (차량 · 로거를 모른다).

규칙
    - 그림 안에는 데이터 · 단위 · 시각 · 구간 이름만 넣는다. 해석과 읽는 법은 문서 본문에 쓴다
    - 한 그림에 축은 하나. 단위가 다른 값은 위아래로 나눈다
    - 계열은 셋까지 (파랑 · 주황 · 청록, 색각 이상에서도 구분되는 조합으로 검증된 순서). 둘 이상이면 범례를 둔다
    - 크기(속도 · 머문 시간)는 파랑 한 색의 밝기로 나타낸다
    - 격자는 가는 실선, 글자는 전부 Pretendard (assets/fonts/, 없으면 맑은 고딕)
"""

from __future__ import annotations

from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.dates as mdates
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
from matplotlib import font_manager
from matplotlib.collections import LineCollection
from matplotlib.colors import LinearSegmentedColormap

from .report import DayReport, Session

SURFACE, INK, INK2, MUTED, GRID, AXIS = "#fcfcfb", "#0b0b0b", "#52514e", "#898781", "#e1e0d9", "#c3c2b7"
BLUE, ORANGE, AQUA = "#2a78d6", "#eb6834", "#1baf7a"
SERIES = [BLUE, ORANGE, AQUA]
BLUE_LIGHT = "#86b6ef"          # 파랑 램프의 250 단계: '덜 갖춘 것'(로거 단독 구간)
BAND = "#f0efec"                # 주행 구간 띠
CRITICAL = "#d03b3b"
SEQ_BLUE = LinearSegmentedColormap.from_list("seq_blue", ["#cde2fb", "#6da7ec", "#2a78d6", "#0d366b"])
FONT_DIR = Path(__file__).resolve().parents[2] / "assets" / "fonts"
DPI = 200
W = 10.0                        # 그림 너비(인치). 문서 너비에 맞춰 줄면 글자가 약 13 px 로 보인다
BLOCK_GAP_MIN = 60              # 기록이 이만큼 끊기면 시간축을 나눈다 (오전 / 오후)


def _font() -> list[str]:
    for f in sorted(FONT_DIR.glob("Pretendard-*.otf")):
        font_manager.fontManager.addfont(str(f))
    return ["Pretendard", "Malgun Gothic", "DejaVu Sans"]


def style() -> dict:
    return {
        "font.family": _font(), "axes.unicode_minus": False, "font.size": 10,
        "figure.facecolor": SURFACE, "axes.facecolor": SURFACE, "savefig.facecolor": SURFACE,
        "text.color": INK, "axes.labelcolor": INK2, "xtick.color": INK2, "ytick.color": INK2,
        "axes.edgecolor": AXIS, "axes.linewidth": 0.8, "axes.spines.top": False, "axes.spines.right": False,
        "axes.spines.left": False, "xtick.major.size": 0, "ytick.major.size": 0, "xtick.major.pad": 6,
        "grid.color": GRID, "grid.linewidth": 0.8, "axes.grid": True, "axes.grid.axis": "y", "axes.axisbelow": True,
        "axes.titlesize": 10.5, "axes.titleweight": "semibold", "axes.titlelocation": "left", "axes.titlepad": 8,
        "legend.frameon": False, "legend.fontsize": 9.5, "legend.handlelength": 1.6,
        "lines.linewidth": 1.5, "lines.solid_capstyle": "round", "lines.solid_joinstyle": "round",
    }


def _save(fig, out: Path) -> Path:
    fig.savefig(out, dpi=DPI, bbox_inches="tight", pad_inches=0.25)
    plt.close(fig)
    return out


def _tz(day: DayReport):
    return day.sessions[0].start.tz if day.sessions else None


# ---------------------------------------------------------------- 시간축 (하루를 기록이 있는 덩어리로 나눈다)

def blocks(day: DayReport) -> list[tuple[pd.Timestamp, pd.Timestamp]]:
    """기록이 있는 시간 덩어리들. 점심처럼 길게 비는 곳에서 축을 나눠, 주행 구간이 눈에 보이는 폭을 갖게 한다."""
    spans = sorted([(s.start, s.end) for s in day.sessions]
                   + ([(r.start, r.end) for r in day.recordings.itertuples()] if day.recordings is not None else []))
    out: list[list] = []
    for a, b in spans:
        if out and (a - out[-1][1]).total_seconds() <= BLOCK_GAP_MIN * 60:
            out[-1][1] = max(out[-1][1], b)
        else:
            out.append([a, b])
    pad = pd.Timedelta(minutes=2)
    return [(a - pad, b + pad) for a, b in out]


def _time_grid(day: DayReport, nrows: int, row_h: float, heights=None):
    """행 nrows × 열(시간 덩어리) 격자. 주행 구간 띠와 구간 이름을 깔고 → (fig, axes[row][block])."""
    bl = blocks(day)
    widths = [max((b - a).total_seconds(), 600) for a, b in bl]
    fig, axes = plt.subplots(nrows, len(bl), figsize=(W, row_h * nrows + 0.5), squeeze=False, sharey="row",
                             gridspec_kw={"width_ratios": widths, "wspace": 0.06, "hspace": 0.42,
                                          "height_ratios": heights or [1] * nrows})
    for j, (a, b) in enumerate(bl):
        for i in range(nrows):
            ax = axes[i][j]
            ax.set_xlim(a, b)
            for s in day.sessions:
                if s.end > a and s.start < b:
                    ax.axvspan(s.start, s.end, color=BAND, lw=0, zorder=0)
            minutes = (b - a).total_seconds() / 60
            room = max(W * 0.84 * widths[j] / sum(widths) / 0.62, 2)       # 이 칸에 들어갈 수 있는 눈금 수
            step = next((s_ for s_ in (5, 10, 15, 20, 30, 60) if minutes / s_ <= room), 60)
            ax.xaxis.set_major_locator(mdates.MinuteLocator(byminute=range(0, 60, step), tz=_tz(day)))
            ax.xaxis.set_major_formatter(mdates.DateFormatter("%H:%M", tz=_tz(day)))
            if i < nrows - 1:
                ax.tick_params(labelbottom=False)
            if j > 0:
                ax.tick_params(labelleft=False)
        for s in day.sessions:                                  # 구간 이름은 맨 윗줄 위에
            if s.end > a and s.start < b:
                axes[0][j].text(s.start + (s.end - s.start) / 2, 1.02, s.key, transform=axes[0][j].get_xaxis_transform(),
                                ha="center", va="bottom", fontsize=9, color=INK2)
    return fig, axes, bl


def _plot_series(axes_row, bl, d: pd.DataFrame | None, color, label=None, lw=1.5):
    if d is None:
        return
    for j, (a, b) in enumerate(bl):
        m = d[(d["t"] >= a) & (d["t"] <= b) | d["t"].isna()]
        x = m["t"].dt.tz_convert("UTC").dt.tz_localize(None).to_numpy()      # 빈 시각(NaT)은 선을 끊는다
        axes_row[j].plot(x, m["v"].to_numpy(), color=color, lw=lw, label=label if j == 0 else None)


def _legend(fig, ax, ncols=3):
    """범례는 그림 오른쪽 위, 데이터 밖에."""
    h, l = ax.get_legend_handles_labels()
    if len(h) > 1:
        fig.legend(h, l, loc="upper right", bbox_to_anchor=(0.985, 1.0), ncols=ncols, frameon=False)


def _row_legend(axes_row, top: bool):
    """시간축 그림의 범례: 그 줄의 오른쪽 끝, 축 위 (데이터와 겹치지 않는 자리). 맨 윗줄은 구간 이름 위로 올린다."""
    h, l = axes_row[0].get_legend_handles_labels()
    if len(h) > 1:
        axes_row[-1].legend(h, l, loc="lower right", bbox_to_anchor=(1.0, 1.12 if top else 1.0), ncols=len(h),
                            borderaxespad=0.0, columnspacing=1.4)


def _coarse(d: pd.DataFrame | None, seconds: int, how: str) -> pd.DataFrame | None:
    """1 초 계열을 더 굵은 칸으로. 빽빽한 톱니를 줄여 윤곽이 보이게 한다 (빈 칸은 그대로 비운다)."""
    if d is None or d["t"].dropna().empty:
        return d
    g = d.dropna(subset=["t"]).set_index("t")["v"].resample(f"{seconds}s").agg(how)
    return g.reset_index()


# ---------------------------------------------------------------- 그림들

def timeline(day: DayReport, out: Path) -> Path:
    fig, axes, bl = _time_grid(day, 1, 1.9)
    lanes = [("로거 기록", "로거"), ("온보드 영상", "영상")]
    for j, (a, b) in enumerate(bl):
        ax = axes[0][j]
        for y, (_, lane) in zip((2, 1), lanes):
            for r in day.recordings[day.recordings["lane"] == lane].itertuples():
                if r.end > a and r.start < b:
                    ax.barh(y, r.end - r.start, left=r.start, height=0.34, color=AXIS, lw=0)
        for s in day.sessions:
            if s.end > a and s.start < b:
                ax.barh(0, s.end - s.start, left=s.start, height=0.34, lw=0, color=BLUE if s.kind == "gps" else BLUE_LIGHT)
        ax.set_ylim(-0.6, 2.6)
        ax.set_yticks([2, 1, 0], [lanes[0][0], lanes[1][0], "주행"])
        ax.grid(False)
        ax.spines["bottom"].set_color(AXIS)
    kinds = {s.kind for s in day.sessions}
    handles = [plt.Rectangle((0, 0), 1, 1, color=c) for c, k in ((BLUE, "gps"), (BLUE_LIGHT, "logger")) if k in kinds]
    labels = [n for n, k in (("주행 (랩 기록 있음)", "gps"), ("주행 (로거 단독)", "logger")) if k in kinds]
    if len(handles) > 1:
        fig.legend(handles, labels, loc="upper right", bbox_to_anchor=(0.985, 1.08), ncols=2, frameon=False)
    return _save(fig, out)


def lap_times(day: DayReport, out: Path) -> Path:
    gps = day.gps_sessions()[:3]
    fig, ax = plt.subplots(figsize=(W, 3.6))
    for s, color in zip(gps, SERIES):
        best = s.laps.loc[s.laps["lap_time_s"].idxmin()]
        ax.plot(s.laps["lap"], s.laps["lap_time_s"], "-o", color=color, ms=6.5, mec=SURFACE, mew=1.5,
                label=f"{s.key} · {s.start:%H:%M} · 최속 {best['lap_time_s']:.2f} s")
        ax.plot(best["lap"], best["lap_time_s"], "o", ms=11, mfc="none", mec=INK, mew=1.3, zorder=5)     # 구간 최속 랩
    n = max(len(s.laps) for s in gps)
    ax.set_xticks(range(1, n + 1))
    ax.set_xlim(0.5, n + 0.5)
    lo = min(s.laps["lap_time_s"].min() for s in gps)
    ax.set_ylim(lo - 2.5, None)
    ax.yaxis.set_major_locator(plt.MultipleLocator(5))
    ax.set_xlabel("랩 (구간 안 순서) · 검은 테두리 = 구간 최속 랩")
    ax.set_title("랩타임 (s)")
    ax.spines["bottom"].set_color(AXIS)
    ax.legend(loc="upper right", ncols=len(gps), bbox_to_anchor=(1.0, 1.16))
    return _save(fig, out)


def _best_session(day: DayReport) -> Session | None:
    gps = [s for s in day.gps_sessions() if s.best_lap is not None]
    return min(gps, key=lambda s: s.stats["best_lap_s"]) if gps else None


def track_map(day: DayReport, out: Path) -> Path | None:
    s = _best_session(day)
    if s is None:
        return None
    d = s.best_lap
    x, y, v = d["x_m"].to_numpy(), d["y_m"].to_numpy(), d["speed_kmh"].to_numpy()
    fig, ax = plt.subplots(figsize=(W * 0.72, W * 0.72))
    pts = np.column_stack([x, y]).reshape(-1, 1, 2)
    lc = LineCollection(np.concatenate([pts[:-1], pts[1:]], axis=1), cmap=SEQ_BLUE, lw=4.5, capstyle="round")
    lc.set_array((v[:-1] + v[1:]) / 2)
    ax.add_collection(lc)
    # 계측선: 출발 지점에서 진행 방향에 수직으로
    dx, dy = x[3] - x[0], y[3] - y[0]
    nrm = np.hypot(dx, dy)
    px, py = -dy / nrm * 9, dx / nrm * 9
    ax.plot([x[0] - px, x[0] + px], [y[0] - py, y[0] + py], color=INK, lw=2.2, solid_capstyle="butt", zorder=5)
    cx, cy = float(np.mean(x)), float(np.mean(y))

    def outward(i, text, dist=12):
        """글자를 주행선 바깥쪽(코스 중심의 반대편)에 둔다."""
        ux, uy = x[i] - cx, y[i] - cy
        n_ = np.hypot(ux, uy) or 1.0
        ux, uy = ux / n_, uy / n_
        ax.annotate(text, (x[i], y[i]), xytext=(ux * dist, uy * dist), textcoords="offset points", fontsize=9, color=INK,
                    ha="left" if ux > 0.3 else "right" if ux < -0.3 else "center",
                    va="bottom" if uy > 0.3 else "top" if uy < -0.3 else "center")
    for frac in (0.12, 0.37, 0.62, 0.87):                           # 진행 방향
        i = int(frac * (len(x) - 6))
        ax.annotate("", (x[i + 5], y[i + 5]), (x[i], y[i]), arrowprops={"arrowstyle": "-|>", "color": INK, "lw": 0, "mutation_scale": 16})
    i_max, i_min = int(np.argmax(v)), int(np.argmin(v))
    near_gate = np.hypot(x[i_max] - x[0], y[i_max] - y[0]) < 25
    outward(0, "계측선" + (f" · 최고 {v[i_max]:.0f} km/h" if near_gate else ""), 20)
    for i, word in ((i_max, "최고"), (i_min, "최저")):
        if i == i_max and near_gate:
            continue
        ax.plot(x[i], y[i], "o", ms=7.5, color=SURFACE, mec=INK, mew=1.4, zorder=6)
        outward(i, f"{word} {v[i]:.0f} km/h")
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
    cb.set_label("속도 (km/h)", fontsize=9.5)
    ax.set_title(f"{s.key} {s.best_lap_no}랩 · {s.stats['best_lap_s']:.2f} s · {d['dist_m'].iloc[-1]:.0f} m", loc="left")
    return _save(fig, out)


def speed_trace(day: DayReport, out: Path) -> Path | None:
    gps = [s for s in day.gps_sessions() if s.best_lap is not None][:3]
    if not gps:
        return None
    fig, ax = plt.subplots(figsize=(W, 3.6))
    for s, color in zip(gps, SERIES):
        d = s.best_lap
        ax.plot(d["dist_m"], d["speed_kmh"], color=color, label=f"{s.key} {s.best_lap_no}랩 · {s.stats['best_lap_s']:.2f} s")
    ax.set_xlim(0, max(s.best_lap["dist_m"].iloc[-1] for s in gps))
    ax.set_ylim(0, None)
    ax.set_xlabel("계측선부터 달린 거리 (m)")
    ax.set_title("속도 (km/h)")
    ax.spines["bottom"].set_color(AXIS)
    ax.legend(loc="upper right", ncols=len(gps), bbox_to_anchor=(1.0, 1.16))
    return _save(fig, out)


def g_trace(day: DayReport, out: Path) -> Path | None:
    s = _best_session(day)
    if s is None:
        return None
    d = s.best_lap
    fig, axes = plt.subplots(2, 1, figsize=(W, 4.6), sharex=True, gridspec_kw={"hspace": 0.38})
    for ax, col, title in ((axes[0], "long_g", "앞뒤 가속도 (g) — 가속 +, 제동 −"), (axes[1], "lat_g", "좌우 가속도 (g) — 좌선회 +, 우선회 −")):
        ax.axhline(0, color=AXIS, lw=0.9)
        ax.plot(d["dist_m"], d[col], color=BLUE)
        ax.fill_between(d["dist_m"], 0, d[col], color=BLUE, alpha=0.10, lw=0)
        ax.set_title(title)
        lim = max(1.0, float(np.ceil(np.nanmax(np.abs(d[col])) * 2) / 2))
        ax.set_ylim(-lim, lim)
        ax.set_xlim(0, d["dist_m"].iloc[-1])
    axes[1].set_xlabel(f"계측선부터 달린 거리 (m) — {s.key} {s.best_lap_no}랩")
    axes[1].spines["bottom"].set_color(AXIS)
    return _save(fig, out)


def gg(day: DayReport, out: Path) -> Path | None:
    keys = [s.key for s in day.gps_sessions() if s.key in day.gg][:3] or list(day.gg)[:3]
    if not keys:
        return None
    lim = 2.0
    fig, axes = plt.subplots(1, len(keys), figsize=(W, W / len(keys) + 0.7), squeeze=False, sharey=True, gridspec_kw={"wspace": 0.08})
    hb = None
    vmax = 0
    for ax, k in zip(axes[0], keys):
        d = day.gg[k]
        hb = ax.hexbin(d["lat_g"], d["long_g"], gridsize=44, extent=(-lim, lim, -lim, lim), cmap=SEQ_BLUE, mincnt=1, linewidths=0)
        vmax = max(vmax, hb.get_array().max())
        for r in (0.5, 1.0, 1.5):
            ax.add_patch(plt.Circle((0, 0), r, fill=False, color=AXIS, lw=0.8))
            ax.text(r * 0.707 + 0.03, r * 0.707 + 0.03, f"{r:g} g", fontsize=8, color=MUTED)
        ax.axhline(0, color=AXIS, lw=0.8)
        ax.axvline(0, color=AXIS, lw=0.8)
        ax.set_xlim(-lim, lim)
        ax.set_ylim(-lim, lim)
        ax.set_aspect("equal")
        ax.grid(False)
        s = next(s for s in day.sessions if s.key == k)
        ax.set_title(f"{k} · {s.start:%H:%M}" + (f" · 최속 {s.stats['best_lap_s']:.2f} s" if s.stats.get("best_lap_s") else ""))
        ax.set_xlabel("좌우 가속도 (g) — 우선회 ◀ ▶ 좌선회")
        ax.set_xticks([-2, -1, 0, 1, 2])
        ax.set_yticks([-2, -1, 0, 1, 2])
    axes[0][0].set_ylabel("앞뒤 가속도 (g) — 제동 ▼ ▲ 가속")
    for ax in axes[0]:
        for c in ax.collections:
            c.set_array(c.get_array() * 0.1)                    # 0.1 초 칸의 개수 → 초
            c.set_clim(0.1, vmax * 0.1)
    cb = fig.colorbar(hb, ax=list(axes[0]), orientation="horizontal", fraction=0.045, pad=0.2, aspect=50)
    cb.outline.set_visible(False)
    cb.ax.tick_params(length=0, labelsize=9)
    cb.set_label("그 가속도 상태에 머문 시간 (s)", fontsize=9.5)
    return _save(fig, out)


def susp(day: DayReport, out: Path) -> Path | None:
    keys = [s.key for s in day.gps_sessions() if s.key in day.susp][:3] or list(day.susp)[:3]
    if not keys:
        return None
    fig, axes = plt.subplots(1, len(keys), figsize=(W, 3.2), squeeze=False, sharey=True, gridspec_kw={"wspace": 0.08})
    edges = np.arange(0, 76.01, 1.0)
    for ax, k in zip(axes[0], keys):
        d = day.susp[k]
        for col, color, label in (("rl_mm", BLUE, "리어 좌"), ("rr_mm", ORANGE, "리어 우")):
            h, _ = np.histogram(d[col].clip(0, 76), bins=edges)
            ax.stairs(h / max(h.sum(), 1) * 100, edges, color=color, lw=1.5, label=label)
        s = next(s for s in day.sessions if s.key == k)
        ax.set_title(f"{k} · {s.start:%H:%M}")
        ax.set_xlim(20, 77)
        ax.set_xlabel("스트로크 (mm)")
        ax.spines["bottom"].set_color(AXIS)
    top = max(ax.get_ylim()[1] for ax in axes[0])
    axes[0][0].set_ylim(0, min(top, 30))                         # 상한에 붙은 센서의 막대가 나머지를 눌러 버리지 않게
    axes[0][0].set_ylabel("머문 시간의 비율 (%, 1 mm 칸)")
    for ax, k in zip(axes[0], keys):
        d = day.susp[k]
        for col, color in (("rl_mm", BLUE), ("rr_mm", ORANGE)):
            share = float((d[col] >= 74.9).mean()) * 100
            if share > 30:                                       # 축 밖으로 나간 막대의 실제 값
                ax.annotate(f"75 mm: {share:.0f} %", (75.5, ax.get_ylim()[1]), xytext=(-6, -14), textcoords="offset points",
                            ha="right", fontsize=9, color=INK)
    _legend(fig, axes[0][0])
    return _save(fig, out)


def soc(day: DayReport, out: Path) -> Path | None:
    if "soc" not in day.series:
        return None
    fig, axes, bl = _time_grid(day, 2, 2.3)
    _plot_series(axes[0], bl, day.series["soc"], BLUE)
    axes[0][0].set_ylim(-4, 104)
    axes[0][0].set_title("SOC (%)", pad=22)
    _plot_series(axes[1], bl, _coarse(day.series.get("cell_max"), 5, "max"), BLUE, "가장 높은 셀 (5 초 최고)")
    _plot_series(axes[1], bl, _coarse(day.series.get("cell_min"), 5, "min"), ORANGE, "가장 낮은 셀 (5 초 최저)", lw=1.2)
    axes[1][0].set_title("셀 전압 (V)")
    _row_legend(axes[1], top=False)
    for j in range(len(bl)):
        axes[1][j].spines["bottom"].set_color(AXIS)
    return _save(fig, out)


def batt_temp(day: DayReport, out: Path) -> Path | None:
    if "batt_high" not in day.series:
        return None
    fig, axes, bl = _time_grid(day, 1, 2.9)
    for name, color, label in (("batt_high", BLUE, "가장 뜨거운 곳"), ("batt_avg", AQUA, "평균"), ("batt_low", ORANGE, "가장 차가운 곳")):
        _plot_series(axes[0], bl, day.series.get(name), color, label)
    limit = day.notes.get("limits", {}).get("batt_temp_max_c")
    if limit:
        for j in range(len(bl)):
            axes[0][j].axhline(limit, color=CRITICAL, lw=1.2)
        axes[0][-1].annotate(f"한계 {limit:g} ℃", (1.0, limit), xycoords=("axes fraction", "data"), xytext=(-4, 4),
                             textcoords="offset points", ha="right", fontsize=9, color=INK)
    axes[0][0].set_title("배터리 온도 (℃)", pad=22)
    _row_legend(axes[0], top=True)
    for j in range(len(bl)):
        axes[0][j].spines["bottom"].set_color(AXIS)
    return _save(fig, out)


def thermistors(day: DayReport, out: Path) -> Path | None:
    if not day.thermistors:
        return None
    ids = sorted(set().union(*[set(d["id"]) for d in day.thermistors.values()]))
    pos, x, last = {}, 0.0, None
    for i in ids:                                               # 번호의 십의 자리가 바뀌면 간격을 둔다 (모듈 묶음)
        x += 1.0 if last is None or i // 10 == last // 10 else 2.2
        pos[i], last = x, i
    fig, ax = plt.subplots(figsize=(W, 3.5))
    ax.axhline(0, color=AXIS, lw=0.9)
    for (name, d), color in zip(day.thermistors.items(), SERIES):
        ax.plot([pos[i] for i in d["id"]], d["below_hot_c"], "o", ms=7, color=color, mec=SURFACE, mew=1.4, label=name)
    ax.set_xticks([pos[i] for i in ids], [str(i) for i in ids], fontsize=8.5)
    ax.set_xlim(min(pos.values()) - 1, max(pos.values()) + 1)
    lo = min(d["below_hot_c"].min() for d in day.thermistors.values())
    ax.set_ylim(np.floor(lo - 1), 1)
    ax.yaxis.set_major_locator(plt.MultipleLocator(2))
    ax.set_xlabel("써미스터 번호")
    ax.set_title("팩에서 가장 뜨거운 곳과의 온도 차 (℃) — 0 이 가장 뜨거운 곳")
    ax.spines["bottom"].set_color(AXIS)
    ax.legend(loc="upper right", ncols=len(day.thermistors), bbox_to_anchor=(1.0, 1.17))
    return _save(fig, out)


def pack_voltage(day: DayReport, out: Path) -> Path | None:
    if "vdc_med" not in day.series:
        return None
    fig, axes, bl = _time_grid(day, 1, 2.9)
    _plot_series(axes[0], bl, _coarse(day.series["vdc_med"], 5, "median"), BLUE, "5 초 중앙값")
    _plot_series(axes[0], bl, _coarse(day.series.get("vdc_min"), 5, "min"), ORANGE, "5 초 최저값", lw=1.2)
    axes[0][0].set_title("팩 전압 (V)", pad=22)
    _row_legend(axes[0], top=True)
    for j in range(len(bl)):
        axes[0][j].spines["bottom"].set_color(AXIS)
    return _save(fig, out)


def drive_temp(day: DayReport, out: Path) -> Path | None:
    if "t_motor" not in day.series:
        return None
    fig, axes, bl = _time_grid(day, 1, 2.9)
    _plot_series(axes[0], bl, day.series["t_motor"], BLUE, "모터")
    _plot_series(axes[0], bl, day.series.get("t_igbt"), ORANGE, "인버터 (IGBT)")
    axes[0][0].set_title("모터 · 인버터 온도 (℃)", pad=22)
    _row_legend(axes[0], top=True)
    for j in range(len(bl)):
        axes[0][j].spines["bottom"].set_color(AXIS)
    return _save(fig, out)


FIGURES = [("timeline", timeline), ("lap_times", lap_times), ("track_map", track_map), ("speed_trace", speed_trace),
           ("g_trace", g_trace), ("gg", gg), ("susp", susp), ("soc", soc), ("batt_temp", batt_temp),
           ("thermistors", thermistors), ("pack_voltage", pack_voltage), ("drive_temp", drive_temp)]


def draw_all(day: DayReport, out_dir: Path) -> dict[str, Path]:
    """그릴 수 있는 그림을 전부 그린다 → {이름: 파일}. 자료가 없는 그림은 건너뛴다."""
    out_dir.mkdir(parents=True, exist_ok=True)
    done = {}
    with plt.rc_context(style()):
        for name, fn in FIGURES:
            if name == "timeline" and (day.recordings is None or not day.sessions):
                continue
            if name == "lap_times" and not day.gps_sessions():
                continue
            path = fn(day, out_dir / f"{name}.png")
            if path is not None:
                done[name] = path
    return done
