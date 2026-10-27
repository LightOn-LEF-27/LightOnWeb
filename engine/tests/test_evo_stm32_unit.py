"""25Evo STM32 백업 로거 변환의 작은 조각들 — 만들어 낸 값으로. 데이터 폴더가 없어도 돈다."""

import numpy as np
import pytest

from lefdata import energymeter, evo_stm32 as es


def test_rows_in_the_same_second_are_spread_evenly_in_order():
    t = es.spread_within_second(np.array([0, 0, 1000, 1000, 1000, 1000, 3000]))
    assert t.tolist() == pytest.approx([0.25, 0.75, 1.125, 1.375, 1.625, 1.875, 3.5])
    assert (np.diff(t) > 0).all()                       # 행 순서는 그대로다
    assert len(es.spread_within_second(np.array([], dtype=np.int64))) == 0


def test_true_time_is_the_logger_time_stretched():
    """로거 시계는 느리다 → 참 시간은 로거 시간보다 길다. 1000 초짜리 로그는 약 1018.5 초다."""
    t_log = np.repeat(np.arange(1000) * 1000, 3)
    t = es.true_rel_ms(t_log, es.CLOCK_F_PRIOR)
    assert t[-1] - t[0] == pytest.approx(1000 * es.CLOCK_F_PRIOR * 1000, rel=2e-3)
    assert (np.diff(t) >= 0).all()


def test_recording_names_carry_the_card_group():
    assert es.recording_id(8) == "stm32_log008" and es.recording_id(8, "day3") == "stm32_day3_log008"
    assert es.parse_recording_id("stm32_log044") == ("", 44)
    assert es.parse_recording_id("stm32_day12_log071") == ("day12", 71)
    with pytest.raises(ValueError):
        es.parse_recording_id("2027-01-02_030405_em")


def test_motor_temperature_table():
    """KTY81/210: 원시 10795 = 25 ℃. 값이 없으면(0) 온도도 없다."""
    c = es.motor_temp_c([10795, 12207, 0, 5000, 20000])
    assert c[0] == pytest.approx(25.0) and c[1] == pytest.approx(50.0)
    assert np.isnan(c[2:]).all()


def low_bytes(pack_v, current_a):
    """BMS 가 낸 0.1 V · 0.1 A 단위 16 비트 값에서 로거가 남긴 하위 바이트와, 0.1 kW 단위 전력."""
    v, i = np.round(np.asarray(pack_v) * 10).astype(int), np.round(np.asarray(current_a) * 10).astype(int)
    p = np.trunc(np.asarray(pack_v) * np.asarray(current_a) / 100).astype(int)
    return v & 0xFF, i & 0xFF, np.where(p < 0, p + 65536, p)


def test_pack_voltage_and_current_come_back_from_their_low_bytes():
    pack_v = np.array([292.5, 270.3, 251.7, 283.7, 240.0])
    current = np.array([0.3, 120.1, 215.6, -3.6, 187.4])
    vol, crt, pw = low_bytes(pack_v, current)
    v, i, p = es.restore_pack(vol, crt, pw, pack_v + 1.3)          # DC 버스는 BMS 보다 1.3 V 쯤 높게 읽는다
    assert v == pytest.approx(pack_v) and i == pytest.approx(current)
    assert p == pytest.approx([0.0, 32.4, 54.2, -1.0, 44.9])


def test_power_settles_the_voltage_when_the_bus_reading_is_from_another_instant():
    """급가속으로 전압이 처지는 중에는 DC 버스 값이 12.8 V 넘게 다를 수 있다. 전압 × 전류 = 전력으로 윗자리를 가린다."""
    vol, crt, pw = low_bytes([248.0], [205.0])
    v, i, _ = es.restore_pack(vol, crt, pw, [265.0])                # 0.3 초 전의 전압
    assert v[0] == pytest.approx(248.0) and i[0] == pytest.approx(205.0)


def test_no_bus_voltage_means_no_pack_voltage():
    """HV 가 꺼져 있으면(옛 형식이면 열이 없으면) 윗자리를 고를 기준이 없다. 전력이 0 일 때의 전류만 0 근처로 본다."""
    v, i, p = es.restore_pack([109, 109], [3, 250], [0, 300], [np.nan, 20.0])
    assert np.isnan(v).all()
    assert i[0] == pytest.approx(0.3) and np.isnan(i[1])
    assert p.tolist() == [0.0, 30.0]


def sag_wave(t, seed=0):
    """달리는 차의 팩 전압처럼: 천천히 내려가면서 가속할 때마다 2 ~ 3 초 처진다."""
    rng = np.random.default_rng(seed)
    v = 290 - 0.004 * t
    for t0, depth, width in zip(rng.uniform(0, t[-1], int(t[-1] // 12)), rng.uniform(8, 35, 400), rng.uniform(1.5, 3.5, 400)):
        v -= depth * np.exp(-0.5 * ((t - t0) / width) ** 2)
    return v


def test_clock_line_recovers_start_and_rate():
    """기준 장비(100 Hz)와 로거(초당 3 행, 1 초 해상도, 시계 1.9 % 느림): 창 맞추기로 시작 시각과 시계 속도를 되찾는다."""
    f_true, a_true = 1.0190, -1.7                      # 기준 시각 = a + f × 로거 시각
    t_ref = np.arange(0, 1500, 0.01)
    logger = np.sort(np.random.default_rng(1).uniform(5, 1400, 4200))        # 로거가 값을 읽은 순간 (로거 시계, 초)
    v_logger = np.interp(a_true + f_true * logger, t_ref, sag_wave(t_ref)) + 3.2
    t_log_ms = (np.floor(logger) * 1000).astype(np.int64)                    # 로그에 남는 것은 초 단위 TIME 뿐
    t_sig = es.true_rel_ms(t_log_ms, es.CLOCK_F_PRIOR) / 1000                # 변환기가 사전값으로 편 시간
    fit = es.fit_clock_line(t_ref, sag_wave(t_ref), t_sig, v_logger, lag0=0.0, slack_s=30.0)
    assert fit is not None and fit["n"] >= 20
    assert es.CLOCK_F_PRIOR * fit["g"] == pytest.approx(f_true, abs=3e-4)
    assert fit["a"] == pytest.approx(a_true, abs=0.3)
    f, measured = es._clock_f(fit["g"], fit["span_s"])
    assert measured and f == pytest.approx(f_true, abs=3e-4)


def test_short_spans_fall_back_to_the_prior_rate():
    assert es._clock_f(1.0004, 200.0) == (es.CLOCK_F_PRIOR, False)           # 200 초로는 0.04 % 를 가릴 수 없다
    assert es._clock_f(1.03, 2000.0) == (es.CLOCK_F_PRIOR, False)            # 말이 안 되는 속도는 버린다


# ---------------------------------------------------------------- 에너지미터

def em_record(tick, hv_v, hv_a, lv_v=12.18, temp_c=31.5, kind=energymeter.T_RECORD):
    body = np.zeros(1, energymeter.RECORD)
    body["magic"], body["type"], body["t_ms"] = energymeter.MAGIC, kind, tick
    body["ch"] = [[round(hv_v * 10), round(hv_a * 10), round(lv_v * 100), round(temp_c * 100)]]
    raw = bytearray(body.tobytes())
    raw[2:4] = int(np.bitwise_xor.reduce(np.frombuffer(bytes(raw), "<u2"))).to_bytes(2, "little")
    return bytes(raw)


def test_energymeter_checksum_is_xor_of_words():
    data = b"\0" * energymeter.HEADER_SIZE + em_record(600, 290.4, 12.5) + em_record(610, 290.3, -3.4)
    assert energymeter.checksum_ok(data, energymeter.HEADER_SIZE, energymeter.REC_SIZE, 2).all()
    broken = bytearray(data)
    broken[energymeter.HEADER_SIZE + 9] ^= 0x01                              # 전압 바이트 한 비트
    assert energymeter.checksum_ok(bytes(broken), energymeter.HEADER_SIZE, energymeter.REC_SIZE, 2).tolist() == [False, True]


def test_energymeter_header_word_is_protocol_version_on_old_firmware():
    head = bytearray(32)
    head[0], head[1] = energymeter.MAGIC, energymeter.T_HEADER
    head[4:8] = (571).to_bytes(4, "little")
    head[8:20] = np.array([0x11112222, 0x3333444A, 0x55556666], "<u4").tobytes()
    head[24:32] = bytes([27, 1, 2, 3, 4, 5]) + (678).to_bytes(2, "little")
    head[20:24] = bytes([2, 0, 0, 0])
    h = energymeter.parse_header(bytes(head))
    assert h["device_id"] == "11112222-3333444A-55556666" and h["tick_ms"] == 571
    assert h["start"].strftime("%Y-%m-%d %H:%M:%S.%f") == "2027-01-02 03:04:05.678000"
    assert h["bytes_20_23"]["protocol_version"] == 2
    head[20:24] = np.array([125, -40], "<i2").tobytes()                       # v1.8.0 부터는 영점 보정값이 든다
    h = energymeter.parse_header(bytes(head))
    assert (h["bytes_20_23"]["v_cal_v"], h["bytes_20_23"]["c_cal_a"]) == (0.125, -0.04)
