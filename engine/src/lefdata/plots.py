"""랩 분석 그림 (matplotlib PNG).

색은 dataviz 기준 팔레트: 범주형 1~3번(파랑·주황·청록, 검증 통과), 크기는 파랑 한 색의 밝기 단계.
글자는 항상 글자색을 쓰고 계열색은 선·점에만 쓴다. 축은 하나만 (랩타임과 최고속도는 그림을 나눈다).
"""

from __future__ import annotations

import json
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
import pyarrow.parquet as pq
from matplotlib.collections import LineCollection
from matplotlib.colors import LinearSegmentedColormap

from . import laps as lapmod

SURFACE, TEXT, TEXT2, GRID = "#fcfcfb", "#0b0b0b", "#52514e", "#e6e5e1"
SERIES = ["#2a78d6", "#eb6834", "#1baf7a"]
SEQ_BLUE = LinearSegmentedColormap.from_list("seq_blue", ["#cde2fb", "#6da7ec", "#2a78d6", "#0d366b"])

plt.rcParams.update({
    "font.family": ["Malgun Gothic", "DejaVu Sans"], "axes.unicode_minus": False,
    "figure.facecolor": SURFACE, "axes.facecolor": SURFACE, "savefig.facecolor": SURFACE,
    "text.color": TEXT, "axes.labelcolor": TEXT2, "xtick.color": TEXT2, "ytick.color": TEXT2,
    "axes.edgecolor": GRID, "axes.spines.top": False, "axes.spines.right": False,
    "grid.color": GRID, "grid.linewidth": 1.0, "axes.grid": True, "axes.axisbelow": True,
    "font.size": 10, "axes.titlesize": 12, "axes.titleweight": "bold", "axes.titlelocation": "left",
})


def _load(event_dir: Path):
    d = event_dir / "derived"
    runs = pd.read_csv(d / "runs.csv", encoding="utf-8-sig")
    laps = pd.read_csv(d / "laps.csv", encoding="utf-8-sig")
    gate = json.loads((d / "gate.json").read_text(encoding="utf-8"))
    return runs, laps, gate


def _xy(event_dir: Path, rec_id: str, gate_info: dict) -> pd.DataFrame:
    g = pq.read_table(event_dir / "normalized" / rec_id / "gps.parquet").to_pandas()
    g = g[g["fix"]]
    x, y = lapmod.to_local_m(g["lat"], g["lon"], gate_info["origin"]["lat0"], gate_info["origin"]["lon0"])
    return pd.DataFrame({"t_s": g["t_rel_ms"].to_numpy() / 1000.0, "x": x, "y": y,
                         "v": g["speed_kmh"].to_numpy()})


def track_map(event_dir: Path, rec_id: str, out: Path) -> Path:
    """가장 빠른 랩의 주행선을 속도로 칠한 트랙 지도. 나머지 랩은 옅은 회색."""
    _, laps, gi = _load(event_dir)
    L = laps[laps["recording_id"] == rec_id]
    best = L.loc[L["lap_time_s"].idxmin()]
    xy = _xy(event_dir, rec_id, gi)

    fig, ax = plt.subplots(figsize=(7.2, 7.6))
    inrun = xy[(xy.t_s >= L.t_start_s.min()) & (xy.t_s <= L.t_end_s.max())]
    ax.plot(inrun.x, inrun.y, color="#d9d8d3", lw=1, zorder=1)

    b = xy[(xy.t_s >= best.t_start_s) & (xy.t_s <= best.t_end_s)]
    pts = np.column_stack([b.x, b.y]).reshape(-1, 1, 2)
    lc = LineCollection(np.concatenate([pts[:-1], pts[1:]], axis=1), cmap=SEQ_BLUE, lw=3.2,
                        capstyle="round", zorder=3)
    lc.set_array(b.v.to_numpy()[:-1])
    lc.set_clim(0, np.ceil(b.v.max() / 10) * 10)
    ax.add_collection(lc)

    g = gi["gate"]
    h = np.radians(g["heading_deg"])
    nx, ny = np.cos(h), -np.sin(h)                       # 계측선 방향 (진행 방향에 수직)
    w = 14
    ax.plot([g["x"] - nx * w, g["x"] + nx * w], [g["y"] - ny * w, g["y"] + ny * w],
            color=TEXT, lw=2.4, solid_capstyle="butt", zorder=4)
    ax.annotate("계측선", (g["x"] + nx * w, g["y"] + ny * w), xytext=(8, 4), textcoords="offset points",
                color=TEXT, fontsize=10)
    k = b.v.idxmax()
    ax.annotate(f"최고 {b.v.max():.1f} km/h", (b.x[k], b.y[k]), xytext=(10, -14), textcoords="offset points",
                color=TEXT, fontsize=10, arrowprops=dict(arrowstyle="-", color=TEXT2, lw=0.8))
    k = b.v.idxmin()
    ax.annotate(f"최저 {b.v.min():.1f} km/h", (b.x[k], b.y[k]), xytext=(-6, -24), textcoords="offset points",
                color=TEXT, fontsize=10, arrowprops=dict(arrowstyle="-", color=TEXT2, lw=0.8))

    ax.set_aspect("equal")
    ax.grid(False)
    for s in ax.spines.values():
        s.set_visible(False)
    ax.set_xticks([]); ax.set_yticks([])
    x0, y0 = inrun.x.min(), inrun.y.min() - 12
    ax.plot([x0, x0 + 50], [y0, y0], color=TEXT2, lw=1.5)
    ax.text(x0 + 25, y0 - 4, "50 m", ha="center", va="top", color=TEXT2, fontsize=9)
    cb = fig.colorbar(lc, ax=ax, orientation="horizontal", fraction=0.035, pad=0.02, aspect=40)
    cb.set_label("GPS 속도 (km/h)")
    cb.outline.set_visible(False)
    ax.set_title(f"LEF-25evo 트랙 지도 — 최고 랩 {best.lap_time_s:.2f} s "
                 f"({rec_id}, {int(best.lap)}랩, {best.distance_m:.0f} m)\n", loc="left")
    ax.text(0, 1.005, "색 = 최고 랩의 속도 · 회색 = 같은 주행의 나머지 랩 · 위쪽이 북",
            transform=ax.transAxes, color=TEXT2, fontsize=9.5)
    fig.savefig(out, dpi=160, bbox_inches="tight")
    plt.close(fig)
    return out


def endurance_laps(event_dir: Path, rec_ids: list[str], names: list[str], out: Path) -> Path:
    """여러 주행의 랩별 랩타임 / 랩별 최고속도 (축을 섞지 않고 위아래 두 그림)."""
    _, laps, _ = _load(event_dir)
    fig, axes = plt.subplots(2, 1, figsize=(8.4, 7.2), sharex=True, gridspec_kw={"hspace": 0.28})
    for ax, col, title, unit in ((axes[0], "lap_time_s", "랩타임", "s"), (axes[1], "vmax_kmh", "랩별 최고속도", "km/h")):
        for rid, name, color in zip(rec_ids, names, SERIES):
            L = laps[laps["recording_id"] == rid]
            ax.plot(L.lap, L[col], color=color, lw=2, marker="o", ms=7, mec=SURFACE, mew=2,
                    label=name, solid_capstyle="round", zorder=3)
            last = L.iloc[-1]
            ax.annotate(f"{last[col]:.1f}", (last.lap, last[col]), xytext=(9, 0), textcoords="offset points",
                        va="center", color=TEXT, fontsize=9.5)
        ax.set_title(f"{title} ({unit})")
        ax.grid(axis="x", visible=False)
        ax.spines["left"].set_visible(False)
        ax.tick_params(length=0)
    axes[0].legend(frameon=False, ncols=3, loc="upper left", bbox_to_anchor=(0, 1.02), handlelength=1.6)
    n = int(laps[laps["recording_id"].isin(rec_ids)].lap.max())
    axes[1].set_xticks(range(1, n + 1))
    axes[1].set_xlim(0.5, n + 1.2)
    axes[1].set_xlabel("랩 (계측선 통과 기준 완주 랩)")
    fig.suptitle("내구레이스 — 주행별 랩 추이", x=0.125, ha="left", fontsize=13, fontweight="bold")
    fig.savefig(out, dpi=160, bbox_inches="tight")
    plt.close(fig)
    return out


def lef26_discharge(event_dir: Path, rec_id: str, out: Path) -> Path:
    """LEF-26 한 레코딩의 방전 과정: SOC / 셀 전압 / 팩 전압, 주행 구간은 옅은 띠.

    단위가 다른 세 값을 한 축에 겹치지 않고 위아래 세 그림으로 나눈다 (시간축만 공유).
    """
    import matplotlib.dates as mdates

    rec = event_dir / "normalized" / rec_id
    runs = pd.read_csv(event_dir / "derived" / "motor_runs.csv", encoding="utf-8-sig")
    runs = runs[runs["recording_id"] == rec_id]

    b = pq.read_table(rec / "bms.parquet").to_pandas()
    b = b[b["valid"] & (b["cell_min_v"] >= 2.0) & (b["cell_max_v"] <= 4.3)]
    cell_min_true = float(b["cell_min_v"].min())
    b = b.set_index("t_abs")[["soc_pct", "cell_min_v", "cell_max_v"]].resample("1s").median()   # 빈 구간은 NaN → 선이 끊긴다
    m = pq.read_table(rec / "motor.parquet").to_pandas()
    v = m[(m["signal"] == "vdc_v") & (m["value"] > 150)].set_index("t_abs")["value"]
    v_min, v_med = v.resample("1s").min(), v.resample("1s").median()      # 고전압이 꺼진 구간은 선을 잇지 않는다
    t0 = pd.Timestamp(m["t_abs"].min()) - pd.to_timedelta(m["t_rel_ms"].min(), unit="ms")

    fig, axes = plt.subplots(3, 1, figsize=(9.2, 8.2), sharex=True, gridspec_kw={"hspace": 0.32})
    for ax in axes:
        for r in runs.itertuples():
            ax.axvspan(t0 + pd.to_timedelta(r.t_start_s, unit="s"), t0 + pd.to_timedelta(r.t_end_s, unit="s"),
                       color="#ecebe6", lw=0, zorder=0)
        ax.grid(axis="x", visible=False)
        ax.spines["left"].set_visible(False)
        ax.tick_params(length=0)

    axes[0].plot(b.index, b["soc_pct"], color=SERIES[0], lw=2)
    axes[0].set_title("배터리 잔량 SOC (%)")
    axes[0].set_ylim(-4, 104)

    axes[1].plot(b.index, b["cell_max_v"], color=SERIES[0], lw=1.6, label="최고 셀")
    axes[1].plot(b.index, b["cell_min_v"], color=SERIES[1], lw=1.6, label="최저 셀")
    axes[1].set_title("셀 전압 (V) — BMS")
    axes[1].legend(frameon=False, ncols=2, loc="upper right", handlelength=1.6)
    k = b["cell_min_v"].idxmin()
    axes[1].annotate(f"최저 {cell_min_true:.2f} V", (k, b["cell_min_v"].min()), xytext=(-70, 2),
                     textcoords="offset points", color=TEXT, fontsize=9.5)

    axes[2].plot(v_med.index, v_med, color=SERIES[0], lw=1.6, label="1초 중앙값")
    axes[2].plot(v_min.index, v_min, color=SERIES[1], lw=1.2, label="1초 최저 (부하 시 전압 강하)")
    axes[2].set_title("팩 전압 (V) — 모터 컨트롤러")
    axes[2].legend(frameon=False, ncols=2, loc="upper right", handlelength=1.6)
    axes[2].xaxis.set_major_formatter(mdates.DateFormatter("%H:%M", tz=b.index.tz))
    axes[2].set_xlabel("시각 (KST 추정) · 회색 띠 = 모터 속도로 검출한 주행 구간")

    fig.suptitle(f"LEF-26 방전 과정 — {rec_id}  (주행 {len(runs)}회, SOC "
                 f"{runs['soc_start_pct'].iloc[0]:.0f} → {runs['soc_end_pct'].iloc[-1]:.0f} %)",
                 x=0.125, ha="left", fontsize=13, fontweight="bold")
    fig.savefig(out, dpi=160, bbox_inches="tight")
    plt.close(fig)
    return out


def _event_tables(event_dir: Path, name: str) -> pd.DataFrame:
    return pd.read_csv(event_dir / "derived" / f"{name}.csv", encoding="utf-8-sig")


def _days(stamps) -> str:
    """날짜로 시작하는 문자열들(시각·레코딩 이름) → '2026-08-14' 또는 '2026-08-14 ~ 08-15'."""
    days = sorted({str(s)[:10] for s in stamps})
    return days[0] if len(days) == 1 else f"{days[0]} ~ {days[-1][5:]}"


def accel_runs(event_dir: Path, out: Path) -> Path:
    """가속 주행: 출발점 기준 거리-속도 곡선과 주행별 75 m 통과 시간."""
    from . import events as ev

    _, _, gi = _load(event_dir)
    runs = _event_tables(event_dir, "accel_runs")
    fw = runs[runs["direction"] == "forward"].sort_values("t75_s").reset_index(drop=True)
    FULL_KMH = 85.0          # 이 아래는 저출력 시험 주행 (기록 비교 대상이 아니다)
    XMAX = 6.0

    fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(11, 4.2), gridspec_kw={"width_ratios": [1.25, 1]})

    for n, r in runs.iterrows():
        xy = _xy(event_dir, r["recording_id"], gi)
        m = xy["t_s"] >= r["t_start_s"]
        xy = xy[m].reset_index(drop=True)
        d = ev._cumdist(xy["x"].to_numpy(), xy["y"].to_numpy())
        k = d <= 90
        fast = r["direction"] == "forward" and r["t75_s"] == fw["t75_s"].min()
        if r["v_max_kmh"] < FULL_KMH:
            col, lw, al, z = SERIES[2], 1.4, 0.9, 2
        elif r["direction"] == "reverse":
            col, lw, al, z = SERIES[1], 1.4, 0.9, 2
        else:
            col, lw, al, z = SERIES[0], 2.2 if fast else 1.0, 1.0 if fast else 0.45, 3 if fast else 2
        ax1.plot(d[k], xy["v"].to_numpy()[k], color=col, lw=lw, alpha=al, zorder=z)
    ax1.axvline(75, color=TEXT2, lw=1.0, ls="--")
    ax1.text(75.8, 4, "75 m", color=TEXT2, fontsize=9)
    ax1.set_title("출발점 기준 거리 - 속도")
    ax1.set_xlabel("출발점부터 거리 (m)"); ax1.set_ylabel("속도 (km/h)")
    ax1.set_xlim(0, 90)
    nfull = int(((runs["direction"] == "forward") & (runs["v_max_kmh"] >= FULL_KMH)).sum())
    ax1.plot([], [], color=SERIES[0], lw=2.2, label=f"정방향 정상출력 {nfull}회 (최속 굵게)")
    if (runs["direction"] == "reverse").any():
        ax1.plot([], [], color=SERIES[1], lw=1.4, label="역방향 주행")
    if (runs["v_max_kmh"] < FULL_KMH).any():
        ax1.plot([], [], color=SERIES[2], lw=1.4, label="저출력 시험 주행")
    ax1.legend(frameon=False, loc="upper left", fontsize=9)

    y = np.arange(len(fw))
    full = fw["v_max_kmh"] >= FULL_KMH
    ax2.barh(y, fw["t75_s"].clip(upper=XMAX), height=0.62,
             color=np.where(full, SERIES[0], SERIES[2]))
    for i, (v, f) in enumerate(zip(fw["t75_s"], full)):
        if v <= XMAX:
            ax2.text(v - 0.06, i, f"{v:.3f}", va="center", ha="right", color=SURFACE, fontsize=9)
        else:      # 축 밖으로 나가는 막대는 잘라서 그리고 값을 따로 적는다
            ax2.text(XMAX - 0.06, i, f"{v:.3f}  ▸", va="center", ha="right", color=SURFACE, fontsize=9)
    ax2.set_yticks(y, [f"{t[11:19]}  {m:.0f} km/h" for t, m in zip(fw["t_abs_start"], fw["v_max_kmh"])],
                   fontsize=8.5)
    ax2.invert_yaxis()
    ax2.set_xlim(0, XMAX)
    ax2.set_xlabel("75 m 통과 시간 (s)")
    rep = fw.loc[full, "t75_s"]
    ax2.set_title(f"정방향 75 m 기록 — 정상출력 {int(full.sum())}회 {rep.min():.3f}~{rep.max():.3f} s")
    ax2.grid(axis="y", visible=False)

    fig.suptitle(f"{event_dir.parent.name} 가속 — {_days(runs['t_abs_start'])}",
                 x=0.007, ha="left", fontsize=13, fontweight="bold")
    fig.tight_layout(rect=[0, 0, 1, 0.94])
    fig.savefig(out, dpi=160)
    plt.close(fig)
    return out


def skidpad(event_dir: Path, out: Path) -> Path:
    """스키드패드: 8자 궤적과 맞춘 원, 그리고 랩타임·횡가속도."""
    from . import events as ev

    _, _, gi = _load(event_dir)
    laps = _event_tables(event_dir, "skidpad_laps")
    arcs = _event_tables(event_dir, "skidpad_arcs")
    # 바퀴를 실제로 채운 호만 스키드패드다. 반지름만 맞은 일반 코너는 제외한다.
    done = arcs[arcs["laps"] > 0]
    key = done.groupby(["recording_id", "run"])["laps"].sum().idxmax()
    rec, run = key
    sel = done[(done["recording_id"] == rec) & (done["run"] == run)]

    xy = _xy(event_dir, rec, gi)
    fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(11, 4.4), gridspec_kw={"width_ratios": [1, 1.2]})

    t0, t1 = sel["t_start_s"].min() - 5, (sel["t_start_s"] + sel["duration_s"]).max() + 5
    w = xy[(xy["t_s"] >= t0) & (xy["t_s"] <= t1)]
    ax1.plot(w["x"], w["y"], color=GRID, lw=1.0, zorder=1)
    mark = {}
    for _, a in sel.iterrows():
        col = SERIES[0] if a["turn"] == "left" else SERIES[1]
        seg = xy[(xy["t_s"] >= a["t_start_s"]) & (xy["t_s"] <= a["t_start_s"] + a["duration_s"])]
        ax1.plot(seg["x"], seg["y"], color=col, lw=1.8, zorder=3)
        cx, cy, r = ev.fit_circle(seg["x"].to_numpy(), seg["y"].to_numpy())
        th = np.linspace(0, 2 * np.pi, 200)
        ax1.plot(cx + r * np.cos(th), cy + r * np.sin(th), color=col, lw=1.0, ls="--", alpha=0.7, zorder=2)
        mark.setdefault(a["turn"], []).append((cx, cy, r))
    for turn, v in mark.items():          # 같은 원을 여러 번 돌기 때문에 방향당 한 번만 적는다
        cx, cy, r = np.mean(v, axis=0)
        lbl = "좌" if turn == "left" else "우"
        ax1.text(cx, cy, lbl + " 원" + chr(10) + f"r = {r:.2f} m",
                 color=SERIES[0] if turn == "left" else SERIES[1],
                 ha="center", va="center", fontsize=9.5, fontweight="bold")
    ax1.set_aspect("equal")
    ax1.set_title("8자 궤적과 맞춘 원", pad=20)
    n_runs = done.groupby(["recording_id", "run"]).ngroups
    ax1.text(0.0, 1.01, f"{rec} run {run} — {n_runs}회 주행 중 1회", transform=ax1.transAxes,
             color=TEXT2, fontsize=8.5, va="bottom")
    ax1.set_xlabel("동 (m)"); ax1.set_ylabel("북 (m)")
    ax1.plot([], [], color=GRID, lw=1.0, label="진입·이탈")
    ax1.legend(frameon=False, fontsize=9, loc="lower left")

    laps = laps.reset_index(drop=True)
    xi = np.arange(len(laps))
    for turn, col, ko in (("left", SERIES[0], "좌회전"), ("right", SERIES[1], "우회전")):
        m = laps["turn"] == turn
        ax2.scatter(xi[m], laps.loc[m, "lap_time_s"], color=col, s=34, zorder=3, label=ko)
    best = laps["lap_time_s"].idxmin()
    ax2.annotate(f"최속 {laps.loc[best, 'lap_time_s']:.3f} s\n{laps.loc[best, 'lat_acc_g']:.2f} g",
                 (best, laps.loc[best, "lap_time_s"]), textcoords="offset points", xytext=(-10, 0),
                 ha="right", va="center", color=TEXT, fontsize=9, fontweight="bold",
                 bbox=dict(facecolor=SURFACE, edgecolor="none", pad=1.5, alpha=0.85), zorder=5)
    ax2.axhline(laps["lap_time_s"].mean(), color=TEXT2, lw=1.0, ls="--")
    ax2.text(0.0, laps["lap_time_s"].mean(), f"평균 {laps['lap_time_s'].mean():.3f} s ",
             color=TEXT2, fontsize=9, va="bottom", ha="left",
             bbox=dict(facecolor=SURFACE, edgecolor="none", pad=1.5, alpha=0.85), zorder=5)
    ax2.set_xlim(-0.6, len(laps) - 0.4)
    ax2.set_title(f"랩타임 — {n_runs}회 주행 총 {len(laps)}랩", pad=20)
    ax2.text(0.0, 1.01, f"횡가속도 v²/r = {laps['lat_acc_g'].min():.2f}~{laps['lat_acc_g'].max():.2f} g",
             transform=ax2.transAxes, color=TEXT2, fontsize=8.5, va="bottom")
    ax2.set_xlabel("랩 (측정 순서)"); ax2.set_ylabel("한 바퀴 시간 (s)")
    ax2.legend(frameon=False, fontsize=9)

    fig.suptitle(f"{event_dir.parent.name} 스키드패드 — {_days(laps['recording_id'])}", x=0.007, ha="left", fontsize=13, fontweight="bold")
    fig.tight_layout(rect=[0, 0, 1, 0.94])
    fig.savefig(out, dpi=160)
    plt.close(fig)
    return out


def stint_laps(event_dir: Path, out: Path, rec_id: str | None = None) -> Path:
    """한 레코딩 안의 주행 구간(스틴트)별 랩타임·최고속도.

    내구처럼 드라이버를 교대하며 여러 번 나눠 달린 경우, 레코딩이 아니라 **run** 으로 나눠 봐야 한다.
    (`endurance_laps` 는 레코딩끼리 비교할 때 쓴다.)
    """
    _, laps, _ = _load(event_dir)
    if rec_id:
        laps = laps[laps["recording_id"] == rec_id]
    laps = laps.reset_index(drop=True)
    xi = np.arange(len(laps))
    runs = list(dict.fromkeys(laps["run"]))

    fig, axes = plt.subplots(2, 1, figsize=(10, 6.4), sharex=True, gridspec_kw={"height_ratios": [1.3, 1]})
    for r, color in zip(runs, SERIES):
        m = (laps["run"] == r).to_numpy()
        n = int(m.sum())
        axes[0].plot(xi[m], laps.loc[m, "lap_time_s"], "o-", color=color, lw=1.8, ms=5,
                     label=f"Run {r} ({n}랩)")
        axes[1].plot(xi[m], laps.loc[m, "vmax_kmh"], "o-", color=color, lw=1.8, ms=5)
    for k in np.flatnonzero(np.diff(laps["run"].to_numpy())) + 0.5:   # 스틴트 경계
        for ax in axes:
            ax.axvline(k, color=TEXT2, lw=1.0, ls="--")

    best = laps["lap_time_s"].idxmin()
    axes[0].annotate(f"최속 {laps.loc[best, 'lap_time_s']:.2f} s", (best, laps.loc[best, "lap_time_s"]),
                     textcoords="offset points", xytext=(0, 9), ha="center",
                     fontsize=9, fontweight="bold", color=SERIES[0])
    axes[0].set_ylabel("랩타임 (s)")
    axes[0].set_title(f"랩타임 — 완주 {len(laps)}랩", pad=16)
    med = " · ".join(f"Run {r} 중앙 {laps[laps['run'] == r]['lap_time_s'].median():.2f} s" for r in runs)
    axes[0].text(0, 1.01, med, transform=axes[0].transAxes, color=TEXT2, fontsize=8.5, va="bottom")
    axes[0].legend(frameon=False, fontsize=9, loc="best")
    axes[1].set_ylabel("랩 최고속도 (km/h)")
    axes[1].set_xlabel("완주 랩 (기록 순서)")
    axes[1].set_title("랩별 최고속도", pad=16)

    name = rec_id or laps["recording_id"].iloc[0]
    fig.suptitle(f"{event_dir.parent.name} {name} — 스틴트별 랩 추이",
                 x=0.007, ha="left", fontsize=13, fontweight="bold")
    fig.tight_layout(rect=[0, 0, 1, 0.95])
    fig.savefig(out, dpi=160)
    plt.close(fig)
    return out


def lap_logger(event_dir: Path, out: Path) -> Path:
    """랩마다 로거가 본 값 — 랩타임 / SOC / 최저 셀 전압 / 배터리 온도 / 전류 최대. 가로축은 랩(기록 순서).

    단위가 다른 값들을 한 축에 겹치지 않고 위아래로 나눈다. 영상 경계는 세로 점선, 주행 구간 안에서만 선을 잇는다.
    """
    laps = _event_tables(event_dir, "lap_logger").sort_values("t_abs_start").reset_index(drop=True)
    xi = np.arange(1, len(laps) + 1)
    stints = [g.index.to_numpy() for _, g in laps.groupby(["recording_id", "run"], sort=False)]
    videos = [g.index.to_numpy() for _, g in laps.groupby("recording_id", sort=False)]

    panels = [("랩타임 (s)", [("lap_time_s", None)]),
              ("랩 끝의 SOC (%)", [("soc_end_pct", None)]),
              ("랩 중 최저 셀 전압 (V)", [("cell_min_v", None)]),
              ("배터리 써미스터 온도 (℃)", [("batt_high_c", "가장 뜨거운 곳"), ("batt_low_c", "가장 차가운 곳")]),
              ("모터 전류 Iq 최대 (A) — 기록된 표본 중", [("iq_peak_a", None)])]
    fig, axes = plt.subplots(len(panels), 1, figsize=(12, 11.5), sharex=True, gridspec_kw={"hspace": 0.38})
    for ax, (title, series) in zip(axes, panels):
        for (col, label), color in zip(series, SERIES):
            for k, idx in enumerate(stints):
                ax.plot(xi[idx], laps.loc[idx, col].astype(float), "o-", color=color, lw=1.6, ms=4,
                        label=label if k == 0 else None)
        for v in videos[1:]:
            ax.axvline(xi[v[0]] - 0.5, color=TEXT2, lw=0.8, ls="--")
        ax.set_title(title)
        ax.grid(axis="x", visible=False)
        ax.spines["left"].set_visible(False)
        ax.tick_params(length=0)
        if len(series) > 1:
            ax.legend(frameon=False, ncols=2, loc="lower right", bbox_to_anchor=(1, 1.0), handlelength=1.6, fontsize=9)
    level = 0
    for k, v in enumerate(videos):                      # 영상 이름: 날짜 · 번호 · 시작 시각
        first = laps.loc[v[0]]
        level = (level + 1) % 3 if k > 0 and len(videos[k - 1]) < 4 else 0     # 한두 랩짜리 영상이 이어지면 글자가 겹친다 → 한 줄씩 올린다
        name = first["recording_id"]
        axes[0].text(xi[v[0]] - 0.3, 1.02 + 0.22 * level, f"{name[5:10]} · {name[11:]}\n{first['t_abs_start'][11:16]}",
                     transform=axes[0].get_xaxis_transform(), fontsize=8, color=TEXT2, va="bottom")
    axes[0].set_title(panels[0][0], pad=76)
    axes[-1].set_xlabel("완주 랩 (기록 순서) · 세로 점선 = 영상 경계 · 선이 끊긴 곳 = 주행 구간 경계")
    axes[-1].set_xlim(0.2, len(laps) + 0.8)
    fig.suptitle(f"{event_dir.parent.name} 랩별 로거 값 — {_days(laps['recording_id'])}  (완주 {len(laps)}랩)",
                 x=0.125, y=1.025, ha="left", fontsize=13, fontweight="bold")
    fig.savefig(out, dpi=160, bbox_inches="tight")
    plt.close(fig)
    return out
