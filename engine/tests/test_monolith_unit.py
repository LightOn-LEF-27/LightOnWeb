"""Monolith V2 `.log` 변환 (`monolith.convert_file`) — 만든 바이트로 (데이터 없이): 시각 · GPS · 가속도 환산 · 깨진 레코드.

레코드 = 24 바이트: 0xAE · 종류 · 체크섬(u16) · 부팅 뒤 ms(u32) · 값 16 바이트 (형식의 정의는 monolith.py 맨 위).
"""

import json

import numpy as np
import pandas as pd
import pytest

from lefdata import monolith as mono

T0 = pd.Timestamp("2027-06-05 10:00:00", tz="Asia/Seoul")


def record(kind: int, ts_ms: int, payload: bytes = b"") -> np.ndarray:
    raw = np.zeros(24, "u1")
    raw[0], raw[1] = mono.MAGIC, kind
    raw[4:8] = np.frombuffer(np.uint32(ts_ms).tobytes(), "u1")
    raw[8:8 + len(payload)] = np.frombuffer(payload, "u1")
    words = raw.view("<u4").copy()
    words[0] &= 0x0000FFFF
    x = int(np.bitwise_xor.reduce(words))
    raw[2:4] = np.frombuffer(np.uint16(((x & 0xFFFF) + (x >> 16)) & 0xFFFF).tobytes(), "u1")
    return raw


def boot(ts_ms: int = 500) -> np.ndarray:
    return record(mono.T_BOOT, ts_ms, np.zeros(1, mono.BOOT_PL).tobytes()[:8] + np.uint64(int(T0.timestamp())).tobytes())


def gps(ts_ms: int, lat_nmea: int, lon_nmea: int, speed_cmh: int, course_cdeg: int, lat_dir: str = "N", lon_dir: str = "E") -> np.ndarray:
    pl = np.zeros(1, mono.GPS_PL)
    pl["lat"], pl["lon"], pl["lat_dir"], pl["lon_dir"], pl["speed"], pl["course"] = lat_nmea, lon_nmea, ord(lat_dir), ord(lon_dir), speed_cmh, course_cdeg
    return record(mono.T_GPS, ts_ms, pl.tobytes())


def gyro(ts_ms: int, values: list[int]) -> np.ndarray:
    return record(mono.T_GYRO, ts_ms, np.array(values, "<i2").tobytes())


def convert(tmp_path, records: list[np.ndarray], name: str = "a.log"):
    ev = tmp_path / "ev"
    (ev / "raw_logger").mkdir(parents=True, exist_ok=True)
    (ev / "normalized").mkdir(exist_ok=True)
    (ev / "raw_logger" / name).write_bytes(np.concatenate(records).tobytes())
    row = mono.convert_file(ev / "raw_logger" / name, ev / "normalized")
    rec = ev / "normalized" / row["recording_id"]
    return row, rec, json.loads((rec / "meta.json").read_text(encoding="utf-8"))


def test_time_starts_at_the_boot_record_and_values_follow_the_format(tmp_path):
    recs = [boot(500),
            gps(1500, 351740000, 1263420000, 3600, 9000),            # 35°17.4' N · 126°34.2' E · 36.00 km/h · 90.00°
            gps(1700, 0, 0, 0, 0),                                   # 위성을 못 잡은 레코드 — 위치를 비운다 (0 으로 두지 않는다)
            gps(1900, 351740000, 1263420000, 0, 0, "S", "W"),
            gyro(2000, [4096, -4096, 8192, 0, 16384, -16384, 0, 0])]
    row, rec, meta = convert(tmp_path, recs)
    assert row["recording_id"] == "2027-06-05_100000" and meta["time"]["boot_ts_ms"] == 500 and meta["time"]["last_ts_ms"] == 2000
    g = pd.read_parquet(rec / "gps.parquet")
    assert g["t_rel_ms"].tolist() == [1000, 1200, 1400] and g["fix"].tolist() == [True, False, True]
    assert g["t_abs"].iloc[0] == T0 + pd.Timedelta(seconds=1)
    assert g["lat"].iloc[0] == pytest.approx(35 + 17.4 / 60) and g["lon"].iloc[0] == pytest.approx(126 + 34.2 / 60)
    assert np.isnan(g["lat"].iloc[1]) and np.isnan(g["lon"].iloc[1])
    assert g["lat"].iloc[2] == pytest.approx(-(35 + 17.4 / 60)) and g["lon"].iloc[2] == pytest.approx(-(126 + 34.2 / 60))
    assert g["speed_kmh"].iloc[0] == 36.0 and g["course_deg"].iloc[0] == 90.0
    i = pd.read_parquet(rec / "imu.parquet").iloc[0]
    assert (i["ax_g"], i["ay_g"], i["az_g"]) == (1.0, -1.0, 2.0) and (i["gx_dps"], i["gy_dps"]) == (250.0, -250.0)


def test_a_broken_record_is_kept_and_flagged_but_not_trusted(tmp_path):
    """체크섬이 깨진 레코드 — 지우지 않고 valid = False. 그 종류 칸 · 시각 칸으로 부팅 수 · 기록 길이를 세지 않는다."""
    bad = gps(1700, 351740000, 1263420000, 3600, 9000)
    bad[1] = mono.T_BOOT                                             # 종류가 BOOT 로 바뀌었고 (체크섬 불일치)
    bad[4:8] = 0xFF                                                  # 시각 칸도 깨졌다
    recs = [boot(500), gps(1500, 351740000, 1263420000, 3600, 9000), bad] + [gps(1900 + 200 * k, 351740000, 1263420000, 3600, 9000) for k in range(18)]
    row, rec, meta = convert(tmp_path, recs)
    frames = pd.read_parquet(rec / "frames.parquet")
    assert row["frames"] == 21 and row["invalid"] == 1 and frames["valid"].tolist() == [True, True, False] + [True] * 18
    assert meta["time"]["last_ts_ms"] == 5300 and meta["time"]["duration_s"] == pytest.approx(4.8)
    assert mono.detect(tmp_path / "ev" / "raw_logger" / "a.log")["recording_id"] == row["recording_id"]    # 판별과 변환이 같은 기준


def test_a_log_without_a_valid_boot_record_is_refused(tmp_path):
    with pytest.raises(ValueError, match="BOOT"):
        convert(tmp_path, [gps(1500, 351740000, 1263420000, 3600, 9000)])
    with pytest.raises(ValueError, match="BOOT"):
        convert(tmp_path, [boot(500), boot(900), gps(1500, 351740000, 1263420000, 3600, 9000)])
