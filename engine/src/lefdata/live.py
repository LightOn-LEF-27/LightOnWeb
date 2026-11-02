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
Orion 스트림(0x6D0)은 값마다 몇 초씩 빈다 — 그래서 채널마다 낡음 기준이 다르다 (CHANNELS).

시계: 장치 시각(부팅 뒤 ms)은 `/d` 의 "받은 시각 − 보낸 순간" 의 최솟값(최근 10분)으로 서버 시계에 옮긴다. 가장 빨리 도착한
메시지가 시계 차이에 가장 가깝다 (가장 빠른 전달 시간만큼 이르게 잡힌다). `boot_epoch` 는 초 단위이고 동기화 전 RTC 값이라 쓰지 않는다.

CAN 풀이는 `evo_can` 과 같은 식을 프레임 하나씩 한다 — 하루치 파일을 다시 읽을 때 빨라야 해서다. 같은 값이 나오는지는 테스트가 맞대 본다.
"""

from __future__ import annotations

import base64
import math
import struct
import tomllib
from collections import deque
from dataclasses import dataclass, replace
from pathlib import Path

import numpy as np

from . import evo_can as ec
from . import venue as venue_mod
from .evo_stm32 import RPM_FULL_SCALE, motor_temp_c
from .laps import MIN_LAP_S, Gate, gate_crossings, to_local_m
from .lef26 import BAMOCAR_VDC_V_PER_NUM, igbt_temp_c
from .monolith import T_ANALOG, T_CAN, T_GPS, checksum_ok, nmea_to_deg

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


@dataclass(frozen=True)
class Ch:
    id: str
    label: str
    unit: str
    sign: str = ""
    digits: int = 1
    stale_s: float = 2.0
    source: str = ""


CHANNELS = {c.id: c for c in (
    Ch("gps.speed_kmh", "속도", "km/h", digits=0, source="gps"),
    Ch("gps.x_m", "위치 (동쪽)", "m", "동쪽 +", 1, source="gps"),
    Ch("gps.y_m", "위치 (북쪽)", "m", "북쪽 +", 1, source="gps"),
    Ch("battery.power_kw", "전력", "kW", "방전 +", 1, source="bms"),
    Ch("battery.voltage_v", "팩 전압", "V", "", 1, source="bms"),
    Ch("battery.current_a", "배터리 전류", "A", "방전 +, 배터리로 돌아오면 −", 0, source="bms"),
    Ch("bms.soc_pct", "SOC", "%", "", 1, source="bms"),
    Ch("bms.cell_min_v", "셀 최저 전압", "V", "", 3, source="bms"),
    Ch("bms.temp_max_c", "셀 최고 온도", "°C", "", 0, source="bms"),
    Ch("bms.temp_min_c", "셀 최저 온도", "°C", "", 0, source="bms"),
    Ch("bms.dcl_kw", "방전 출력 한계 (DCL)", "kW", "", 1, 120.0, "bms"),          # 0x6D0 슬롯이 돌아가며 실려 드물다
    Ch("motor.t_motor_c", "모터 온도", "°C", "", 0, 15.0, "inverter"),             # 0x181 레지스터가 돌아가며 실린다
    Ch("motor.t_igbt_c", "인버터(IGBT) 온도", "°C", "", 0, 15.0, "inverter"),
    Ch("motor.vdc_v", "DC 버스 전압", "V", "", 0, 15.0, "inverter"),
    Ch("motor.rpm", "회전수 (로거 환산)", "rpm", "전진할 때 −", 0, 15.0, "inverter"),
    Ch("motor.enabled", "구동 허용 (Ena)", "", "", 0, 15.0, "inverter"),
    Ch("motor.ready", "운전 준비 (Rdy)", "", "", 0, 15.0, "inverter"),
    Ch("motor.error_raw", "오류 비트", "", "", 0, 15.0, "inverter"),
    Ch("pedal.apps_pct", "가속페달", "%", "밟을수록 +", 0, source="pedal"),
    Ch("pedal.torque_cmd_pct", "토크 명령", "%", "가속 +", 0, source="pedal"),
    Ch("lv.voltage_v", "LV 전압 (추정 환산)", "V", "", 1, source="lv"),
)}
TILES = ("gps.speed_kmh", "battery.power_kw", "battery.voltage_v", "battery.current_a", "bms.soc_pct", "bms.cell_min_v",
         "bms.temp_max_c", "motor.t_motor_c", "motor.t_igbt_c", "pedal.apps_pct", "lv.voltage_v")
CHART = ("gps.speed_kmh", "battery.power_kw", "battery.current_a", "bms.temp_max_c", "motor.t_motor_c", "motor.t_igbt_c",
         "bms.soc_pct", "gps.x_m", "gps.y_m")
SOURCES = (("bms", "BMS"), ("inverter", "인버터"), ("pedal", "가속페달"), ("gps", "GPS"), ("lv", "LV"))
STREAM_TO_CH = {"dcl_kw": "bms.dcl_kw", "soc_pct": "bms.soc_pct", "temp_max_c": "bms.temp_max_c", "temp_min_c": "bms.temp_min_c"}
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
        out.append(("pedal.torque_cmd_pct", ec.TORQUE_SIGN * _s16(_u16le(d, 4)) / ec.TORQUE_FULL * 100))
    elif can_id == ec.BAMOCAR:
        reg = d[0]
        if dlc == 4:
            raw = _s16(_u16le(d, 1))
        elif dlc == 6:
            raw = _u16le(d, 1) | _u16le(d, 3) << 16
        else:
            return out
        if reg == ec.REG_SPEED:
            out.append(("motor.rpm", raw / RPM_FULL_SCALE * ec.LOGGER_N100_RPM))
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


# ---------------------------------------------------------------- 상태

class Live:
    """하루치 수신 기록 하나를 따라가는 상태. feed 를 받은 순서대로 부른다 (스레드 안전하지 않다 — 부르는 쪽이 잠근다)."""

    def __init__(self, car: str = "LEF-25evo", venue: str | dict | None = None, rules: Path | str | None = None,
                 data_root: Path | str | None = None):
        self.car = car
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
        # GPS · 랩
        self.gps_ts: int | None = None
        self.analog_ts: int | None = None
        self.prev_pt: tuple[float, float, float, float] | None = None
        self.crossings: list[float] = []
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
            self._receiver(t, row["event"])
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

    def _receiver(self, t: float, ev: str) -> None:
        status = {"connected": "connected", "disconnected": "disconnected"}.get(ev)
        if status is None:
            self.bad_rows += 1
            return
        self.receiver = (status, t)
        self._event(t, "receiver", "수신기 연결" if status == "connected" else "수신기 연결 끊김")

    def _boot(self, t: float, p: bytes, retain: bool) -> None:
        if p.startswith(OFFLINE):
            # 장치와 브로커의 연결이 끊기면 브로커가 대신 보낸다 (last will) — 전원을 껐는지 통신이 끊겼는지는 모른다
            if self.offline_at is None or self.offline_at < (self.last_rx or 0):
                self.offline_at = t
                if not retain:
                    self._event(t, "offline", "장치 연결 끊김 — 전원 꺼짐 또는 통신 끊김")
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
        self.boots += 1
        if not first:
            self._restarted = True
        self._event(t, "boot", "장치 수신 시작" if first else "장치 재시작")

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
        if self.last_rx is not None and not self._restarted:
            gap = t - self.last_rx
            if gap > self._limits()[1]:
                self.outages.append((self.last_rx, t))
                self._event(t, "restored", f"수신 회복 — {gap:.1f} s 끊김")
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
        self._clock(t, struct.unpack_from("<I", p, 0)[0])
        self._periodic(t, True)
        for r in _records(p[8:]):
            ts = int.from_bytes(r[4:8].tobytes(), "little")
            if r[1] == T_GPS and ts != self.gps_ts:
                self.gps_ts = ts
                self._gps(self._srv(ts, t), r[8:24].tobytes())
            elif r[1] == T_ANALOG and ts != self.analog_ts:
                self.analog_ts = ts
                supply = struct.unpack_from("<h", r[8:24].tobytes(), 12)[0]
                if supply > 0:
                    self._put("lv.voltage_v", self._srv(ts, t), supply * LV_V_PER_RAW)
        self._grid(t)

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
        for r in _records(p):
            if r[1] != T_CAN:
                continue
            ts = self._srv(int.from_bytes(r[4:8].tobytes(), "little"), t)
            can_id = int.from_bytes(r[8:12].tobytes(), "little")
            if can_id == ec.BAMOCAR:
                self.seen["inverter"] = max(self.seen.get("inverter", ts), ts)
            for ch, v in decode_frame(can_id, int(r[14]), r[16:24].tobytes()):
                self._put(ch, ts, v)

    # ------------------------------------------------------------ 값 · 경고

    def _put(self, ch: str, ts: float, v: float) -> None:
        old = self.vals.get(ch)
        if old is not None and ts < old[0]:
            return                                                # 늦게 온 옛 값으로 덮지 않는다
        self.vals[ch] = (ts, float(v))
        src = CHANNELS[ch].source
        self.seen[src] = max(self.seen.get(src, ts), ts)
        if ch == "motor.error_raw":
            self._inverter_errors(ts, int(v))
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

    def _event(self, t: float, kind: str, text: str) -> None:
        self.events.append({"t": round(t, 3), "kind": kind, "text": text})

    def _grid(self, t: float) -> None:
        g = math.floor(t / GRID_S) * GRID_S
        if self.grid_t is None:
            self.grid_t = g - GRID_S
        if g <= self.grid_t:
            return
        steps = int(round((g - self.grid_t) / GRID_S))
        for k in range(1, min(steps, self.chart.maxlen)):        # 끊긴 동안의 칸은 비워 둔다 (선을 끊는다)
            self.chart.append((round(self.grid_t + k * GRID_S, 3), (None,) * len(CHART)))
        self.chart.append((round(g, 3), tuple(_r(self._fresh(ch, g)) for ch in CHART)))
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

    def _device(self, now: float, link: dict) -> dict:
        if link["status"] in ("ok", "late"):
            status = "on"
        elif self.offline_at is not None and (self.last_rx is None or self.offline_at >= self.last_rx):
            status = "disconnected"
        else:
            status = "unknown"
        return {"status": status, "boot_at": _r(self.boot_at, 1), "firmware": self.firmware, "boots": self.boots}

    def state(self, now: float, since: float | None = None) -> dict:
        link = self._link(now)
        device = self._device(now, link)
        tiles = []
        for ch in TILES:
            st, v, age, alert = self._status(ch, now)
            c = CHANNELS[ch]
            tiles.append({"id": ch, "label": c.label, "unit": c.unit, "sign": c.sign, "digits": c.digits, "value": _r(v, 4),
                          "age_s": _r(age, 1), "status": st, "alert": alert})
        sources = []
        for sid, label in SOURCES:
            last = self.seen.get(sid)
            age = None if last is None else max(0.0, now - last)
            st = "none" if age is None else "stale" if age > SOURCE_STALE_S else "ok"
            sources.append({"id": sid, "label": label, "status": st, "age_s": _r(age, 1)})

        alerts = [a for a in self.active.values() if self._fresh(a["channel"], now) is not None]
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

        position = laps = None
        if self.venue is not None and "gps.x_m" in self.vals:
            tx, x = self.vals["gps.x_m"]
            position = {"frame_id": self.venue.get("frame_id"), "x_m": _r(x, 1), "y_m": _r(self.vals["gps.y_m"][1], 1),
                        "age_s": _r(max(0.0, now - tx), 1)}
        if self.venue is not None and self.crossings:
            done = np.diff(self.crossings)
            laps = {"count": int(len(done)), "current_s": _r(max(0.0, now - self.crossings[-1]), 1),
                    "last_s": _r(done[-1], 1) if len(done) else None, "best_s": _r(done.min(), 1) if len(done) else None,
                    "basis": "GPS 계측선 통과 — GPS 주기만큼 오차"}

        lo = since if since is not None else now - CHART_DEFAULT_S
        rows = [r for r in self.chart if r[0] > lo]
        chart = {"t": [r[0] for r in rows], "series": {ch: [r[1][i] for r in rows] for i, ch in enumerate(CHART)}}

        return {"contract": CONTRACT, "now": _r(now, 3), "car": self.car,
                "source": {"replay": self.replay, "bad_rows": self.bad_rows, "unknown_topics": self.unknown},
                "receiver": {"status": self.receiver[0], "since": _r(self.receiver[1], 3)},
                "device": device, "link": link, "sources": sources, "tiles": tiles, "inverter": inverter,
                "position": position, "laps": laps, "alerts": [{k: v for k, v in a.items() if k != "channel"} for a in alerts],
                "events": list(reversed(self.events)), "chart": chart}

    def track(self) -> dict | None:
        if self.venue is None:
            return None
        v = self.venue
        return {"frame_id": v.get("frame_id"), "line": {"x_m": list(v["line"]["x_m"]), "y_m": list(v["line"]["y_m"])},
                "gate": {**v["gate"], "half_width_m": v.get("half_width_m")}}
