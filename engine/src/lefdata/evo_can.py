"""25Evo 차량 CAN — Monolith 가 받은 프레임 그대로(normalized/<레코딩>/frames.parquet 의 kind = CAN) → 신호 표 (팀 계층).

    lefdata evo-can --event E
        → normalized/<레코딩>/apps.parquet          가속페달 (APPS 보드 진단 0x7A0 · 0x7A1 · 0x7A2, 20 Hz)
                              orion_can.parquet     Orion 요약 · 팩 V/I (0x446 · 0x447, 96 ms)
                              orion_stream.parquet  Orion Connect 스트림 (0x6D0, 평균 80 Hz 대 — 팩 V/I + 슬롯 하나씩)

정의의 출처 (팀 문서):
    0x7A0 ~ 0x7A2   25Evo APPS 보드 저장소의 진단 DBC (리틀 엔디언, 세 프레임이 같은 순번)
    0x446 · 0x447   Orion BMS 2 Utility 가 내보낸 DBC (빅 엔디언). 단 팩 전류 · 전력은 DBC 에 부호 없음으로 적혀 있지만
                    실제로는 부호 있는 16비트다 (회생 때 0x8000 이상) — 여기서는 부호 있는 값으로 읽는다
    0x6D0           Orion 의 Connect 모듈용 스트림 — 공식 정의가 없어 Orion 로그와 맞대 푼 것. [0] 슬롯 번호 · [1..2] 팩 전류
                    (방전 −) · [3..4] 팩 전압 · [5..6] 슬롯 값. 슬롯 표는 STREAM_SLOTS (맞댄 확신이 높은 것만)

체크섬: 0x446 · 0x447 = 바이트 0 ~ 6 의 합, 0x6D0 = (ID + 8 + 바이트 0 ~ 6 의 합) 의 하위 8비트. 틀린 프레임은 버리지 않고 checksum_ok 에 표시한다.
시각은 Monolith 시계 그대로 (같은 레코딩의 GPS 와 같은 축) — 다른 장비에 맞추는 것은 세션을 만드는 쪽이 한다.
"""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pandas as pd
import pyarrow.parquet as pq

from .writer import write_json

APPS_RAW, APPS_SCALED, APPS_STATUS = 0x7A0, 0x7A1, 0x7A2
ORION_SUMMARY, ORION_VI, ORION_STREAM = 0x446, 0x447, 0x6D0
TORQUE_FULL = 32767          # BAMOCAR 토크 명령의 100 %
TORQUE_SIGN = -1             # 25Evo 는 전진 토크 명령이 음수다 (APPS 보드 설정) — 가속 + 로 바꿀 때 곱한다
JOIN_MS = 40                 # 같은 순번의 0x7A1 · 0x7A2 를 0x7A0 에 붙일 때 이 안에서 찾는다 (20 Hz = 50 ms 간격)
APPS_FLAGS = {0: "VALID", 1: "CONFIGURED", 2: "ADC_OK", 3: "CAN_TX_OK", 4: "CAN_TX_MUTED", 5: "FAULT_LATCHED"}
APPS_FAULTS = {0x01: "NOT_CONFIGURED", 0x02: "ADC_READ_FAULT", 0x04: "SIGNAL1_RANGE_FAULT", 0x08: "SIGNAL2_RANGE_FAULT",
               0x10: "DIFF_FAULT", 0x20: "FAULT_LATCHED", 0x40: "CAN_TX_FAULT"}
# 0x6D0 슬롯 → (열 이름, 배율, 부호 있음). Orion 로그와 같은 값이 90 % 넘게 나온 슬롯만 (값이 세션 내내 같았던 DCL · 최대 DCL 은 가르지 못해 뺐다)
STREAM_SLOTS = {10: ("ccl_a", 1.0, False), 15: ("soc_pct", 0.5, False), 16: ("pack_ah", 0.1, False), 17: ("pack_resistance_ohm", 0.001, False),
                18: ("dod_pct", 0.5, False), 27: ("dcl_kw", 0.1, False), 40: ("temp_max_c", 1.0, True), 41: ("temp_min_c", 1.0, True),
                42: ("temp_avg_c", 1.0, True), 45: ("bms_heatsink_c", 1.0, True), 46: ("temp_max_id", 1.0, False), 47: ("temp_min_id", 1.0, False),
                56: ("cell_r_min_mohm", 0.01, False), 57: ("cell_r_max_mohm", 0.01, False), 58: ("cell_r_avg_mohm", 0.01, False)}
TABLES = ("apps", "orion_can", "orion_stream")


# ---------------------------------------------------------------- 프레임 → 값 (데이터 8 바이트 배열 [n × 8])

def _u16le(d: np.ndarray, i: int) -> np.ndarray:
    return d[:, i].astype(np.int64) | (d[:, i + 1].astype(np.int64) << 8)


def _u16be(d: np.ndarray, i: int) -> np.ndarray:
    return (d[:, i].astype(np.int64) << 8) | d[:, i + 1].astype(np.int64)


def _s16(u: np.ndarray) -> np.ndarray:
    return np.where(u >= 0x8000, u - 0x10000, u)


def checksum_ok(d: np.ndarray, can_id: int | None = None) -> np.ndarray:
    """마지막 바이트 = 바이트 0 ~ 6 의 합 (can_id 를 주면 + ID + 8) 의 하위 8비트인가."""
    s = d[:, :7].astype(np.int64).sum(axis=1) + (can_id + 8 if can_id is not None else 0)
    return (s & 0xFF) == d[:, 7]


def decode_apps_raw(d: np.ndarray) -> pd.DataFrame:
    """0x7A0 — 원시 ADC 2채널 · 최종 페달값(%) · 플래그 · 순번."""
    return pd.DataFrame({"apps1_raw": _u16le(d, 0), "apps2_raw": _u16le(d, 2), "pedal_pct": _u16le(d, 4) * 0.1,
                         "flags": d[:, 6].astype(np.int64), "counter": d[:, 7].astype(np.int64)})


def decode_apps_scaled(d: np.ndarray) -> pd.DataFrame:
    """0x7A1 — 보정된 위치 2채널(%) · BAMOCAR 로 보낸 토크 명령(부호 있는 원시값) · 순번."""
    return pd.DataFrame({"apps1_pct": _u16le(d, 0) * 0.1, "apps2_pct": _u16le(d, 2) * 0.1, "torque_cmd_raw": _s16(_u16le(d, 4)),
                         "counter": d[:, 7].astype(np.int64)})


def decode_apps_status(d: np.ndarray) -> pd.DataFrame:
    """0x7A2 — 상태 · 확정된 고장 비트마스크 · 진단 송신 실패 누적 · 순번."""
    return pd.DataFrame({"status": _u16le(d, 0), "fault_latched": _u16le(d, 2), "diag_tx_fail": _u16le(d, 4),
                         "counter": d[:, 7].astype(np.int64)})


def decode_orion_summary(d: np.ndarray) -> pd.DataFrame:
    """0x446 — 팩 전력(kW, 방전 +) · 최저 셀(V) · 최고 · 최저 온도(℃) · SOC(%)."""
    return pd.DataFrame({"power_kw": _s16(_u16be(d, 0)) * 0.1, "cell_min_v": _u16be(d, 2) * 1e-4, "temp_max_c": d[:, 4].astype(np.int8).astype(float),
                         "temp_min_c": d[:, 5].astype(np.int8).astype(float), "soc_pct": d[:, 6] * 0.5, "checksum_ok": checksum_ok(d)})


def decode_orion_vi(d: np.ndarray) -> pd.DataFrame:
    """0x447 — 팩 전압(V) · 팩 전류(A, 방전 +, 순간값)."""
    return pd.DataFrame({"pack_v": _u16be(d, 0) * 0.1, "current_a": _s16(_u16be(d, 2)) * 0.1, "checksum_ok": checksum_ok(d)})


def decode_orion_stream(d: np.ndarray) -> pd.DataFrame:
    """0x6D0 — 슬롯 번호 · 팩 전류(A, 방전 + 로 뒤집음) · 팩 전압(V) · 슬롯 원시값(16비트)."""
    return pd.DataFrame({"slot": d[:, 0].astype(np.int64), "current_a": -_s16(_u16be(d, 1)) * 0.1, "pack_v": _u16be(d, 3) * 0.1,
                         "slot_raw": _u16be(d, 5), "checksum_ok": checksum_ok(d, ORION_STREAM)})


def stream_signals(stream: pd.DataFrame, t_col: str = "t_s") -> dict[str, tuple[np.ndarray, np.ndarray]]:
    """0x6D0 표 → 슬롯 값마다 (시각, 값). 체크섬이 맞는 프레임만."""
    ok = stream[stream["checksum_ok"]]
    out = {}
    for slot, (name, scale, signed) in STREAM_SLOTS.items():
        s = ok[ok["slot"] == slot]
        if len(s):
            raw = s["slot_raw"].to_numpy(np.int64)
            out[name] = (s[t_col].to_numpy(float), (_s16(raw) if signed else raw) * scale)
    return out


def fault_names(mask: int) -> list[str]:
    return [name for bit, name in APPS_FAULTS.items() if int(mask) & bit]


# ---------------------------------------------------------------- 레코딩 하나

def _frames(rec_dir: Path) -> pd.DataFrame:
    f = pq.read_table(rec_dir / "frames.parquet", columns=["t_rel_ms", "t_abs", "kind", "can_id", "payload", "valid", "dup"]).to_pandas()
    f = f[(f["kind"] == "CAN") & f["valid"] & ~f["dup"].fillna(False).astype(bool)]
    return f.assign(can_id=f["can_id"].astype("int64"))


def _data(f: pd.DataFrame, can_id: int) -> tuple[pd.DataFrame, np.ndarray]:
    """그 ID 의 행과 데이터 8 바이트 배열 (Monolith CAN 레코드: id u32 · 확장 · 원격 · 길이 · 예약 · 데이터 8)."""
    s = f[f["can_id"] == can_id]
    d = np.frombuffer(b"".join(s["payload"]), dtype=np.uint8).reshape(-1, 16)[:, 8:16] if len(s) else np.zeros((0, 8), np.uint8)
    return s[["t_rel_ms", "t_abs"]].reset_index(drop=True), d


def decode_recording(rec_dir: Path) -> dict[str, pd.DataFrame]:
    """frames.parquet → {apps, orion_can, orion_stream} (있는 것만)."""
    f = _frames(rec_dir)
    out = {}
    t, d = _data(f, APPS_RAW)
    if len(d):
        apps = pd.concat([t, decode_apps_raw(d)], axis=1)
        for cid, dec in ((APPS_SCALED, decode_apps_scaled), (APPS_STATUS, decode_apps_status)):
            t2, d2 = _data(f, cid)
            if len(d2):
                other = pd.concat([t2[["t_rel_ms"]], dec(d2)], axis=1).rename(columns={"t_rel_ms": "_t"}).sort_values("_t")
                apps = pd.merge_asof(apps.sort_values("t_rel_ms"), other, left_on="t_rel_ms", right_on="_t", by="counter",
                                     direction="nearest", tolerance=JOIN_MS).drop(columns="_t")
        out["apps"] = apps
    t, d = _data(f, ORION_VI)
    if len(d):
        vi = pd.concat([t, decode_orion_vi(d)], axis=1).sort_values("t_rel_ms")
        t2, d2 = _data(f, ORION_SUMMARY)
        if len(d2):
            sm = pd.concat([t2[["t_rel_ms"]], decode_orion_summary(d2)], axis=1).rename(columns={"t_rel_ms": "_t", "checksum_ok": "summary_ok"})
            vi = pd.merge_asof(vi, sm.sort_values("_t"), left_on="t_rel_ms", right_on="_t", direction="nearest", tolerance=JOIN_MS).drop(columns="_t")
        out["orion_can"] = vi
    t, d = _data(f, ORION_STREAM)
    if len(d):
        out["orion_stream"] = pd.concat([t, decode_orion_stream(d)], axis=1)
    return out


def summarize(tables: dict[str, pd.DataFrame]) -> dict:
    """meta.json 에 남길 요약 — 행 수 · 체크섬 · 순번 빠짐 · APPS 고장."""
    s: dict = {}
    if "apps" in tables:
        a = tables["apps"]
        gap = (np.diff(a["counter"].to_numpy()) % 256)
        faults = int(np.bitwise_or.reduce(a["fault_latched"].fillna(0).astype(np.int64).to_numpy())) if "fault_latched" in a else 0
        s["apps"] = {"rows": int(len(a)), "counter_missed": int((gap - 1).clip(min=0).sum()), "fault_latched_any": fault_names(faults),
                     "valid_share": round(float(((a["flags"] & 1) > 0).mean()), 4), "pedal_max_pct": round(float(a["pedal_pct"].max()), 1)}
    for name in ("orion_can", "orion_stream"):
        if name in tables:
            t = tables[name]
            s[name] = {"rows": int(len(t)), "checksum_fail": int((~t["checksum_ok"]).sum())}
    return s


def convert_event(event_dir: Path) -> list[dict]:
    """이벤트의 Monolith 레코딩마다 CAN 을 풀어 표를 쓴다 → 레코딩마다 요약 한 줄. CAN 이 없는 레코딩은 건너뛴다."""
    rows = []
    for meta_path in sorted((Path(event_dir) / "normalized").glob("*/meta.json")):
        rec_dir = meta_path.parent
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        if not meta.get("source", {}).get("logger", "").startswith("Monolith") or not (rec_dir / "frames.parquet").exists():
            continue
        tables = decode_recording(rec_dir)
        for name in TABLES:                                     # 다시 돌리면 옛 표가 남지 않게
            (rec_dir / f"{name}.parquet").unlink(missing_ok=True)
        for name, t in tables.items():
            t.to_parquet(rec_dir / f"{name}.parquet", index=False, compression="zstd")
        meta["can_decoded"] = summarize(tables) if tables else {}
        write_json(meta, meta_path)
        if tables:
            rows.append({"recording_id": rec_dir.name, **{k: v.get("rows", 0) for k, v in meta["can_decoded"].items()}})
    return rows
