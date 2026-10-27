"""LEF-26 로거 통합본 CSV 변환 검증.

형식 · 물리에서 늘 참인 것(원본 줄 전부 보존, 리튬 셀 전압 범위, 방위 0 ~ 360, 믿을 수 있는 시각끼리 겹치지 않음, 회전수 % 범위,
NTC 표 격자점, 써미스터 최저 ≤ 평균 ≤ 최고, 프레임을 내용으로 가르기)은 어느 날의 로그에도 돈다.
조사로 이미 알고 있는 값과 그 이유는 기대값 파일 `lef26_golden.toml` 에 있다. 먼저 `lefdata lef26` 로 변환한 뒤 실행한다.
"""

import csv
import datetime as dt
import json

import numpy as np
import pandas as pd
import pyarrow.compute as pc
import pyarrow.dataset as ds
import pyarrow.parquet as pq
import pytest

from expected import Expected
from lefdata import lef26, paths

E = Expected("lef26_golden")
NORM = paths.LEF26_EVENT / "normalized"
pytestmark = pytest.mark.skipif(not (NORM / "recordings.csv").exists(),
                                reason="normalized/recordings.csv 없음 — 먼저 lefdata lef26 실행")
SESSIONS = E.cases("sessions")


def index():
    return list(csv.DictReader((NORM / "recordings.csv").open(encoding="utf-8-sig")))


def meta(rec_id):
    return json.loads((NORM / rec_id / "meta.json").read_text(encoding="utf-8"))


def recs(session):
    return [r for r in index() if r["session"] == session]


def dataset(session, name):
    return ds.dataset(sorted(str(NORM / r["recording_id"] / f"{name}.parquet") for r in recs(session)))


def everywhere(name):
    """로거 레코딩 전부의 같은 표 (같은 폴더의 온보드 영상 레코딩은 빼고)."""
    return ds.dataset(sorted(str(NORM / r["recording_id"] / f"{name}.parquet") for r in index()
                             if (NORM / r["recording_id"] / f"{name}.parquet").exists()))


@E.need
def test_L1_recording_count_equals_boot_events():
    assert [len(recs(s)) for s in E["sessions"]] == E["recordings_per_session"]


def test_L2_frames_table_keeps_every_counted_line():
    """색인에 적은 줄 수와 frames 표의 행 수가 레코딩마다 같다."""
    for r in index():
        assert int(r["frames"]) == pq.ParquetFile(NORM / r["recording_id"] / "frames.parquet").metadata.num_rows, r["recording_id"]


@pytest.mark.parametrize("session", SESSIONS)
def test_L2_every_source_line_is_kept(session):
    assert sum(int(r["frames"]) for r in recs(session)) == E["lines"][session]


@pytest.mark.parametrize("session", SESSIONS)
def test_duplicate_count(session):
    assert sum(int(r["dup"]) for r in recs(session)) == E["dups"][session]


@pytest.mark.parametrize("session", SESSIONS)
def test_L3_bms_checksum_matches_split_files(session):
    total = sum(meta(r["recording_id"])["counts"]["bms_rows"] for r in recs(session))
    fails = sum(round(meta(r["recording_id"])["quality"]["bms_checksum_fail_rate"] *
                      meta(r["recording_id"])["counts"]["bms_rows"])
                for r in recs(session) if meta(r["recording_id"])["counts"]["bms_rows"])
    assert total == E["bms_total"][session]
    assert total - fails == E["bms_pass"][session]


def _plausible_bms(session):
    t = dataset(session, "bms").to_table().filter(pc.field("valid"))
    # 8비트 합 체크섬은 약해서 깨진 프레임이 통과하기도 한다 → 리튬 셀 물리 범위로 한 번 더 거른다
    return t.filter((pc.field("cell_min_v") >= 2.0) & (pc.field("cell_max_v") <= 4.3)
                    & (pc.field("cell_min_v") <= pc.field("cell_max_v")))


@E.need
def test_L4_lowest_cell():
    for session, v in E["bms.lowest_cell_v"].items():
        assert round(pc.min(_plausible_bms(session)["cell_min_v"]).as_py(), 4) == v, session


@E.need
def test_L5_max_temp():
    assert pc.max(_plausible_bms(E["bms.max_temp"]["session"])["temp_c"]).as_py() == E["bms.max_temp"]["temp_c"]


@E.need
def test_L6_can_0x80_kept():
    """분할본에서 누락됐던 0x80 이 통합본 변환에서는 전부 남아 있어야 한다."""
    s = E["sources.can_0x80"]
    assert sum(meta(r["recording_id"])["counts"]["by_source"].get("0x80", 0) for r in recs(s["session"])) == s["rows"]


@E.need
def test_ped_source_kept():
    s = E["sources.ped"]
    assert sum(meta(r["recording_id"])["counts"]["by_source"].get("PED", 0) for r in recs(s["session"])) == s["rows"]


@E.need
def test_time_trusted_recordings_expected():
    trusted = [r["recording_id"] for r in index() if r["time_trusted"] == "True"]
    assert len(trusted) == E["time.trusted"]
    for rid in E["time.untrusted_examples"]:
        assert rid not in trusted


def test_trusted_recordings_do_not_overlap_in_time():
    prev_end = None
    for r in index():
        if r["time_trusted"] != "True":
            continue
        s, e = dt.datetime.fromisoformat(r["t_abs_start"]), dt.datetime.fromisoformat(r["t_abs_end"])
        assert s <= e, r["recording_id"]
        if prev_end is not None:
            assert s >= prev_end - dt.timedelta(seconds=2), r["recording_id"]
        prev_end = e


@E.need
def test_trusted_times_are_daytime_kst():
    lo, hi = E.range("time.daytime_hours")
    for r in index():
        if r["time_trusted"] == "True":
            for k in ("t_abs_start", "t_abs_end"):
                assert lo <= dt.datetime.fromisoformat(r[k]).hour <= hi, (r["recording_id"], r[k])


def test_untrusted_recordings_have_no_abs_time():
    for r in index():
        if r["time_trusted"] != "True":
            assert meta(r["recording_id"])["quality"]["t_abs_null_rate"] == 1.0, r["recording_id"]


@E.need
def test_backcorrected_example():
    """부팅 후 동기화 전 구간을 동기화 기준으로 역보정 — 다음 레코딩보다 앞서야 한다."""
    a, b = E["time.backcorrected"]
    m0, m1 = meta(a), meta(b)
    assert m0["time"]["segments_by_quality"] == E["time.backcorrected_segments"]
    assert m0["time"]["t_abs_start"].startswith(E["time.backcorrected_start"])
    assert m0["time"]["t_abs_end"] < m1["time"]["t_abs_start"]


@E.need
def test_config_fail_event_stays_in_its_recording():
    """레코딩 끝 이벤트가 다음 레코딩 시각을 가져가면 안 된다."""
    a, b = E["time.config_fail"]
    assert meta(a)["time"]["t_abs_end"] < meta(b)["time"]["t_abs_start"]


@E.need
def test_imu_accel_is_in_g():
    az = dataset(E["imu.session"], "imu").to_table(columns=["az_g"])["az_g"]
    lo, hi = E.range("imu.az_mean_g")
    assert lo < pc.mean(az).as_py() < hi


# ---------------------------------------------------------------- 펌웨어로 확정한 채널 정의

def test_F1_column_names_follow_physical_meaning():
    """칼럼 이름은 센서 칩 축이 아니라 물리적 의미를 따른다 (축을 반대로 읽는 사고 방지)."""
    assert {"ax_lat_g", "ay_long_g", "az_g", "pitch_dps", "roll_dps", "yaw_dps"} <= set(everywhere("imu").schema.names)
    assert "course_deg" in everywhere("gps").schema.names
    assert {"lin_rl_mm", "lin_rr_mm"} <= set(everywhere("lin").schema.names)


def test_F2_gps_course_is_a_bearing():
    """GPS 세 번째 값은 진북 기준 방위다 — 0 ~ 360 을 벗어나면 안 된다."""
    c = everywhere("gps").to_table(columns=["course_deg"])
    assert 0 <= pc.min(c["course_deg"]).as_py() and pc.max(c["course_deg"]).as_py() <= 360


@E.need
def test_F3_linear_sensor_is_within_stroke():
    """(adc - bias) x 행정 / 4095 → 0 ~ 행정 mm 를 벗어날 수 없다."""
    t = dataset(E["lin.session"], "lin").to_table(columns=["lin_rl_mm", "lin_rr_mm"])
    for c in ("lin_rl_mm", "lin_rr_mm"):
        assert 0 <= pc.min(t[c]).as_py() and pc.max(t[c]).as_py() <= E["lin.stroke_mm"]


@E.need
def test_F4_pedal_channel():
    t = everywhere("ped").to_table(columns=["ped"])
    assert t.num_rows == E["pedal.rows"]
    assert (pc.min(t["ped"]).as_py() == pc.max(t["ped"]).as_py() == 0) is E["pedal.all_zero"]


@E.need
def test_F5_gyro_yaw_is_plausible_for_a_formula_car():
    """요레이트가 자이로 설정 범위 안이면서 정지 시 bias 가 제거돼 있어야 한다."""
    t = dataset(E["imu.session"], "imu").to_table(columns=["yaw_dps"])["yaw_dps"]
    rng = E["imu.gyro_range_dps"]
    assert abs(pc.min(t).as_py()) < rng and abs(pc.max(t).as_py()) < rng
    assert abs(pc.mean(t).as_py()) < E["imu.yaw_bias_max_dps"]


@E.need
def test_F6_mag_is_not_in_microtesla():
    """지자기 크기가 µT 처럼 보이지만 아니다 — 그 '그럴듯함' 을 테스트로 박아 둔다 (lef26.MAG_TO_UT 로 되돌린다)."""
    t = dataset(E["imu.session"], "mag").to_table(columns=["mx", "my", "mz"]).to_pandas()
    mag = np.sqrt(t.mx ** 2 + t.my ** 2 + t.mz ** 2).median()
    lo, hi = E.range("imu.mag_raw")
    assert lo < mag < hi
    lo, hi = E.range("imu.mag_ut")
    assert lo < mag * lef26.MAG_TO_UT < hi


# ---------------------------------------------------------------- 모터 컨트롤러 0x181 (BAMOCAR)

def _motor(session, signal):
    return dataset(session, "motor").to_table(filter=pc.field("signal") == signal)


@E.need
def test_motor_decode_rate():
    """0x181 프레임 대부분이 자동 전송 레지스터로 해독돼야 한다."""
    tot = sum(meta(r["recording_id"])["counts"]["motor_rows_nodup"] for r in index())
    dec = sum(meta(r["recording_id"])["counts"]["motor_decoded"] for r in index())
    assert tot == E["motor.rows_nodup"]
    assert dec / tot > E["motor.decoded_share_min"]


@E.need
def test_motor_signals_are_physical():
    s = E["motor.session"]
    iq = _motor(s, "iq_act_a")["value"]
    assert -E["motor.iq_abs_max_a"] <= pc.min(iq).as_py() and pc.max(iq).as_py() <= E["motor.iq_abs_max_a"]
    vdc = _motor(s, "vdc_v")["value"]
    assert 0 <= pc.min(vdc).as_py() and pc.max(vdc).as_py() < E["motor.vdc_max_v"]


def test_motor_speed_percent_is_within_definition():
    """회전수는 최대 회전수(N-100 %)에 대한 % 다 — ±100 을 넘을 수 없다."""
    n = everywhere("motor").to_table(filter=pc.field("signal") == "n_act_pct", columns=["value"])["value"]
    assert max(abs(pc.min(n).as_py()), abs(pc.max(n).as_py())) <= 100


def test_F8_igbt_temperature_matches_bamocar_table():
    """제조사 NTC 표(BaC2/NTC-infin)의 격자점이 그대로 재현돼야 한다."""
    assert lef26.igbt_temp_c(18387) == pytest.approx(20.0)
    assert lef26.igbt_temp_c(20793) == pytest.approx(45.0)
    assert lef26.igbt_temp_c(17151) == pytest.approx(0.0)
    assert np.isnan(lef26.igbt_temp_c(5000))          # 표 범위 밖은 추측하지 않는다


@E.need
def test_F9_igbt_temperature_is_physical():
    """실측이 표 범위 안에 있고, 모터 온도와 같은 대역이어야 한다 (범위 밖이면 NaN 이 섞여 min / max 가 깨진다)."""
    s = E["motor.session"]
    ig, mo = _motor(s, "t_igbt_c")["value"], _motor(s, "t_motor_c")["value"]
    lo, hi = E.range("motor.igbt_c")
    assert lo < pc.min(ig).as_py() and pc.max(ig).as_py() < hi
    assert abs(pc.mean(ig).as_py() - pc.mean(mo).as_py()) < E["motor.igbt_minus_motor_c_max"]


@E.need
def test_F7_motor_speed_within_physical_max():
    n = everywhere("motor").to_table(filter=pc.field("signal") == "n_act_pct", columns=["value"])["value"]
    pct = max(abs(pc.min(n).as_py()), abs(pc.max(n).as_py()))
    assert pct / 100 * lef26.N_MAX_RPM <= E["motor.rpm_physical_max"]


@E.need
def test_motor_vdc_tracks_bms_cell_voltage():
    """서로 다른 장치의 독립 측정: 컨트롤러 DC 버스 전압과 BMS 셀 전압이 같이 움직여야 한다."""
    rid = E["vdc_vs_bms.recording"]
    m = pq.read_table(NORM / rid / "motor.parquet").to_pandas()
    v = m[(m.signal == "vdc_v") & (m.value > 150)][["t_rel_ms", "value"]].sort_values("t_rel_ms")
    b = pq.read_table(NORM / rid / "bms.parquet").to_pandas()
    b = b[b.valid & (b.cell_min_v > 2.0) & (b.cell_max_v < 4.3)].sort_values("t_rel_ms")
    x = pd.merge_asof(v, b[["t_rel_ms", "cell_min_v", "cell_max_v"]], on="t_rel_ms", tolerance=1000, direction="nearest").dropna()
    assert len(x) > E["vdc_vs_bms.rows_min"]
    assert np.corrcoef(x.value, x.cell_min_v)[0, 1] > E["vdc_vs_bms.corr_min"]
    ratio = (x.value / ((x.cell_min_v + x.cell_max_v) / 2)).median()
    lo, hi = E.range("vdc_vs_bms.ratio")
    assert lo < ratio < hi          # = 직렬 셀 수


# ---------------------------------------------------------------- 시계 모델 v2 · 써미스터 모듈

def test_C1_only_free_running_clocks_are_rescaled():
    """시계 속도는 혼자 돈 레코딩에만 바로잡는다. 나머지는 f = 1."""
    assert all(float(r["clock_f"]) == 1.0 for r in index() if r["clock_mode"] != "free")


@E.need
def test_C1_clock_mode_is_decided_from_the_log():
    modes, basis = {}, {}
    for r in index():
        modes[r["clock_mode"]] = modes.get(r["clock_mode"], 0) + 1
        basis[r["clock_f_basis"]] = basis.get(r["clock_f_basis"], 0) + 1
    assert modes == E["clock.modes"] and basis == E["clock.basis"]
    free = [float(r["clock_f"]) for r in index() if r["clock_mode"] == "free"]
    lo, hi = E.range("clock.free_f")
    assert lo < min(free) and max(free) < hi


@E.need
def test_C2_elapsed_time_is_divided_by_clock_rate():
    """t_rel_ms = 로거 경과시간(t_log_ms) ÷ f. 로거가 찍은 그대로는 frames 에 남는다."""
    for rid, expect in E["clock.f"]:
        f = meta(rid)["time"]["clock"]["f"]
        assert f == pytest.approx(expect, abs=E["clock.f_abs"])
        t = pq.read_table(NORM / rid / "frames.parquet", columns=["t_rel_ms", "t_log_ms"]).to_pandas()
        assert np.abs(t["t_rel_ms"] - t["t_log_ms"] / f).max() <= 0.5
    m = meta(E["clock.duration_recording"])
    start, end = (dt.datetime.fromisoformat(m["time"][k]) for k in ("t_abs_start", "t_abs_end"))
    assert (end - start).total_seconds() == E.approx("clock.duration_s")


def test_C3_thermistor_legacy_frames_all_pass_checksum():
    assert sum(meta(r["recording_id"])["counts"]["thermistor_legacy_checksum_fail"] for r in index()) == 0


@E.need
def test_C3_thermistor_frames_are_split_from_bms():
    """확장 ID 프레임을 내용으로 가려 써미스터 표로 보내고 BMS 표에서 뺀다."""
    assert sum(int(r["thermistor_rows"]) for r in index()) == E.approx("thermistor.rows")
    for session in E["sessions"]:
        t = dataset(session, "bms").to_table().filter(pc.field("valid"))
        bad = t.filter((pc.field("cell_min_v") < 2.0) | (pc.field("cell_max_v") > 4.3))
        assert bad.num_rows <= E["thermistor.bms_bad_rows_max"], session


def test_C4_thermistor_table_is_consistent():
    t = everywhere("thermistor").to_table().to_pandas()
    assert set(t["source"]) <= {"module_to_bms", "single", "legacy_lowest", "legacy_highest"}
    m = t[t["source"] == "module_to_bms"]
    assert len(m) and (m["lowest_c"] <= m["average_c"]).all() and (m["average_c"] <= m["highest_c"]).all()


@E.need
def test_C4_thermistor_table_golden():
    t = dataset(E["thermistor.session"], "thermistor").to_table().to_pandas()
    m, s = t[t["source"] == "module_to_bms"], t[t["source"] == "single"]
    assert set(t["source"]) == {"module_to_bms", "single", "legacy_lowest", "legacy_highest"}
    assert m["highest_c"].max() == E["thermistor.highest_max_c"] and m["lowest_c"].min() == E["thermistor.lowest_min_c"]
    assert sorted(s["thermistor_id"].unique()) == E["thermistor.ids"]
    assert not t["fault"].fillna(False).any()


def test_C5_frame_classifier():
    frames = np.array([
        [0xA0, 0x2F, 0x23, 0xBC, 0xA0, 0x1B, 0x00, 0x69],     # BMS: 4.1007 V, 35 ℃, SOC 94 %
        [0x00, 0x27, 0x2F, 0x2B, 0x20, 0x43, 0x47, 0x6C],     # 모듈 → BMS: 최저 39 · 최고 47 · 평균 43, 32 개
        [0x00, 0x4B, 0x23, 0x4B, 0x21, 0x23, 0x37, 0x2B],     # 개별: 75 번 = 35 ℃
        [0xF3, 0x00, 0x80, 0xF3, 0x00, 0x40, 0x1E, 0x90],     # 주소 선언
        [0x92, 0xB8, 0x1B, 0x56, 0x92, 0x91, 0x00, 0xDD],     # 체크섬이 1 어긋난 BMS 프레임
    ])
    assert lef26.classify_frames(frames).tolist() == [lef26.KIND_BMS, lef26.KIND_MODULE, lef26.KIND_SINGLE,
                                                      lef26.KIND_CLAIM, lef26.KIND_OTHER]


# ---------------------------------------------------------------- 온보드 영상 파이프라인 (대회 이벤트)

VIDEO_EVENT = paths.LEF26_FSK
video_only = pytest.mark.skipif(not (VIDEO_EVENT / "normalized").exists(), reason="영상 변환 결과 없음 — lefdata gopro 먼저 실행")


@video_only
def test_V1_video_gps_is_a_normal_source():
    """영상 GPS 가 다른 로거와 같은 스키마로 들어와야 기존 분석 코드가 그대로 돈다."""
    p = next((VIDEO_EVENT / "normalized").glob("*/gps.parquet"))
    assert {"t_rel_ms", "t_abs", "t_quality", "fix", "lat", "lon", "speed_kmh", "course_deg"} <= set(pq.read_schema(p).names)


@video_only
@E.need
def test_V2_race_laps_match_manual_analysis():
    laps_df = pd.read_csv(VIDEO_EVENT / "derived" / "laps.csv", encoding="utf-8-sig")
    runs = pd.read_csv(VIDEO_EVENT / "derived" / "runs.csv", encoding="utf-8-sig")
    assert len(runs) == E["race_video.runs"] and len(laps_df) == E["race_video.laps"]
    lo, hi = E.range("race_video.best_s")
    assert lo < laps_df["lap_time_s"].min() < hi
    assert (laps_df["lap_time_s"] < E["race_video.lap_max_s"]).all()


@video_only
@E.need
def test_V3_stints_are_consistent_with_endurance():
    runs = pd.read_csv(VIDEO_EVENT / "derived" / "runs.csv", encoding="utf-8-sig")
    lo, hi = E.range("race_video.total_m")
    assert lo < runs["distance_m"].sum() < hi
