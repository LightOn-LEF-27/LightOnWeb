"""가짜 연결부 — 계약 모양을 그대로 따르는 **합성** 세션 (실제 주행이 아니다).

쓰는 곳: (1) 엔진 · 데이터 없이 사이트 개발 · 테스트, (2) 공개 데모 · README 화면, (3) 저장소에 진짜 데이터가 들어갈 일이 없게.
차량 이름부터 `DEMO-*` 라 화면에서 진짜와 헷갈리지 않는다. 수치는 그럴듯한 범위에서 손으로 지은 값이다 — 어떤 차의 측정값도 아니다.

진짜 엔진과 모양이 같은지는 `tests/test_fake_parity.py` 가 본다 (엔진 · 세션 저장소가 있는 PC 에서만 돈다).
지금 있는 것: 파일 올리기(F9 ~ F11)를 뺀 전부 — 그래프 값은 `fake_series`, 코스 · 구간 · 시간 차는 `fake_track` (랩 시계 하나에서 꺼내 서로 맞는다).
"""

from __future__ import annotations

import random
import tempfile
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

from . import fake_series, fake_track
from .base import EngineError, check_file_id, check_sid

CONTRACT = "0.11"  # 이 가짜가 따르는 계약 버전 (분석엔진_인터페이스.md)

# 블록마다 화면 이름 · 장비 설명 · 채널 (이름, 라벨, 단위, 부호, 주기 Hz, 자릿수, 계산한 채널인가)
BLOCKS: dict[str, dict] = {
    "gps": {"title": "위치 · 속도 (GPS)", "device": "데모 GNSS 수신기 · 20 Hz", "channels": [
        ("speed_kmh", "속도", "km/h", "", 20.0, 2, False),
        ("x_m", "동쪽 위치", "m", "동쪽 +", 20.0, 2, True),
        ("y_m", "북쪽 위치", "m", "북쪽 +", 20.0, 2, True)]},
    "imu": {"title": "차량 거동 (가속도 · 요레이트)", "device": "데모 IMU · 200 Hz", "channels": [
        ("long_g", "앞뒤 가속도", "g", "가속 +, 제동 −", 200.0, 3, False),
        ("lat_g", "좌우 가속도", "g", "좌선회 +", 200.0, 3, False),
        ("yaw_dps", "요레이트", "°/s", "좌회전 +", 200.0, 2, False)]},
    "pedal": {"title": "가속 페달 · 브레이크 압력", "device": "데모 로거 — 차량 CAN · 100 Hz", "channels": [
        ("apps_pct", "가속 페달", "%", "", 100.0, 1, False),
        ("brake_f_bar", "브레이크 압력 (앞)", "bar", "", 100.0, 1, False)]},
    "steer": {"title": "조향각", "device": "데모 로거 — 조향 센서 · 100 Hz", "channels": [
        ("angle_deg", "조향각", "°", "좌 +", 100.0, 1, False)]},
    "wheel": {"title": "휠 속도", "device": "데모 로거 — 휠 속도 센서 4개 · 100 Hz", "channels": [
        ("fl_kmh", "휠 속도 앞 왼쪽", "km/h", "", 100.0, 2, False),
        ("fr_kmh", "휠 속도 앞 오른쪽", "km/h", "", 100.0, 2, False)]},
    "battery": {"title": "팩 전류 · 전력", "device": "데모 전류 센서 · 100 Hz", "channels": [
        ("current_a", "팩 전류", "A", "방전 +", 100.0, 1, False),
        ("voltage_v", "팩 전압", "V", "", 100.0, 1, False),
        ("power_kw", "팩 전력", "kW", "방전 +", 100.0, 2, True)]},
    "bms": {"title": "배터리 상태 (BMS)", "device": "데모 BMS — 차량 CAN · 10 Hz", "channels": [
        ("soc_pct", "SOC", "%", "", 10.0, 1, False),
        ("cell_min_v", "셀 최저 전압", "V", "", 10.0, 3, False),
        ("temp_max_c", "셀 최고 온도", "°C", "", 10.0, 1, False)]},
    "motor": {"title": "모터 · 인버터", "device": "데모 모터 컨트롤러 — 차량 CAN · 50 Hz", "channels": [
        ("rpm", "모터 회전수", "rpm", "전진 +", 50.0, 0, False),
        ("inverter_c", "인버터 온도", "°C", "", 50.0, 1, False),
        ("motor_c", "모터 온도", "°C", "", 50.0, 1, False)]},
    "cooling": {"title": "냉각수 온도", "device": "데모 로거 — 온도 센서 · 10 Hz", "channels": [
        ("coolant_in_c", "냉각수 입구 온도", "°C", "", 10.0, 1, False)]},
    "suspension": {"title": "서스펜션 스트로크", "device": "데모 로거 — 리니어 센서 4개 · 200 Hz", "channels": [
        ("fl_mm", "스트로크 앞 왼쪽", "mm", "압축 +", 200.0, 1, False),
        ("rr_mm", "스트로크 뒤 오른쪽", "mm", "압축 +", 200.0, 1, False)]},
    "tire": {"title": "타이어 온도", "device": "데모 적외선 센서 · 10 Hz", "channels": [
        ("fl_c", "타이어 온도 앞 왼쪽", "°C", "", 10.0, 1, False)]},
    "video": {"title": "온보드 영상", "device": "데모 카메라 (롤후프에서 전방)", "channels": []},
    "drive": {"title": "주행 상태 (추정)", "device": "엔진 계산 — 팩 전력 + GPS 속도 변화율 · 10 Hz", "channels": [
        ("state_est", "주행 상태 (추정)", "", "+1 가속 · 0 타력 · −1 제동", 10.0, 0, True)]},
}
COMPUTED = {"drive"}   # 엔진이 다른 블록에서 계산해 만드는 블록
LEVELS = {"drive.state_est": [{"value": 1, "label": "가속"}, {"value": 0, "label": "타력"}, {"value": -1, "label": "제동"}]}

FULL = ["gps", "imu", "pedal", "steer", "wheel", "battery", "bms", "motor", "cooling", "suspension", "tire", "video", "drive"]


@dataclass(frozen=True)
class Event:
    event_id: str
    kind: str
    title: str
    place: str
    frame_id: str


@dataclass(frozen=True)
class FakeSession:
    sid: str
    event: Event
    discipline: str
    name: str
    driver: str            # "A" · "B" · "" (모름)
    start: str | None
    duration_s: float
    laps: int | None       # 0 = 랩이 없는 종목, None = 코스 기록 없음
    best_lap_s: float | None
    best_lap: int | None
    distance_km: float | None
    vmax_kmh: float | None
    net_wh: int | None
    i_max_a: int | None
    status: dict[str, str]                       # 블록 → have · warn · seek · none (빠진 블록은 기록 안 함과 같다)
    split_by: str = "gps"
    cautions: dict[str, list[str]] = field(default_factory=dict)
    notes: tuple[str, ...] = ()
    quality: tuple[tuple, ...] = ()              # (level, code, block, message, t_ranges | None) — 계약 §6-7

    @property
    def car(self) -> str:
        return self.event.event_id.split("/")[0]

    @property
    def has_track(self) -> bool:
        return self.status.get("gps") in ("have", "warn")


E_TEST = Event("DEMO-1/2027-05-08_demo-circuit", "테스트", "데모 주행 시험 (05-08)", "가상 서킷", "venue/demo-circuit")
E_CAMPUS = Event("DEMO-1/2027-04-24_demo-campus", "교내 시험", "데모 교내 시험 (04-24)", "가상 주차장", "DEMO-1/2027-04-24_demo-campus")
E_TRACKDAY = Event("DEMO-2/2026-11-14_demo-circuit", "트랙데이", "데모 트랙데이 (11-14)", "가상 서킷", "venue/demo-circuit")


def _all(have: str = "have", **override: str) -> dict[str, str]:
    st = {b: have for b in FULL}
    st["drive"] = "warn"            # 추정이라 늘 주의
    st.update(override)
    return st


# 합성 세션. S 번호는 일부러 그날 순서와 다르다 (계약 §3-1 — 나중에 들어온 세션은 시간상 앞이어도 다음 번호)
SESSIONS: list[FakeSession] = [
    FakeSession("DEMO-1_2027-05-08_S1", E_TEST, "연습", "기본 세팅 확인", "A", "2027-05-08T09:58:12.400+09:00",
                612.4, 7, 61.84, 5, 6.3, 88.4, 1480, 210, _all()),
    FakeSession("DEMO-1_2027-05-08_S2", E_TEST, "연습", "댐퍼 앞 · 브레이크 바이어스", "A", "2027-05-08T10:46:30.050+09:00",
                705.9, 8, 60.97, 6, 7.2, 90.1, 1702, 221, _all(bms="warn"),
                cautions={"bms": ["셀 전압 프레임이 41.2 s 동안 들어오지 않음 (그 구간은 값 없음)"]},
                quality=(("warn", "logger_not_recording", "bms", "BMS 프레임이 2 s 넘게 들어오지 않은 구간 1곳 · 합계 41.2 s", [[212.4, 253.6]]),)),
    FakeSession("DEMO-1_2027-05-08_S5", E_TEST, "연습", "토크 맵 100 %", "B", "2027-05-08T11:05:44.900+09:00",
                498.2, 6, 60.42, 4, 5.4, 91.6, 1391, 248, _all(tire="none"),
                notes=("나중에 들어온 기록으로 만든 세션이라, 시간상 앞이지만 다음 번호를 받았습니다",)),
    FakeSession("DEMO-1_2027-05-08_S3", E_TEST, "연습", "타이어 공기압", "B", "2027-05-08T11:38:02.700+09:00",
                655.0, 8, 61.12, 3, 7.1, 89.7, 1655, 230, _all()),
    FakeSession("DEMO-1_2027-05-08_S4", E_TEST, "내구", "완주 연습", "B", "2027-05-08T13:10:21.300+09:00",
                1395.6, 20, 62.03, 12, 17.9, 87.2, 4310, 226, _all(suspension="warn", video="seek"),
                cautions={"suspension": ["뒤 오른쪽 센서가 14 ~ 15랩에서 끝값(75 mm)에 붙음 — 배선 확인 필요"]},
                quality=(("warn", "sensor_at_limit", "suspension", "뒤 오른쪽 스트로크가 센서 끝값에 붙은 구간 2곳 · 합계 9.6 s",
                          [[905.2, 911.0], [968.3, 972.1]]),
                         ("info", "battery_gap", "battery", "팩 전류 표본이 빠진 곳 4곳 · 합계 0.3 s", [[120.0, 120.1], [480.5, 480.6]]))),
    FakeSession("DEMO-1_2027-04-24_S1", E_CAMPUS, "가속", "가속 1", "A", "2027-04-24T11:18:40.000+09:00",
                38.6, 0, None, None, 0.31, 97.3, 52, 262,
                _all(cooling="none", tire="none", steer="warn"), cautions={"steer": ["0점이 1.8° 치우침 — 직진에서 0 이 아님"]}),
    FakeSession("DEMO-1_2027-04-24_S2", E_CAMPUS, "스키드패드", "스키드패드 1", "B", "2027-04-24T14:02:15.500+09:00",
                41.3, 0, None, None, 0.34, 46.8, 31, 118,
                _all(cooling="none", tire="none", steer="warn"), cautions={"steer": ["0점이 1.8° 치우침 — 직진에서 0 이 아님"]}),
    FakeSession("DEMO-1_2027-04-24_S3", E_CAMPUS, "연습", "첫 주행", "", "2027-04-24T15:40:03.200+09:00",
                402.7, 5, 48.66, 4, 3.4, 71.5, 690, 174, _all(cooling="none", tire="none", pedal="warn"),
                cautions={"pedal": ["가속 페달 센서 둘이 10 % 넘게 어긋난 구간이 있음"]},
                quality=(("warn", "apps_fault", "pedal", "가속 페달 센서 둘이 10 % 넘게 어긋난 구간 1곳 · 합계 0.8 s", [[233.1, 233.9]]),)),
    FakeSession("DEMO-2_2026-11-14_S1", E_TRACKDAY, "연습", "트랙데이 오전", "A", "2026-11-14T10:45:30.000+09:00",
                540.0, 7, 66.81, 6, 5.9, 84.9, None, None,
                {"gps": "have", "imu": "have", "motor": "have", "bms": "have", "video": "have", "pedal": "seek", "battery": "none"}),
    FakeSession("DEMO-2_2026-11-14_S2", E_TRACKDAY, "연습", "트랙데이 오후", "B", "2026-11-14T13:20:00.000+09:00",
                600.0, None, None, None, None, None, 1210, 196,
                {"battery": "have", "motor": "have", "bms": "have", "gps": "seek", "imu": "none"}, split_by="time",
                notes=("시각으로 나눈 세션 — 데모 BMS 시계 기준 13:20 ~ 13:30 (GPS 기록 없음)",),
                quality=(("warn", "clock_mismatch", "battery", "팩 전류와 모터 전류의 파형이 1.6 s 어긋나 보인다 — 장비 시계 차를 다시 볼 것", None),)),
]

# 없어진 sid (계약 §3-1 · §6-10) — 화면이 "넘기기" · "고르기" · "없어진 세션" 을 다 보여 볼 수 있게
RETIRED: dict[str, dict] = {
    "DEMO-1_2027-05-08_S6": {"why": "replaced", "retired_at": "2027-05-09", "replaced_by": ["DEMO-1_2027-05-08_S3"]},
    "DEMO-1_2027-04-24_S4": {"why": "split", "retired_at": "2027-04-25", "replaced_by": ["DEMO-1_2027-04-24_S1", "DEMO-1_2027-04-24_S2"]},
    "DEMO-2_2026-11-14_S3": {"why": "removed", "retired_at": "2026-11-15", "replaced_by": []},
}

MISSING_TEXT = {
    "seek": ("기록했는데 파일이 우리한테 없음 (합성 예시)", "그날 로거 SD 카드 사본을 가진 분"),
    "none": ("이 차에 그 센서가 없었음 (합성 예시)", ""),
}


def _day_order() -> dict[str, int]:
    """그 차 · 그날 세션 중 몇 번째인가 (시작 시각 순, 1 부터)."""
    days: dict[tuple[str, str], list[FakeSession]] = {}
    for s in SESSIONS:
        if s.start:
            days.setdefault((s.car, s.start[:10]), []).append(s)
    return {s.sid: i for group in days.values() for i, s in enumerate(sorted(group, key=lambda s: s.start), 1)}


def _data_status(s: FakeSession) -> list[dict]:
    order = {"have": 0, "warn": 0, "seek": 1, "none": 2}   # 엔진처럼 가진 것 먼저
    rows = []
    for b in sorted(s.status, key=lambda b: (order[s.status[b]], FULL.index(b))):
        st = s.status[b]
        detail = BLOCKS[b]["device"] if st in ("have", "warn") else MISSING_TEXT[st][0]
        rows.append({"block": b, "title": BLOCKS[b]["title"], "status": st, "detail": detail})
    return rows


def _row(s: FakeSession, day_order: dict[str, int]) -> dict:
    blocks = {k: [b for b in FULL if s.status.get(b) == k] for k in ("have", "warn", "seek", "none")}
    mm_dd, hhmm = (s.start[5:10], s.start[11:16]) if s.start else ("", "")
    stats = {"vmax_kmh": s.vmax_kmh}
    if s.net_wh is not None:
        stats["net_wh"] = s.net_wh
    if s.net_wh is not None and s.distance_km:
        stats["wh_per_km"] = round(s.net_wh / s.distance_km)
    if s.i_max_a is not None:
        stats["i_max_a"] = s.i_max_a
    return {
        "sid": s.sid, "title": f"{s.car} · {mm_dd} · {s.name}", "name": s.name, "car": s.car,
        "event_id": s.event.event_id, "event_kind": s.event.kind, "event_title": s.event.title,
        "discipline": s.discipline, "place": s.event.place, "driver_label": f"드라이버 {s.driver}" if s.driver else "",
        "start": s.start, "duration_s": s.duration_s, "distance_km": s.distance_km, "laps": s.laps, "best_lap_s": s.best_lap_s,
        "stats": stats, "frame_id": s.event.frame_id if s.has_track else None, "split_by": s.split_by, "has_track": s.has_track,
        "blocks": blocks, "data_status": _data_status(s), "day_order": day_order.get(s.sid),
    }


def _channel_ids(block: str) -> list[str]:
    return [f"{block}.{c[0]}" for c in BLOCKS[block]["channels"]]


def _block(s: FakeSession, b: str) -> dict:
    meta, chans = BLOCKS[b], _channel_ids(b)
    computed = b in COMPUTED
    return {
        "block": b, "title": meta["title"], "status": s.status[b], "device": meta["device"],
        "facts": [f"합성 데이터 — {s.duration_s:.0f} s 분량 (실제 주행 아님)"],
        "checks": [] if computed else ["가짜 연결부: 시계 맞춤 검사 없음"],
        "cautions": list(s.cautions.get(b, [])) + (["추정 — 가속페달 · 브레이크 압력 신호가 아니다"] if computed else []),
        "raw": _raw(s, b),
        "outputs": [] if computed else [{"label": "변환된 표 (합성)", "name": f"{b}.parquet", "note": "", "where": "가짜 연결부 — 메모리"}],
        "exports": _exports(s, b),
        "code": [{"key": "fake", "title": "가짜 연결부", "role": "계약 모양을 따르는 합성 세션"}],
        "channels": chans,
        "default_views": {"time": [c for c in chans if c not in ("gps.x_m", "gps.y_m")][:2],
                          "distance": [c for c in chans if c not in ("gps.x_m", "gps.y_m")][:1] if s.has_track else [],
                          "map": chans[0] if chans and s.has_track else None, "gg": b == "imu"},
    }


# ---------------------------------------------------------------- 랩 표 (계약 §6-5)

# 랩 표 열: (key, 라벨, 단위, 자릿수, 블록, 그래프로 그릴 만한가) — 그 블록이 있는 세션에만 열이 생긴다
LAP_COLUMNS = [
    ("lap_time_s", "랩타임", "s", 2, "gps", True), ("s1_s", "S1", "s", 2, "gps", False), ("s2_s", "S2", "s", 2, "gps", False),
    ("s3_s", "S3", "s", 2, "gps", False), ("vmax_kmh", "최고 속도", "km/h", 1, "gps", True), ("distance_m", "달린 거리", "m", 0, "gps", False),
    ("long_g_max", "가속 최대", "g", 2, "imu", True), ("long_g_min", "제동 최대", "g", 2, "imu", True),
    ("lat_left_max", "좌선회 최대", "g", 2, "imu", True), ("lat_right_max", "우선회 최대", "g", 2, "imu", True),
    ("net_wh", "순 에너지", "Wh", 0, "battery", True), ("i_max_a", "전류 최대", "A", 0, "battery", True),
    ("cell_t_max_c", "셀 최고 온도", "°C", 0, "bms", True), ("inverter_t_max_c", "인버터 최고 온도", "°C", 1, "motor", True),
    ("coolant_t_max_c", "냉각수 최고 온도", "°C", 1, "cooling", True), ("full_throttle_pct", "풀스로틀 비율", "%", 0, "pedal", True),
]
SECTOR_KEYS = ("s1_s", "s2_s", "s3_s")


def _lap_columns(s: FakeSession) -> list[dict]:
    have = {b for b, st in s.status.items() if st in ("have", "warn")}
    return [{"key": k, "label": lab, "unit": u, "digits": d, "block": b, "chart": c} for k, lab, u, d, b, c in LAP_COLUMNS if b in have]


def _lap_rows(s: FakeSession) -> list[dict]:
    """합성 랩 — 세션 ID 로 정한 난수라 늘 같다. 최속 랩 = 요약의 best_lap · best_lap_s, 구간 셋의 합 = 랩타임."""
    if not s.has_track or not s.laps:
        return []
    rng = random.Random(s.sid)
    keys = {c["key"] for c in _lap_columns(s)}
    others = [i for i in range(1, s.laps + 1) if i != s.best_lap]
    peak_lap = rng.choice(others) if others else s.best_lap      # 최고 속도 · 전류 최대가 나온 랩
    rows, t0 = [], 14.0                                         # 첫 랩은 나가는 랩 뒤에 시작한다
    for i in range(1, s.laps + 1):
        lap_t = s.best_lap_s if i == s.best_lap else round(s.best_lap_s + rng.uniform(0.25, 2.8) + (2.0 if i == 1 else 0.0), 2)
        s1, s2, s3 = fake_track.sector_times(s, {"lap": i, "lap_time_s": lap_t})   # 구간 · 미니 구간 · 시간 차와 같은 랩 시계
        row = {"lap": i, "t_start_s": round(t0, 3), "t_end_s": round(t0 + lap_t, 3), "lap_time_s": lap_t,
               "s1_s": s1, "s2_s": s2, "s3_s": s3,
               "vmax_kmh": s.vmax_kmh if i == peak_lap else round(s.vmax_kmh - rng.uniform(0.5, 4.0), 1),
               "distance_m": round(858 + rng.uniform(-4, 4)),
               "long_g_max": round(rng.uniform(0.55, 0.75), 2), "long_g_min": round(-rng.uniform(0.9, 1.2), 2),
               "lat_left_max": round(rng.uniform(1.1, 1.4), 2), "lat_right_max": round(rng.uniform(1.1, 1.4), 2),
               "net_wh": round((s.net_wh or 0) / (s.laps + 1.5) * rng.uniform(0.92, 1.08)),
               "i_max_a": s.i_max_a if i == peak_lap else round((s.i_max_a or 0) - rng.uniform(1, 25)),
               "cell_t_max_c": round(31 + i * 0.7 + rng.uniform(0, 0.5)), "inverter_t_max_c": round(38 + i * 1.1 + rng.uniform(0, 1), 1),
               "coolant_t_max_c": round(35 + i * 0.9 + rng.uniform(0, 1), 1), "full_throttle_pct": round(rng.uniform(38, 52))}
        rows.append({k: v for k, v in row.items() if k in keys or k in ("lap", "t_start_s", "t_end_s")})
        t0 += lap_t
    best = {k: min(rows, key=lambda r: r[k]) for k in SECTOR_KEYS}
    for r in rows:
        r["best"] = r["lap"] == s.best_lap
        r["best_sectors"] = [n for n, k in enumerate(SECTOR_KEYS, 1) if best[k] is r]
    return rows


def _sectors(rows: list[dict]) -> dict | None:
    if not rows:
        return None
    best = {k: min(rows, key=lambda r: r[k]) for k in SECTOR_KEYS}
    return {"best": {k: {"value": best[k][k], "lap": best[k]["lap"]} for k in SECTOR_KEYS},
            "mean": {k: round(sum(r[k] for r in rows) / len(rows), 3) for k in SECTOR_KEYS}, "n_laps": len(rows),
            "theoretical_best_s": round(sum(best[k][k] for k in SECTOR_KEYS), 3)}


# ---------------------------------------------------------------- 파일 (계약 §6-2 raw · exports, F12)

def _raw(s: FakeSession, b: str) -> list[dict]:
    if b in COMPUTED:
        return []
    if b == "video":   # 용량 때문에 "있는 곳" 만 적힌 원본 — 내려받을 수 없다 (file_id 가 null)
        return [{"file_id": None, "label": "영상 파일 (합성 예시)", "name": "DEMO0001.MP4", "size_bytes": 4_000_000_000, "note": "",
                 "where": "DAQ 파트 보관 (용량이 커서 페이지에 붙이지 않음)", "content_type": "video/mp4"}]
    fid = f"{s.sid}/raw/{FULL.index(b)}"
    return [{"file_id": fid, "label": "원본 기록 (합성 예시)", "name": f"{b}_demo.log", "size_bytes": len(_content(fid)), "note": "",
             "where": "", "content_type": "application/octet-stream"}]


def _exports(s: FakeSession, b: str) -> list[dict]:
    if b in COMPUTED or b == "video":
        return []
    ids = [f"{s.sid}/files/{b}.csv"] + ([f"{s.sid}/files/laps.csv"] if b == "gps" and _lap_rows(s) else [])
    return [{"file_id": fid, "name": fid.rsplit("/", 1)[1],
             "label": "이 세션의 랩별 값" if fid.endswith("laps.csv") else f"이 세션의 {BLOCKS[b]['title']}",
             "note": f"{_rows(fid):,} 행", "rows": _rows(fid), "size_bytes": len(_content(fid))} for fid in ids]


def _files(s: FakeSession) -> dict[str, dict]:
    return {f["file_id"]: f for b in FULL if s.status.get(b) in ("have", "warn") for f in _raw(s, b) + _exports(s, b) if f["file_id"]}


@lru_cache(maxsize=256)
def _content(file_id: str) -> bytes:
    """합성 파일 내용 — 같은 ID 면 늘 같은 바이트."""
    sid, kind, name = file_id.split("/", 2)
    rng = random.Random(file_id)
    if kind == "raw":
        return b"DEMO RAW LOG (synthetic, not a real recording)\n" + bytes(rng.randrange(256) for _ in range(2048))
    if name == "laps.csv":
        s = next(x for x in SESSIONS if x.sid == sid)
        cols = ["lap", "t_start_s", "t_end_s"] + [c["key"] for c in _lap_columns(s)]
        lines = [",".join(cols)] + [",".join(str(r.get(c, "")) for c in cols) for r in _lap_rows(s)]
        return ("\n".join(lines) + "\n").encode()
    block = name.removesuffix(".csv")
    cols = ["t_s"] + [c[0] for c in BLOCKS[block]["channels"]]
    lines = [",".join(cols)] + [",".join([f"{i * 0.1:.1f}"] + [f"{rng.uniform(0, 100):.2f}" for _ in cols[1:]]) for i in range(200)]
    return ("\n".join(lines) + "\n").encode()


def _rows(file_id: str) -> int:
    return _content(file_id).count(b"\n") - 1


class FakeEngine:
    kind = "fake"

    def __init__(self):
        self._order = _day_order()
        self._by_sid = {s.sid: s for s in SESSIONS}
        self._dir = Path(tempfile.mkdtemp(prefix="lefsite-fake-"))   # 내려받기용 합성 파일을 여기 쓴다

    def _get(self, sid: str) -> FakeSession:
        check_sid(sid)
        if sid in self._by_sid:
            return self._by_sid[sid]
        if sid in RETIRED:
            r = self.resolve_session(sid)
            raise EngineError("session_retired", f"세션 {sid} 는 없어졌다 ({r['why']}) — 이어지는 세션: {', '.join(r['current']) or '없음'}",
                              {k: r[k] for k in ("sid", "why", "retired_at", "replaced_by", "current")})
        raise EngineError("unknown_session", f"세션 {sid} 가 없음")

    def engine_info(self) -> dict:
        return {"contract": CONTRACT, "engine": {"name": "lefdata (가짜 연결부 — 합성 세션)", "version": "fake"},
                "store": {"format": 1, "sessions": len(SESSIONS), "retired_sids": len(RETIRED)},
                "formats": [], "functions": ["engine_info", "list_sessions", "get_session", "list_channels", "get_series", "get_laps",
                                             "get_track", "get_quality", "open_file", "get_sectors", "get_delta", "resolve_session"],
                "not_yet": ["detect", "ingest", "get_job"]}

    def list_sessions(self, car=None, event_id=None, discipline=None, limit=None, offset=0) -> list[dict]:
        for name, v, low in (("limit", limit, 1), ("offset", offset, 0)):
            if v is not None and (isinstance(v, bool) or not isinstance(v, int) or v < low):
                raise EngineError("bad_argument", f"{name} 는 {low} 이상의 정수: {v!r}")
        hit = [_row(s, self._order) for s in sorted(SESSIONS, key=lambda s: s.start or "", reverse=True)
               if (car is None or s.car == car) and (event_id is None or s.event.event_id == event_id)
               and (discipline is None or s.discipline == discipline)]
        return hit[offset:] if limit is None else hit[offset:offset + limit]

    def get_session(self, sid: str) -> dict:
        s = self._get(sid)
        missing = [{"block": b, "data": BLOCKS[b]["title"], "status": st, "reason": MISSING_TEXT[st][0], "ask": MISSING_TEXT[st][1]}
                   for b, st in s.status.items() if st in ("seek", "none")]
        return {"summary": _row(s, self._order), "notes": ["합성 데이터입니다 (실제 주행 아님 · 가짜 연결부)", *s.notes],
                "blocks": [_block(s, b) for b in FULL if s.status.get(b) in ("have", "warn")],
                "missing": missing, "best_lap": s.best_lap,
                "made": {"built_at": "2027-05-09T09:00:00+09:00", "commit": "fake", "command": "가짜 연결부 (lefsite.engines.fake)"}}

    def list_channels(self, sid: str) -> list[dict]:
        s = self._get(sid)
        out = []
        for b in FULL:
            if s.status.get(b) not in ("have", "warn"):
                continue
            for name, label, unit, sign, rate, digits, derived in BLOCKS[b]["channels"]:
                cid = f"{b}.{name}"
                ch = {"channel_id": cid, "block": b, "label": label, "unit": unit, "sign": sign, "rate_hz": rate,
                      "samples": int(rate * s.duration_s),
                      "source": {"recording_id": None if b in COMPUTED else f"{s.sid}_demo", "device": BLOCKS[b]["device"]},
                      "derived": derived, "derivation": "합성 값" if derived else "", "t_range": [0.0, s.duration_s],
                      "cautions": list(s.cautions.get(b, [])), "digits": digits}
                if cid in LEVELS:
                    ch["levels"] = LEVELS[cid]
                out.append(ch)
        return out

    def get_series(self, sid: str, channel_ids, x: str = "time", t0=None, t1=None, lap=None, max_points: int = 4000,
                   smooth_s=None, d0=None, d1=None) -> dict:
        s = self._get(sid)
        channels = {c["channel_id"]: c for c in self.list_channels(sid)}
        return fake_series.get_series(s, channels, _lap_rows(s), s.quality, channel_ids, x=x, t0=t0, t1=t1, lap=lap,
                                      max_points=max_points, smooth_s=smooth_s, d0=d0, d1=d1)

    def get_track(self, sid: str, lap=None, color=None, smooth_s=None) -> dict:
        s = self._get(sid)
        channels = {c["channel_id"]: c for c in self.list_channels(sid)}
        return fake_track.get_track(s, _lap_rows(s), channels, lap=lap, color=color, smooth_s=smooth_s)

    def get_sectors(self, sid: str, mini_m=25) -> dict:
        s = self._get(sid)
        return fake_track.get_sectors(s, _lap_rows(s), mini_m=mini_m)

    def get_delta(self, sid: str, lap, ref_sid: str, ref_lap, mini_m=25, max_points: int = 4000) -> dict:
        s, r = self._get(sid), self._get(ref_sid)
        return fake_track.get_delta(s, _lap_rows(s), lap, r, _lap_rows(r), ref_lap, mini_m=mini_m, max_points=max_points)

    def get_laps(self, sid: str) -> dict:
        s = self._get(sid)
        rows = _lap_rows(s)
        notes = ["합성 랩 — 실제 주행이 아닙니다", "t_start_s · t_end_s 는 세션 시간"]
        if rows:
            notes.append(f"구간 S1 ~ S3 은 기준 거리 위의 경계. 평균은 완주 랩 {len(rows)}개, 이론 최속 = 구간마다 최속의 합")
        return {"columns": _lap_columns(s), "rows": rows, "sectors": _sectors(rows), "notes": notes, "best_lap": s.best_lap}

    def get_quality(self, sid: str) -> list[dict]:
        s = self._get(sid)
        return [{"level": lv, "code": code, "block": b, "message": msg, "t_ranges": tr} for lv, code, b, msg, tr in s.quality]

    def open_file(self, file_id: str) -> dict:
        sid = check_file_id(file_id).split("/", 1)[0]
        entry = _files(self._get(sid)).get(file_id)
        if entry is None:
            raise EngineError("unknown_file", f"파일 {file_id} 가 없음")
        path = self._dir / file_id.replace("/", "__")
        if not path.exists():
            path.write_bytes(_content(file_id))
        return {"file_id": file_id, "path": str(path), "name": entry["name"],
                "content_type": entry.get("content_type", "text/csv"), "size_bytes": path.stat().st_size}

    def resolve_session(self, sid: str) -> dict:
        check_sid(sid)
        if sid in self._by_sid:
            return {"sid": sid, "status": "active", "why": None, "retired_at": None, "replaced_by": [], "current": [sid]}
        if sid not in RETIRED:
            raise EngineError("unknown_session", f"세션 {sid} 가 없음")
        r = RETIRED[sid]
        current, todo = [], list(r["replaced_by"])
        while todo:                                  # 뒤를 이은 세션이 또 없어졌으면 끝까지 따라간다
            nxt = todo.pop(0)
            if nxt in RETIRED:
                todo.extend(RETIRED[nxt]["replaced_by"])
            elif nxt not in current:
                current.append(nxt)
        return {"sid": sid, "status": "retired", "why": r["why"], "retired_at": r["retired_at"],
                "replaced_by": list(r["replaced_by"]), "current": current}
