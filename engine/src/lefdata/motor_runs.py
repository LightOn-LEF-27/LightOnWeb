"""LEF-26: 모터 속도(BAMOCAR 0x181)로 주행 구간(run)을 나누고 구간별 요약을 만든다.

LEF-26 은 GPS 가 사실상 없어서 laps.py 의 방법을 쓸 수 없다. 대신 컨트롤러의 실제 속도 n_act_pct 를 쓴다.

속도 단위: 컨트롤러가 주는 값은 설정 최대 속도 대비 % 다. 26팀 확인으로 그 설정값(N-100 %)이
6000 rpm 이라 rpm 을 같이 낸다. 차속 `v_max_kmh` 는 온보드 영상 GPS 로 잰 환산값
(lef26.KMH_PER_PCT)으로 낸다 — 문서값으로 계산한 것과 조금 다르다.
시간은 변환기가 로거 시계 속도를 바로잡은 값이다 (lef26.py 시계 모델 v2).
    - 주행 중 = |n_act_pct| > MOVING_PCT
    - 속도 메시지가 띄엄띄엄 기록돼 있어(수 초 공백도 있음) 샘플 사이가 MERGE_GAP_S 이하면 같은 run 으로 본다
결과는 해석이므로 derived/ 에 표로 둔다 — `motor_runs.csv` (GPS 로 나눈 `runs.csv` 는 laps.py 가 쓴다.
같은 이벤트에 영상 GPS 가 들어오면서 두 표가 한 폴더에 놓이게 되어 이름을 갈랐다, 2026-10-01).

전력·에너지는 계산하지 않는다: Iq 는 모터 상전류(교류)라서 팩 전압에 곱해도 배터리 전력이 아니다.
"""

from __future__ import annotations

import csv
from pathlib import Path

import numpy as np
import pandas as pd
import pyarrow.parquet as pq

from . import lef26

MOVING_PCT = 2.0        # N-100 % 대비. 6500 rpm 가정 시 130 rpm
MERGE_GAP_S = 10.0
MIN_RUN_S = 20.0
ON_GAP_S = 1.0          # 0x181 메시지가 이보다 오래 끊기면 컨트롤러가 꺼진 것으로 본다
SIGNALS = ["n_cmd_pct", "n_act_pct", "iq_cmd_a", "iq_act_a", "vout_raw", "vdc_v", "t_igbt_c", "t_motor_c"]


def _runs(t_s: np.ndarray, moving: np.ndarray) -> list[tuple[float, float]]:
    tm = t_s[moving]
    if not len(tm):
        return []
    br = np.flatnonzero(np.diff(tm) > MERGE_GAP_S)
    starts = np.concatenate([[tm[0]], tm[br + 1]])
    ends = np.concatenate([tm[br], [tm[-1]]])
    return [(float(a), float(b)) for a, b in zip(starts, ends) if b - a >= MIN_RUN_S]


def _on_seconds(t_s: np.ndarray) -> float:
    """컨트롤러가 켜져 있던 시간: 메시지 간격이 ON_GAP_S 이하인 구간의 합."""
    d = np.diff(np.sort(t_s))
    return float(d[d <= ON_GAP_S].sum())


def analyze_recording(rec_dir: Path):
    m = pq.read_table(rec_dir / "motor.parquet", columns=["t_rel_ms", "t_abs", "signal", "value"]).to_pandas()
    if m.empty:
        return [], None
    m["t_s"] = m["t_rel_ms"] / 1000.0
    sig = {k: v.sort_values("t_s") for k, v in m.groupby("signal")}

    on_s = _on_seconds(m["t_s"].to_numpy())
    capture = {"recording_id": rec_dir.name, "controller_on_s": round(on_s, 1)}
    # 신호별 초당 기록 횟수. 매뉴얼 기본 주기(10/100 ms)와 맞지 않는 레코딩이 많아 '손실률'로 해석하지 않는다
    for k in SIGNALS:
        n = len(sig[k]) if k in sig else 0
        capture[k + "_hz"] = round(n / on_s, 2) if on_s > 0 else None

    n_act = sig.get("n_act_pct")
    if n_act is None or len(n_act) < 50:
        return [], capture

    b = pq.read_table(rec_dir / "bms.parquet").to_pandas()
    b = b[b["valid"] & (b["cell_min_v"] >= 2.0) & (b["cell_max_v"] <= 4.3)]   # 약한 체크섬 보완
    b = b.assign(t_s=b["t_rel_ms"] / 1000.0).sort_values("t_s")

    def window(df, a, z):
        return df[(df["t_s"] >= a) & (df["t_s"] <= z)] if df is not None else None

    runs = []
    t = n_act["t_s"].to_numpy()
    for i, (a, z) in enumerate(_runs(t, n_act["value"].abs().to_numpy() > MOVING_PCT), 1):
        n = window(n_act, a, z)
        iq, vdc, tm = (window(sig.get(k), a, z) for k in ("iq_act_a", "vdc_v", "t_motor_c"))
        bw = window(b, a, z)
        hv = vdc[vdc["value"] > 150] if vdc is not None else None
        t_abs = n["t_abs"].iloc[0]
        runs.append({
            "recording_id": rec_dir.name, "run": i,
            "t_abs_start": None if pd.isna(t_abs) else t_abs.isoformat(timespec="seconds"),
            "t_start_s": round(a, 1), "t_end_s": round(z, 1), "duration_s": round(z - a, 1),
            "n_max_pct": round(float(n["value"].abs().max()), 1),
            "n_max_rpm": round(float(n["value"].abs().max()) / 100 * lef26.N_MAX_RPM),
            "v_max_kmh": round(float(n["value"].abs().max()) * lef26.KMH_PER_PCT, 1),
            "speed_samples": len(n),
            "speed_hz": round(len(n) / (z - a), 2),
            "iq_peak_a": None if iq is None or iq.empty else round(float(iq["value"].abs().max()), 0),
            "vdc_min_v": None if hv is None or hv.empty else round(float(hv["value"].min()), 1),
            "vdc_max_v": None if hv is None or hv.empty else round(float(hv["value"].max()), 1),
            "soc_start_pct": None if bw.empty else float(bw["soc_pct"].iloc[:50].median()),
            "soc_end_pct": None if bw.empty else float(bw["soc_pct"].iloc[-50:].median()),
            "cell_min_v": None if bw.empty else round(float(bw["cell_min_v"].min()), 4),
            "bms_temp_max_c": None if bw.empty else int(bw["temp_c"].max()),
            "t_motor_max_c": None if tm is None or tm.empty else int(tm["value"].max()),
        })
    return runs, capture


def run_event(event_dir: Path):
    norm = event_dir / "normalized"
    runs, captures = [], []
    # 한 이벤트에 다른 장비(온보드 영상 등)가 섞일 수 있다 - 모터 테이블이 있는 레코딩만 본다
    for rec in sorted(p for p in norm.iterdir() if p.is_dir() and (p / "motor.parquet").exists()):
        r, c = analyze_recording(rec)
        runs += r
        if c is not None:
            captures.append(c)
    out = event_dir / "derived"
    out.mkdir(exist_ok=True)
    runs_df, cap_df = pd.DataFrame(runs), pd.DataFrame(captures)
    runs_df.to_csv(out / "motor_runs.csv", index=False, encoding="utf-8-sig", quoting=csv.QUOTE_MINIMAL)
    cap_df.to_csv(out / "motor_signal_rates.csv", index=False, encoding="utf-8-sig")
    return runs_df, cap_df
