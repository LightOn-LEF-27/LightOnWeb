"""LEF-26 로거 통합본 변환 (`lef26`) — 만든 CSV 로 (데이터 없이): 파일 이름 규칙 · 시각 · 시계 속도 · BMS · 모터 값.

형식: 한 줄 = `HH:MM:SS.mmm,<소스>,<값>,…` (소스가 hex 면 CAN ID · 값은 데이터 바이트). 날짜는 파일 이름에서 온다.
"""

import numpy as np
import pandas as pd
import pytest

from lefdata import api, lef26

DAY = "2027-06-05"


def _stamp(ms: int) -> str:
    """10:00:00 부터 ms 뒤의 `HH:MM:SS.mmm`."""
    s, ms = divmod(ms, 1000)
    return f"10:{s // 60:02d}:{s % 60:02d}.{ms:03d}"


def lines(seconds: int = 20, pps: bool = True, sync: bool = True) -> list[str]:
    """부팅 → (GPS 동기화) → IMU 를 10 ms 마다. pps = GPS 가 매초 시계를 맞추는 로그의 모습 (밀리초가 960 을 넘지 않는다)."""
    out = ["10:00:00.000,SYSTEM,CONFIG ON"] + (["10:00:00.000,SYSTEM,RTC-UTC SYNC"] if sync else [])
    for ms in range(0, seconds * 1000, 10):
        if pps and ms % 1000 >= 930:
            continue
        out.append(f"{_stamp(ms)},IMU,0.1,0.2,1.0,1,2,3")
    return out


def write(event, name: str, rows: list[str]):
    (event / "raw_merged").mkdir(parents=True, exist_ok=True)
    (event / "raw_merged" / name).write_text("\n".join(rows) + "\n", encoding="utf-8")
    return event


def table(event, rec: str, name: str) -> pd.DataFrame:
    return pd.read_parquet(event / "normalized" / rec / f"{name}.parquet")


@pytest.mark.parametrize("name, session", [(f"{DAY}_merged.csv", DAY), (f"{DAY}_pm_merged.csv", f"{DAY}_pm"), (f"{DAY}_b_merged.csv", f"{DAY}_b")])
def test_file_names_the_uploader_makes_are_converted(tmp_path, name, session):
    """같은 날짜에 둘째로 올린 통합본은 `<날짜>_b_merged.csv` 로 놓인다 — 그 이름도 읽어야 한다."""
    rows = lef26.convert_event(write(tmp_path / "ev", name, lines()))
    assert [r["recording_id"] for r in rows] == [f"{session}_r00"] and rows[0]["session"] == session


def test_a_name_without_a_date_is_refused(tmp_path):
    with pytest.raises(ValueError):
        lef26.convert_event(write(tmp_path / "ev", "data_merged.csv", lines()))


def test_synced_clock_gives_the_time_on_the_line_and_the_date_of_the_file(tmp_path):
    ev = write(tmp_path / "ev", f"{DAY}_merged.csv", lines())
    row = lef26.convert_event(ev)[0]
    imu = table(ev, row["recording_id"], "imu")
    assert (row["clock_mode"], row["clock_f"], row["time_trusted"]) == ("gps_pps", 1.0, True)
    assert imu["t_rel_ms"].iloc[0] == 0 and imu["t_rel_ms"].is_monotonic_increasing
    assert imu["t_abs"].iloc[0] == pd.Timestamp(f"{DAY} 10:00:00", tz="Asia/Seoul")
    assert (imu["t_abs"] - imu["t_abs"].iloc[0]).dt.total_seconds().to_numpy() == pytest.approx(imu["t_rel_ms"].to_numpy() / 1000, abs=1e-3)
    assert imu["t_rel_ms"].iloc[-1] == 19_920 and set(imu["t_quality"]) == {"synced"}
    assert imu[["ax_lat_g", "ay_long_g", "az_g", "pitch_dps", "roll_dps", "yaw_dps"]].iloc[0].tolist() == [0.1, 0.2, 1.0, 1.0, 2.0, 3.0]


def test_free_running_clock_is_stretched_and_unsynced_boot_has_no_absolute_time(tmp_path):
    """밀리초가 고르게 퍼져 있으면 RTC 가 혼자 돈 것 — 경과 시간을 시계 속도로 나누고, 로거가 찍은 그대로는 frames.t_log_ms 에."""
    ev = write(tmp_path / "ev", f"{DAY}_merged.csv", lines(pps=False, sync=False))
    row = lef26.convert_event(ev)[0]
    frames = table(ev, row["recording_id"], "frames")
    data = frames[frames["kind"] == "IMU"]
    assert row["clock_mode"] == "free" and row["clock_f"] == lef26.F_FREE and not row["time_trusted"]
    assert data["t_log_ms"].iloc[-1] == 19_990
    assert data["t_rel_ms"].to_numpy() == pytest.approx(data["t_log_ms"].to_numpy() / lef26.F_FREE, abs=0.51)
    assert data["t_abs"].isna().all()                              # 부팅 때의 RTC 는 믿지 않는다 — 지어내지 않고 비운다


def test_bms_and_motor_frames_are_decoded_with_their_scales(tmp_path):
    bms = [156, 64, 30, 180, 152, 88, 0]                           # 셀 최고 4.0000 V · 30 °C · SOC 90 % · 셀 최저 3.9000 V
    good = ",".join(str(b) for b in bms + [sum(bms) & 0xFF])
    bad = ",".join(str(b) for b in bms + [(sum(bms) + 1) & 0xFF])  # 체크섬이 틀린 프레임 — 지우지 않고 valid 로 표시
    rows = lines(seconds=5)
    rows += [f"10:00:05.000,6b1,{good}", f"10:00:05.100,6b1,{bad}",
             "10:00:05.200,181,48,0,192,0",                         # 0x30 실제 속도: 0xC000 = −16384 → −50 %
             "10:00:05.300,181,102,208,86,0"]                       # 0x66 DC 버스 전압: 22224 Num
    ev = write(tmp_path / "ev", f"{DAY}_merged.csv", rows)
    rec = lef26.convert_event(ev)[0]["recording_id"]
    b = table(ev, rec, "bms")
    assert b["valid"].tolist() == [True, False]
    assert b[["cell_max_v", "cell_min_v", "temp_c", "soc_pct"]].iloc[0].tolist() == [4.0, 3.9, 30, 90.0]
    m = table(ev, rec, "motor").set_index("signal")
    assert m.loc["n_act_pct", "value"] == pytest.approx(-16384 / 32767 * 100) and m.loc["n_act_pct", "raw"] == -16384
    assert m.loc["vdc_v", "value"] == pytest.approx(22224 * lef26.BAMOCAR_VDC_V_PER_NUM)


def test_igbt_table_is_blank_outside_its_range():
    t = lef26.igbt_temp_c([lef26.IGBT_TEMP_RAW[0] - 1, lef26.IGBT_TEMP_RAW[0], lef26.IGBT_TEMP_RAW[-1], lef26.IGBT_TEMP_RAW[-1] + 1])
    assert np.isnan(t[0]) and np.isnan(t[3]) and t[1] == lef26.IGBT_TEMP_C[0] and t[2] == lef26.IGBT_TEMP_C[-1]


def test_a_second_upload_of_the_same_day_is_converted_and_the_date_guess_is_told(tmp_path):
    """올리기(ingest) → 같은 날짜의 둘째 파일도 변환되고, 날짜를 주지 않았으면 이벤트 날짜로 넣었다고 알린다."""
    data = tmp_path / "data"
    data.mkdir()
    api.configure(store_root=data / "_sessions", data_root=data)
    try:
        first, second = tmp_path / "a" / "data.csv", tmp_path / "b" / "data.csv"
        for path, rows in ((first, lines()), (second, lines(seconds=30))):
            path.parent.mkdir()
            path.write_text("\n".join(rows) + "\n", encoding="utf-8")
        one = api.get_job(api.ingest([first], {"car": "LEF-26", "place_id": "x", "date": DAY}))
        two = api.get_job(api.ingest([second], {"car": "LEF-26", "event_id": f"LEF-26/{DAY}_x"}))
    finally:
        api.configure()
    assert [(s["step"], s["state"]) for s in one["steps"]][0] == ("lef26", "done") and not any("날짜가 없는 형식" in w for w in one["warnings"])
    assert two["files"][0]["placed"].endswith(f"raw_merged/{DAY}_b_merged.csv")
    assert [(s["step"], s["state"]) for s in two["steps"]][0] == ("lef26", "done")
    assert sorted(two["recordings"]) == [f"{DAY}_b_r00"] and any("날짜가 없는 형식" in w and DAY in w for w in two["warnings"])
    assert (data / "LEF-26" / f"{DAY}_x" / "normalized" / f"{DAY}_r00" / "imu.parquet").exists()
