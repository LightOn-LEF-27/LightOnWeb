"""한 이벤트 안의 장비 시계 차이를 잰다 — 25Evo 대회 (에너지미터 · Orion · Monolith · STM32).

2026 FSK 25Evo 는 시계가 셋이었고 서로 어긋나 있었다. 같은 물리량을 두 장비가 같이 쟀으면 파형을 겹쳐
차이를 구할 수 있다.

    에너지미터 ↔ Orion     팩 전류 (둘 다 HV 전류)
    Monolith  ↔ STM32     GPS 속도 ↔ 모터 회전수 (STM32 는 에너지미터 시계로 정렬돼 있다)

결과는 `derived/clock_offsets.csv` — 세션마다 "이 장비 시각에 몇 초를 더하면 Orion 시계가 되는가".
**어느 시계가 참인지는 모른다.** Orion 을 기준으로 삼는 건 인터넷에 붙은 장비라 가장 그럴듯해서다 (추정).

파형 없이도 되는 짝짓기가 하나 더 있다 (`power_cycles`, 2026-10-02): 25Evo 의 Monolith 와 에너지미터는 **같은 전원 스위치**에
물려 있어 같이 켜지고 같이 꺼진다. 그래서 "전원이 들어온 뒤 마지막 기록까지 흐른 시간" 이 같은 레코딩끼리가 짝이고,
두 장비의 틱(부팅 후 ms)은 그대로 같은 시간축이다. 결과는 `derived/power_cycles.csv`.
"""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pandas as pd
import pyarrow.parquet as pq

REFERENCE = "orion"


def _per_second(path: Path, col: str, absval: bool = False, where: str | None = None) -> pd.Series:
    cols = ["t_abs", col] + ([where] if where else [])
    df = pq.read_table(path, columns=cols).to_pandas()
    if where:
        df = df[df[where]]
    df = df.dropna(subset=["t_abs", col])
    s = df.set_index(df["t_abs"].dt.floor("s"))[col]
    s = s.abs() if absval else s
    return s.groupby(level=0).mean()


def best_offset(a: pd.Series, b: pd.Series, lags: range, min_overlap: int = 30,
                min_frac: float = 0.8) -> tuple[int, float, int] | None:
    """b 의 시각에 lag 초를 더해 a 와 겹쳤을 때 상관이 최대인 lag. 반환 (lag, 상관, 겹침).

    짧은 쪽의 min_frac 이상이 겹쳐야 후보로 본다. 일부만 겹친 자리에서 우연히 상관이 높게 나온다
    (처음엔 이 조건 없이 일부 세션이 수십 ~ 수백 초씩 튀었다).
    """
    need = max(min_overlap, int(min_frac * min(len(a), len(b))))
    best = None
    for lag in lags:
        j = pd.concat([a, b.set_axis(b.index + pd.Timedelta(seconds=lag))], axis=1, join="inner").dropna()
        if len(j) < need or j.iloc[:, 0].std() < 1e-6 or j.iloc[:, 1].std() < 1e-6:
            continue
        c = float(j.corr().iloc[0, 1])
        if best is None or c > best[1]:
            best = (lag, c, len(j))
    return best


def _overlapping(recs: list[Path], start: pd.Timestamp, end: pd.Timestamp, slack_s: int = 600) -> list[Path]:
    out = []
    for p in recs:
        t = pq.read_table(p, columns=["t_abs"]).column(0).to_pandas().dropna()
        if len(t) and t.iloc[0] < end + pd.Timedelta(seconds=slack_s) and t.iloc[-1] > start - pd.Timedelta(seconds=slack_s):
            out.append(p)
    return out


def measure_event(event_dir: Path, min_corr: float = 0.5) -> pd.DataFrame:
    norm = event_dir / "normalized"
    rows = []
    orion = sorted(norm.glob("*_orion"))

    # 에너지미터 ↔ Orion : 팩 전류. HV 가 켜진 레코딩만
    for em in sorted(norm.glob("*_em")):
        e = _per_second(em / "energy.parquet", "current_a", where="valid")
        if e.abs().max() < 20:
            continue
        cands = _overlapping([o / "bms.parquet" for o in orion], e.index[0] - pd.Timedelta(seconds=400),
                             e.index[-1])
        best = None
        for op in cands:
            r = best_offset(_per_second(op, "current_a"), e, range(-400, 401))
            if r and (best is None or r[1] > best[1]):
                best = (*r, op.parent.name)
        if best and best[1] >= min_corr:
            rows.append({"device": "energymeter", "recording_id": em.name, "matched": best[3],
                         "signal": "팩 전류", "offset_to_orion_s": best[0], "corr": round(best[1], 3),
                         "overlap_s": best[2]})

    # Monolith ↔ STM32(에너지미터 시계) : GPS 속도 ↔ |rpm|. Orion 기준으로 바꾸려면 에너지미터 차이를 더한다
    em_off = pd.Series({r["recording_id"]: r["offset_to_orion_s"] for r in rows if r["device"] == "energymeter"})
    for mono in sorted(p for p in norm.iterdir() if p.is_dir() and (p / "gps.parquet").exists()):
        g = _per_second(mono / "gps.parquet", "speed_kmh", where="fix")
        if len(g) < 60 or g.max() < 20:
            continue
        for st in sorted(norm.glob("stm32_log*")):
            m = pq.read_table(st / "motor.parquet", columns=["t_abs"]).column(0).to_pandas()
            if m.isna().all():
                continue
            meta = pd.read_json(st / "meta.json", typ="series")
            em_rec = meta["time"]["alignment"]["em_recording"]
            if em_rec not in em_off:
                continue
            r = best_offset(_per_second(st / "motor.parquet", "rpm", absval=True), g, range(-400, 401),
                            min_overlap=60, min_frac=0.5)
            if r and r[1] >= min_corr:
                rows.append({"device": "monolith", "recording_id": mono.name, "matched": st.name,
                             "signal": "GPS 속도 ↔ 모터 rpm", "offset_to_orion_s": int(r[0] + em_off[em_rec]),
                             "corr": round(r[1], 3), "overlap_s": r[2]})

    # 열 이름을 고정한다 — 짝이 하나도 없는 이벤트(Orion 이 없는 테스트위크)에서도 머리글은 남아야 다시 읽힌다
    out = pd.DataFrame(rows, columns=["device", "recording_id", "matched", "signal", "offset_to_orion_s",
                                      "corr", "overlap_s"])
    (event_dir / "derived").mkdir(exist_ok=True)
    out.to_csv(event_dir / "derived" / "clock_offsets.csv", index=False, encoding="utf-8-sig")
    return out


def power_cycles(event_dir: Path, tol_s: float = 1.5, clock_tol_s: float = 5.0) -> pd.DataFrame:
    """Monolith ↔ 에너지미터를 '같은 전원 주기' 로 짝짓는다. 결과는 derived/power_cycles.csv.

    짝의 조건
      1. 전원이 들어온 뒤 마지막 기록까지의 시간이 tol_s 안에서 같다 (Monolith `last_ts_ms` ↔ 에너지미터 `last_tick_ms`)
      2. 그 짝이 말하는 두 시계의 차이(에너지미터 − Monolith)가 같은 날의 다른 짝들과 clock_tol_s 안에서 같다
         — 몇 초짜리 짧은 레코딩은 길이만으로는 우연히 맞는다
    열: energymeter · monolith (레코딩), on_s (켜져 있던 시간), off_diff_s (에너지미터가 더 늦게 꺼진 시간),
        em_minus_monolith_s (같은 순간에 에너지미터 시계가 Monolith 시계보다 앞선 초. Monolith 시각 = 에너지미터 시각 − 이 값)
    틱은 그대로 같은 시간축이다: Monolith t_rel_ms ≈ 에너지미터 t_rel_ms − Monolith boot_ts_ms (정밀도는 off_diff_s 만큼, 1 초 안쪽).
    """
    norm = event_dir / "normalized"
    monos, ems = [], []
    for p in sorted(q for q in norm.iterdir() if q.is_dir()):
        mp = p / "meta.json"
        if not mp.exists():
            continue
        m = json.loads(mp.read_text(encoding="utf-8"))
        t = m.get("time", {})
        if p.name.endswith("_em") and t.get("last_tick_ms") is not None:
            ems.append({"id": p.name, "on_s": t["last_tick_ms"] / 1000,
                        "power_on": pd.Timestamp(t["t_abs_start"])})
        elif "last_ts_ms" in t and "boot_ts_ms" in t:
            monos.append({"id": p.name, "on_s": t["last_ts_ms"] / 1000,
                          "power_on": pd.Timestamp(t["t_abs_start"]) - pd.Timedelta(milliseconds=t["boot_ts_ms"])})
    cands = [{"energymeter": e["id"], "monolith": m["id"], "on_s": round(m["on_s"], 1),
              "off_diff_s": round(e["on_s"] - m["on_s"], 2), "day": e["power_on"].strftime("%Y-%m-%d"),
              "em_minus_monolith_s": round((e["power_on"] - m["power_on"]).total_seconds(), 1)}
             for e in ems for m in monos if abs(e["on_s"] - m["on_s"]) <= tol_s]
    out = pd.DataFrame(cands, columns=["energymeter", "monolith", "on_s", "off_diff_s", "day", "em_minus_monolith_s"])
    if len(out):
        # 길게 켜져 있던 짝(우연히 맞을 수 없다)으로 그날의 시계 차이를 정하고, 거기서 벗어난 짝을 버린다
        ref = out[out["on_s"] >= 60].groupby("day")["em_minus_monolith_s"].median()
        ref = ref.reindex(out["day"].unique()).fillna(out[out["on_s"] >= 60]["em_minus_monolith_s"].median())
        out = out[(out["em_minus_monolith_s"] - out["day"].map(ref)).abs() <= clock_tol_s].reset_index(drop=True)
    (event_dir / "derived").mkdir(exist_ok=True)
    out.to_csv(event_dir / "derived" / "power_cycles.csv", index=False, encoding="utf-8-sig")
    return out
