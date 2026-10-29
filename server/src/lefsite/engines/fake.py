"""가짜 연결부 — 계약 모양을 그대로 따르는 **합성** 세션 (실제 주행이 아니다).

쓰는 곳: (1) 엔진 · 데이터 없이 사이트 개발 · 테스트, (2) 공개 데모 · README 화면, (3) 저장소에 진짜 데이터가 들어갈 일이 없게.
차량 이름부터 `DEMO-*` 라 화면에서 진짜와 헷갈리지 않는다. 수치는 그럴듯한 범위에서 손으로 지은 값이다 — 어떤 차의 측정값도 아니다.

진짜 엔진과 모양이 같은지는 `tests/test_fake_parity.py` 가 본다 (엔진 · 세션 저장소가 있는 PC 에서만 돈다).
그래프 값 · 랩 표(F5 ~ F14)는 그 화면을 만들 때 여기에 더한다.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .base import EngineError, check_sid

CONTRACT = "0.10"  # 이 가짜가 따르는 계약 버전 (분석엔진_인터페이스.md)

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
                cautions={"bms": ["셀 전압 프레임이 41.2 s 동안 들어오지 않음 (그 구간은 값 없음)"]}),
    FakeSession("DEMO-1_2027-05-08_S5", E_TEST, "연습", "토크 맵 100 %", "B", "2027-05-08T11:05:44.900+09:00",
                498.2, 6, 60.42, 4, 5.4, 91.6, 1391, 248, _all(tire="none"),
                notes=("나중에 들어온 기록으로 만든 세션이라, 시간상 앞이지만 다음 번호를 받았습니다",)),
    FakeSession("DEMO-1_2027-05-08_S3", E_TEST, "연습", "타이어 공기압", "B", "2027-05-08T11:38:02.700+09:00",
                655.0, 8, 61.12, 3, 7.1, 89.7, 1655, 230, _all()),
    FakeSession("DEMO-1_2027-05-08_S4", E_TEST, "내구", "완주 연습", "B", "2027-05-08T13:10:21.300+09:00",
                1395.6, 20, 62.03, 12, 17.9, 87.2, 4310, 226, _all(suspension="warn", video="seek"),
                cautions={"suspension": ["뒤 오른쪽 센서가 14 ~ 15랩에서 끝값(75 mm)에 붙음 — 배선 확인 필요"]}),
    FakeSession("DEMO-1_2027-04-24_S1", E_CAMPUS, "가속", "가속 1", "A", "2027-04-24T11:18:40.000+09:00",
                38.6, 0, None, None, 0.31, 97.3, 52, 262,
                _all(cooling="none", tire="none", steer="warn"), cautions={"steer": ["0점이 1.8° 치우침 — 직진에서 0 이 아님"]}),
    FakeSession("DEMO-1_2027-04-24_S2", E_CAMPUS, "스키드패드", "스키드패드 1", "B", "2027-04-24T14:02:15.500+09:00",
                41.3, 0, None, None, 0.34, 46.8, 31, 118,
                _all(cooling="none", tire="none", steer="warn"), cautions={"steer": ["0점이 1.8° 치우침 — 직진에서 0 이 아님"]}),
    FakeSession("DEMO-1_2027-04-24_S3", E_CAMPUS, "연습", "첫 주행", "", "2027-04-24T15:40:03.200+09:00",
                402.7, 5, 48.66, 4, 3.4, 71.5, 690, 174, _all(cooling="none", tire="none", pedal="warn"),
                cautions={"pedal": ["가속 페달 센서 둘이 10 % 넘게 어긋난 구간이 있음"]}),
    FakeSession("DEMO-2_2026-11-14_S1", E_TRACKDAY, "연습", "트랙데이 오전", "A", "2026-11-14T10:45:30.000+09:00",
                540.0, 7, 66.81, 6, 5.9, 84.9, None, None,
                {"gps": "have", "imu": "have", "motor": "have", "bms": "have", "video": "have", "pedal": "seek", "battery": "none"}),
    FakeSession("DEMO-2_2026-11-14_S2", E_TRACKDAY, "연습", "트랙데이 오후", "B", "2026-11-14T13:20:00.000+09:00",
                600.0, None, None, None, None, None, 1210, 196,
                {"battery": "have", "motor": "have", "bms": "have", "gps": "seek", "imu": "none"}, split_by="time",
                notes=("시각으로 나눈 세션 — 데모 BMS 시계 기준 13:20 ~ 13:30 (GPS 기록 없음)",)),
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
        "raw": [] if computed else [{"file_id": None, "label": "원본 기록 (합성 예시)", "name": f"{b}.log", "size_bytes": 0, "note": "",
                                     "where": "가짜 연결부 — 파일 없음"}],
        "outputs": [], "exports": [],
        "code": [{"key": "fake", "title": "가짜 연결부", "role": "계약 모양을 따르는 합성 세션"}],
        "channels": chans,
        "default_views": {"time": chans[:3], "distance": chans[:2] if s.has_track else [], "map": chans[0] if chans and s.has_track else None,
                          "gg": b == "imu"},
    }


class FakeEngine:
    kind = "fake"

    def __init__(self):
        self._order = _day_order()
        self._by_sid = {s.sid: s for s in SESSIONS}

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
                "formats": [], "functions": ["engine_info", "list_sessions", "get_session", "list_channels", "resolve_session"],
                "not_yet": ["get_series", "get_laps", "get_track", "get_quality", "open_file", "get_sectors", "get_delta",
                            "detect", "ingest", "get_job"]}

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
