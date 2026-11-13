"""실시간 화면 — 수신기가 받은 그대로 남긴 MQTT 메시지를 화면 상태로 (계약 §12, v0.16).

    L = Live(car="LEF-25evo", venue="yeonggwang")
    for row in rows:                 # 수신기 파일의 한 줄 (json.loads 한 것)
        L.feed(row)
    L.state(now) · L.track()

입력은 Monolith V2 의 토픽이다.
    <이름>/d        104 바이트 — 보낸 순간의 장치 ms · 상태 · 레코드 4칸 (GPS · 자이로 · 아날로그 · 디지털, 가장 최근 값)
    <이름>/d/can    SD 와 같은 24 바이트 레코드의 배열 (CAN)
    <이름>/d/sl     같은 레코드의 배열 (시스템 메시지)
    <이름>/d/boot   부팅 시각 8 바이트, 또는 장치 연결이 끊겼을 때 브로커가 대신 보내는 "OFFLINE"
    <이름>/d/ver    펌웨어 판
펌웨어는 CAN 을 ID 마다 `intv`(0.5 s)에 한 프레임만 보낸다. 한 ID 에 여러 값이 돌아가며 실리는 모터 컨트롤러(0x181)와
Orion 스트림(0x6D0)은 값마다 몇 초 ~ 몇 분씩 빈다 — 그래서 채널마다 낡음 기준이 다르다 (CHANNELS).

원천별 수신률: CAN 은 (ID, 버스)마다 장치 시각이 새로운 프레임만 센다 (펌웨어가 옛 프레임을 다시 보내도 부풀지 않게).
와야 할 수는 그 ID 가 실제로 오는 간격(최근 간격의 아래쪽 4분위 — 끊겨 벌어진 간격에 끌려가지 않게)으로 잡는다.
펌웨어 거르기는 "마지막으로 보낸 지 intv 가 지난 뒤 처음 들어온 프레임" 이라, 원래 주기가 intv 를 나누지 않는 ID 는 intv 보다 길게 온다.

랩 값: 계측선을 지난 뒤 LAP_SETTLE_S 가 지나면 그 랩의 에너지 · SOC · 셀 최저 전압 · 셀 최고 온도를 굳힌다 (CAN 묶음이 GPS 보다 늦게 올 수 있어서).
에너지는 Orion 요약(0x446)의 전력을 장치 시각으로 이어 적분한다 — 거른 뒤 약 2 Hz 순간값이라 랩마다 몇 % 오차가 있다.

달린 궤적: 콘으로 그때그때 까는 코스는 경기장 기준선과 다를 수 있다. 그래서 그날 달린 위치를 PATH_STEP_M 마다 한 점씩 모아
track() 에 같이 준다 (주행 중 — 사후 분석의 MOVING_KMH 를 넘을 때만, 서 있을 때 GPS 가 흔들려 점이 쌓이지 않게).
기준선 위를 달리는지는 최근 (기준선 길이 × COURSE_WINDOW) 만큼 달린 궤적이 기준선 COURSE_TOL_M 안에 드는 비율로 본다 (사후 분석과 같은 문턱).
이건 "같은 코스" 가 아니다 — 좁은 터에서는 다른 콘 코스도 기준선 20 m 안에 들어 맞음으로 나온다. 기준선에서 멀리 벗어났을 때 알려 주는 정도다.

시계: 장치 시각(부팅 뒤 ms)은 `/d` 의 "받은 시각 − 보낸 순간" 의 최솟값(최근 10분)으로 서버 시계에 옮긴다. 가장 빨리 도착한
메시지가 시계 차이에 가장 가깝다 (가장 빠른 전달 시간만큼 이르게 잡힌다). `boot_epoch` 는 초 단위이고 동기화 전 RTC 값이라 쓰지 않는다.

CAN 풀이는 `evo_can` 과 같은 식을 프레임 하나씩 한다 — 하루치 파일을 다시 읽을 때 빨라야 해서다. 같은 값이 나오는지는 테스트가 맞대 본다.
"""

from __future__ import annotations

import base64
import math
import struct
import tomllib
from collections import defaultdict, deque
from dataclasses import dataclass, replace
from pathlib import Path

import numpy as np

from . import evo_can as ec
from . import venue as venue_mod
from .evo_stm32 import RPM_FULL_SCALE, motor_temp_c
from .laps import COURSE_SHARE, COURSE_TOL_M, MIN_LAP_S, MOVING_KMH, Gate, distance_to_line, gate_crossings, to_local_m
from .lef26 import BAMOCAR_VDC_V_PER_NUM, igbt_temp_c
from .monolith import ADC_FSR_V, T_ANALOG, T_CAN, T_GPS, checksum_ok, nmea_to_deg

CONTRACT = "0.16"
REC = 24
LOGBUF_SIZE = 104
GRID_S = 0.5                    # 그래프 격자 (서버 시계)
CHART_KEEP_S = 600.0
CHART_DEFAULT_S = 300.0
EVENTS_KEEP = 50
CLOCK_WINDOW_S = 600.0          # 시계 차이를 잡는 최솟값 창 — 시계 속도 차이가 쌓이지 않게 창을 둔다
RATE_WINDOW_S = 10.0
HOLD_S = 2.0                    # 값 경고의 지속 (설계안 §4-6)
SOURCE_STALE_S = 2.0
LV_V_PER_RAW = 12.1 / 2690      # Monolith 공급 전압 raw → V [추정 환산 — 25Evo 테스트위크에서 12.1 V 고정 공급일 때 2,690]
OFFLINE = b"OFFLINE"
# 0x6D0 슬롯 — 슬롯 124개가 돌아가는데 0.5 s 에 아무 슬롯 하나만 온다. 같은 슬롯이 다시 오기까지 보통 수십 초, 길면 몇 분
SLOT_STALE_S = 300.0
# 원천별 수신률 (계약 §12-4 sources[].rate_10s)
SOURCE_IDS = {ec.ORION_SUMMARY: "bms", ec.ORION_VI: "bms", ec.ORION_STREAM: "bms", ec.BAMOCAR: "inverter",
              ec.APPS_RAW: "pedal", ec.APPS_SCALED: "pedal", ec.APPS_STATUS: "pedal"}
SPACING_KEEP = 120              # ID 마다 최근 간격 몇 개로 "실제로 오는 간격" 을 잡나
SPACING_MIN_N = 5
SPACING_MAX_MS = 5000           # 이보다 긴 간격은 끊김이나 장치가 멈춘 것 — 간격 추정에 넣지 않는다
SPACING_Q = 25                  # 아래쪽 4분위
# 랩 값 (계약 §12-4 laps.rows)
LAP_CH = ("battery.power_kw", "bms.soc_pct", "bms.cell_min_v", "bms.temp_max_c", "gps.speed_kmh")
LAP_KEEP = 4000                 # 채널마다 표본 수 (약 2 Hz 로 30분 넘게) — 랩이 이보다 길면 앞쪽이 빠진다 (cover 로 보인다)
LAP_SETTLE_S = 3.0
ENERGY_GAP_S = 2.0              # 전력 표본 사이가 이보다 벌어지면 그 사이는 적분하지 않는다
SOC_NEAR_S = 2.0                # 랩 시작 · 끝의 SOC 는 이 안의 표본으로
# 달린 궤적 (계약 §12-5 path · course)
PATH_STEP_M = 3.0               # 마지막 점에서 이만큼 떨어지면 한 점 [정한 값 — 1 km 코스면 한 바퀴 약 330점]
PATH_KEEP = 2000                # 점 개수 상한 (그런 코스면 약 6바퀴) — 넘으면 오래된 점부터 빠진다
PATH_GAP_S = 5.0                # 앞 주행 표본과 이보다 벌어지면 (끊김 · 정지 뒤) 선을 잇지 않는다
PATH_JUMP_M = 100.0             # 앞 점과 이보다 멀면 (위치 튐) 선을 잇지 않는다
COURSE_WINDOW = 2.0             # 기준선 위를 달리는지는 최근 (기준선 길이 × 이 값) 만큼 달린 궤적으로 본다


@dataclass(frozen=True)
class Ch:
    id: str
    label: str
    unit: str
    sign: str = ""
    digits: int = 1
    stale_s: float = 2.0
    source: str = ""
    gauge: tuple[float, float] | None = None     # 화면 막대 게이지의 양 끝 (계약 §12-4 tiles[].scale). 문턱은 경고 규칙에서 온다


CHANNELS = {c.id: c for c in (
    Ch("gps.speed_kmh", "속도", "km/h", digits=0, source="gps"),
    Ch("gps.x_m", "위치 (동쪽)", "m", "동쪽 +", 1, source="gps"),
    Ch("gps.y_m", "위치 (북쪽)", "m", "북쪽 +", 1, source="gps"),
    Ch("battery.power_kw", "전력", "kW", "방전 +", 1, source="bms", gauge=(-20.0, 90.0)),      # 회생 쪽 조금 · 규정 80 kW 위 조금
    Ch("battery.voltage_v", "팩 전압", "V", "", 1, source="bms"),
    Ch("battery.current_a", "배터리 전류", "A", "방전 +, 배터리로 돌아오면 −", 0, source="bms"),
    Ch("bms.soc_pct", "SOC", "%", "", 1, source="bms", gauge=(0.0, 100.0)),
    Ch("bms.cell_min_v", "셀 최저 전압", "V", "", 3, source="bms", gauge=(2.5, 4.2)),          # 리튬이온 셀의 일반적인 하한 · 만충
    Ch("bms.temp_max_c", "셀 최고 온도", "°C", "", 0, source="bms", gauge=(0.0, 70.0)),       # 규정 60 °C 위 조금
    Ch("bms.temp_min_c", "셀 최저 온도", "°C", "", 0, source="bms"),
    Ch("bms.temp_spread_c", "셀 온도 차", "°C", "최고 − 최저", 0, source="bms"),     # 엔진이 낸 값 — 최고 · 최저가 둘 다 새로울 때만
    # 0x6D0 슬롯 — 세션의 bms 블록과 같은 ID (있는 것만). 충전 한도 CCL(슬롯 10)은 세션들에서 Orion 로그와 반쯤만 맞아 내지 않는다
    Ch("bms.dcl_kw", "방전 출력 한계 (DCL)", "kW", "", 1, SLOT_STALE_S, "bms"),
    Ch("bms.temp_avg_c", "셀 평균 온도", "°C", "", 0, SLOT_STALE_S, "bms"),
    Ch("bms.temp_max_id", "최고 온도 써미스터 번호", "", "", 0, SLOT_STALE_S, "bms"),
    Ch("bms.temp_min_id", "최저 온도 써미스터 번호", "", "", 0, SLOT_STALE_S, "bms"),
    Ch("bms.bms_heatsink_c", "BMS 방열판 온도", "°C", "", 0, SLOT_STALE_S, "bms"),
    Ch("bms.pack_resistance_ohm", "팩 저항", "Ω", "", 3, SLOT_STALE_S, "bms"),
    Ch("bms.pack_ah", "팩 Ah", "Ah", "", 1, SLOT_STALE_S, "bms"),
    Ch("bms.dod_pct", "방전 깊이 (DoD)", "%", "", 1, SLOT_STALE_S, "bms"),
    # 0x181 — 레지스터가 한 ID 에 돌아가며 실려 0.5 s 에 하나만 온다. 레지스터마다 원래 주기가 달라 낡음 기준도 다르다
    # (거른 뒤 그 레지스터가 다시 오는 간격의 거의 최대 — 25Evo 대회 SD 에 펌웨어 거르기를 돌려 잼)
    Ch("motor.t_motor_c", "모터 온도", "°C", "", 0, 40.0, "inverter"),
    Ch("motor.t_igbt_c", "인버터(IGBT) 온도", "°C", "", 0, 20.0, "inverter"),
    Ch("motor.vdc_v", "DC 버스 전압", "V", "", 0, 40.0, "inverter"),
    Ch("motor.rpm", "회전수 (로거 환산)", "rpm", "전진 +", 0, 5.0, "inverter"),     # 기록은 전진할 때 − — 엔진 세션 값처럼 뒤집는다
    Ch("motor.enabled", "구동 허용 (Ena)", "", "", 0, 180.0, "inverter"),
    Ch("motor.ready", "운전 준비 (Rdy)", "", "", 0, 180.0, "inverter"),
    Ch("motor.error_raw", "오류 · 경고 비트", "", "", 0, 75.0, "inverter"),
    Ch("pedal.apps_pct", "가속페달", "%", "밟을수록 +", 0, source="pedal", gauge=(0.0, 100.0)),
    Ch("pedal.torque_cmd_pct", "토크 명령", "%", "가속 +", 0, source="pedal"),
    Ch("pedal.apps1_pct", "가속페달 센서 1", "%", "", 1, source="pedal"),
    Ch("pedal.apps2_pct", "가속페달 센서 2", "%", "", 1, source="pedal"),
    Ch("pedal.fault_raw", "APPS 고장 비트", "", "", 0, source="pedal"),
    Ch("lv.voltage_v", "LV 전압 (추정 환산)", "V", "", 1, source="lv"),
    Ch("brake.ain1_v", "브레이크 압력 센서 (AIN1 전압)", "V", "", 2, source="lv"),     # 25Evo 가 Monolith AIN1 에 단 센서. 전압까지만 — 환산식을 받으면 bar 로. ADC 끝 4.096 V 위는 잘린다
)}
TILES = ("gps.speed_kmh", "battery.power_kw", "battery.voltage_v", "battery.current_a", "bms.soc_pct", "bms.cell_min_v",
         "bms.temp_max_c", "motor.t_motor_c", "motor.t_igbt_c", "pedal.apps_pct", "lv.voltage_v",
         # v0.16 추가 (사이트 #0ae7 — 화면을 뷰로 나눔)
         "bms.temp_min_c", "bms.temp_spread_c", "bms.dcl_kw", "bms.temp_avg_c", "bms.temp_max_id", "bms.temp_min_id", "bms.bms_heatsink_c",
         "bms.pack_resistance_ohm", "bms.pack_ah", "bms.dod_pct",
         "motor.vdc_v", "motor.rpm", "motor.enabled", "motor.ready", "motor.error_raw",
         "pedal.torque_cmd_pct", "pedal.apps1_pct", "pedal.apps2_pct", "pedal.fault_raw",
         "brake.ain1_v")     # 11-13 추가 (25Evo 가 AIN1 에 브레이크 압력 센서)
CHART = ("gps.speed_kmh", "battery.power_kw", "battery.current_a", "bms.temp_max_c", "motor.t_motor_c", "motor.t_igbt_c",
         "bms.soc_pct", "gps.x_m", "gps.y_m",
         # v0.16 추가 (사이트 #38fa — 뷰마다 최근 5분)
         "bms.cell_min_v", "bms.temp_min_c", "motor.rpm", "pedal.apps_pct", "pedal.torque_cmd_pct",
         "brake.ain1_v")
# 수신률 기록 (0 ~ 1) — 격자마다 그 순간의 link.rate_10s · sources[].rate_10s · fix_10s
RATE_SERIES = ("rate.link", "rate.bms", "rate.inverter", "rate.pedal", "rate.gps_fix")
# Monolith 상태 비트 (/d 의 u32, 펌웨어 main.h) — 비트 n = 오류, n + 12 = 치명
DEVICE_COMPONENTS = ("CORE", "NVS", "RTC", "SD", "WIFI", "MQTT", "CAN", "GPS", "ANALOG", "DIGITAL", "GYRO")
DEVICE_FATAL_SHIFT = 12
SOURCES = (("bms", "BMS"), ("inverter", "인버터"), ("pedal", "가속페달"), ("gps", "GPS"), ("lv", "LV"))
# 수신 · 장치 · 원천 사건은 영어 문구 (2026-11-03 사용자 결정, 사이트 #0820 — '장치' 가 무엇인지 애매해서). 값 경고 사건은 경고 띠 문구 그대로
SOURCE_EVENT_NAMES = {"bms": "BMS", "inverter": "Inverter", "pedal": "APPS", "gps": "GPS"}     # 원천별 사건을 내는 원천 (LV 는 /d 안이라 빼다)
DEVICE_NAMES = {"LEF-25evo": "Monolith"}         # 차의 송신 장치 이름 — 사건 문구에 쓴다
RATE_WARN, RATE_DANGER = 0.8, 0.5               # 원천별 수신률 색 (2026-11-03 사용자 확인, 사이트 #2c73)
STREAM_TO_CH = {"dcl_kw": "bms.dcl_kw", "soc_pct": "bms.soc_pct", "temp_max_c": "bms.temp_max_c", "temp_min_c": "bms.temp_min_c",
                "temp_avg_c": "bms.temp_avg_c", "temp_max_id": "bms.temp_max_id", "temp_min_id": "bms.temp_min_id",
                "bms_heatsink_c": "bms.bms_heatsink_c", "pack_resistance_ohm": "bms.pack_resistance_ohm", "pack_ah": "bms.pack_ah",
                "dod_pct": "bms.dod_pct"}
LEVELS = {"danger": 3, "warn": 2, "info": 1}


# ---------------------------------------------------------------- 경고 문턱

@dataclass(frozen=True)
class Rule:
    """값 경고 한 줄 — channel 이 op 쪽으로 warn · danger 를 넘어 hold_s 동안 이어지면 켠다."""
    id: str
    channel: str
    op: str                         # ">=" 이상에서 · "<=" 이하에서
    warn: float | None
    danger: float | None
    text: str
    hold_s: float = HOLD_S
    info: bool = False              # 판정이 아니라 참고 — 단계를 info 로 낮춘다
    note: str = ""
    when_speed_kmh: float | None = None    # 이 속도를 넘을 때만 본다

    def level(self, v: float) -> str | None:
        hit = (lambda th: th is not None and (v >= th if self.op == ">=" else v <= th))
        if hit(self.danger):
            return "danger"
        if hit(self.warn):
            return "warn"
        return None

    def message(self, level: str) -> str:
        th = self.danger if level == "danger" else self.warn
        unit = CHANNELS[self.channel].unit if self.channel in CHANNELS else ""
        word = "이상" if self.op == ">=" else "이하"
        return f"{self.text} {_fmt(th)} {unit} {word}".replace("  ", " ") + (f" {self.note}" if self.note else "")


# 규정 · 장치 비트로 정할 수 있는 것만. 차의 설정값 · 실측이 필요한 문턱(셀 최저 · SOC · LV)은 차별 문턱 파일에 둔다 (계약 §12-6)
DEFAULT_RULES = (
    Rule("W-TB-NOW", "bms.temp_max_c", ">=", 55.0, 60.0, "셀 최고 온도"),
    Rule("W-P80", "battery.power_kw", ">=", None, 80.0, "전력", info=True, note="[참고 — 2 Hz 순간값]"),
    # 150 V = 사후 분석이 "고전압 켜짐" 을 가르는 선과 같은 값 (lef26_laps.HV_ON_V · motor_runs — 인버터가 본 DC 전압 기준).
    # 5 km/h 는 HV 를 끈 채 차를 미는 것을 빼려고 정한 값. 둘 다 [정한 값 — 팩 구성이 바뀌면 다시]
    Rule("W-HV-OFF", "motor.vdc_v", "<=", None, 150.0, "주행 중 DC 버스 전압", when_speed_kmh=5.0),
)


def load_rules(path: Path | str | None) -> tuple[Rule, ...]:
    """기본 문턱 + 차별 문턱 파일(TOML, [[rule]] 여러 개). 같은 id 는 파일 쪽이 이긴다."""
    rules = {r.id: r for r in DEFAULT_RULES}
    if path:
        doc = tomllib.loads(Path(path).read_text(encoding="utf-8"))
        for d in doc.get("rule", []):
            base = rules.get(d.get("id"))
            r = replace(base, **d) if base else Rule(**{"warn": None, "danger": None, **d})
            if r.channel not in CHANNELS or r.op not in (">=", "<="):
                raise ValueError(f"경고 문턱 파일의 줄이 잘못됨: {d}")
            rules[r.id] = r
    return tuple(rules.values())


# ---------------------------------------------------------------- 작은 도구

def _fmt(v: float | None) -> str:
    if v is None:
        return "—"
    return f"{v:g}"


def _u16le(d: bytes, i: int) -> int:
    return d[i] | d[i + 1] << 8


def _u16be(d: bytes, i: int) -> int:
    return d[i] << 8 | d[i + 1]


def _s16(u: int) -> int:
    return u - 0x10000 if u >= 0x8000 else u


def _sum_ok(d: bytes, extra: int = 0) -> bool:
    return (sum(d[:7]) + extra) & 0xFF == d[7]


def _r(v, digits: int = 3):
    if v is None or (isinstance(v, float) and not math.isfinite(v)):
        return None
    return round(float(v), digits)


# ---------------------------------------------------------------- CAN 한 프레임 → 채널 값 (evo_can 과 같은 식)

def decode_frame(can_id: int, dlc: int, d: bytes) -> list[tuple[str, float]]:
    """25Evo 차량 CAN 프레임 하나 → [(채널 ID, 값)]. 모르는 ID · 체크섬이 틀린 프레임은 빈 목록."""
    out: list[tuple[str, float]] = []
    if can_id == ec.ORION_VI:
        if _sum_ok(d):
            out += [("battery.voltage_v", _u16be(d, 0) * 0.1), ("battery.current_a", _s16(_u16be(d, 2)) * 0.1)]
    elif can_id == ec.ORION_SUMMARY:
        if _sum_ok(d):
            out += [("battery.power_kw", _s16(_u16be(d, 0)) * 0.1), ("bms.cell_min_v", _u16be(d, 2) * 1e-4),
                    ("bms.temp_max_c", float(struct.unpack("b", d[4:5])[0])), ("bms.temp_min_c", float(struct.unpack("b", d[5:6])[0])),
                    ("bms.soc_pct", d[6] * 0.5)]
    elif can_id == ec.ORION_STREAM:
        if _sum_ok(d, ec.ORION_STREAM + 8):
            out += [("battery.current_a", -_s16(_u16be(d, 1)) * 0.1), ("battery.voltage_v", _u16be(d, 3) * 0.1)]
            slot = ec.STREAM_SLOTS.get(d[0])
            if slot and slot[0] in STREAM_TO_CH:
                name, scale, signed = slot
                raw = _u16be(d, 5)
                out.append((STREAM_TO_CH[name], (_s16(raw) if signed else raw) * scale))
    elif can_id == ec.APPS_RAW:
        out.append(("pedal.apps_pct", _u16le(d, 4) * 0.1))
    elif can_id == ec.APPS_SCALED:
        out += [("pedal.torque_cmd_pct", ec.TORQUE_SIGN * _s16(_u16le(d, 4)) / ec.TORQUE_FULL * 100),
                ("pedal.apps1_pct", _u16le(d, 0) * 0.1), ("pedal.apps2_pct", _u16le(d, 2) * 0.1)]
    elif can_id == ec.APPS_STATUS:
        # 상태(지금 고장) | 확정된 고장 — 세션의 가속페달 블록이 고장 이름을 내는 것과 같은 묶음
        out.append(("pedal.fault_raw", float(_u16le(d, 0) | _u16le(d, 2))))
    elif can_id == ec.BAMOCAR:
        reg = d[0]
        if dlc == 4:
            raw = _s16(_u16le(d, 1))
        elif dlc == 6:
            raw = _u16le(d, 1) | _u16le(d, 3) << 16
        else:
            return out
        if reg == ec.REG_SPEED:
            out.append(("motor.rpm", 0.0 - raw / RPM_FULL_SCALE * ec.LOGGER_N100_RPM))     # 부호를 뒤집어 전진 +
        elif reg == ec.REG_VDC:
            out.append(("motor.vdc_v", raw * BAMOCAR_VDC_V_PER_NUM))
        elif reg == ec.REG_T_IGBT:
            out.append(("motor.t_igbt_c", float(igbt_temp_c(raw))))
        elif reg == ec.REG_T_MOTOR:
            out.append(("motor.t_motor_c", float(motor_temp_c(raw))))
        elif reg == ec.REG_STATUS:
            out += [("motor.enabled", float(raw >> ec.STATUS_ENABLED & 1)), ("motor.ready", float(raw >> ec.STATUS_READY & 1))]
        elif reg == ec.REG_ERROR:
            out.append(("motor.error_raw", float(raw)))
    return [(k, v) for k, v in out if math.isfinite(v)]


def _records(payload: bytes) -> np.ndarray:
    """24 바이트 레코드 배열 중 매직 · 체크섬이 맞는 것."""
    if not payload or len(payload) % REC:
        return np.zeros((0, REC), np.uint8)
    raw = np.frombuffer(payload, np.uint8).reshape(-1, REC)
    return raw[(raw[:, 0] == 0xAE) & checksum_ok(raw)]


class _IdRate:
    """CAN (ID, 버스) 하나의 수신 — 새 프레임을 받은 시각 · 장치 시각 간격."""
    __slots__ = ("first_t", "last_ts", "spacing", "rx")

    def __init__(self, t: float):
        self.first_t = t
        self.last_ts: int | None = None
        self.spacing: deque[int] = deque(maxlen=SPACING_KEEP)
        self.rx: deque[float] = deque()

    def add(self, t: float, dev_ms: int) -> None:
        if self.last_ts is not None and dev_ms <= self.last_ts:
            return                                                # 같은(또는 옛) 프레임 — 세지 않는다
        if self.last_ts is not None and dev_ms - self.last_ts <= SPACING_MAX_MS:
            self.spacing.append(dev_ms - self.last_ts)
        self.last_ts = dev_ms
        self.rx.append(t)
        while self.rx and self.rx[0] <= t - RATE_WINDOW_S:
            self.rx.popleft()

    def counts(self, now: float, default_s: float, loop_s: float) -> tuple[int, int]:
        """(최근 10 s 에 받은 새 프레임 수, 와야 할 수).

        간격 = 실제로 오는 간격과 보내기 주기 중 긴 것 (보내기 한 번에 ID 마다 하나뿐이라).
        다음 프레임이 아직 늦지 않았으면(간격 × 2 안) 창의 끝을 그 ID 의 마지막 수신에 둔다 — 묶음 사이에 물어도 1 이 되게.
        """
        base = float(np.percentile(self.spacing, SPACING_Q)) / 1000.0 if len(self.spacing) >= SPACING_MIN_N else default_s
        base = max(base, loop_s, 0.01)
        last = self.rx[-1] if self.rx else None
        end = last if last is not None and 0.0 <= now - last <= 2 * base else now
        n = sum(1 for x in self.rx if end - RATE_WINDOW_S < x <= end)
        span = end - self.first_t
        want = math.ceil(RATE_WINDOW_S / base - 1e-6) if span >= RATE_WINDOW_S else math.floor(max(span, 0.0) / base + 1e-6) + 1
        return n, max(want, 1)


# ---------------------------------------------------------------- 상태

class Live:
    """하루치 수신 기록 하나를 따라가는 상태. feed 를 받은 순서대로 부른다 (스레드 안전하지 않다 — 부르는 쪽이 잠근다)."""

    def __init__(self, car: str = "LEF-25evo", venue: str | dict | None = None, rules: Path | str | None = None,
                 data_root: Path | str | None = None, device_name: str | None = None):
        self.car = car
        self.dev_name = device_name or DEVICE_NAMES.get(car, "Device")
        if isinstance(venue, str):
            venue = venue_mod.load(venue, base=Path(data_root) / "_venues" if data_root else None)
        self.venue = venue
        self.gate = Gate(venue["gate"]["x"], venue["gate"]["y"], venue["gate"]["heading_deg"]) if venue else None
        self.rules = load_rules(rules)
        self.by_channel: dict[str, list[Rule]] = {}
        for r in self.rules:
            self.by_channel.setdefault(r.channel, []).append(r)

        self.vals: dict[str, tuple[float, float]] = {}          # 채널 → (서버 시각, 값)
        self.seen: dict[str, float] = {}                         # 원천 → 마지막 서버 시각
        self.replay = False
        self.bad_rows = 0
        self.unknown = 0
        # 수신기 · 장치
        self.receiver = ("unknown", None)
        self.firmware: str | None = None
        self.offline_at: float | None = None
        self.boots = 0
        self.boot_at: float | None = None
        self.dev_state: int | None = None                         # /d 의 상태 비트 (마지막 값)
        self.bad_records = 0                                      # 머리 · 체크섬이 맞지 않아 버린 /d 칸 · CAN 레코드
        # 시계
        self.dev_last: int | None = None
        self.win: deque[tuple[float, float]] = deque()           # (받은 시각, 받은 시각 − 보낸 순간) — 최솟값이 앞
        self.offset: float | None = None
        self.periods: deque[int] = deque(maxlen=40)
        # 링크
        self.last_rx: float | None = None
        self.d_rx: deque[float] = deque()
        self.outages: list[tuple[float, float]] = []
        self._restarted = False
        self.link_since: float | None = None                     # 지금 이어진 수신이 시작된 시각 (원천별 사건은 이어진 동안만)
        self.lost_logged: float | None = None                    # "no data" 를 남긴 끊김 (그 끊김 직전 last_rx)
        self.src_down: dict[str, float] = {}                     # "no data" 를 남긴 원천 → 그때의 마지막 값 시각
        self.ids: dict[tuple[int, int], _IdRate] = {}            # (CAN ID, 버스) → 원천별 수신률
        self.fix_win: deque[tuple[float, bool]] = deque()        # (받은 시각, 이 /d 에 새 GPS 위치가 있었나)
        # GPS · 랩
        self.gps_ts: int | None = None
        self.analog_ts: int | None = None
        self.prev_pt: tuple[float, float, float, float] | None = None
        self.crossings: list[float] = []
        self.lap_samples = {ch: deque(maxlen=LAP_KEEP) for ch in LAP_CH}
        self.lap_rows: list[dict] = []
        self.path: deque[tuple[float | None, float | None]] = deque(maxlen=PATH_KEEP)   # (x, y) · (None, None) = 선 끊김
        self.path_rev = 0                                         # 지금까지 더한 점 수 (줄지 않는다 — 화면이 track 을 다시 받을 때를 안다)
        self.path_last: tuple[float, float] | None = None         # 마지막으로 더한 점
        self.path_seen: float | None = None                       # 마지막 주행 표본의 장치 시각 (서버 시계)
        self.course_at: tuple[int, dict] | None = None            # (path_rev, _course 결과) — 궤적이 그대로면 다시 재지 않는다
        line = venue["line"] if venue else None
        self.line_m = float(np.hypot(np.diff(line["x_m"]), np.diff(line["y_m"])).sum()) if line and len(line["x_m"]) > 1 else None
        # 경고 · 사건 · 그래프
        self.pending: dict[str, tuple[str, float]] = {}
        self.active: dict[str, dict] = {}
        self.events: deque[dict] = deque(maxlen=EVENTS_KEEP)
        self.chart: deque[tuple[float, tuple]] = deque(maxlen=int(CHART_KEEP_S / GRID_S))
        self.grid_t: float | None = None

    # ------------------------------------------------------------ 받기

    def feed(self, row: dict) -> None:
        try:
            t = float(row["t_ns"]) / 1e9
        except (KeyError, TypeError, ValueError):
            self.bad_rows += 1
            return
        self.replay = bool(row.get("replay", False))
        if "event" in row:
            self._receiver(t, row["event"], carry=bool(row.get("carry")))
            return
        topic, b64 = row.get("topic"), row.get("b64")
        if not isinstance(topic, str) or not isinstance(b64, str):
            self.bad_rows += 1
            return
        try:
            payload = base64.b64decode(b64, validate=True)
        except ValueError:
            self.bad_rows += 1
            return
        sub = topic.split("/", 1)[1] if "/" in topic else ""
        if sub == "d":
            self._logbuf(t, payload)
        elif sub == "d/can":
            self._can(t, payload)
        elif sub == "d/boot":
            self._boot(t, payload, bool(row.get("retain")))
        elif sub == "d/ver":
            self.firmware = payload.decode("utf-8", "replace")
        elif sub == "d/sl":
            pass                                                  # 시스템 메시지 — v1 에서는 쓰지 않는다
        else:
            self.unknown += 1

    def _receiver(self, t: float, ev: str, carry: bool = False) -> None:
        """carry = 전날 파일의 마지막 수신기 사건을 이어 받은 줄 — 상태만 잡고 사건 목록에는 넣지 않는다."""
        status = {"connected": "connected", "disconnected": "disconnected"}.get(ev)
        if status is None:
            self.bad_rows += 1
            return
        self.receiver = (status, t)
        if not carry:
            self._event(t, "receiver", "Receiver connected" if status == "connected" else "Receiver disconnected")

    def _boot(self, t: float, p: bytes, retain: bool) -> None:
        if p.startswith(OFFLINE):
            # 장치와 브로커의 연결이 끊기면 브로커가 대신 보낸다 (last will) — 전원을 껐는지 통신이 끊겼는지는 모른다
            if self.offline_at is None or self.offline_at < (self.last_rx or 0):
                self.offline_at = t
                if not retain:
                    self._event(t, "offline", f"{self.dev_name} offline — power off or link lost")
        elif len(p) == 8 and not retain:
            self.offline_at = None                                # 장치가 다시 붙었다

    def _clock(self, t: float, dev_ms: int) -> None:
        if self.dev_last is None or dev_ms + 1000 < self.dev_last:
            self._new_boot(t, first=self.dev_last is None)
        elif dev_ms > self.dev_last:
            self.periods.append(dev_ms - self.dev_last)
        self.dev_last = dev_ms
        transit = t - dev_ms / 1000.0
        while self.win and self.win[-1][1] >= transit:
            self.win.pop()
        self.win.append((t, transit))
        while self.win[0][0] < t - CLOCK_WINDOW_S:
            self.win.popleft()
        self.offset = self.win[0][1]
        if self.boot_at is None:
            self.boot_at = self.offset

    def _new_boot(self, t: float, first: bool) -> None:
        self.win.clear()
        self.periods.clear()
        self.offset = None
        self.boot_at = None
        self.gps_ts = self.analog_ts = None
        self.prev_pt = None
        for k in self.ids.values():
            k.last_ts = None                                      # 장치 시각이 처음부터 다시 — 간격 추정은 이어 간다
        self.boots += 1
        if not first:
            self._restarted = True
        self._event(t, "boot", f"{self.dev_name} online" if first else f"{self.dev_name} reboot")

    def _srv(self, dev_ms: int, t: float) -> float:
        return dev_ms / 1000.0 + self.offset if self.offset is not None else t

    def period_s(self) -> float:
        if not self.periods:
            return 0.5
        loop = float(np.median(self.periods))
        return max(math.floor(loop / 10.0) * 10.0, 10.0) / 1000.0     # 보내기 주기 = 설정값 + 보내는 데 든 시간

    def _limits(self) -> tuple[float, float]:
        p = self.period_s()
        return max(2.0, p * 2), max(10.0, p * 4)

    def _periodic(self, t: float, is_d: bool) -> None:
        late, lost = self._limits()
        if self.last_rx is None:
            self.link_since = t
        else:
            gap = t - self.last_rx
            if gap > late:
                self.link_since = t                               # 늦음 이상으로 끊겼다 — 원천별 사건은 다시 이어진 뒤부터
            if gap > lost:
                self._log_lost(lost)                              # 화면이 그동안 묻지 않았어도(파일을 다시 읽을 때) 같은 사건이 남게
                if not self._restarted:                           # 다시 부팅한 것은 끊김으로 세지 않는다
                    self.outages.append((self.last_rx, t))
                    self._event(t, "restored", f"{self.dev_name} data back — {gap:.1f} s gap", gap_s=_r(gap, 1))
        self._restarted = False
        self.last_rx = t if self.last_rx is None else max(self.last_rx, t)
        if is_d:
            self.d_rx.append(t)
            while self.d_rx and self.d_rx[0] < t - RATE_WINDOW_S:
                self.d_rx.popleft()

    def _logbuf(self, t: float, p: bytes) -> None:
        if len(p) != LOGBUF_SIZE:
            self.bad_rows += 1
            return
        dev_ms, state = struct.unpack_from("<II", p, 0)
        self._clock(t, dev_ms)
        self._periodic(t, True)
        self._device_state(t, state)
        fix = False
        recs = _records(p[8:])
        self.bad_records += sum(1 for k in range(4) if any(p[8 + 24 * k:32 + 24 * k])) - len(recs)   # 빈 칸(0)은 세지 않는다
        for r in recs:
            ts = int.from_bytes(r[4:8].tobytes(), "little")
            if r[1] == T_GPS and ts != self.gps_ts:
                self.gps_ts = ts
                fix = int.from_bytes(r[8:12].tobytes(), "little") != 0
                self._gps(self._srv(ts, t), r[8:24].tobytes())
            elif r[1] == T_ANALOG and ts != self.analog_ts:
                self.analog_ts = ts
                ain = struct.unpack("<8h", r[8:24].tobytes())          # AIN1 ~ 6 · 공급 전압 · 보드 온도 (monolith.py 와 같은 배치)
                if ain[6] > 0:
                    self._put("lv.voltage_v", self._srv(ts, t), ain[6] * LV_V_PER_RAW)
                self._put("brake.ain1_v", self._srv(ts, t), ain[0] / 32768 * ADC_FSR_V)
        self.fix_win.append((t, fix))
        while self.fix_win and self.fix_win[0][0] <= t - RATE_WINDOW_S:
            self.fix_win.popleft()
        self._source_events(t, self._srv(dev_ms, t))
        self._grid(t)
        self._close_laps(t)

    def _device_state(self, t: float, state: int) -> None:
        """Monolith 가 스스로 낸 부품 상태. 치명(SD 를 못 씀 등)이면 경고 — v2.3.0 은 SD 가 없으면 실시간 레코드에도 머리를 안 찍어 값이 다 버려진다."""
        self.dev_state = state
        fatal = [n for i, n in enumerate(DEVICE_COMPONENTS) if state >> (i + DEVICE_FATAL_SHIFT) & 1]
        if fatal:
            text = f"{self.dev_name} 장치 오류 — " + ", ".join(fatal)
            cur = self.active.get("W-DEV")
            if cur is None or cur["text"] != text:
                self.active["W-DEV"] = {"id": "W-DEV", "level": "warn", "text": text, "value": state, "since": t, "channel": None}
                self._event(t, "alert", text)
        elif "W-DEV" in self.active:
            self.active.pop("W-DEV")
            self._event(t, "alert", f"{self.dev_name} 장치 오류 — 해제")

    def _source_events(self, t: float, dev_now: float) -> None:
        """원천이 멈추면 "<원천> no data" — /d 가 이어지는 동안만 본다 (장치가 꺼졌거나 수신이 끊긴 동안은 내지 않는다).
        나이는 이 /d 를 보낸 순간(장치 시각)으로 잰다 — 전달이 늦어져도 원천이 멈춘 것으로 보지 않게."""
        if self.link_since is None or t - self.link_since < SOURCE_STALE_S + 1.0:
            return
        for sid, name in SOURCE_EVENT_NAMES.items():
            last = self.seen.get(sid)
            if last is not None and sid not in self.src_down and dev_now - last > SOURCE_STALE_S:
                self.src_down[sid] = last
                self._event(last + SOURCE_STALE_S, "source_lost", f"{name} no data", source=sid)

    def _seen(self, src: str, ts: float) -> None:
        self.seen[src] = max(self.seen.get(src, ts), ts)
        down = self.src_down.get(src)
        if down is not None and ts > down:
            del self.src_down[src]
            self._event(ts, "source_back", f"{SOURCE_EVENT_NAMES[src]} data back — {ts - down:.1f} s gap", source=src, gap_s=_r(ts - down, 1))

    def _log_lost(self, lost_s: float) -> None:
        """끊김 기준을 넘은 순간의 "<송신 장치> no data" — 끊김 하나에 한 번."""
        if self.last_rx is not None and self.lost_logged != self.last_rx:
            self.lost_logged = self.last_rx
            self._event(self.last_rx + lost_s, "lost", f"{self.dev_name} no data")

    def _gps(self, ts: float, pl: bytes) -> None:
        lat_raw, lon_raw, lat_dir, lon_dir, _, speed, course = struct.unpack("<IIBBHHH", pl)
        if lat_raw == 0:
            return                                                # 위치를 못 잡음
        lat = float(nmea_to_deg(np.float64(lat_raw))) * (-1 if lat_dir == ord("S") else 1)
        lon = float(nmea_to_deg(np.float64(lon_raw))) * (-1 if lon_dir == ord("W") else 1)
        v, course = speed / 100.0, course / 100.0
        self._put("gps.speed_kmh", ts, v)
        if self.venue is None:
            return
        x, y = (float(a) for a in to_local_m(lat, lon, self.venue["origin"]["lat0"], self.venue["origin"]["lon0"]))
        self._put("gps.x_m", ts, x)
        self._put("gps.y_m", ts, y)
        self._path(ts, x, y, v)
        if self.prev_pt is not None and ts > self.prev_pt[0]:
            t0, x0, y0, c0 = self.prev_pt
            hit = gate_crossings(np.array([t0, ts]), np.array([x0, x]), np.array([y0, y]), np.array([c0, course]), self.gate)
            for tc in hit:
                if not self.crossings or tc - self.crossings[-1] > MIN_LAP_S:
                    self.crossings.append(float(tc))
        self.prev_pt = (ts, x, y, course)

    def _can(self, t: float, p: bytes) -> None:
        if len(p) % REC:
            self.bad_rows += 1
            return
        self._periodic(t, False)
        recs = _records(p)
        self.bad_records += len(p) // REC - len(recs)
        for r in recs:
            if r[1] != T_CAN:
                continue
            dev = int.from_bytes(r[4:8].tobytes(), "little")
            ts = self._srv(dev, t)
            can_id = int.from_bytes(r[8:12].tobytes(), "little")
            if can_id in SOURCE_IDS:
                key = (can_id, int(r[12]))
                k = self.ids.get(key)
                if k is None:
                    k = self.ids[key] = _IdRate(t)
                k.add(t, dev)
            if can_id == ec.BAMOCAR:
                self._seen("inverter", ts)
            for ch, v in decode_frame(can_id, int(r[14]), r[16:24].tobytes()):
                self._put(ch, ts, v)

    # ------------------------------------------------------------ 값 · 경고

    def _put(self, ch: str, ts: float, v: float) -> None:
        old = self.vals.get(ch)
        if old is not None and ts < old[0]:
            return                                                # 늦게 온 옛 값으로 덮지 않는다
        self.vals[ch] = (ts, float(v))
        src = CHANNELS[ch].source
        if src in SOURCE_EVENT_NAMES:
            self._seen(src, ts)
        else:
            self.seen[src] = max(self.seen.get(src, ts), ts)
        if ch in self.lap_samples and self.venue is not None:
            self.lap_samples[ch].append((ts, float(v)))
        if ch in ("bms.temp_max_c", "bms.temp_min_c"):
            other = self.vals.get("bms.temp_min_c" if ch == "bms.temp_max_c" else "bms.temp_max_c")
            if other is not None and abs(ts - other[0]) <= CHANNELS["bms.temp_spread_c"].stale_s:
                hi, lo = (float(v), other[1]) if ch == "bms.temp_max_c" else (other[1], float(v))
                self._put("bms.temp_spread_c", min(ts, other[0]), hi - lo)     # 더 옛 쪽 시각 — 낡음을 보수적으로
        if ch == "motor.error_raw":
            self._inverter_errors(ts, int(v))
        elif ch == "pedal.fault_raw":
            self._apps_faults(ts, int(v))
        for rule in self.by_channel.get(ch, ()):
            self._rule(rule, ts, float(v))

    def _fresh(self, ch: str, at: float) -> float | None:
        x = self.vals.get(ch)
        if x is None or at - x[0] > CHANNELS[ch].stale_s:
            return None
        return x[1]

    def _rule(self, rule: Rule, ts: float, v: float) -> None:
        level = rule.level(v)
        if level and rule.when_speed_kmh is not None:
            speed = self._fresh("gps.speed_kmh", ts)
            if speed is None or speed <= rule.when_speed_kmh:
                level = None
        if level is None:
            self.pending.pop(rule.id, None)
            if rule.id in self.active:
                gone = self.active.pop(rule.id)
                self._event(ts, "alert", f"{gone['text']} — 해제")
            return
        p = self.pending.get(rule.id)
        if p is None or p[0] != level:
            p = self.pending[rule.id] = (level, ts)
        if ts - p[1] < rule.hold_s:
            return
        shown = "info" if rule.info else level
        cur = self.active.get(rule.id)
        if cur is None or cur["level"] != shown:
            text = rule.message(level)
            self.active[rule.id] = {"id": rule.id, "level": shown, "text": text, "value": _r(v), "since": p[1], "channel": rule.channel}
            self._event(ts, "alert", f"{text} ({_fmt(_r(v, CHANNELS[rule.channel].digits))} {CHANNELS[rule.channel].unit})".replace(" )", ")"))
        else:
            cur["value"] = _r(v)

    def _inverter_errors(self, ts: float, raw: int) -> None:
        bits = int(ec.bamocar_error_bits(np.array([raw]))[0])
        if bits:
            text = "인버터 오류 — " + ", ".join(ec.bamocar_error_names(bits))
            cur = self.active.get("W-INV-ERR")
            if cur is None or cur["text"] != text:
                self.active["W-INV-ERR"] = {"id": "W-INV-ERR", "level": "danger", "text": text, "value": raw, "since": ts,
                                             "channel": "motor.error_raw"}
                self._event(ts, "alert", text)
        elif "W-INV-ERR" in self.active:
            self.active.pop("W-INV-ERR")
            self._event(ts, "alert", "인버터 오류 — 해제")

    def _apps_faults(self, ts: float, raw: int) -> None:
        names = ec.fault_names(raw)
        if names:
            text = "APPS 고장 — " + ", ".join(names)
            cur = self.active.get("W-APPS-FLT")
            if cur is None or cur["text"] != text:
                self.active["W-APPS-FLT"] = {"id": "W-APPS-FLT", "level": "warn", "text": text, "value": raw, "since": ts,
                                              "channel": "pedal.fault_raw"}
                self._event(ts, "alert", text)
        elif "W-APPS-FLT" in self.active:
            self.active.pop("W-APPS-FLT")
            self._event(ts, "alert", "APPS 고장 — 해제")

    # ------------------------------------------------------------ 랩 값

    def _close_laps(self, now: float) -> None:
        """계측선을 지나고 LAP_SETTLE_S 가 지난 랩을 굳히고, 열린 랩보다 앞의 표본은 버린다."""
        while len(self.lap_rows) < len(self.crossings) - 1:
            i = len(self.lap_rows)
            a, b = self.crossings[i], self.crossings[i + 1]
            if now < b + LAP_SETTLE_S:
                break
            row = {"lap": i + 1, **self._lap_stats(a, b)}
            prev = self.lap_rows[-1]["lap_time_s"] if self.lap_rows else None
            row["lap_delta_s"] = _r(row["lap_time_s"] - prev, 1) if prev is not None else None
            self.lap_rows.append(row)
        if len(self.crossings) > len(self.lap_rows):
            keep = self.crossings[len(self.lap_rows)] - max(ENERGY_GAP_S, SOC_NEAR_S)
            for dq in self.lap_samples.values():
                while dq and dq[0][0] < keep:
                    dq.popleft()

    def _lap_stats(self, a: float, b: float) -> dict:
        p = [x for x in self.lap_samples["battery.power_kw"] if a - ENERGY_GAP_S <= x[0] <= b + ENERGY_GAP_S]
        kws = covered = 0.0
        for (t0, v0), (t1, v1) in zip(p, p[1:]):
            lo, hi = max(t0, a), min(t1, b)
            if t1 - t0 > ENERGY_GAP_S or hi <= lo:
                continue
            at = (lambda x: v0 + (v1 - v0) * (x - t0) / (t1 - t0))
            kws += 0.5 * (at(lo) + at(hi)) * (hi - lo)
            covered += hi - lo
        soc0, soc1 = self._near("bms.soc_pct", a), self._near("bms.soc_pct", b)
        cell = [v for t, v in self.lap_samples["bms.cell_min_v"] if a <= t <= b]
        temp = [v for t, v in self.lap_samples["bms.temp_max_c"] if a <= t <= b]
        speed = [v for t, v in self.lap_samples["gps.speed_kmh"] if a <= t <= b]
        return {"t_start": _r(a, 3), "t_end": _r(b, 3), "lap_time_s": _r(b - a, 1),
                "net_wh": _r(kws / 3.6, 0) if covered > 0 else None, "cover": _r(covered / (b - a), 2) if b > a else None,
                "soc_start_pct": soc0, "soc_end_pct": soc1, "soc_delta_pct": _r(soc1 - soc0, 1) if soc0 is not None and soc1 is not None else None,
                "cell_v_min": _r(min(cell), 3) if cell else None, "cell_t_max_c": _r(max(temp), 0) if temp else None,
                "speed_max_kmh": _r(max(speed), 1) if speed else None}

    def _near(self, ch: str, at: float) -> float | None:
        """at 에 가장 가까운 표본 (앞쪽 먼저) — SOC_NEAR_S 안에 없으면 None."""
        before = after = None
        for t, v in self.lap_samples[ch]:
            if t <= at:
                before = (t, v)
            else:
                after = (t, v)
                break
        if before is not None and at - before[0] <= SOC_NEAR_S:
            return _r(before[1], 1)
        if after is not None and after[0] - at <= SOC_NEAR_S:
            return _r(after[1], 1)
        return None

    def _event(self, t: float, kind: str, text: str, **extra) -> None:
        """사건을 시각 순서로 넣는다 — 끊긴 순간 · 원천이 멈춘 순간은 나중에 알게 되어 앞 시각으로 들어온다."""
        e = {"t": round(t, 3), "kind": kind, "text": text, **extra}
        i = len(self.events)
        while i > 0 and self.events[i - 1]["t"] > e["t"]:
            i -= 1
        if i == len(self.events):
            self.events.append(e)
            return
        if len(self.events) == self.events.maxlen:                # 꽉 찬 deque 에는 insert 가 안 된다 — 가장 옛것을 먼저 버린다
            if i == 0:
                return
            self.events.popleft()
            i -= 1
        self.events.insert(i, e)

    def _grid(self, t: float) -> None:
        g = math.floor(t / GRID_S) * GRID_S
        if self.grid_t is None:
            self.grid_t = g - GRID_S
        if g <= self.grid_t:
            return
        steps = int(round((g - self.grid_t) / GRID_S))
        n = len(CHART) + len(RATE_SERIES)
        for k in range(1, min(steps, self.chart.maxlen)):        # 끊긴 동안의 칸은 비워 둔다 (선을 끊는다)
            self.chart.append((round(self.grid_t + k * GRID_S, 3), (None,) * n))
        self.chart.append((round(g, 3), tuple(_r(self._fresh(ch, g)) for ch in CHART) + self._rates_at(g)))
        self.grid_t = g

    # ------------------------------------------------------------ 내보내기

    def _status(self, ch: str, now: float) -> tuple[str, float | None, float | None, str | None]:
        x = self.vals.get(ch)
        if x is None:
            return "none", None, None, None
        age = max(0.0, now - x[0])
        if age > CHANNELS[ch].stale_s:
            return "stale", x[1], age, None
        hits = [a for a in self.active.values() if a["channel"] == ch and a["level"] in ("danger", "warn")]
        if hits:
            top = max(hits, key=lambda a: LEVELS[a["level"]])
            return top["level"], x[1], age, top["id"]
        return "ok", x[1], age, None

    def _scale(self, c: Ch) -> dict | None:
        """막대 게이지 — 양 끝은 채널 표, 문턱은 그 채널의 경고 규칙(기본값 + 차별 문턱 파일)과 같은 값."""
        if c.gauge is None:
            return None
        rule = next(iter(self.by_channel.get(c.id, ())), None)
        return {"min": c.gauge[0], "max": c.gauge[1], "warn": rule.warn if rule else None, "danger": rule.danger if rule else None,
                "dir": rule.op if rule else None}

    def _link(self, now: float) -> dict:
        late, lost = self._limits()
        p = self.period_s()
        if self.last_rx is None:
            return {"status": "none", "age_s": None, "period_s": p, "rate_10s": None, "outages": 0, "longest_s": None, "since_s": None}
        age = max(0.0, now - self.last_rx)
        n = sum(1 for x in self.d_rx if now - RATE_WINDOW_S < x <= now)
        rate = min(1.0, n / (RATE_WINDOW_S / p))
        status = "ok" if age <= late else "late" if age <= lost else "lost"
        longest = max((b - a for a, b in self.outages), default=None)
        return {"status": status, "age_s": _r(age, 1), "period_s": p, "rate_10s": _r(rate, 2), "outages": len(self.outages),
                "longest_s": _r(longest, 1), "since_s": _r(age, 1) if status == "lost" else None}

    def _rates_at(self, at: float) -> tuple:
        """그래프용 수신률 (RATE_SERIES 순서) — 그 순간의 link.rate_10s · 원천별 · GPS 위치 비율."""
        src = self._source_rates(at)
        fixes = [f for x, f in self.fix_win if at - RATE_WINDOW_S < x <= at]
        return (self._link(at)["rate_10s"], src.get("bms"), src.get("inverter"), src.get("pedal"),
                _r(sum(fixes) / len(fixes), 2) if fixes else None)

    def _source_rates(self, now: float) -> dict[str, float]:
        """원천마다 최근 10 s 에 받은 새 CAN 프레임 수 ÷ 와야 할 수 (ID 가 여럿이면 합산)."""
        got: dict[str, float] = defaultdict(float)
        want: dict[str, float] = defaultdict(float)
        p = self.period_s()
        loop = float(np.median(self.periods)) / 1000.0 if self.periods else p
        for (cid, _), k in self.ids.items():
            n, w = k.counts(now, p, loop)
            got[SOURCE_IDS[cid]] += n
            want[SOURCE_IDS[cid]] += w
        return {s: _r(min(1.0, got[s] / want[s]), 2) for s in want if want[s] > 0}

    def _device(self, now: float, link: dict) -> dict:
        if link["status"] in ("ok", "late"):
            status = "on"
        elif self.offline_at is not None and (self.last_rx is None or self.offline_at >= self.last_rx):
            status = "disconnected"
        else:
            status = "unknown"
        st = self.dev_state
        bit = (lambda i, sh: st is not None and st >> (i + sh) & 1)
        return {"status": status, "boot_at": _r(self.boot_at, 1), "firmware": self.firmware, "boots": self.boots,
                "errors": [n for i, n in enumerate(DEVICE_COMPONENTS) if bit(i, 0)] if st is not None else None,
                "fatal": [n for i, n in enumerate(DEVICE_COMPONENTS) if bit(i, DEVICE_FATAL_SHIFT)] if st is not None else None}

    def state(self, now: float, since: float | None = None) -> dict:
        link = self._link(now)
        if link["status"] == "lost":
            self._log_lost(self._limits()[1])                     # 끊긴 동안 화면이 물으면 그때 "<송신 장치> no data" (다시 받으면 _periodic 이 같은 것을)
        device = self._device(now, link)
        tiles = []
        for ch in TILES:
            st, v, age, alert = self._status(ch, now)
            c = CHANNELS[ch]
            tiles.append({"id": ch, "label": c.label, "unit": c.unit, "sign": c.sign, "digits": c.digits, "value": _r(v, 4),
                          "age_s": _r(age, 1), "status": st, "alert": alert, "scale": self._scale(c)})
        rates = self._source_rates(now)
        fixes = [f for x, f in self.fix_win if now - RATE_WINDOW_S < x <= now]
        sources = []
        for sid, label in SOURCES:
            last = self.seen.get(sid)
            age = None if last is None else max(0.0, now - last)
            st = "none" if age is None else "stale" if age > SOURCE_STALE_S else "ok"
            r = rates.get(sid)
            s = {"id": sid, "label": label, "status": st, "age_s": _r(age, 1), "rate_10s": r,
                 "rate_status": None if r is None else "danger" if r < RATE_DANGER else "warn" if r < RATE_WARN else "ok"}
            if sid == "gps":
                s["fix_10s"] = _r(sum(fixes) / len(fixes), 2) if fixes else None
            sources.append(s)

        alerts = [a for a in self.active.values()
                  if (self._fresh(a["channel"], now) is not None if a["channel"] else link["status"] in ("ok", "late"))]   # 장치 경고는 받는 동안만
        if link["status"] in ("late", "lost") and device["status"] != "disconnected":
            lv = "warn" if link["status"] == "late" else "danger"
            alerts.append({"id": "W-LINK", "level": lv, "text": f"수신 {'늦음' if lv == 'warn' else '끊김'} — {link['age_s']:.0f} s",
                           "value": link["age_s"], "since": _r(self.last_rx, 3), "channel": None})
        if device["status"] == "on":
            for s in sources:
                if s["status"] == "stale":
                    alerts.append({"id": f"W-SRC-{s['id']}", "level": "info", "text": f"{s['label']} 값 안 들어옴 — {s['age_s']:.0f} s",
                                   "value": s["age_s"], "since": None, "channel": None})
        alerts.sort(key=lambda a: -LEVELS[a["level"]])

        inv_src = next(s for s in sources if s["id"] == "inverter")
        err = self._fresh("motor.error_raw", now)
        bits = int(ec.bamocar_error_bits(np.array([int(err)]))[0]) if err is not None else 0
        en, rd = self._fresh("motor.enabled", now), self._fresh("motor.ready", now)
        inverter = {"status": inv_src["status"] if inv_src["status"] != "ok" else ("danger" if bits else "ok"),
                    "enabled": None if en is None else bool(en), "ready": None if rd is None else bool(rd),
                    "errors": ec.bamocar_error_names(bits) if bits else [],
                    "warnings": ec.bamocar_warning_names(int(err)) if err is not None else [], "age_s": inv_src["age_s"]}
        ped_src = next(s for s in sources if s["id"] == "pedal")
        flt = self._fresh("pedal.fault_raw", now)
        faults = ec.fault_names(int(flt)) if flt is not None else []
        pedal = {"status": ped_src["status"] if ped_src["status"] != "ok" else ("warn" if faults else "ok"),
                 "faults": faults, "age_s": ped_src["age_s"]}

        position = laps = None
        if self.venue is not None and "gps.x_m" in self.vals:
            tx, x = self.vals["gps.x_m"]
            position = {"frame_id": self.venue.get("frame_id"), "x_m": _r(x, 1), "y_m": _r(self.vals["gps.y_m"][1], 1),
                        "age_s": _r(max(0.0, now - tx), 1)}
        if self.venue is not None and self.crossings:
            done = np.diff(self.crossings)
            a = self.crossings[-1]
            laps = {"count": int(len(done)), "current_s": _r(max(0.0, now - a), 1),
                    "last_s": _r(done[-1], 1) if len(done) else None, "best_s": _r(done.min(), 1) if len(done) else None,
                    "basis": "GPS 계측선 통과 — GPS 주기만큼 오차",
                    "rows": list(self.lap_rows),
                    "current": {"lap": len(self.crossings), **self._lap_stats(a, max(a, now)), "lap_delta_s": None},
                    "energy_basis": "배터리 전력(Orion 요약, 약 2 Hz 순간값)을 이어 적분 — 랩마다 몇 % 오차. "
                                    "cover = 전력 값이 이어진 시간의 비율 (1 보다 작으면 빈 곳이 있어 에너지가 작게 나옴)"}

        lo = since if since is not None else now - CHART_DEFAULT_S
        rows = [r for r in self.chart if r[0] > lo]
        chart = {"t": [r[0] for r in rows], "series": {ch: [r[1][i] for r in rows] for i, ch in enumerate(CHART + RATE_SERIES)}}

        return {"contract": CONTRACT, "now": _r(now, 3), "car": self.car,
                "source": {"replay": self.replay, "bad_rows": self.bad_rows, "unknown_topics": self.unknown, "bad_records": self.bad_records},
                "receiver": {"status": self.receiver[0], "since": _r(self.receiver[1], 3)},
                "device": device, "link": link, "sources": sources, "tiles": tiles, "inverter": inverter, "pedal": pedal,
                "position": position, "track_rev": self.path_rev if self.venue is not None else None, "laps": laps, "alerts": [{k: v for k, v in a.items() if k != "channel"} for a in alerts],
                "events": list(reversed(self.events)), "chart": chart}

    def _path(self, ts: float, x: float, y: float, kmh: float) -> None:
        """주행 중인 GPS 점을 PATH_STEP_M 마다 하나씩 모은다. 오래 비었거나 위치가 튀면 선을 끊는다."""
        if kmh <= MOVING_KMH or (self.path_seen is not None and ts < self.path_seen):
            return
        gap = self.path_seen is None or ts - self.path_seen > PATH_GAP_S
        self.path_seen = ts
        if self.path_last is not None:
            d = math.hypot(x - self.path_last[0], y - self.path_last[1])
            if not gap and d < PATH_STEP_M:
                return
            if gap or d > PATH_JUMP_M:
                self.path.append((None, None))
        self.path.append((x, y))
        self.path_last = (x, y)
        self.path_rev += 1

    def _course(self, pts: list) -> dict:
        """최근 달린 궤적이 경기장 기준선 위인가 — 짧으면 판단하지 않는다 (share · on_line = None)."""
        if self.course_at is not None and self.course_at[0] == self.path_rev:
            return dict(self.course_at[1])
        need = self.line_m * COURSE_WINDOW if self.line_m else None
        out = {"share": None, "on_line": None, "tol_m": COURSE_TOL_M, "need": COURSE_SHARE, "window_m": _r(need, 0)}
        self.course_at = (self.path_rev, out)
        if not need:
            return dict(out)
        xs, ys, run, prev = [], [], 0.0, None
        for x, y in reversed(pts):
            if x is None:
                prev = None
                continue
            if prev is not None:
                run += math.hypot(x - prev[0], y - prev[1])
            xs.append(x)
            ys.append(y)
            prev = (x, y)
            if run >= need:
                break
        if run < need:
            return dict(out)
        line = self.venue["line"]
        share = float(np.mean(distance_to_line(np.array(xs), np.array(ys), line["x_m"], line["y_m"]) <= COURSE_TOL_M))
        out.update(share=_r(share, 2), on_line=share >= COURSE_SHARE)
        return dict(out)

    def track(self) -> dict | None:
        if self.venue is None:
            return None
        v = self.venue
        pts = list(self.path)
        return {"frame_id": v.get("frame_id"), "line": {"x_m": list(v["line"]["x_m"]), "y_m": list(v["line"]["y_m"])},
                "gate": {**v["gate"], "half_width_m": v.get("half_width_m")},
                "path": {"x_m": [_r(x, 1) for x, _ in pts], "y_m": [_r(y, 1) for _, y in pts], "rev": self.path_rev,
                         "step_m": PATH_STEP_M, "min_kmh": MOVING_KMH},
                "course": self._course(pts)}
