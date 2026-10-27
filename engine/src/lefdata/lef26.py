"""LEF-26 자작 로거(STM32F446) 통합본 CSV → 공통 포맷.

입력: 07_데이터/LEF-26/<이벤트>/raw_merged/<세션>_merged.csv  (헤더 없음, 수천만 줄 규모)
한 줄 = `HH:MM:SS.mmm,<소스>,<값>,<값>,...`
    소스가 hex 이면 CAN ID, 값 = 데이터 바이트(10진). 아니면 IMU/MAG/LIN/PED/GPS/SYSTEM.

채널 정의는 26팀 펌웨어 소스로 확정했다 (2026-09-21).
    github.com/GambitIsSalvation/FSK-2026-Datalogger — `Core/Src/main.c`, `Core/Src/MPU9250.c`
    STM32F446RE + MPU9250(9축). 기록은 RTD ON 상태에서만 일어난다.

데이터가 크므로 파일을 두 번 스트리밍한다.
    1차 (scan_time_plan): 레코딩 경계와 시계 점프만 모아 '시간 계획'을 만든다
    2차 (convert_file)   : 시간 계획을 적용해 레코딩별 Parquet 을 쓴다

시계 모델 (2026-09-17 조사)
    - `SYSTEM,CONFIG ON` = 로거 부팅 = 레코딩 경계
    - RTC 백업 전원이 없어 부팅 시 시계는 직전 전원 차단 시각에서 이어진다 (틀린 시각)
    - `RTC-UTC SYNC` 또는 이벤트 없는 점프로 시계가 바뀐다. 한 레코딩 안에서는 로거가 계속
      돌고 있었으므로 점프 순간의 경과시간은 0 으로 본다 (이어 붙임, 오차 ±1 s)
    - 동기화 직후 `09:00:0x` 는 GPS 시각 미수신 기본값(00:00 UTC + 9h)으로 보고 신뢰하지 않는다
    - 시(hour) ≥ 24 로 점프한 시각은 신뢰하지 않는다
    - 레코딩 안에 신뢰 가능한 기준점이 하나라도 있으면 전체에 절대시각을 준다. 없으면 null

시계 모델 v2 (2026-10-01, 온보드 영상의 GPS 시각과 대조해 알게 된 것)
    - 줄마다 찍힌 시각은 RTC 값이고, RTC 는 내부 RC 발진기(LSI)로 돌아 **약 7 % 느리다**
    - GPS 가 PPS 를 주는 동안은 펌웨어가 매초 RTC 를 다시 맞춘다 → '초' 는 맞다 (`gps_pps`)
    - GPS 가 죽으면 RTC 가 혼자 돈다 (`free`) → **경과 시간을 시계 속도 f 로 나눠 바로잡는다**
          t_rel_ms = 로거 경과시간 ÷ f          t_abs = 기준점 시각 + (기준점부터의 로거 경과시간 ÷ f)
      f 는 로그만으로 잰다: SD 동기화(MCU 틱 5 초)가 남긴 기록 공백의 간격 ÷ 5 × TICK_RATE.
      주기를 못 찾으면 F_FREE 를 쓴다. 영상으로 잰 값과 대조해 확인했다 (lef26_clock)
    - 어느 쪽인지는 밀리초 분포로 가린다: PPS 가 매초 맞추면 밀리초가 약 930 에서 끊겨 960 이상이 없다
    - 로거가 찍은 그대로의 경과시간은 frames.t_log_ms 에, 원문은 frames.t_raw 에 남는다
    - 바로잡지 않는 것: `gps_pps` 의 초 안 톱니(0~70 ms), `mixed` 레코딩(도중에 바뀜), 기준점 시각 자체의 오차

CAN 프레임 분류 (2026-10-01)
    - 펌웨어는 표준 ID 칸만 적는다. 확장 ID 프레임은 **직전 프레임의 ID 로 찍혀 있다** → 내용으로 가린다
    - Orion 써미스터 확장 모듈의 프레임(0x1839F380 · 0x1838F380, 그리고 표준 ID 0x80)은 `thermistor` 표로,
      `bms` 표에서는 뺀다
"""

from __future__ import annotations

import datetime as dt
import re
import time
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
import pyarrow as pa
import pyarrow.compute as pc
import pyarrow.csv as pacsv
import pyarrow.parquet as pq

from . import __version__
from .writer import TZ, T_ABS_TYPE, dir_bytes, fresh_dir, write_index, write_json

KST = dt.timezone(dt.timedelta(hours=9))
NON_CAN = {"IMU", "MAG", "LIN", "PED", "GPS", "SYSTEM"}
BMS_ID = 0x6B1
MOTOR_ID = 0x181

# BAMOCAR-PG-D3-400-400 (Unitek). 출처: Unitek "CAN-BUS" 매뉴얼 2025/V1
#   §7.1 Automatic CAN transmission — 로거용으로 미리 정해진 8개 변수가 주기 전송된다
#   telegram = [RegID, 값 little endian ...]. DLC 4 → 16 bit, DLC 6 → 32 bit
#   환산 계수는 p20 표의 "Bamocar 400-400" 행
BAMOCAR_I_A_PER_NUM = 0.373831776      # Arms / Num
BAMOCAR_VDC_V_PER_NUM = 0.0181423      # V / Num
MOTOR_REGS = {                          # RegID → (신호 이름, 값 바이트 수, 환산 배율 또는 "igbt")
    0x32: ("n_cmd_pct", 2, 100 / 32767),          # 속도 지령(램프 후). 32767 = N-100 % = N_MAX_RPM
    0x30: ("n_act_pct", 2, 100 / 32767),          # 실제 속도. rpm = pct/100 × N_MAX_RPM
    0x22: ("iq_cmd_a", 2, BAMOCAR_I_A_PER_NUM),   # 토크 전류 지령
    0x27: ("iq_act_a", 2, BAMOCAR_I_A_PER_NUM),   # 실제 토크 전류
    0x66: ("vdc_v", 2, BAMOCAR_VDC_V_PER_NUM),    # DC 버스 전압
    0x4A: ("t_igbt_c", 2, "igbt"),                # 모터 컨트롤러(IGBT) 온도. NTC 비선형 → 표로 변환
    0x49: ("t_motor_c", 2, 1.0),                  # 모터 온도. 26팀 데이터 가이드: raw 그대로 ℃
    0x8A: ("vout_raw", 4, 1.0),                   # VOUT = 출력 전압 사용률(독일어 매뉴얼 p28). 4바이트 분해 방식은 미확인
}
MOTOR_SIGNAL_NAMES = pa.array([v[0] for v in MOTOR_REGS.values()], pa.string())
# --- 26팀 펌웨어에서 확정한 채널 정의 (main.c 의 snprintf 포맷, MPU9250.c 의 스케일)
#
#   IMU  (200 Hz) : acc[0..2], cal_gyro[0..2]
#       가속도 ±8 g (1/4096 LSB/g) → g 단위. **X 가 횡(좌우), Y 가 종(앞뒤)** 이다.
#           가속할 때 Y 양수 / 감속할 때 Y 음수 · **우회전 X 양수** / 좌회전 X 음수
#           (2026-10-01 정정: 영상 GPS 궤적과 대 보니 '좌회전 X 양수' 는 반대였다. X 오른쪽 · Y 앞 · Z 위)
#           (펌웨어도 acc[0] 을 lat_g, acc[1] 을 long_g 로 쓴다)
#       자이로 ±2000 °/s (1/16.4) → °/s. 부팅 시 구한 bias 를 뺀 값이다.
#           X 축이 횡이므로 X 축 둘레 회전 = 피치, Y 축 둘레 = 롤, Z 축 둘레 = 요 순서다.
#           ⚠ 26팀 데이터 가이드 문서에는 "ROLL, PITCH, YAW" 로 적혀 있으나 **데이터가 반박한다** —
#             두 번째 축이 횡가속도 변화율과 강하게 상관한다 (다른 축은 거의 0). 담당자 답변과도 일치
#           오른쪽으로 기울면 롤 양수 · 뒤로 기울면 피치 양수 · 좌회전에서 요 양수
#   MAG  (100 Hz) : 지자기 3축. ⚠ 단위가 µT 가 아니다 (아래 MAG_TO_UT 참고)
#   LIN  (200 Hz) : 서스펜션 리니어 센서. **RL(리어 좌) · RR(리어 우)** — 26팀 데이터 가이드
#                   (adc - bias) × 75 / 4095 → mm, 0~75 범위
#       오프셋 없음. 서스펜션이 압축하면 값이 줄고 늘어나면 값도 는다
#   PED  (200 Hz) : 브레이크 스위치 GPIO. 센서를 연결하지 못해 **전 구간 0** 이다
#   GPS  (10 Hz)  : 십진도 위도, 십진도 경도, course_t(진북 기준 진행 방위 °)
#       위경도는 펌웨어가 이미 NMEA → 십진도로 바꿔서 쓴다 (25evo Monolith 와 다르다)
#
# MAG 단위: AK8963 16 bit 해상도는 0.15 µT/LSB 라서 raw × 0.15 가 µT 인데,
#   펌웨어는 raw ÷ 0.15 를 한 뒤 deg2rad 까지 곱한다 (MPU9250.c ReadMag).
#   결과적으로 기록된 값 = µT × 0.7757 이다. 벡터 크기가
#   지구 자기장(약 50 µT)과 비슷해 보이지만 우연이므로 µT 로 쓰면 안 된다.
MAG_TO_UT = 0.15 / (1 / 0.15 * np.pi / 180)   # 기록값 × 이 값 = µT (하드아이언 보정 전)

# BAMOCAR 컨트롤러 설정값 N-100 % (RegID 0xC8). 26팀 확인: NDrive 에서 6000 rpm 으로 설정.
#   모터(EMRAX 208) 물리 최대는 6500 rpm (LEF-26 설계보고서 p47) — 설정값과 다른 값이다.
#   속도 신호는 이 값 대비 % 라서, rpm 은 해석 단계에서 환산한다 (정규화 테이블은 % 그대로).
N_MAX_RPM = 6000.0

# 모터 컨트롤러(IGBT) 온도 변환표. 26팀에게 받은 BAMOCAR 표 "BaC2 / NTC-infin" (2026-09-21).
#   NTC 서미스터라 저항-온도가 비선형이다. 표의 n(raw) ↔ Tigbt(℃) 를 선형 보간해서 쓴다.
IGBT_TEMP_RAW = np.array([
    16245, 16308, 16387, 16487, 16609, 16759, 16938, 17151, 17400, 17688,
    18017, 18387, 18797, 19247, 19733, 20250, 20793, 21357, 21933, 22515,
    23097, 23671, 24232, 24775, 25296, 25792, 26261, 26702, 27114, 27497,
    27851, 28179, 28480, 28757, 29011, 29243, 29456, 29650, 29827,
])
IGBT_TEMP_C = np.arange(-35, 156, 5, dtype=float)


def igbt_temp_c(raw):
    """IGBT 온도 raw → ℃. 표 범위를 벗어나면 NaN (추측으로 채우지 않는다)."""
    raw = np.asarray(raw, dtype=float)
    out = np.interp(raw, IGBT_TEMP_RAW, IGBT_TEMP_C)
    return np.where((raw < IGBT_TEMP_RAW[0]) | (raw > IGBT_TEMP_RAW[-1]), np.nan, out)
FINAL_DRIVE = 4.2727          # 11T : 47T (설계보고서 p49)
TIRE_RADIUS_M = 0.2286        # Hoosier 18.0x7.5-10, 무하중 반지름
# 모터 속도 1 % 당 차속 (km/h). 위 세 문서값으로 계산한 값이 아니라 온보드 영상 GPS 로 잰 값이다
# (lef26_clock.speed_calibration). 타이어가 하중을 받아 구르는 반지름이 무하중 반지름보다 작기 때문이다
KMH_PER_PCT = 1.174

# --- 시계 (v2)
F_FREE = 0.931              # RTC 가 혼자 돌 때의 속도 사전값 (영상으로 잰 레코딩 10 개의 중앙)
TICK_RATE = 1.0202          # MCU 틱(HSI) 속도, 참 시간 대비. 영상으로 잰 값 (1.0193 ~ 1.0211, 레코딩 10 개)
LATE_MS = 960               # PPS 가 매초 RTC 를 맞추면 밀리초가 여기까지 가지 못한다
FREE_LATE_SHARE = 10 / 256  # 혼자 돌 때 밀리초 ≥ 960 인 줄의 비율 (subsecond 256 단계 중 10 단계)
TICK_SYNC_S = 5.0           # 펌웨어: 앞 f_sync 가 끝나고 5000 ms(틱) 뒤 첫 쓰기에서 다시 f_sync
SYNC_GAP_MIN_S = 0.030      # f_sync 동안 기록이 멈춘 흔적 (약 39 ms)
SYNC_SPAN_RANGE_S = (4.2, 5.4)
SYNC_PEAK_HALF_S = 0.03
SYNC_MIN_SPANS = 10

# --- Orion 써미스터 확장 모듈 (https://www.orionbms.com/downloads/misc/thermistor_module_canbus.pdf)
THERMISTOR_LEGACY_ID = 0x80
THERMISTOR_CHECK = 0x39 + 8     # 모듈 → BMS 메시지: 체크섬 = 바이트 합 + 0x39 + 길이
THERMISTORS_ENABLED = 32        # 이 차의 모듈 설정 (모듈 → BMS 프레임의 99.9 % 가 이 값)
THERMISTOR_CLAIM = (0xF3, 0x00, 0x80, 0xF3, 0x00, 0x40, 0x1E, 0x90)
KIND_BMS, KIND_MODULE, KIND_SINGLE, KIND_CLAIM, KIND_OTHER = range(5)
KIND_NAMES = ["bms", "module_to_bms", "single", "address_claim", "other"]

HEX = set("0123456789abcdefABCDEF")
QUALITY = ["none", "continuity", "synced"]

JUMP_BACK_S = -1.0      # 이보다 뒤로 가면 시계 점프
JUMP_FWD_S = 60.0       # 이보다 앞으로 가면 시계 점프 (그 이하는 실제 기록 공백으로 본다)
GAP_REPORT_S = 1.0
BATCH_BYTES = 1 << 26

SESSION_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})(?:_(am|pm))?_merged$")


# ---------------------------------------------------------------- 읽기

def iter_lines(path: Path):
    """파일을 줄 단위 StringArray 배치로 스트리밍한다."""
    reader = pacsv.open_csv(
        path,
        read_options=pacsv.ReadOptions(column_names=["line"], block_size=BATCH_BYTES),
        parse_options=pacsv.ParseOptions(delimiter="\x1f", quote_char=False, escape_char=False,
                                         double_quote=False),
        convert_options=pacsv.ConvertOptions(column_types={"line": pa.string()},
                                             strings_can_be_null=False),
    )
    offset = 0
    for i, batch in enumerate(reader):
        lines = batch.column(0)
        if i == 0:
            lines = pc.utf8_ltrim(lines, characters="﻿")
        yield offset, lines
        offset += len(lines)


@dataclass
class Batch:
    offset: int
    lines: pa.Array
    parts: pa.Array
    t_raw: pa.Array
    secs: np.ndarray         # float64, HH 가 24 이상이어도 그대로 누적 초
    src_dict: list           # 이 배치의 소스 이름 목록
    src_code: np.ndarray     # 행별 src_dict 인덱스
    is_sys: np.ndarray
    msg: np.ndarray          # object[str|None], SYSTEM 행만 채움

    @property
    def line_no(self):
        return np.arange(self.offset, self.offset + len(self.lines), dtype=np.int64)

    def code(self, name: str) -> int:
        return self.src_dict.index(name) if name in self.src_dict else -1


def parse_batch(offset: int, lines: pa.Array) -> Batch:
    parts = pc.split_pattern(lines, ",")
    t_raw = pc.list_element(parts, 0)
    if not pc.all(pc.equal(pc.utf8_length(t_raw), 12)).as_py():
        raise ValueError(f"시각 형식이 HH:MM:SS.mmm 이 아닌 줄이 있음 (offset {offset})")
    hh = pc.cast(pc.utf8_slice_codeunits(t_raw, 0, 2), pa.int32()).to_numpy()
    mm = pc.cast(pc.utf8_slice_codeunits(t_raw, 3, 5), pa.int32()).to_numpy()
    ss = pc.cast(pc.utf8_slice_codeunits(t_raw, 6, 12), pa.float64()).to_numpy()
    secs = hh * 3600.0 + mm * 60.0 + ss

    enc = pc.dictionary_encode(pc.list_element(parts, 1))
    src_dict = enc.dictionary.to_pylist()
    src_code = enc.indices.to_numpy()
    sys_code = src_dict.index("SYSTEM") if "SYSTEM" in src_dict else -1
    is_sys = src_code == sys_code
    msg = np.full(len(lines), None, dtype=object)
    if is_sys.any():
        idx = np.flatnonzero(is_sys)
        msg[idx] = pc.list_element(pc.take(parts, pa.array(idx)), 2).to_pylist()
    return Batch(offset, lines, parts, t_raw, secs, src_dict, src_code, is_sys, msg)


def _signed8(v):
    return np.where(v > 127, v - 256, v)


def classify_frames(b: np.ndarray) -> np.ndarray:
    """8 바이트 CAN 프레임 (n, 8) 을 **내용으로** 가린다 → KIND_* 배열.

    로그에 찍힌 ID 는 확장 ID 프레임에서 틀린다 (직전 프레임의 ID 가 찍힌다).
        모듈 → BMS   [모듈 번호, 최저, 최고, 평균, 켜진 수, 가장 뜨거운 ID, 가장 차가운 ID, 체크섬]
        개별 방송    [ID (2 바이트), 값, 모듈 안 ID(0x80 = 고장), 최저, 최고, 가장 뜨거운 ID, 가장 차가운 ID]  체크섬 없음
    개별 방송은 체크섬이 없어 BMS 의 8 비트 합을 우연히 통과하기도 한다 (셀 전압 0.0x V 로 읽히던 3,082 행).
    진짜 BMS 프레임은 첫 바이트가 셀 전압의 상위 바이트라 0 일 수 없으므로, 첫 바이트 0 을 먼저 본다.
    """
    s7 = b[:, :7].sum(axis=1)
    module = (((s7 + THERMISTOR_CHECK) & 0xFF) == b[:, 7]) & ((b[:, 4] & 0x7F) == THERMISTORS_ENABLED) \
        & (_signed8(b[:, 1]) <= _signed8(b[:, 3])) & (_signed8(b[:, 3]) <= _signed8(b[:, 2]))
    claim = (b == np.array(THERMISTOR_CLAIM)).all(axis=1)
    single = ~module & ~claim & (b[:, 0] == 0) & (b[:, 1] == (b[:, 3] & 0x7F)) \
        & (_signed8(b[:, 4]) <= _signed8(b[:, 2])) & (_signed8(b[:, 2]) <= _signed8(b[:, 5]))
    bms = ~module & ~single & ~claim & ((s7 & 0xFF) == b[:, 7])
    return np.select([bms, module, single, claim], [KIND_BMS, KIND_MODULE, KIND_SINGLE, KIND_CLAIM], KIND_OTHER)


def clock_mode(pps_share: float | None) -> str:
    """gps_pps = GPS 가 매초 시계를 맞춤 · free = 혼자 돎 (느리게 감) · mixed = 레코딩 도중에 바뀜."""
    if pps_share is None:
        return "unknown"
    return "gps_pps" if pps_share > 0.9 else "free" if pps_share < 0.1 else "mixed"


def sync_spans_from_gaps(start, end) -> tuple[np.ndarray, np.ndarray]:
    """기록 공백(시작·끝 시각)의 끝에서 4.2 ~ 5.4 s 뒤에 시작하는 다른 공백까지 → (구간 시작 시각, 구간 길이).

    SD 동기화가 아닌 공백도 섞여 있으므로 가능한 짝을 전부 낸다. 진짜 주기는 한 값에 몰린다.
    """
    start, end = np.asarray(start, float), np.asarray(end, float)
    none = (np.array([]), np.array([]))
    if not len(start):
        return none
    a = np.searchsorted(start, end + SYNC_SPAN_RANGE_S[0])
    n = np.searchsorted(start, end + SYNC_SPAN_RANGE_S[1]) - a
    if not n.sum():
        return none
    owner = np.repeat(np.arange(len(start)), n)
    nxt = np.arange(n.sum()) - np.repeat(np.cumsum(n) - n, n) + np.repeat(a, n)
    return end[owner], start[nxt] - end[owner]


def sync_period_from_spans(spans) -> dict:
    """SD 동기화 주기로 'RTC 가 MCU 틱보다 얼마나 느린가' 를 잰다. **RTC 가 혼자 돈 레코딩에만 뜻이 있다.**

    틱 5 초가 로그에 4.57 s 로 찍혀 있으면 rtc_per_tick = 0.914 다. 참 시간 대비 속도는 여기에 TICK_RATE 를 곱한다.
    """
    none = {"sync_spans": 0, "sync_span_s": None, "rtc_per_tick": None}
    spans = np.asarray(spans, float)
    if len(spans) < SYNC_MIN_SPANS:
        return none
    width = 0.004                                                  # 타임스탬프 해상도 (1/256 s)
    edges = np.arange(SYNC_SPAN_RANGE_S[0], SYNC_SPAN_RANGE_S[1] + width, width)
    hist = np.convolve(np.histogram(spans, edges)[0], np.ones(5), "same")
    peak = edges[int(np.argmax(hist))] + width / 2
    near = spans[np.abs(spans - peak) <= SYNC_PEAK_HALF_S]
    if len(near) < SYNC_MIN_SPANS or len(near) < 0.5 * len(spans):  # 한 값에 몰리지 않으면 주기를 못 찾은 것이다
        return none
    mean = float(near.mean())
    return {"sync_spans": int(len(near)), "sync_span_s": round(mean, 4), "rtc_per_tick": round(mean / TICK_SYNC_S, 5)}


def session_of(path: Path) -> tuple[str, dt.date]:
    m = SESSION_RE.match(path.stem)
    if not m:
        raise ValueError(f"세션 이름을 알 수 없음: {path.name}")
    session = path.stem.removesuffix("_merged")
    return session, dt.date.fromisoformat(m.group(1))


# ---------------------------------------------------------------- 1차: 시간 계획

@dataclass
class Anchor:
    line: int
    kind: str                 # boot | sync | jump
    t_raw: str
    secs: float
    prev_secs: float | None
    trusted: bool
    reason: str


@dataclass
class Recording:
    index: int
    boot_line: int | None
    boot_t_raw: str | None
    first_line: int | None = None
    last_line: int | None = None
    first_secs: float | None = None
    last_secs: float | None = None
    rows: int = 0
    anchors: list = field(default_factory=list)
    sync_events: list = field(default_factory=list)
    fail_events: list = field(default_factory=list)
    gaps: list = field(default_factory=list)
    gap_count: int = 0
    backsteps: int = 0
    # 시간 계획 결과 (build_offsets)
    seg_start: np.ndarray | None = None
    seg_rel_off: np.ndarray | None = None
    seg_abs_off: np.ndarray | None = None
    seg_quality: list | None = None
    seg_abs_base: np.ndarray | None = None      # 그 구간의 기준점이 찍은 시각 (하루 안의 초)
    seg_abs_trel: np.ndarray | None = None      # 그 기준점의 로거 경과시간
    # 시계 (v2)
    rel_off: float = 0.0                        # 스캔 중: 지금 구간의 raw 초 → 로거 경과시간 오프셋
    last_trel: float | None = None
    late_rows: int = 0
    gap_start: list = field(default_factory=list)
    gap_end: list = field(default_factory=list)
    clock: dict = field(default_factory=dict)   # mode, f, f_basis, pps_share, sync_spans, sync_span_s, rtc_per_tick


def trust(t_raw: str, via_sync: bool) -> tuple[bool, str]:
    if int(t_raw[:2]) >= 24:
        return False, "hour>=24"
    if via_sync and t_raw[:7] == "09:00:0" and t_raw[7] in "012":
        return False, "gps_default_09:00:0x"
    return True, "ok"


def scan_time_plan(path: Path) -> list[Recording]:
    recs: list[Recording] = []
    cur: Recording | None = None
    prev_secs = None
    pending_sync = False

    for offset, lines in iter_lines(path):
        b = parse_batch(offset, lines)
        ln = b.line_no
        # 이벤트는 수가 적으므로 파이썬 루프, 데이터는 이벤트 사이 구간을 numpy 로 처리
        ev_idx = np.flatnonzero(b.is_sys)
        bounds = list(ev_idx) + [len(ln)]
        start = 0
        for e in bounds:
            if e > start:                                   # 데이터 구간 [start, e)
                if cur is None:
                    cur = Recording(index=len(recs), boot_line=None, boot_t_raw=None)
                    recs.append(cur)
                s = b.secs[start:e]
                lines_seg = ln[start:e]
                prevs = np.concatenate([[np.nan if prev_secs is None else prev_secs], s[:-1]])
                d = s - prevs
                jump = (d < JUMP_BACK_S) | (d > JUMP_FWD_S)
                first = cur.first_line is None
                if cur.first_line is None:
                    cur.first_line = int(lines_seg[0])
                    cur.first_secs = float(s[0])
                    via_sync = pending_sync
                    t0 = b.t_raw[start].as_py()
                    ok, why = trust(t0, via_sync) if via_sync else (False, "boot_frozen_rtc")
                    cur.anchors.append(Anchor(int(lines_seg[0]), "sync" if via_sync else "boot",
                                              t0, float(s[0]), None, ok, why))
                    jump[0] = False
                    pending_sync = False
                elif pending_sync:
                    jump[0] = True                          # 동기화 이벤트 직후 첫 행은 무조건 기준점
                for j in np.flatnonzero(jump):
                    via = pending_sync and j == 0
                    t = b.t_raw[start + int(j)].as_py()
                    ok, why = trust(t, via)
                    cur.anchors.append(Anchor(int(lines_seg[j]), "sync" if via else "jump",
                                              t, float(s[j]), float(prevs[j]), ok, why))
                pending_sync = False
                fwd_gap = (~jump) & (d > GAP_REPORT_S)
                cur.gap_count += int(fwd_gap.sum())
                for j in np.flatnonzero(fwd_gap)[: max(0, 50 - len(cur.gaps))]:
                    cur.gaps.append({"line": int(lines_seg[j]), "t_raw": b.t_raw[start + int(j)].as_py(),
                                     "gap_s": round(float(d[j]), 3)})
                cur.backsteps += int(((~jump) & (d < 0)).sum())
                # 시계 흔적: 로거 경과시간(점프를 0 초로 접은 것)에서의 기록 공백, 그리고 밀리초 ≥ 960 인 줄 수
                if first:
                    cur.rel_off = -float(s[0])
                step = np.where(jump, np.nan_to_num(prevs - s), 0.0)
                trel = s + cur.rel_off + np.cumsum(step)
                cur.rel_off += float(step.sum())
                dt_log = np.diff(np.concatenate([[trel[0] if cur.last_trel is None else cur.last_trel], trel]))
                g = np.flatnonzero(dt_log >= SYNC_GAP_MIN_S)
                cur.gap_end += trel[g].tolist()
                cur.gap_start += (trel[g] - dt_log[g]).tolist()
                cur.last_trel = float(trel[-1])
                cur.late_rows += int((np.round((s - np.floor(s)) * 1000) >= LATE_MS).sum())
                cur.rows += e - start
                cur.last_line = int(lines_seg[-1])
                cur.last_secs = float(s[-1])
                prev_secs = float(s[-1])
            if e < len(ln):                                 # 이벤트 행
                msg = (b.msg[e] or "").strip()
                t = b.t_raw[e].as_py()
                if msg.startswith("CONFIG ON"):
                    cur = Recording(index=len(recs), boot_line=int(ln[e]), boot_t_raw=t)
                    recs.append(cur)
                    prev_secs = None
                    pending_sync = False
                elif msg.startswith("RTC-UTC SYNC"):
                    if cur is None:
                        cur = Recording(index=len(recs), boot_line=None, boot_t_raw=None)
                        recs.append(cur)
                    cur.sync_events.append({"line": int(ln[e]), "t_raw": t})
                    pending_sync = True
                elif msg.startswith("CONFIG FAIL") and cur is not None:
                    cur.fail_events.append({"line": int(ln[e]), "t_raw": t})
            start = e + 1

    for r in recs:
        build_offsets(r)
        r.clock = measure_clock(r)
    return recs


def measure_clock(r: Recording) -> dict:
    """레코딩의 시계 상태와 속도를 로그만으로 정한다 (모듈 설명의 '시계 모델 v2')."""
    if not r.rows:
        return {"mode": "unknown", "f": 1.0, "f_basis": "none", "pps_share": None,
                "sync_spans": 0, "sync_span_s": None, "rtc_per_tick": None}
    pps_share = float(np.clip(1 - (r.late_rows / r.rows) / FREE_LATE_SHARE, 0, 1))
    mode = clock_mode(pps_share)
    period = sync_period_from_spans(sync_spans_from_gaps(r.gap_start, r.gap_end)[1]) if mode == "free" \
        else sync_period_from_spans([])
    if mode == "gps_pps":
        f, basis = 1.0, "gps_pps"
    elif mode == "free" and period["rtc_per_tick"]:
        f, basis = round(period["rtc_per_tick"] * TICK_RATE, 5), "sd_sync"
    elif mode == "free":
        f, basis = F_FREE, "assumed"
    else:
        f, basis = 1.0, "none"                  # 도중에 바뀐 레코딩: 속도 하나로 말할 수 없어 그대로 둔다
    return {"mode": mode, "f": f, "f_basis": basis, "pps_share": round(pps_share, 3), **period}


def build_offsets(r: Recording) -> None:
    """기준점 사이 구간마다 raw 초 → 상대/절대 초 변환 오프셋을 정한다."""
    if not r.anchors:
        r.seg_start = np.array([], dtype=np.int64)
        r.seg_abs_base = r.seg_abs_trel = np.array([])
        return
    a = r.anchors
    n = len(a)
    rel = np.zeros(n)
    rel[0] = -a[0].secs
    for k in range(1, n):
        rel[k] = rel[k - 1] + (a[k].prev_secs - a[k].secs)      # 점프를 0 초로 접는다
    trusted = [k for k in range(n) if a[k].trusted]
    abs_off = np.full(n, np.nan)
    abs_base, abs_trel = np.full(n, np.nan), np.full(n, np.nan)
    quality = ["none"] * n
    for k in range(n):
        before = [j for j in trusted if j <= k]
        j = before[-1] if before else (trusted[0] if trusted else None)
        if j is None:
            continue
        abs_off[k] = rel[k] - rel[j]
        abs_base[k], abs_trel[k] = a[j].secs, a[j].secs + rel[j]
        quality[k] = "synced" if j == k else "continuity"
    r.seg_start = np.array([x.line for x in a], dtype=np.int64)
    r.seg_rel_off = rel
    r.seg_abs_off = abs_off
    r.seg_abs_base, r.seg_abs_trel = abs_base, abs_trel
    r.seg_quality = quality


# ---------------------------------------------------------------- 2차: 변환

SCHEMAS = {
    "frames": pa.schema([
        ("row_no", pa.int64()), ("t_rel_ms", pa.int64()), ("t_abs", T_ABS_TYPE),
        ("t_quality", pa.string()), ("t_raw", pa.string()), ("t_log_ms", pa.int64()), ("kind", pa.string()),
        ("can_id", pa.int64()), ("payload", pa.binary()), ("valid", pa.bool_()), ("dup", pa.bool_()),
    ]),
}
_BASE = [("t_rel_ms", pa.int64()), ("t_abs", T_ABS_TYPE), ("t_quality", pa.string())]
SCHEMAS["bms"] = pa.schema(_BASE + [("valid", pa.bool_()), ("cell_max_v", pa.float64()),
                                    ("cell_min_v", pa.float64()), ("temp_c", pa.int16()),
                                    ("soc_pct", pa.float64())])
SCHEMAS["motor"] = pa.schema(_BASE + [("signal", pa.string()), ("value", pa.float64()), ("raw", pa.int64())])
# 이름은 펌웨어가 정의한 물리적 의미를 따른다 (센서 칩 축 이름이 아니다).
SCHEMAS["imu"] = pa.schema(_BASE + [(c, pa.float64()) for c in
                                    ("ax_lat_g", "ay_long_g", "az_g",
                                     "pitch_dps", "roll_dps", "yaw_dps")])
SCHEMAS["mag"] = pa.schema(_BASE + [(c, pa.float64()) for c in ("mx", "my", "mz")])
SCHEMAS["lin"] = pa.schema(_BASE + [(c, pa.float64()) for c in ("lin_rl_mm", "lin_rr_mm")])
SCHEMAS["ped"] = pa.schema(_BASE + [("ped", pa.float64())])
SCHEMAS["gps"] = pa.schema(_BASE + [("lat", pa.float64()), ("lon", pa.float64()),
                                    ("course_deg", pa.float64())])
SCHEMAS["events"] = pa.schema(_BASE + [("message", pa.string())])
# 배터리 써미스터 모듈. source: module_to_bms (최저·최고·평균) · single (써미스터 하나) · legacy_lowest / legacy_highest (0x80)
THERMISTOR_INT = ("thermistor_id", "temp_c", "lowest_c", "highest_c", "average_c", "hottest_id", "coldest_id")
SCHEMAS["thermistor"] = pa.schema(_BASE + [("source", pa.string())] + [(c, pa.int16()) for c in THERMISTOR_INT]
                                  + [("fault", pa.bool_())])
SIGNALS = {"IMU": ("imu", 6), "MAG": ("mag", 3), "LIN": ("lin", 2), "PED": ("ped", 1), "GPS": ("gps", 3)}


class RecWriter:
    def __init__(self, out: Path):
        self.out = out
        self.w: dict[str, pq.ParquetWriter] = {}

    def write(self, name: str, cols: dict) -> None:
        schema = SCHEMAS[name]
        tbl = pa.table({f.name: cols[f.name] for f in schema}, schema=schema)
        if tbl.num_rows == 0:
            return
        if name not in self.w:
            self.w[name] = pq.ParquetWriter(self.out / f"{name}.parquet", schema, compression="zstd")
        self.w[name].write_table(tbl)

    def close(self) -> None:
        for name, schema in SCHEMAS.items():
            if name in self.w:
                self.w[name].close()
            else:
                pq.write_table(schema.empty_table(), self.out / f"{name}.parquet", compression="zstd")


def _floats(parts: pa.Array, idx: np.ndarray, first: int, count: int) -> list[np.ndarray]:
    sub = pc.take(parts, pa.array(idx))
    return [pc.cast(pc.list_element(sub, first + i), pa.float64()).to_numpy() for i in range(count)]


def _can_bytes(parts: pa.Array, idx: np.ndarray) -> tuple[pa.Array, np.ndarray]:
    """CAN 행의 값들을 바이트 배열로. (BinaryArray, 행별 길이)"""
    sub = pc.take(parts, pa.array(idx))
    vals = pc.list_slice(sub, 2)
    flat = pc.list_flatten(vals)
    keep = pc.not_equal(flat, "")
    parent = pc.list_parent_indices(vals).to_numpy()
    keep_np = keep.to_numpy(zero_copy_only=False)
    data = pc.cast(pc.filter(flat, keep), pa.uint8()).to_numpy()
    lens = np.bincount(parent[keep_np], minlength=len(idx)).astype(np.int32)
    offsets = np.concatenate([[0], np.cumsum(lens)]).astype(np.int32)
    arr = pa.BinaryArray.from_buffers(pa.binary(), len(idx),
                                      [None, pa.py_buffer(offsets), pa.py_buffer(data.astype(np.uint8))])
    return arr, lens


def _gather_bytes(arr: pa.Array, width: int) -> np.ndarray:
    """가변 길이 BinaryArray 의 각 원소 앞 width 바이트 → (n, width) 배열. 원소는 width 이상이어야 한다."""
    offs = np.frombuffer(arr.buffers()[1], dtype=np.int32)[arr.offset: arr.offset + len(arr) + 1]
    flat = np.frombuffer(arr.buffers()[2], dtype=np.uint8)
    return flat[offs[:-1, None] + np.arange(width)[None, :]].astype(np.int64)


def decode_motor(can_bin: pa.Array, can_len: np.ndarray, pos: np.ndarray):
    """0x181 프레임들 → (해독된 행의 pos 내 인덱스, 신호 코드(MOTOR_REGS 순번), 물리값, raw)."""
    if not len(pos):
        return np.array([], dtype=np.int64), [], np.array([]), np.array([], dtype=np.int64)
    ln = can_len[pos]
    usable = np.flatnonzero(ln >= 4)
    b = _gather_bytes(pc.take(can_bin, pa.array(pos[usable])), 4)
    reg = b[:, 0]
    keep, names, values, raws = [], [], [], []
    for code, (rid, (name, nbytes, scale)) in enumerate(MOTOR_REGS.items()):
        m = (reg == rid) & (ln[usable] == nbytes + 2)
        if not m.any():
            continue
        idx = usable[m]
        if nbytes == 2:
            raw = b[m, 1] | (b[m, 2] << 8)
            raw = np.where(raw > 32767, raw - 65536, raw)
        else:
            b6 = _gather_bytes(pc.take(can_bin, pa.array(pos[idx])), 5)
            raw = b6[:, 1] | (b6[:, 2] << 8) | (b6[:, 3] << 16) | (b6[:, 4] << 24)
        val = igbt_temp_c(raw) if scale == "igbt" else raw * scale
        keep.append(idx); raws.append(raw); values.append(val); names.append(np.full(len(idx), code))
    if not keep:
        return np.array([], dtype=np.int64), [], np.array([]), np.array([], dtype=np.int64)
    keep, values, raws, names = np.concatenate(keep), np.concatenate(values), np.concatenate(raws), np.concatenate(names)
    order = np.argsort(keep, kind="stable")
    return keep[order], names[order], values[order], raws[order]


def convert_file(path: Path, out_root: Path) -> list[dict]:
    t_start = time.perf_counter()
    session, day = session_of(path)
    day_epoch_ms = int(dt.datetime(day.year, day.month, day.day, tzinfo=KST).timestamp() * 1000)

    print(f"  [{session}] 1차 스캔 (시간 계획)...", flush=True)
    recs = scan_time_plan(path)
    t_scan = time.perf_counter() - t_start
    print(f"  [{session}] 레코딩 {len(recs)}개, 스캔 {t_scan:.0f}s → 2차 변환", flush=True)

    rec_start = np.array([r.boot_line if r.boot_line is not None else (r.first_line or 0) for r in recs],
                         dtype=np.int64)
    seg_line = np.concatenate([r.seg_start for r in recs])
    seg_rel = np.concatenate([r.seg_rel_off for r in recs if r.anchors] or [np.array([])])
    seg_abs = np.concatenate([r.seg_abs_off for r in recs if r.anchors] or [np.array([])])
    seg_qc = np.array([QUALITY.index(q) for r in recs if r.anchors for q in r.seg_quality], dtype=np.int8)
    seg_base = np.concatenate([r.seg_abs_base for r in recs if r.anchors] or [np.array([])])
    seg_trel = np.concatenate([r.seg_abs_trel for r in recs if r.anchors] or [np.array([])])
    rec_f = np.array([r.clock["f"] for r in recs])                  # 레코딩별 시계 속도 (v2)

    ids = [f"{session}_r{r.index:02d}" for r in recs]
    outs = [fresh_dir(out_root / i) for i in ids]
    writers = [RecWriter(o) for o in outs]
    stats = [{"frames": 0, "dup": 0, "by_source": {}, "bms_rows": 0, "bms_rows_nodup": 0, "motor_rows_nodup": 0, "motor_decoded": 0,
              "bms_invalid": 0, "bms_invalid_nodup": 0, "t_abs_null": 0,
              "kinds": {}, "thermistor_rows": 0, "legacy_bad": 0,
              "t_abs_min": None, "t_abs_max": None} for _ in recs]

    q_names = pa.array(QUALITY)
    prev_line_text = None
    for offset, lines in iter_lines(path):
        b = parse_batch(offset, lines)
        n = len(lines)
        ln = b.line_no

        # 중복: 바로 윗줄과 완전히 같은 줄
        eq = pc.equal(lines.slice(1), lines.slice(0, n - 1)).to_numpy(zero_copy_only=False)
        dup = np.concatenate([[prev_line_text is not None and lines[0].as_py() == prev_line_text], eq])
        prev_line_text = lines[n - 1].as_py()

        # 소스 사전 -> CAN 여부 / CAN ID / 표시 이름
        d_is_can = np.array([x not in NON_CAN and all(c in HEX for c in x) for x in b.src_dict], dtype=bool)
        d_can_id = np.array([int(x, 16) if c else 0 for x, c in zip(b.src_dict, d_is_can)], dtype=np.int64)
        d_kind = ["CAN" if c else x for x, c in zip(b.src_dict, d_is_can)]
        d_label = [f"0x{i:X}" if c else x for x, i, c in zip(b.src_dict, d_can_id, d_is_can)]
        can_mask = d_is_can[b.src_code]
        can_id = d_can_id[b.src_code]
        kind = pc.take(pa.array(d_kind, pa.string()), pa.array(b.src_code))

        rec_of = np.maximum(np.searchsorted(rec_start, ln, side="right") - 1, 0)

        # 시간 (모듈 설명의 시계 모델 v2): 구간 오프셋 적용. 이벤트 행은 같은 레코딩의 가장 가까운 다음 데이터 행 시각,
        # 다음 행이 없거나 다른 레코딩이면 이전 데이터 행 시각을 쓴다 (예: 레코딩 끝의 CONFIG FAIL)
        seg = np.searchsorted(seg_line, ln, side="right") - 1
        secs = b.secs.copy()
        if b.is_sys.any():
            idx = np.arange(n)
            nxt = np.minimum.accumulate(np.where(~b.is_sys, idx, n)[::-1])[::-1]
            prv = np.maximum.accumulate(np.where(~b.is_sys, idx, -1))
            nxt_same = (nxt < n) & (rec_of[np.minimum(nxt, n - 1)] == rec_of)
            src_row = np.where(nxt_same, nxt, np.where(prv >= 0, prv, np.minimum(nxt, n - 1)))
            si = np.flatnonzero(b.is_sys)
            secs[si] = b.secs[src_row[si]]
            seg[si] = seg[src_row[si]]
        has_seg = seg >= 0
        segc = np.maximum(seg, 0)
        rel_off = np.where(has_seg, seg_rel[segc], np.nan) if len(seg_rel) else np.full(n, np.nan)
        base = np.where(has_seg, seg_base[segc], np.nan) if len(seg_base) else np.full(n, np.nan)
        base_trel = np.where(has_seg, seg_trel[segc], np.nan) if len(seg_trel) else np.full(n, np.nan)
        f = rec_f[rec_of]
        t_log = np.nan_to_num(secs + rel_off)                       # 로거가 찍은 대로의 경과시간
        t_log_ms = np.round(t_log * 1000).astype(np.int64)
        t_rel_ms = np.round(t_log / f * 1000).astype(np.int64)      # 시계 속도를 바로잡은 경과시간
        abs_f = day_epoch_ms + (base + (secs + rel_off - base_trel) / f) * 1000
        abs_null = np.isnan(abs_f)
        t_abs_ms = np.round(np.nan_to_num(abs_f)).astype(np.int64)
        q_code = np.where(has_seg, seg_qc[segc] if len(seg_qc) else 0, 0).astype(np.int8)
        quality = pc.take(q_names, pa.array(q_code))

        # payload: CAN = 데이터 바이트, 그 외 = 값 텍스트
        can_idx = np.flatnonzero(can_mask)
        other_idx = np.flatnonzero(~can_mask)
        if len(can_idx):
            can_bin, can_len = _can_bytes(b.parts, can_idx)
        else:
            can_bin, can_len = pa.array([], pa.binary()), np.array([], dtype=np.int32)
        if len(other_idx):
            rest = pc.replace_substring_regex(pc.take(lines, pa.array(other_idx)),
                                              pattern=r"^[^,]*,[^,]*,?", replacement="")
            other_bin = pc.cast(rest, pa.binary())
        else:
            other_bin = pa.array([], pa.binary())
        order = np.empty(n, dtype=np.int64)
        order[can_idx] = np.arange(len(can_idx))
        order[other_idx] = len(can_idx) + np.arange(len(other_idx))
        payload = pc.take(pa.concat_arrays([can_bin, other_bin]), pa.array(order))

        # BMS(0x6B1) 체크섬: valid_code -1 = 판정 불가(null), 0 = 실패, 1 = 통과
        valid_code = np.full(n, -1, dtype=np.int8)
        bms_rows = np.flatnonzero(can_id == BMS_ID)
        bms_bytes = np.zeros((len(bms_rows), 8), dtype=np.int64)
        if len(bms_rows):
            pos = order[bms_rows]
            ok_len = can_len[pos] == 8
            good = np.flatnonzero(ok_len)
            if len(good):
                sub = pc.take(can_bin, pa.array(pos[good]))
                bms_bytes[good] = _gather_bytes(sub, 8)
            ck = ok_len & ((bms_bytes[:, :7].sum(axis=1) & 0xFF) == bms_bytes[:, 7])
            valid_code[bms_rows] = ck.astype(np.int8)

        # 내용으로 가리기: 길이 8 인 CAN 프레임 전부 (찍힌 ID 와 무관), 그리고 0x80 의 4 바이트 프레임
        ckind = np.full(n, -1, dtype=np.int8)
        bytes8 = np.zeros((n, 8), dtype=np.int64)
        i8 = np.flatnonzero(can_len == 8)
        if len(i8):
            rows8 = can_idx[i8]
            bytes8[rows8] = _gather_bytes(pc.take(can_bin, pa.array(i8)), 8)
            ckind[rows8] = classify_frames(bytes8[rows8])
        is_tem = (ckind == KIND_MODULE) | (ckind == KIND_SINGLE) | (ckind == KIND_CLAIM)
        i4 = np.flatnonzero((can_len == 4) & (can_id[can_idx] == THERMISTOR_LEGACY_ID))
        legacy_rows = can_idx[i4]
        legacy = _gather_bytes(pc.take(can_bin, pa.array(i4)), 4) if len(i4) else np.zeros((0, 4), dtype=np.int64)
        legacy_ok = ((legacy[:, :3].sum(axis=1) + THERMISTOR_LEGACY_ID + 4) & 0xFF) == legacy[:, 3]

        # 레코딩별로 잘라 쓰기
        cut = np.flatnonzero(np.diff(rec_of)) + 1
        for lo, hi in zip(np.concatenate([[0], cut]), np.concatenate([cut, [n]])):
            lo, hi = int(lo), int(hi)
            ri = int(rec_of[lo])
            W, st = writers[ri], stats[ri]
            sl = slice(lo, hi)
            m = hi - lo
            null_mask = abs_null[sl]
            vc = valid_code[sl]
            W.write("frames", {
                "row_no": pa.array(ln[sl]), "t_rel_ms": pa.array(t_rel_ms[sl]),
                "t_abs": pa.array(t_abs_ms[sl], mask=null_mask).cast(T_ABS_TYPE),
                "t_quality": quality.slice(lo, m), "t_raw": b.t_raw.slice(lo, m),
                "t_log_ms": pa.array(t_log_ms[sl]),
                "kind": kind.slice(lo, m),
                "can_id": pa.array(can_id[sl], mask=~can_mask[sl]),
                "payload": payload.slice(lo, m),
                "valid": pa.array(vc == 1, mask=vc < 0),
                "dup": pa.array(dup[sl]),
            })
            st["frames"] += m
            st["dup"] += int(dup[sl].sum())
            st["t_abs_null"] += int(null_mask.sum())
            if (~null_mask).any():
                av = t_abs_ms[sl][~null_mask]
                st["t_abs_min"] = int(av.min()) if st["t_abs_min"] is None else min(st["t_abs_min"], int(av.min()))
                st["t_abs_max"] = int(av.max()) if st["t_abs_max"] is None else max(st["t_abs_max"], int(av.max()))
            for code, cnt in enumerate(np.bincount(b.src_code[sl], minlength=len(b.src_dict))):
                if cnt:
                    lab = d_label[code]
                    st["by_source"][lab] = st["by_source"].get(lab, 0) + int(cnt)

            def base(rows):
                return {"t_rel_ms": pa.array(t_rel_ms[rows]),
                        "t_abs": pa.array(t_abs_ms[rows], mask=abs_null[rows]).cast(T_ABS_TYPE),
                        "t_quality": pc.take(quality, pa.array(rows))}

            # BMS (신호 테이블은 중복 제외). 0x6B1 로 찍혔어도 내용이 써미스터 모듈 프레임이면 BMS 표에 넣지 않는다
            in_rec = (bms_rows >= lo) & (bms_rows < hi)
            if in_rec.any():
                rows_all = bms_rows[in_rec]
                v_all = valid_code[rows_all] == 1
                st["bms_rows"] += len(rows_all)
                st["bms_invalid"] += int((~v_all).sum())
                nd = ~dup[rows_all]
                st["bms_rows_nodup"] += int(nd.sum())
                st["bms_invalid_nodup"] += int((~v_all[nd]).sum())
                nd &= ~is_tem[rows_all]
                rows, bb, v = rows_all[nd], bms_bytes[in_rec][nd], (v_all & (ckind[rows_all] == KIND_BMS))[nd]
                W.write("bms", {**base(rows), "valid": pa.array(v),
                                "cell_max_v": pa.array((bb[:, 0] << 8 | bb[:, 1]) / 10000),
                                "cell_min_v": pa.array((bb[:, 4] << 8 | bb[:, 5]) / 10000),
                                "temp_c": pa.array(bb[:, 2].astype(np.int16)),
                                "soc_pct": pa.array(bb[:, 3] * 0.5)})

            # 배터리 써미스터 모듈 (중복 제외). 찍힌 ID 가 무엇이든 내용으로 가려낸 것
            keep_sl = ~dup[sl]
            for code, name in enumerate(KIND_NAMES):
                cnt = int(((ckind[sl] == code) & keep_sl).sum())
                if cnt:
                    st["kinds"][name] = st["kinds"].get(name, 0) + cnt
            # 세 가지 메시지를 한 표에 시간순(원본 줄 순서)으로 넣는다. 없는 칸은 null
            parts = []                                             # (줄 번호, source, {열: 값}, fault)
            rows = np.flatnonzero((ckind[sl] == KIND_MODULE) & keep_sl) + lo
            if len(rows):
                x = bytes8[rows]
                parts.append((rows, "module_to_bms", {"lowest_c": _signed8(x[:, 1]), "highest_c": _signed8(x[:, 2]),
                                                       "average_c": _signed8(x[:, 3]), "hottest_id": x[:, 5],
                                                       "coldest_id": x[:, 6]}, (x[:, 4] & 0x80) > 0))
            rows = np.flatnonzero((ckind[sl] == KIND_SINGLE) & keep_sl) + lo
            if len(rows):
                x = bytes8[rows]
                parts.append((rows, "single", {"thermistor_id": x[:, 1], "temp_c": _signed8(x[:, 2]),
                                                "lowest_c": _signed8(x[:, 4]), "highest_c": _signed8(x[:, 5]),
                                                "hottest_id": x[:, 6], "coldest_id": x[:, 7]}, (x[:, 3] & 0x80) > 0))
            in_leg = (legacy_rows >= lo) & (legacy_rows < hi) & ~dup[legacy_rows]
            st["legacy_bad"] += int((in_leg & ~legacy_ok).sum())
            for which, src, col in ((5, "legacy_lowest", "lowest_c"), (6, "legacy_highest", "highest_c")):
                pick = in_leg & legacy_ok & (legacy[:, 0] == which)
                if pick.any():
                    parts.append((legacy_rows[pick], src, {col: _signed8(legacy[pick][:, 1])}, None))
            if parts:
                rows = np.concatenate([q[0] for q in parts])
                order_t = np.argsort(rows, kind="stable")
                rows = rows[order_t]
                cols = {}
                for c in THERMISTOR_INT:
                    val = np.concatenate([q[2].get(c, np.zeros(len(q[0]), np.int64)) for q in parts])[order_t]
                    have = np.concatenate([np.full(len(q[0]), c in q[2]) for q in parts])[order_t]
                    cols[c] = pa.array(val, pa.int16(), mask=~have)
                fault = np.concatenate([q[3] if q[3] is not None else np.zeros(len(q[0]), bool) for q in parts])[order_t]
                no_fault = np.concatenate([np.full(len(q[0]), q[3] is None) for q in parts])[order_t]
                source = np.concatenate([np.full(len(q[0]), q[1], dtype=object) for q in parts])[order_t]
                W.write("thermistor", {**base(rows), "source": pa.array(source, pa.string()), **cols,
                                       "fault": pa.array(fault, mask=no_fault)})
                st["thermistor_rows"] += len(rows)

            # 모터 컨트롤러 0x181 (중복 제외). 레지스터마다 따로 오므로 긴 형식(signal, value)으로 둔다
            mrows = np.flatnonzero((can_id[sl] == MOTOR_ID) & ~dup[sl]) + lo
            if len(mrows):
                k, names, values, raws = decode_motor(can_bin, can_len, order[mrows])
                st["motor_rows_nodup"] += len(mrows)
                st["motor_decoded"] += len(k)
                if len(k):
                    W.write("motor", {**base(mrows[k]), "signal": pc.take(MOTOR_SIGNAL_NAMES, pa.array(names)),
                                      "value": pa.array(values), "raw": pa.array(raws)})

            # 온보드 센서 (중복 제외)
            keep = np.arange(lo, hi)[~dup[sl]]
            for src_name, (tbl, cnt) in SIGNALS.items():
                c = b.code(src_name)
                if c < 0:
                    continue
                rows = keep[b.src_code[keep] == c]
                if not len(rows):
                    continue
                cols = [f.name for f in SCHEMAS[tbl]][3:]
                W.write(tbl, {**base(rows), **dict(zip(cols, _floats(b.parts, rows, 2, cnt)))})

            rows = np.flatnonzero(b.is_sys[sl]) + lo
            if len(rows):
                W.write("events", {**base(rows),
                                   "message": pa.array([(b.msg[i] or "").strip() for i in rows], pa.string())})

    for w in writers:
        w.close()

    # meta.json + 인덱스
    index_rows = []
    total_sec = time.perf_counter() - t_start
    for r, rid, out, st in zip(recs, ids, outs, stats):
        fmt = (lambda ms: None if ms is None else
               dt.datetime.fromtimestamp(ms / 1000, KST).isoformat(timespec="milliseconds"))
        q = {}
        if r.anchors:
            for k in range(len(r.anchors)):
                q[r.seg_quality[k]] = q.get(r.seg_quality[k], 0) + 1
        trusted_any = any(a.trusted for a in r.anchors)
        meta = {
            "car": "LEF-26",
            "event": out_root.parent.name,
            "recording_id": rid,
            "source": {"logger": "LEF-26 STM32F446RE + MPU9250 (26팀 자작)",
                       "firmware": "github.com/GambitIsSalvation/FSK-2026-Datalogger",
                       "file": f"raw_merged/{path.name}",
                       "line_start": r.boot_line if r.boot_line is not None else r.first_line,
                       "line_end": r.last_line},
            "label": None,
            "time": {
                "tz": TZ,
                "tz_basis": ("KST 확정 — 동기화 기본값 09:00:0x (= GPS 00:00 UTC + 9h), "
                             "시각 범위, 팀원 확인이 모두 일치"),
                "abs_basis": ("레코딩 내 신뢰 기준점 기준, 시계 점프는 경과 0 초로 이어 붙임 (점프당 ±1 s)"
                              if trusted_any else "신뢰 가능한 기준점 없음 → t_abs 전부 null"),
                # 시계 모델 v2: t_rel_ms · t_abs 는 로거 경과시간을 f 로 나눈 값이다. 로거가 찍은 그대로는 frames.t_log_ms
                "clock": {**r.clock,
                          "note": {"gps_pps": "GPS 가 매초 시계를 맞춤 — 그대로 둠 (초 안에 0~70 ms 톱니가 남는다)",
                                   "free": "RTC 가 혼자 돎 — 경과시간을 f 로 나눠 바로잡음",
                                   "mixed": "도중에 바뀜 — 바로잡지 못함. 구간에 따라 최대 7 % 틀릴 수 있다",
                                   "unknown": "줄이 없어 판정 불가"}[r.clock["mode"]]},
                "t_abs_start": fmt(st["t_abs_min"]),
                "t_abs_end": fmt(st["t_abs_max"]),
                "boot_t_raw": r.boot_t_raw,
                "anchors": [{"line": a.line, "kind": a.kind, "t_raw": a.t_raw,
                             "jump_s": None if a.prev_secs is None else round(a.secs - a.prev_secs, 3),
                             "trusted": a.trusted, "reason": a.reason} for a in r.anchors],
                "sync_events": r.sync_events,
                "config_fail_events": r.fail_events,
                "segments_by_quality": q,
                "gaps_1_to_60s": r.gap_count,
                "gaps_sample": r.gaps,
                "backsteps_under_1s": r.backsteps,
            },
            "counts": {"frames": st["frames"], "dup": st["dup"], "by_source": st["by_source"],
                       "bms_rows": st["bms_rows"], "bms_rows_nodup": st["bms_rows_nodup"],
                       "motor_rows_nodup": st["motor_rows_nodup"], "motor_decoded": st["motor_decoded"],
                       # 길이 8 인 CAN 프레임을 내용으로 가린 결과 (중복 제외). 찍힌 ID 와 다를 수 있다
                       "can8_by_content": st["kinds"], "thermistor_rows": st["thermistor_rows"],
                       "thermistor_legacy_checksum_fail": st["legacy_bad"]},
            "quality": {
                "dup_rate": st["dup"] / st["frames"] if st["frames"] else 0.0,
                "t_abs_null_rate": st["t_abs_null"] / st["frames"] if st["frames"] else 0.0,
                "bms_checksum_fail_rate": st["bms_invalid"] / st["bms_rows"] if st["bms_rows"] else None,
                "bms_checksum_fail_rate_nodup": (st["bms_invalid_nodup"] / st["bms_rows_nodup"]
                                                 if st["bms_rows_nodup"] else None),
            },
            "perf": {"output_bytes": dir_bytes(out)},
            "converter_version": __version__,
            "created_at": dt.datetime.now(KST).isoformat(timespec="seconds"),
        }
        write_json(meta, out / "meta.json")
        index_rows.append({
            "recording_id": rid, "session": session, "t_abs_start": meta["time"]["t_abs_start"],
            "t_abs_end": meta["time"]["t_abs_end"], "time_trusted": trusted_any,
            "boot_t_raw": r.boot_t_raw, "frames": st["frames"], "dup": st["dup"],
            "bms_rows_nodup": st["bms_rows_nodup"],
            "bms_checksum_fail_rate": meta["quality"]["bms_checksum_fail_rate"],
            "anchors": len(r.anchors), "clock_mode": r.clock["mode"], "clock_f": r.clock["f"],
            "clock_f_basis": r.clock["f_basis"], "thermistor_rows": st["thermistor_rows"],
            "output_bytes": meta["perf"]["output_bytes"],
        })
    print(f"  [{session}] 완료 {total_sec:.0f}s", flush=True)
    return index_rows


def convert_event(event_dir: Path, sessions: list[str] | None = None) -> list[dict]:
    """raw_merged/*_merged.csv 변환. sessions 를 주면 그 세션만 (인덱스는 전체 변환 때만 갱신)."""
    out_root = event_dir / "normalized"
    out_root.mkdir(exist_ok=True)
    rows = []
    for path in sorted((event_dir / "raw_merged").glob("*_merged.csv")):
        if sessions and path.stem.removesuffix("_merged") not in sessions:
            continue
        rows += convert_file(path, out_root)
    if not sessions:
        write_index(rows, out_root / "recordings.csv")
    return rows
