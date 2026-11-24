"""25Evo 차량 CAN — Monolith 가 받은 프레임 그대로(normalized/<레코딩>/frames.parquet 의 kind = CAN) → 신호 표 (팀 계층).

    lefdata evo-can --event E
        → normalized/<레코딩>/apps.parquet          가속페달 (APPS 보드 진단 0x7A0 · 0x7A1 · 0x7A2, 20 Hz)
                              orion_can.parquet     Orion 요약 · 팩 V/I (0x446 · 0x447, 96 ms)
                              orion_stream.parquet  Orion Connect 스트림 (0x6D0, 평균 80 Hz 대 — 팩 V/I + 슬롯 하나씩)
                              bamocar.parquet       모터 컨트롤러 응답 (0x181, 레지스터마다 10 Hz) — 시각 · 레지스터 · 원시값, 한 줄에 하나
                              thermistor.parquet    Orion 써미스터 확장 모듈 (0x1838F380 써미스터 하나씩 10 Hz · 0x1839F380 모듈 요약) — LEF-26 의 같은 표와 같은 열
                              orion_cells.parquet   Orion 셀 방송 (0x036, 셀 하나씩 돌아가며) — 셀마다 순간 전압 · 개방 전압 · 내부 저항 · 균형 중

정의의 출처 (팀 문서):
    0x7A0 ~ 0x7A2   25Evo APPS 보드 저장소의 진단 DBC (리틀 엔디언, 세 프레임이 같은 순번)
    0x446 · 0x447   Orion BMS 2 Utility 가 내보낸 DBC (빅 엔디언). 단 팩 전류 · 전력은 DBC 에 부호 없음으로 적혀 있지만
                    실제로는 부호 있는 16비트다 (회생 때 0x8000 이상) — 여기서는 부호 있는 값으로 읽는다
    0x6D0           Orion 의 Connect 모듈용 스트림 — 공식 정의가 없어 Orion 로그와 맞대 푼 것. [0] 슬롯 번호 · [1..2] 팩 전류
                    (방전 −) · [3..4] 팩 전압 · [5..6] 슬롯 값. 슬롯 표는 STREAM_SLOTS (맞댄 확신이 높은 것만)
    0x181           BAMOCAR 주기 응답 (Unitek CAN 매뉴얼). [0] 레지스터 번호 · 리틀 엔디언 값 — DLC 4 = 부호 있는 16비트, DLC 6 = 부호 없는 32비트.
                    0x66 (필터 없는 DC 버스) · 0x27 · 0x28 (Iq · Id) 는 요청할 때만 온다 — 0x66 은 0xEB 와 같은 배율. 상태 · 오류 비트는 Unitek N-Drive 매뉴얼. 환산은 LEF-26 과 같은 제조사 표 (lef26) · 모터 온도는 evo_stm32 의 KTY81 표
    0x1838F380 · 0x1839F380   Orion 써미스터 확장 모듈의 CAN 문서 (lef26 과 같은 출처) — 써미스터 하나씩 내는 방송 · 모듈 → BMS 요약. 확장 ID
    0x036           Orion BMS 2 Utility 도움말의 셀 방송 (Cell Broadcast Message). [0] 셀 번호 (0 부터 — 표에는 + 1 해 Orion 이 0x6D0 에 내는
                    최고 · 최저 저항 셀 번호와 같은 번호로) · [1..2] 순간 전압 0.1 mV · [3..4] 내부 저항 0.01 mΩ (15비트, 최상위 비트 = 균형 중) ·
                    [5..6] 개방 전압 0.1 mV. 빅 엔디언

Orion 이 켜진 직후 약 1 초 (0x446 열 프레임) 는 써미스터를 아직 읽지 않아 최고 · 최저 온도가 25 / 25 ℃ 로 나온다 (0x6D0 의 온도 슬롯도 25, 최고 · 최저 번호는 0).
첫 프레임의 최저 셀이 다른 값으로 튀기도 한다. 그 구간은 orion_boot 로 표시하고 그 온도 · 튄 셀 전압은 빈 값으로 둔다 (팩 전압 · 전류는 그대로).

셀 저항은 Orion 이 부하 때 재서 바꾸는 추정값이다. 아직 재지 않았을 때는 모든 셀이 같은 값(프로파일의 저항표 값으로 보인다)을 낸다 —
한 바퀴의 셀이 모두 같은 저항이면 기본값으로 보고, 셀마다 그 값이 바뀌기 전까지 빈 값으로 둔다 (resistance_default).

체크섬: 0x446 · 0x447 = 바이트 0 ~ 6 의 합, 0x6D0 · 0x036 = (ID + 8 + 바이트 0 ~ 6 의 합) 의 하위 8비트, 0x1839F380 = 바이트 0 ~ 6 의 합 + 0x39 + 8
(0x1838F380 은 체크섬이 없다). 틀린 프레임은 버리지 않고 checksum_ok 에 표시한다.
시각은 Monolith 시계 그대로 (같은 레코딩의 GPS 와 같은 축) — 다른 장비에 맞추는 것은 세션을 만드는 쪽이 한다.
"""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pandas as pd
import pyarrow.parquet as pq

from .evo_stm32 import N100_RPM, RPM_FULL_SCALE, motor_temp_c
from .lef26 import BAMOCAR_I_A_PER_NUM, BAMOCAR_VDC_V_PER_NUM, igbt_temp_c
from .writer import write_json

APPS_RAW, APPS_SCALED, APPS_STATUS = 0x7A0, 0x7A1, 0x7A2
ORION_SUMMARY, ORION_VI, ORION_STREAM = 0x446, 0x447, 0x6D0
ORION_BOOT_TEMP_C = 25        # Orion 이 켜진 직후 써미스터를 읽기 전에 내는 최고 · 최저 온도
ORION_BOOT_MAX = 15           # 그 기본값이 이어지는 길이의 상한 (켜진 뒤 약 1 s = 열 프레임 남짓). 이보다 오래 25 / 25 면 실제 온도로 본다
ORION_BOOT_GAP_MS = 1500      # 0x446 (96 ms) 이 이만큼 끊겼다 다시 오면 Orion 이 다시 켜진 것으로 본다
ORION_BOOT_CELL_V = 0.05      # 켠 직후 구간에서 최저 셀이 그 구간의 중앙값과 이만큼 넘게 다르면 튄 값으로 본다
THERM_SINGLE, THERM_MODULE = 0x1838F380, 0x1839F380   # 써미스터 확장 모듈 (모듈 0) — 하나씩 방송 · 모듈 → BMS 요약
THERM_MODULE_CHECK = 0x39 + 8                          # 모듈 → BMS 체크섬 = 바이트 합 + 0x39 + 길이 (lef26.THERMISTOR_CHECK 와 같다)
ORION_CELLS = 0x036           # Orion 셀 방송 — 셀 하나씩 차례로 (번호가 다시 작아지면 새 바퀴)
CELL_SWEEP_GAP_MS = 1000      # 셀 방송이 이만큼 끊겼다 다시 오면 새 바퀴로 본다
CELL_DEFAULT_MIN_CELLS = 10   # 기록 첫머리에서 잇단 이만큼의 프레임이 모두 같은 저항이면 아직 재지 않은 기본값 (잰 값은 셀마다 0.01 mΩ 단위로 다르다)
CELL_SWEEP_MIN_SHARE = 0.9    # 한 바퀴에 셀이 이 비율 넘게 와야 그 바퀴의 최고 · 최저를 낸다 (덜 온 바퀴는 최저 셀을 놓칠 수 있다)
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
BAMOCAR = 0x181
REG_SPEED, REG_STATUS, REG_ERROR, REG_VDC, REG_T_IGBT, REG_T_MOTOR = 0x30, 0x40, 0x8F, 0xEB, 0x4A, 0x49
REG_IQ, REG_ID = 0x27, 0x28  # 토크 · 자속 전류 (Iq · Id actual) — LEF-26 과 같은 배율. 기록은 구동할 때 − (세션이 뒤집는다). 요청할 때만 온다
REG_VDC_FAST = 0x66          # DC 버스 전압 — 필터 없는 값 (0xEB 는 저역 필터를 거친 값). 배율은 0xEB 와 같다. 누가 요청할 때만 온다
STATUS_ENABLED, STATUS_READY = 0, 14                    # 0x40 비트: Ena (구동 허용) · Rdy (BTB 닫힘 — 오류가 나면 꺼진다)
ERROR_VOLTAGE_MIN, ERROR_VOLTAGE_MAX = 5, 8             # 0x8F 비트. 5 = 전원 전압 없음 — HV 를 꺼 두면 늘 켜져 있어 고장으로 세지 않는다
BAMOCAR_ERRORS = {0: "Eprom Read Error", 1: "HW Fault detected", 2: "RFE input not present", 3: "CAN TimeOut Error", 4: "Feedback Signal Error",
                  5: "Mains Voltage Min. Limit", 6: "Motor-Temp. Max. Limit", 7: "IGBT-Temp. Max. Limit", 8: "Mains Voltage Max. Limit",
                  9: "Critical AC Current", 10: "Race Away detected", 11: "ECode TimeOut Error", 12: "Watchdog Reset", 13: "I Offset problem",
                  14: "Internal HW voltage problem", 15: "Bleed resistor overload"}
BAMOCAR_WARNINGS = {16: "Parameter conflict detected", 17: "Special CPU Fault", 18: "RFE input not present", 19: "Auxiliary Voltage Min. Limit",
                    20: "Feedback Signal problem", 22: "Motor-Temperature (>87%)", 23: "IGBT Temperature (>87%)", 24: "Vout Saturation Max. Limit",
                    26: "SpeedActual resolution Limit", 27: "Check ECode ID: 0x94", 28: "Tripzone Glitch detected", 29: "ADC Sequencer problem",
                    30: "ADC Measurement problem", 31: "Bleeder resistor load (>87%)"}
TABLES = ("apps", "orion_can", "orion_stream", "bamocar", "thermistor", "orion_cells")


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


def orion_boot(t_ms: np.ndarray, temp_max: np.ndarray, temp_min: np.ndarray) -> np.ndarray:
    """0x446 프레임 (시각순) → Orion 이 켜진 직후의 기본값 프레임인가.

    Orion 이 켜질 때(레코딩 첫 프레임 · ORION_BOOT_GAP_MS 넘게 끊긴 뒤 첫 프레임)부터 최고 · 최저가 둘 다 25 ℃ 로 이어지는 앞부분 —
    ORION_BOOT_MAX 프레임 안에서 끝날 때만. 기록 중간의 25 / 25 나 오래 이어지는 25 / 25 는 실제 온도로 둔다.
    """
    t = np.asarray(t_ms, float)
    hit = (np.asarray(temp_max) == ORION_BOOT_TEMP_C) & (np.asarray(temp_min) == ORION_BOOT_TEMP_C)
    boot = np.zeros(len(t), bool)
    for s in np.r_[0, np.flatnonzero(np.diff(t) > ORION_BOOT_GAP_MS) + 1]:
        k = 0
        while s + k < len(t) and hit[s + k] and k <= ORION_BOOT_MAX:
            k += 1
        if 0 < k <= ORION_BOOT_MAX:
            boot[s:s + k] = True
    return boot


def _boot_spans(t_ms: np.ndarray, boot: np.ndarray) -> list[tuple[float, float]]:
    """켠 직후 구간마다 (직전 프레임 시각, 구간 뒤 첫 프레임 시각) — 그 사이의 다른 Orion 프레임(0x6D0)도 같은 구간으로 본다."""
    t, out, i = np.asarray(t_ms, float), [], 0
    while i < len(t):
        if not boot[i]:
            i += 1
            continue
        j = i
        while j < len(t) and boot[j]:
            j += 1
        out.append((t[i - 1] if i else -np.inf, t[j] if j < len(t) else np.inf))
        i = j
    return out


def mark_orion_boot(summary: pd.DataFrame, t_col: str = "t_rel_ms") -> pd.DataFrame:
    """0x446 표 (decode_orion_summary + 시각) → orion_boot 열을 붙이고 그 구간의 온도 · 튄 최저 셀을 빈 값으로."""
    s = summary.sort_values(t_col, kind="stable").reset_index(drop=True)
    boot = orion_boot(s[t_col].to_numpy(float), s["temp_max_c"].to_numpy(), s["temp_min_c"].to_numpy())
    cell = s["cell_min_v"].to_numpy(float).copy()
    for lo, hi in _boot_spans(s[t_col].to_numpy(float), boot):
        m = boot & (s[t_col].to_numpy(float) > lo) & (s[t_col].to_numpy(float) < hi)
        cell[m & (np.abs(cell - np.median(cell[m])) > ORION_BOOT_CELL_V)] = np.nan
    return s.assign(cell_min_v=cell, temp_max_c=s["temp_max_c"].mask(boot), temp_min_c=s["temp_min_c"].mask(boot), orion_boot=boot)


def decode_thermistor_single(d: np.ndarray) -> pd.DataFrame:
    """0x1838F380 — 써미스터 하나: [번호 (2 바이트, 빅 엔디언), 온도 ℃, 모듈 안 번호 (최상위 비트 = 고장), 그 순간 최저 · 최고 ℃, 가장 뜨거운 · 차가운 번호]."""
    return pd.DataFrame({"thermistor_id": _u16be(d, 0), "temp_c": d[:, 2].astype(np.int8).astype(np.int64),
                         "lowest_c": d[:, 4].astype(np.int8).astype(np.int64), "highest_c": d[:, 5].astype(np.int8).astype(np.int64),
                         "hottest_id": d[:, 6].astype(np.int64), "coldest_id": d[:, 7].astype(np.int64), "fault": (d[:, 3] & 0x80) > 0})


def decode_thermistor_module(d: np.ndarray) -> pd.DataFrame:
    """0x1839F380 — 모듈 → BMS: [모듈 번호, 최저, 최고, 평균 ℃, 켜진 수 (최상위 비트 = 고장), 가장 뜨거운 · 차가운 번호, 체크섬]."""
    return pd.DataFrame({"lowest_c": d[:, 1].astype(np.int8).astype(np.int64), "highest_c": d[:, 2].astype(np.int8).astype(np.int64),
                         "average_c": d[:, 3].astype(np.int8).astype(np.int64), "enabled": (d[:, 4] & 0x7F).astype(np.int64),
                         "hottest_id": d[:, 5].astype(np.int64), "coldest_id": d[:, 6].astype(np.int64), "fault": (d[:, 4] & 0x80) > 0,
                         "checksum_ok": ((d[:, :7].astype(np.int64).sum(axis=1) + THERM_MODULE_CHECK) & 0xFF) == d[:, 7]})


def decode_orion_cells(d: np.ndarray) -> pd.DataFrame:
    """0x036 — 셀 하나: 셀 번호 (1 부터) · 순간 전압 (V) · 개방 전압 (V) · 내부 저항 (mΩ) · 균형 중 (저항의 최상위 비트)."""
    return pd.DataFrame({"cell": d[:, 0].astype(np.int64) + 1, "cell_v": _u16be(d, 1) * 1e-4, "ocv_v": _u16be(d, 5) * 1e-4,
                         "resistance_mohm": (_u16be(d, 3) & 0x7FFF) * 0.01, "balancing": (d[:, 3] & 0x80) > 0,
                         "checksum_ok": checksum_ok(d, ORION_CELLS)})


def cell_sweeps(t_ms: np.ndarray, cell: np.ndarray) -> np.ndarray:
    """셀 방송 (시각순) → 바퀴 번호 (0 부터). 셀 번호가 줄거나 같으면 · CELL_SWEEP_GAP_MS 넘게 끊기면 새 바퀴."""
    t, c = np.asarray(t_ms, float), np.asarray(cell, np.int64)
    if not len(t):
        return np.zeros(0, np.int64)
    return np.cumsum(np.r_[True, (np.diff(c) <= 0) | (np.diff(t) > CELL_SWEEP_GAP_MS)]) - 1


def resistance_default(cell: np.ndarray, r: np.ndarray) -> np.ndarray:
    """셀 방송 (시각순) → 그 저항이 Orion 이 아직 재지 않은 기본값인가.

    잇단 프레임 셀 수만큼(= 한 바퀴 — 서버에 2 Hz 로 줄여 남은 기록도 같은 수면 셀이 거의 다 나온다)이 모두 같은 저항이면 기본값이다.
    기록 첫머리는 그때까지 온 프레임으로 (CELL_DEFAULT_MIN_CELLS 개부터) 본다. Orion 이 다시 켜지면 기본값 한 바퀴가 다시 오므로 따로 끊지 않는다.
    Orion 은 새로 잰 값을 셀마다 따로 바꿔 내므로, 셀마다 그 같은 값이 이어지는 동안도 기본값으로 본다.
    잰 값이 우연히 기본값과 같으면 바뀔 때까지 빈 값이 된다 (빈 쪽으로 틀린다).
    """
    c, v = np.asarray(cell, np.int64), np.asarray(r, float)
    if not len(c):
        return np.zeros(0, bool)
    k = max(len(np.unique(c)), CELL_DEFAULT_MIN_CELLS)
    roll = pd.Series(v).rolling(k, min_periods=CELL_DEFAULT_MIN_CELLS)
    end = (roll.max() == roll.min()).to_numpy().astype(float)      # 그 프레임까지 한 바퀴 (첫머리는 온 만큼) 가 한 값
    flat = pd.Series(end[::-1]).rolling(k, min_periods=1).max().to_numpy()[::-1] > 0   # 그런 창에 든 프레임 전부
    order = np.lexsort((np.arange(len(c)), c))                     # 셀마다, 시각순
    co, ro = c[order], v[order]
    run = np.cumsum(np.r_[True, (np.diff(co) != 0) | (np.diff(ro) != 0)])      # 한 셀의 같은 값이 이어지는 덩어리
    out = np.empty(len(c), bool)
    out[order] = pd.Series(flat[order]).groupby(run).transform("any").to_numpy()
    return out


def _sweep_need(cells: pd.DataFrame) -> int:
    return int(np.ceil(CELL_SWEEP_MIN_SHARE * cells["cell"].nunique()))


def cell_sweep_frames(cells: pd.DataFrame, t_col: str = "t_s") -> pd.DataFrame:
    """셀 방송 표 → 체크섬이 맞는 프레임 (시각순) + sweep (바퀴 번호) · full (셀이 CELL_SWEEP_MIN_SHARE 넘게 온 바퀴인가).
    t_col 이 _ms 로 끝나면 밀리초, 아니면 초."""
    ok = cells[cells["checksum_ok"]].sort_values(t_col, kind="stable").reset_index(drop=True)
    t = ok[t_col].to_numpy(float)
    sw = cell_sweeps(t if t_col.endswith("_ms") else t * 1000, ok["cell"].to_numpy())
    full = pd.Series(ok["cell"].to_numpy()).groupby(sw).transform("nunique").to_numpy() >= _sweep_need(ok) if len(ok) else np.zeros(0, bool)
    return ok.assign(sweep=sw, full=full)


def cell_sweep_stats(cells: pd.DataFrame, t_col: str = "t_s") -> pd.DataFrame:
    """셀 방송 표 (decode_orion_cells + 시각) → 한 바퀴마다 한 줄.

    열: sweep (cell_sweep_frames 의 바퀴 번호) · t (바퀴의 가운데 시각) · n (온 셀 수) · v_max · v_min · spread_mv · v_min_cell (가장 낮은 셀 번호) · r_max · r_median (잰 저항만,
    잰 셀이 CELL_SWEEP_MIN_SHARE 넘게 있을 때) · r_max_cell · balancing_n. 체크섬이 맞는 프레임만, 셀이 덜 온 바퀴는 뺀다.
    """
    cols = ["sweep", "t", "n", "v_max", "v_min", "spread_mv", "v_min_cell", "r_max", "r_median", "r_max_cell", "balancing_n"]
    f = cell_sweep_frames(cells, t_col)
    need = _sweep_need(f) if len(f) else 0
    f = f[f["full"]].reset_index(drop=True)
    if not len(f):
        return pd.DataFrame(columns=cols)
    f = f.assign(_r=f["resistance_mohm"].fillna(-np.inf))
    g = f.groupby("sweep")
    s = g.agg(t0=(t_col, "first"), t1=(t_col, "last"), n=("cell", "nunique"), v_max=("cell_v", "max"), v_min=("cell_v", "min"),
              r_n=("resistance_mohm", "count"), r_max=("resistance_mohm", "max"), r_median=("resistance_mohm", "median"), balancing_n=("balancing", "sum"))
    s["v_min_cell"] = f.loc[g["cell_v"].idxmin(), "cell"].to_numpy()
    s["r_max_cell"] = pd.array(f.loc[g["_r"].idxmax(), "cell"].to_numpy(), dtype="Int64")
    few = s["r_n"] < need
    s.loc[few, ["r_max", "r_median"]] = np.nan
    s.loc[few, "r_max_cell"] = pd.NA
    return s.assign(sweep=s.index.to_numpy(), t=(s["t0"] + s["t1"]) / 2, spread_mv=((s["v_max"] - s["v_min"]) * 1000).round(1),
                    balancing_n=s["balancing_n"].astype(np.int64))[cols].reset_index(drop=True)


def stream_signals(stream: pd.DataFrame, t_col: str = "t_s") -> dict[str, tuple[np.ndarray, np.ndarray]]:
    """0x6D0 표 → 슬롯 값마다 (시각, 값). 체크섬이 맞는 프레임만, Orion 이 켜진 직후(orion_boot) 는 뺀다."""
    ok = stream[stream["checksum_ok"] & ~(stream["orion_boot"] if "orion_boot" in stream else False)]
    out = {}
    for slot, (name, scale, signed) in STREAM_SLOTS.items():
        s = ok[ok["slot"] == slot]
        if len(s):
            raw = s["slot_raw"].to_numpy(np.int64)
            out[name] = (s[t_col].to_numpy(float), (_s16(raw) if signed else raw) * scale)
    return out


def fault_names(mask: int) -> list[str]:
    return [name for bit, name in APPS_FAULTS.items() if int(mask) & bit]


def decode_bamocar(d: np.ndarray, dlc: np.ndarray) -> pd.DataFrame:
    """0x181 — 레지스터 번호 · 원시값. DLC 4 = 부호 있는 16비트, DLC 6 = 부호 없는 32비트 (상태 · 오류). 다른 길이는 raw 가 빈 값."""
    u16, u32 = _u16le(d, 1), _u16le(d, 1) | (_u16le(d, 3) << 16)
    raw = pd.array(np.where(dlc == 6, u32, _s16(u16)), dtype="Int64")
    raw[~np.isin(dlc, (4, 6))] = pd.NA
    return pd.DataFrame({"reg": d[:, 0].astype(np.int64), "raw": raw})


def bamocar_error_bits(raw: np.ndarray) -> np.ndarray:
    """0x8F 원시값 → 고장으로 볼 오류 비트만 (하위 16비트에서 '전원 전압 없음' 을 뺀 것)."""
    return np.asarray(raw, np.int64) & 0xFFFF & ~(1 << ERROR_VOLTAGE_MIN)


def bamocar_error_names(mask: int) -> list[str]:
    return [name for bit, name in BAMOCAR_ERRORS.items() if int(mask) >> bit & 1]


def bamocar_warning_names(mask: int) -> list[str]:
    return [name for bit, name in BAMOCAR_WARNINGS.items() if int(mask) >> bit & 1]


def bamocar_signals(table: pd.DataFrame, t_col: str = "t_s") -> dict[str, tuple[np.ndarray, np.ndarray]]:
    """bamocar 표 → 신호마다 (시각, 값). 있는 레지스터만. error_raw 는 0x8F 원시값 그대로."""
    def reg(r: int) -> tuple[np.ndarray, np.ndarray]:
        s = table[(table["reg"] == r) & table["raw"].notna()]
        return s[t_col].to_numpy(float), s["raw"].to_numpy(np.int64)
    out = {}
    for name, r, conv in (("rpm", REG_SPEED, lambda v: v / RPM_FULL_SCALE * N100_RPM), ("vdc_v", REG_VDC, lambda v: v * BAMOCAR_VDC_V_PER_NUM),
                          ("vdc_fast_v", REG_VDC_FAST, lambda v: v * BAMOCAR_VDC_V_PER_NUM),
                          ("iq_a", REG_IQ, lambda v: v * BAMOCAR_I_A_PER_NUM), ("id_a", REG_ID, lambda v: v * BAMOCAR_I_A_PER_NUM),
                          ("t_igbt_c", REG_T_IGBT, igbt_temp_c), ("t_motor_c", REG_T_MOTOR, motor_temp_c),
                          ("enabled", REG_STATUS, lambda v: (v >> STATUS_ENABLED & 1).astype(float)),
                          ("ready", REG_STATUS, lambda v: (v >> STATUS_READY & 1).astype(float)), ("error_raw", REG_ERROR, lambda v: v)):
        t, v = reg(r)
        if len(t):
            out[name] = (t, conv(v))
    return out


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
    """frames.parquet → {apps, orion_can, orion_stream, bamocar, thermistor, orion_cells} (있는 것만)."""
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
    t2, d2 = _data(f, ORION_SUMMARY)
    sm = mark_orion_boot(pd.concat([t2[["t_rel_ms"]], decode_orion_summary(d2)], axis=1)) if len(d2) else None
    spans = _boot_spans(sm["t_rel_ms"].to_numpy(float), sm["orion_boot"].to_numpy()) if sm is not None else []
    t, d = _data(f, ORION_VI)
    if len(d):
        vi = pd.concat([t, decode_orion_vi(d)], axis=1).sort_values("t_rel_ms")
        if sm is not None:
            vi = pd.merge_asof(vi, sm.rename(columns={"t_rel_ms": "_t", "checksum_ok": "summary_ok"}), left_on="t_rel_ms", right_on="_t",
                               direction="nearest", tolerance=JOIN_MS).drop(columns="_t")
            vi["orion_boot"] = vi["orion_boot"].eq(True)
        out["orion_can"] = vi
    t, d = _data(f, ORION_STREAM)
    if len(d):
        ts = t["t_rel_ms"].to_numpy(float)
        boot = np.zeros(len(ts), bool)
        for lo, hi in spans:
            boot |= (ts > lo) & (ts < hi)
        out["orion_stream"] = pd.concat([t, decode_orion_stream(d)], axis=1).assign(orion_boot=boot)
    therm = []
    for cid, dec, source in ((THERM_SINGLE, decode_thermistor_single, "single"), (THERM_MODULE, decode_thermistor_module, "module_to_bms")):
        t, d = _data(f, cid)
        if len(d):
            therm.append(pd.concat([t, dec(d)], axis=1).assign(source=source))
    if therm:
        th = pd.concat(therm, ignore_index=True).sort_values("t_rel_ms", kind="stable").reset_index(drop=True)
        for c in ("thermistor_id", "temp_c", "average_c", "enabled"):              # 한쪽 프레임만 있는 레코딩도 같은 열
            th[c] = th[c].astype("Int64") if c in th else pd.array([pd.NA] * len(th), dtype="Int64")
        th["checksum_ok"] = th["checksum_ok"].astype("boolean") if "checksum_ok" in th else pd.array([pd.NA] * len(th), dtype="boolean")
        out["thermistor"] = th
    t, d = _data(f, ORION_CELLS)
    if len(d):
        cl = pd.concat([t, decode_orion_cells(d)], axis=1).sort_values("t_rel_ms", kind="stable").reset_index(drop=True)
        dflt = resistance_default(cl["cell"].to_numpy(), cl["resistance_mohm"].to_numpy())
        out["orion_cells"] = cl.assign(resistance_mohm=cl["resistance_mohm"].mask(dflt), resistance_default=dflt)
    s = f[f["can_id"] == BAMOCAR]
    if len(s):
        p = np.frombuffer(b"".join(s["payload"]), dtype=np.uint8).reshape(-1, 16)      # 이 ID 는 길이가 섞여 있다 — 오프셋 6 = 길이, 8 ~ = 데이터
        out["bamocar"] = pd.concat([s[["t_rel_ms", "t_abs"]].reset_index(drop=True), decode_bamocar(p[:, 8:16], p[:, 6])], axis=1)
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
            s[name] = {"rows": int(len(t)), "checksum_fail": int((~t["checksum_ok"]).sum()),
                       "boot_rows": int(t["orion_boot"].sum()) if "orion_boot" in t else 0}
    if "thermistor" in tables:
        th = tables["thermistor"]
        one = th[th["source"] == "single"]
        s["thermistor"] = {"rows": int(len(th)), "single_rows": int(len(one)), "thermistors": int(one["thermistor_id"].nunique()),
                           "module_checksum_fail": int((th["checksum_ok"] == False).sum()), "fault_rows": int(th["fault"].sum())}  # noqa: E712
    if "orion_cells" in tables:
        c = tables["orion_cells"]
        ok = c[c["checksum_ok"]]
        sw = cell_sweep_stats(c, "t_rel_ms")
        s["orion_cells"] = {"rows": int(len(c)), "checksum_fail": int((~c["checksum_ok"]).sum()), "cells": int(ok["cell"].nunique()),
                            "sweeps": int(len(sw)), "resistance_measured_rows": int(ok["resistance_mohm"].notna().sum()),
                            "resistance_default_rows": int(ok["resistance_default"].sum()), "balancing_rows": int(ok["balancing"].sum())}
    if "bamocar" in tables:
        b = tables["bamocar"]
        sig = bamocar_signals(b, "t_rel_ms")
        err = sig["error_raw"][1] if "error_raw" in sig else np.zeros(0, np.int64)
        any_err = int(np.bitwise_or.reduce(err)) if len(err) else 0
        s["bamocar"] = {"rows": int(len(b)), "regs": {f"0x{int(r):02X}": int(n) for r, n in b["reg"].value_counts().sort_index().items()},
                        "vdc_max_v": round(float(sig["vdc_v"][1].max()), 1) if "vdc_v" in sig else None,
                        "vdc_fast_max_v": round(float(sig["vdc_fast_v"][1].max()), 1) if "vdc_fast_v" in sig else None,
                        "overvoltage_frames": int((err >> ERROR_VOLTAGE_MAX & 1).sum()),
                        "errors_any": bamocar_error_names(int(bamocar_error_bits(np.array([any_err]))[0])), "warnings_any": bamocar_warning_names(any_err)}
    return s


def convert_event(event_dir: Path, skipped: list | None = None) -> list[dict]:
    """이벤트의 Monolith 레코딩마다 CAN 을 풀어 표를 쓴다 → 레코딩마다 요약 한 줄. CAN 이 없는 레코딩은 건너뛴다.
    skipped 를 주면 건너뛴 레코딩을 (레코딩, 이유) 로 담는다."""
    rows = []
    skip = skipped if skipped is not None else []
    for meta_path in sorted((Path(event_dir) / "normalized").glob("*/meta.json")):
        rec_dir = meta_path.parent
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        if not meta.get("source", {}).get("logger", "").startswith("Monolith"):
            skip.append((rec_dir.name, "Monolith 아님"))
            continue
        if not (rec_dir / "frames.parquet").exists():
            skip.append((rec_dir.name, "frames.parquet 없음"))
            continue
        tables = decode_recording(rec_dir)
        if not tables:
            skip.append((rec_dir.name, "CAN 프레임 없음"))
        for name in TABLES:                                     # 다시 돌리면 옛 표가 남지 않게
            (rec_dir / f"{name}.parquet").unlink(missing_ok=True)
        for name, t in tables.items():
            t.to_parquet(rec_dir / f"{name}.parquet", index=False, compression="zstd")
        meta["can_decoded"] = summarize(tables) if tables else {}
        write_json(meta, meta_path)
        if tables:
            rows.append({"recording_id": rec_dir.name, **{k: v.get("rows", 0) for k, v in meta["can_decoded"].items()}})
            if "bamocar" in meta["can_decoded"]:                # 요청한 레지스터가 다 오는지 · 과전압이 있었는지를 변환할 때 바로 본다
                bam = meta["can_decoded"]["bamocar"]
                rows[-1]["레지스터"] = " · ".join(f"{k} {n:,}" for k, n in bam["regs"].items())
                if bam["errors_any"]:
                    rows[-1]["오류"] = ", ".join(bam["errors_any"])
    return rows
