"""실시간 화면 상태 (`live`) — 만들어 낸 Monolith 메시지로 풀이 · 시계 · 링크 · 경고 · 랩을 확인한다 (계약 §12).

우리 주행 데이터는 쓰지 않는다. 풀이가 사후 분석(`evo_can`)과 같은 값을 내는지는 무작위 프레임으로 맞대 본다.
"""

import base64
import json
import math
import struct
import time

import numpy as np
import pandas as pd
import pytest

from lefdata import evo_can as ec, live

LAT0, LON0 = 35.0, 126.0
VENUE = {"frame_id": "venue/test", "origin": {"lat0": LAT0, "lon0": LON0}, "gate": {"x": 0.0, "y": 0.0, "heading_deg": 90.0},
         "half_width_m": 30.0, "line": {"x_m": [-50.0, 50.0], "y_m": [0.0, 0.0]}}


# ---------------------------------------------------------------- 메시지 만들기

def rec(rtype: int, ts: int, pl: bytes) -> bytes:
    body = bytearray(struct.pack("<BBHI", 0xAE, rtype, 0, ts) + pl)
    x = 0
    for w in struct.unpack("<6I", bytes(body)):
        x ^= w
    struct.pack_into("<H", body, 2, ((x & 0xFFFF) + (x >> 16)) & 0xFFFF)
    return bytes(body)


def can_rec(ts: int, can_id: int, data: bytes, dlc: int = 8) -> bytes:
    return rec(2, ts, struct.pack("<IBBBB", can_id, 0, 0, dlc, 0) + data.ljust(8, b"\0"))


def nmea(deg: float) -> int:
    d = math.floor(deg)
    return round((d + (deg - d) * 60 / 100) * 1e7)


def gps_rec(ts: int, x_m: float, y_m: float, speed: float, course: float) -> bytes:
    lat = LAT0 + y_m / 110_950.0
    lon = LON0 + x_m / (math.cos(math.radians(LAT0)) * 111_320.0)
    return rec(3, ts, struct.pack("<IIBBHHH", nmea(lat), nmea(lon), ord("N"), ord("E"), 0, round(speed * 100), round(course * 100)))


def analog_rec(ts: int, supply_raw: int) -> bytes:
    return rec(4, ts, struct.pack("<8h", 0, 0, 0, 0, 0, 0, supply_raw, 0))


def logbuf(dev_ms: int, gps: bytes | None = None, analog: bytes | None = None) -> bytes:
    return struct.pack("<II", dev_ms, 0) + (gps or bytes(24)) + bytes(24) + (analog or bytes(24)) + bytes(24)


def row(t: float, topic: str, payload: bytes, retain: bool = False) -> dict:
    return {"t_ns": int(round(t * 1e9)), "topic": f"car/{topic}", "retain": retain, "b64": base64.b64encode(payload).decode()}


def be(v, n=2, signed=False):
    return int(v).to_bytes(n, "big", signed=signed)


def le(v, n=2, signed=False):
    return int(v).to_bytes(n, "little", signed=signed)


def with_sum(body7: bytes, extra: int = 0) -> bytes:
    return body7 + bytes([(sum(body7) + extra) & 0xFF])


def summary(power_kw=10.0, cell_v=3.9, tmax=40, tmin=35, soc=80.0) -> bytes:
    return with_sum(be(round(power_kw * 10), signed=True) + be(round(cell_v * 1e4)) + bytes([tmax & 0xFF, tmin & 0xFF, round(soc * 2)]))


def vi(v=280.0, a=50.0) -> bytes:
    return with_sum(be(round(v * 10)) + be(round(a * 10), signed=True) + bytes(3))


def bamo(reg: int, raw: int, dlc: int) -> bytes:
    return bytes([reg]) + (le(raw, 2, signed=True) if dlc == 4 else le(raw, 4))


class Car:
    """0.5 s 마다 /d 와 /d/can 을 보내는 차. clock = 서버 시계 − 장치 시계, delay = 전달 시간."""

    def __init__(self, L: live.Live, clock: float = 1_000_000.0, delay: float = 0.2):
        self.L, self.clock, self.delay, self.dev = L, clock, delay, 10_000

    def tick(self, frames=(), gps=None, analog=None, skip=False, delay=None):
        self.dev += 500
        t = self.dev / 1000 + self.clock + (self.delay if delay is None else delay)
        if not skip:
            self.L.feed(row(t, "d", logbuf(self.dev, gps, analog)))
            if frames:
                self.L.feed(row(t, "d/can", b"".join(can_rec(self.dev - 100, cid, d, dlc) for cid, d, dlc in frames)))
        return t

    def run(self, n, **kw):
        t = None
        for _ in range(n):
            t = self.tick(**kw)
        return t


def tile(st, ch):
    return next(x for x in st["tiles"] if x["id"] == ch)


# ---------------------------------------------------------------- 풀이가 사후 분석과 같다

def test_decode_frame_matches_evo_can():
    rng = np.random.default_rng(7)
    d = rng.integers(0, 256, size=(200, 8), dtype=np.uint8)
    d[:, 7] = d[:, :7].astype(int).sum(axis=1) & 0xFF            # 체크섬을 맞춘다
    s = ec.decode_orion_summary(d)
    v = ec.decode_orion_vi(d)
    for i in range(len(d)):
        got = dict(live.decode_frame(ec.ORION_SUMMARY, 8, d[i].tobytes()))
        assert got["battery.power_kw"] == pytest.approx(s["power_kw"][i])
        assert got["bms.cell_min_v"] == pytest.approx(s["cell_min_v"][i])
        assert got["bms.temp_max_c"] == pytest.approx(s["temp_max_c"][i]) and got["bms.temp_min_c"] == pytest.approx(s["temp_min_c"][i])
        assert got["bms.soc_pct"] == pytest.approx(s["soc_pct"][i])
        got = dict(live.decode_frame(ec.ORION_VI, 8, d[i].tobytes()))
        assert got["battery.voltage_v"] == pytest.approx(v["pack_v"][i]) and got["battery.current_a"] == pytest.approx(v["current_a"][i])


def test_stream_frames_match_evo_can():
    rng = np.random.default_rng(8)
    slots = [k for k, (name, _, _) in ec.STREAM_SLOTS.items() if name in live.STREAM_TO_CH]
    d = rng.integers(0, 256, size=(len(slots) * 20, 8), dtype=np.uint8)
    d[:, 0] = np.repeat(slots, 20)
    d[:, 7] = (d[:, :7].astype(int).sum(axis=1) + ec.ORION_STREAM + 8) & 0xFF
    tab = ec.decode_orion_stream(d).assign(t_s=np.arange(len(d), dtype=float))
    sig = ec.stream_signals(tab)
    for i in range(len(d)):
        got = dict(live.decode_frame(ec.ORION_STREAM, 8, d[i].tobytes()))
        assert got["battery.current_a"] == pytest.approx(tab["current_a"][i]) and got["battery.voltage_v"] == pytest.approx(tab["pack_v"][i])
        name = ec.STREAM_SLOTS[int(d[i, 0])][0]
        t, vals = sig[name]
        assert got[live.STREAM_TO_CH[name]] == pytest.approx(vals[list(t).index(float(i))])


def test_bamocar_and_pedal_match_evo_can():
    rows = []
    for reg in (ec.REG_SPEED, ec.REG_VDC, ec.REG_T_IGBT, ec.REG_T_MOTOR):
        for raw in (-12000, -1, 0, 500, 12000, 17000, 25000, 31000):
            rows.append((reg, raw, 4))
    for raw in (0, 1 << 14, 1 | 1 << 14, 1 << 6, 1 << 5 | 1 << 22):
        rows += [(ec.REG_STATUS, raw, 6), (ec.REG_ERROR, raw, 6)]
    d = np.array([list(bamo(r, x, n).ljust(8, b"\0")) for r, x, n in rows], np.uint8)
    tab = ec.decode_bamocar(d, np.array([n for _, _, n in rows])).assign(t_s=np.arange(len(rows), dtype=float))
    sig = ec.bamocar_signals(tab)
    names = {"rpm": "motor.rpm", "vdc_v": "motor.vdc_v", "t_igbt_c": "motor.t_igbt_c", "t_motor_c": "motor.t_motor_c",
             "enabled": "motor.enabled", "ready": "motor.ready", "error_raw": "motor.error_raw"}
    for name, (t, vals) in sig.items():
        for ti, want in zip(t, vals):
            r, x, n = rows[int(ti)]
            got = dict(live.decode_frame(ec.BAMOCAR, n, bytes(d[int(ti)])))
            if np.isfinite(want):
                assert got[names[name]] == pytest.approx(float(want))
            else:
                assert names[name] not in got                     # 표 밖 온도는 값을 만들지 않는다
    raw = 1234
    apps = with_sum(le(100) + le(200) + le(456) + b"\x01")[:8]
    assert dict(live.decode_frame(ec.APPS_RAW, 8, apps))["pedal.apps_pct"] == pytest.approx(ec.decode_apps_raw(np.frombuffer(apps, np.uint8)[None])["pedal_pct"][0])
    cmd = le(0) + le(0) + le(-raw, signed=True) + b"\0\0"
    want = ec.TORQUE_SIGN * ec.decode_apps_scaled(np.frombuffer(cmd, np.uint8)[None])["torque_cmd_raw"][0] / ec.TORQUE_FULL * 100
    assert dict(live.decode_frame(ec.APPS_SCALED, 8, cmd))["pedal.torque_cmd_pct"] == pytest.approx(want) and want > 0


def test_bad_checksum_frames_are_dropped():
    good = summary()
    bad = good[:7] + bytes([good[7] ^ 1])
    assert live.decode_frame(ec.ORION_SUMMARY, 8, good) and not live.decode_frame(ec.ORION_SUMMARY, 8, bad)
    assert live.decode_frame(0x123, 8, bytes(8)) == []


# ---------------------------------------------------------------- 시계 · 나이

def test_age_uses_device_time_not_arrival():
    L = live.Live()
    car = Car(L, delay=0.2)
    t = car.run(20, frames=[(ec.ORION_SUMMARY, summary(), 8)])
    st = L.state(t)
    # 프레임은 보내기 100 ms 전에 찍혔다 — 나이는 전달 시간(0.2 s, 바닥으로 빠짐)이 아니라 그 0.1 s 만큼
    assert tile(st, "battery.power_kw")["age_s"] == pytest.approx(0.1, abs=0.05)
    # 한 번 늦게 온 묶음: 받은 시각은 늦어도 값의 시각은 장치 시각
    t = car.tick(frames=[(ec.ORION_SUMMARY, summary(power_kw=33), 8)], delay=3.0)
    assert tile(L.state(t), "battery.power_kw")["age_s"] == pytest.approx(2.9, abs=0.05)


def test_older_value_does_not_overwrite_newer():
    L = live.Live()
    car = Car(L)
    t = car.tick(frames=[(ec.ORION_SUMMARY, summary(power_kw=20), 8)])
    L.feed(row(t + 0.1, "d/can", can_rec(car.dev - 400, ec.ORION_SUMMARY, summary(power_kw=99))))
    assert tile(L.state(t + 0.2), "battery.power_kw")["value"] == pytest.approx(20)


# ---------------------------------------------------------------- 링크 · 끊김 · 재시작

def test_link_states_and_outage_counted_when_it_ends():
    L = live.Live()
    car = Car(L)
    t = car.run(30)
    st = L.state(t)
    assert st["link"]["status"] == "ok" and st["link"]["rate_10s"] == pytest.approx(1.0) and st["device"]["status"] == "on"
    assert L.state(t + 3)["link"]["status"] == "late"
    lost = L.state(t + 12)
    assert lost["link"]["status"] == "lost" and lost["link"]["since_s"] == pytest.approx(12, abs=0.1)
    assert [a["id"] for a in lost["alerts"]][:1] == ["W-LINK"] and lost["alerts"][0]["level"] == "danger"
    assert lost["link"]["outages"] == 0                          # 끝나기 전에는 세지 않는다 (전원을 껐을 수도 있다)
    car.run(30, skip=True)                                        # 15 s 동안 못 받음, 같은 부팅
    t = car.run(4)
    st = L.state(t)
    assert st["link"]["outages"] == 1 and st["link"]["longest_s"] == pytest.approx(15.5, abs=0.1)
    assert st["events"][0]["kind"] == "restored" or any(e["kind"] == "restored" for e in st["events"])


def test_restart_is_not_an_outage():
    L = live.Live()
    car = Car(L)
    car.run(10)
    t = car.run(40, skip=True)
    car.dev = 2_000                                               # 다시 켜졌다
    t = car.run(4)
    st = L.state(t)
    assert st["link"]["outages"] == 0 and st["device"]["boots"] == 2
    assert any(e["text"] == "장치 재시작" for e in st["events"])


def test_last_will_marks_device_disconnected_and_silences_link_alert():
    L = live.Live()
    car = Car(L)
    t = car.run(10)
    L.feed(row(t + 45, "d/boot", b"OFFLINE"))
    st = L.state(t + 60)
    assert st["device"]["status"] == "disconnected" and st["link"]["status"] == "lost"
    assert not any(a["id"] == "W-LINK" for a in st["alerts"])
    assert any(e["kind"] == "offline" for e in st["events"])


def test_retained_offline_at_receiver_start_sets_status_without_event():
    L = live.Live()
    L.feed({"t_ns": 1_000 * 10**9, "event": "connected"})
    L.feed(row(1_000.01, "d/boot", b"OFFLINE", retain=True))
    L.feed(row(1_000.02, "d/ver", b"v2.3.0 (abc)", retain=True))
    st = L.state(1_001)
    assert st["receiver"]["status"] == "connected" and st["device"]["status"] == "disconnected"
    assert st["device"]["firmware"] == "v2.3.0 (abc)" and st["link"]["status"] == "none"
    assert [e["kind"] for e in st["events"]] == ["receiver"]


# ---------------------------------------------------------------- 타일 · 경고

def test_tile_status_none_ok_warn_danger_stale():
    L = live.Live()
    car = Car(L)
    t = car.run(10, frames=[(ec.ORION_SUMMARY, summary(tmax=40), 8)])
    st = L.state(t)
    assert tile(st, "bms.temp_max_c")["status"] == "ok" and tile(st, "motor.t_motor_c")["status"] == "none"
    assert tile(st, "motor.t_motor_c")["value"] is None
    t = car.run(6, frames=[(ec.ORION_SUMMARY, summary(tmax=56), 8)])
    assert tile(L.state(t), "bms.temp_max_c")["status"] == "warn"
    t = car.run(6, frames=[(ec.ORION_SUMMARY, summary(tmax=61), 8)])
    st = L.state(t)
    assert tile(st, "bms.temp_max_c")["status"] == "danger" and tile(st, "bms.temp_max_c")["alert"] == "W-TB-NOW"
    assert st["alerts"][0]["level"] == "danger" and "60 °C 이상" in st["alerts"][0]["text"]
    stale = L.state(t + 5)
    assert tile(stale, "bms.temp_max_c")["status"] == "stale" and tile(stale, "bms.temp_max_c")["value"] == 61
    assert not any(a["id"] == "W-TB-NOW" for a in stale["alerts"])   # 낡은 값으로는 울리지 않는다


def test_alert_needs_to_hold_two_seconds_and_clears():
    L = live.Live()
    car = Car(L)
    car.run(4, frames=[(ec.ORION_SUMMARY, summary(tmax=40), 8)])
    t = car.run(2, frames=[(ec.ORION_SUMMARY, summary(tmax=61), 8)])
    assert not L.state(t)["alerts"]
    t = car.run(4, frames=[(ec.ORION_SUMMARY, summary(tmax=61), 8)])
    assert L.state(t)["alerts"][0]["id"] == "W-TB-NOW"
    t = car.run(1, frames=[(ec.ORION_SUMMARY, summary(tmax=40), 8)])
    st = L.state(t)
    assert not st["alerts"] and st["events"][0]["text"].endswith("해제")


def test_info_rule_does_not_color_tile():
    L = live.Live()
    car = Car(L)
    t = car.run(8, frames=[(ec.ORION_SUMMARY, summary(power_kw=85), 8)])
    st = L.state(t)
    assert st["alerts"][0]["id"] == "W-P80" and st["alerts"][0]["level"] == "info"
    assert tile(st, "battery.power_kw")["status"] == "ok"


def test_hv_off_only_while_moving():
    L = live.Live(venue=VENUE)
    car = Car(L)
    low = (ec.BAMOCAR, bamo(ec.REG_VDC, round(100 / 0.0181423), 4), 4)
    for _ in range(8):
        t = car.tick(frames=[low], gps=gps_rec(car.dev + 500 - 50, 0, 0, 0.0, 90))
    assert not L.state(t)["alerts"]
    for _ in range(8):
        t = car.tick(frames=[low], gps=gps_rec(car.dev + 500 - 50, 0, 0, 30.0, 90))
    assert any(a["id"] == "W-HV-OFF" and a["level"] == "danger" for a in L.state(t)["alerts"])


def test_inverter_errors_named_and_supply_missing_is_not_a_fault():
    L = live.Live()
    car = Car(L)
    t = car.run(3, frames=[(ec.BAMOCAR, bamo(ec.REG_ERROR, 1 << ec.ERROR_VOLTAGE_MIN, 6), 6)])
    st = L.state(t)
    assert st["inverter"]["status"] == "ok" and st["inverter"]["errors"] == [] and not st["alerts"]
    t = car.run(1, frames=[(ec.BAMOCAR, bamo(ec.REG_ERROR, 1 << 6, 6), 6),
                           (ec.BAMOCAR, bamo(ec.REG_STATUS, 1 | 1 << 14, 6), 6)])
    st = L.state(t)
    assert st["inverter"]["status"] == "danger" and st["inverter"]["errors"] == ["Motor-Temp. Max. Limit"]
    assert st["alerts"][0]["id"] == "W-INV-ERR"


def test_rules_file_overrides_and_adds(tmp_path):
    p = tmp_path / "car.toml"
    p.write_text('[[rule]]\nid = "W-TB-NOW"\nwarn = 45.0\n\n[[rule]]\nid = "W-SOC"\nchannel = "bms.soc_pct"\nop = "<="\nwarn = 20.0\n'
                 'danger = 10.0\ntext = "SOC"\n', encoding="utf-8")
    rules = {r.id: r for r in live.load_rules(p)}
    assert rules["W-TB-NOW"].warn == 45.0 and rules["W-TB-NOW"].danger == 60.0 and rules["W-SOC"].level(9) == "danger"
    L = live.Live(rules=p)
    car = Car(L)
    t = car.run(6, frames=[(ec.ORION_SUMMARY, summary(soc=15), 8)])
    assert {a["id"] for a in L.state(t)["alerts"]} == {"W-SOC"}
    bad = tmp_path / "bad.toml"
    bad.write_text('[[rule]]\nid = "X"\nchannel = "nope"\nop = ">="\ntext = "x"\n', encoding="utf-8")
    with pytest.raises(ValueError):
        live.load_rules(bad)


def test_source_stale_alert_only_while_device_on():
    L = live.Live()
    car = Car(L)
    car.run(4, frames=[(ec.APPS_RAW, le(0) + le(0) + le(100) + b"\x01\x00", 8)])
    t = car.run(10)                                               # 페달 프레임만 멈춤
    st = L.state(t)
    assert next(s for s in st["sources"] if s["id"] == "pedal")["status"] == "stale"
    assert any(a["id"] == "W-SRC-pedal" and a["level"] == "info" for a in st["alerts"])
    assert next(s for s in st["sources"] if s["id"] == "bms")["status"] == "none"


# ---------------------------------------------------------------- 위치 · 랩 · LV

def test_position_and_laps_from_gate_crossings():
    L = live.Live(venue=VENUE)
    car = Car(L)
    t = None
    for lap in range(3):
        for k in range(60):                                       # 30 s 에 한 바퀴: x 를 −60 → +58 로 지나고 되돌아간다
            x = -60 + k * 2
            t = car.tick(gps=gps_rec(car.dev + 500 - 50, x, 1.0, 40.0, 90))
    st = L.state(t)
    assert st["laps"]["count"] == 2 and st["laps"]["last_s"] == pytest.approx(30.0, abs=0.1)
    assert st["position"]["x_m"] == pytest.approx(58, abs=0.5) and st["position"]["frame_id"] == "venue/test"
    assert tile(st, "gps.speed_kmh")["value"] == pytest.approx(40)
    assert L.track()["gate"]["half_width_m"] == 30.0 and live.Live().track() is None


def test_no_fix_gives_no_gps_values():
    L = live.Live(venue=VENUE)
    car = Car(L)
    t = car.run(4, gps=rec(3, 100, bytes(16)))
    st = L.state(t)
    assert tile(st, "gps.speed_kmh")["status"] == "none" and st["position"] is None


def test_lv_voltage_estimated_scale():
    L = live.Live()
    car = Car(L)
    t = car.run(3, analog=analog_rec(5_000, 2690))
    assert tile(L.state(t), "lv.voltage_v")["value"] == pytest.approx(12.1)


# ---------------------------------------------------------------- 그래프 · 내보내기

def test_chart_grid_since_and_gap():
    L = live.Live()
    car = Car(L)
    t1 = car.run(10, frames=[(ec.ORION_SUMMARY, summary(power_kw=12), 8)])
    st = L.state(t1)
    ts = st["chart"]["t"]
    assert np.allclose(np.diff(ts), 0.5) and st["chart"]["series"]["battery.power_kw"][-1] == pytest.approx(12)
    car.run(10, skip=True)
    t2 = car.run(2, frames=[(ec.ORION_SUMMARY, summary(power_kw=14), 8)])
    new = L.state(t2, since=ts[-1])["chart"]
    assert new["t"][0] > ts[-1] and np.allclose(np.diff(new["t"]), 0.5)
    assert None in new["series"]["battery.power_kw"] and new["series"]["battery.power_kw"][-1] == pytest.approx(14)


def test_state_is_strict_json_and_contract():
    L = live.Live(venue=VENUE)
    car = Car(L)
    t = car.run(10, frames=[(ec.ORION_SUMMARY, summary(), 8), (ec.BAMOCAR, bamo(ec.REG_T_MOTOR, 0, 4), 4)],
                gps=gps_rec(0, 1, 1, 10, 90), analog=analog_rec(0, 2700))
    st = L.state(t)
    json.dumps(st, allow_nan=False)
    assert st["contract"] == "0.16" and set(st) >= {"receiver", "device", "link", "sources", "tiles", "inverter", "position", "laps",
                                                    "alerts", "events", "chart", "source"}
    assert [x["id"] for x in st["tiles"]] == list(live.TILES)


def test_tile_scale_follows_rules(tmp_path):
    st = live.Live().state(0.0)
    sc = {x["id"]: x["scale"] for x in st["tiles"]}
    assert sc["bms.temp_max_c"] == {"min": 0.0, "max": 70.0, "warn": 55.0, "danger": 60.0, "dir": ">="}
    assert sc["battery.power_kw"]["danger"] == 80.0 and sc["battery.power_kw"]["warn"] is None
    assert sc["bms.cell_min_v"] == {"min": 2.5, "max": 4.2, "warn": None, "danger": None, "dir": None}   # 차별 문턱이 없으면 게이지 끝만
    assert sc["motor.t_motor_c"] is None and sc["gps.speed_kmh"] is None
    p = tmp_path / "car.toml"
    p.write_text('[[rule]]\nid = "W-CELL"\nchannel = "bms.cell_min_v"\nop = "<="\nwarn = 3.3\ndanger = 3.1\ntext = "셀"\n', encoding="utf-8")
    sc = {x["id"]: x["scale"] for x in live.Live(rules=p).state(0.0)["tiles"]}
    assert sc["bms.cell_min_v"]["warn"] == 3.3 and sc["bms.cell_min_v"]["dir"] == "<="


def test_live_contract_is_the_engine_contract():
    from lefdata import api
    assert live.CONTRACT == api.CONTRACT_VERSION


def test_bad_rows_unknown_topics_and_replay_flag():
    L = live.Live()
    for r in ({}, {"t_ns": 1}, {"t_ns": 1, "topic": "car/d", "b64": "!!"}, {"t_ns": "x"}):
        L.feed(r)
    L.feed(row(1, "d", b"short"))
    L.feed(row(1, "d/what", b"x"))
    L.feed({**row(2, "d/ver", b"v"), "replay": True})
    st = L.state(3)
    assert st["source"] == {"replay": True, "bad_rows": 5, "unknown_topics": 1}


def test_feed_is_fast_enough_for_a_day_file():
    L = live.Live(venue=VENUE)
    car = Car(L)
    frames = [(ec.ORION_SUMMARY, summary(), 8), (ec.ORION_VI, vi(), 8), (ec.BAMOCAR, bamo(ec.REG_SPEED, 1000, 4), 4),
              (ec.APPS_RAW, le(0) + le(0) + le(100) + b"\x01\x00", 8)] * 4
    t0 = time.perf_counter()
    for k in range(3000):
        car.tick(frames=frames, gps=gps_rec(car.dev, k % 100, 0, 30, 90), analog=analog_rec(car.dev, 2690))
    per_msg = (time.perf_counter() - t0) / 6000
    assert per_msg < 2e-3                                         # 하루(약 6만 메시지)를 2분 안에 다시 읽는다
