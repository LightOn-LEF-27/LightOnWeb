"""25Evo 대회 데이터(에너지미터 · STM32 백업 로거 · Orion) 변환 · 정렬 검증.

형식에서 늘 참인 것(파일 수 = 레코딩 수, 헤더 시각 = 파일명, 체크섬, 100 Hz, 원본 줄 전부 보존, 전압 × 전류 = 전력, 정렬된 레코딩끼리 겹치지 않음)은
어느 행사의 같은 장비 로그에도 돈다. 답을 아는 값과 그 이유는 기대값 파일 `fsk_evo_golden.toml` 에 있다.
먼저 `lefdata energymeter` → `lefdata evo-stm32` 를 실행한다.
"""

import datetime as dt
import json

import numpy as np
import pandas as pd
import pyarrow.parquet as pq
import pytest

from expected import Expected
from lefdata import clocks, energymeter, evo_stm32, paths

E = Expected("fsk_evo_golden")
EV = paths.LEF25EVO_FSK
NORM = EV / "normalized"
ALIGN = EV / "derived" / "stm32_alignment.csv"

pytestmark = pytest.mark.skipif(not ALIGN.exists(), reason="먼저 lefdata energymeter / lefdata evo-stm32 실행")


def meta(rec):
    return json.loads((NORM / rec / "meta.json").read_text(encoding="utf-8"))


def ems():
    return sorted(p.name for p in NORM.glob("*_em"))


def align_table():
    return pd.read_csv(ALIGN).set_index("recording_id")


# ---------------------------------------------------------------- 에너지미터

def test_em_every_nonempty_file_becomes_a_recording():
    raw = [p for p in (EV / "raw_energymeter").glob("*.log") if p.stat().st_size > energymeter.HEADER_SIZE]
    assert len(raw) == len(ems())


@E.need
def test_em_recording_count():
    assert len(ems()) == E["meter.recordings"]


def test_em_header_time_equals_filename_time():
    """헤더 시각 해독이 맞다는 근거 — 파일명(미터가 따로 붙인 이름)과 ms 까지 같다."""
    assert ems() and all(meta(r)["time"]["filename_time_match"] for r in ems())


@E.need
def test_em_device_id():
    assert sorted({meta(r)["source"]["device_id"] for r in ems()}) == E["meter.device_ids"]


def test_em_is_100hz_and_monotonic():
    """이 에너지미터는 10 ms 마다 한 레코드를 쓴다."""
    for r in ems():
        m = meta(r)
        assert m["time"]["median_dt_ms"] == 10 and m["time"]["ts_monotonic"], r


@pytest.mark.parametrize("case", E.cases("meter.cases"), ids=lambda c: c["recording"])
def test_em_golden_values(case):
    m = meta(case["recording"])
    assert m["counts"]["records"] == case["records"]
    assert m["summary"]["voltage_max_v"] == pytest.approx(case["voltage_max_v"], abs=0.05)
    assert m["summary"]["current_max_a"] == pytest.approx(case["current_max_a"], abs=0.05)


@E.need
def test_em_hv_on_count():
    assert sum(meta(r)["summary"]["hv_on"] for r in ems()) == E["meter.hv_on"]


def test_em_matches_the_published_format():
    """공개된 펌웨어의 정의로 읽으면 레코드 · 헤더 체크섬이 전부 맞는다."""
    for r in ems():
        m = meta(r)
        assert m["counts"]["checksum_fail"] == 0 and m["counts"]["invalid"] == 0 and m["source"]["header_checksum_ok"], r


@E.need
def test_em_format_golden():
    assert {meta(r)["source"]["header_bytes_20_23"]["protocol_version"] for r in ems()} == {E["meter.protocol_version"]}
    assert sum(meta(r)["counts"]["records"] for r in ems()) == E["meter.records_total"]
    e = pq.read_table(NORM / E["meter.lv_recording"] / "energy.parquet").to_pandas()
    assert e["lv_voltage_v"].median() == E.approx("meter.lv_median_v")
    assert e["temp_c"].between(*E.range("meter.temp_c")).all()


def test_energy_integration_on_known_shape():
    """300 V × 100 A 를 1 시간 = 30 kWh. 회생(음수 전류)은 따로 센다."""
    t = np.arange(0, 3_600_001, 1000)
    v = np.full(len(t), 300.0)
    dis, chg = energymeter.energy_wh(t, v, np.full(len(t), 100.0))
    assert dis == pytest.approx(30_000) and chg == 0
    dis, chg = energymeter.energy_wh(t, v, np.full(len(t), -10.0))
    assert dis == 0 and chg == pytest.approx(3_000)


# ---------------------------------------------------------------- STM32

def _stm32_rows():
    raw = sum(len(pd.read_csv(p, comment="#")) for p in (EV / "raw_stm32").glob("*.csv"))
    kept = sum(pq.ParquetFile(p / "frames.parquet").metadata.num_rows for p in NORM.glob("stm32_log*"))
    return raw, kept


def test_stm32_all_rows_kept():
    raw, kept = _stm32_rows()
    assert raw == kept > 0


@E.need
def test_stm32_row_count():
    assert _stm32_rows()[1] == E["stm32.rows"]


@E.need
def test_stm32_dc_bus_scale_matches_energy_meter():
    """정렬된 구간에서 두 장비의 DC 버스 전압 차이가 일정하다 (배율이 맞는다)."""
    assert pd.read_csv(ALIGN)["offset_v"].iloc[0] == E.approx("stm32.dc_offset_v")


@E.need
def test_stm32_rpm_equals_logger_rpm_column():
    """rpm = HV_RPM / 32767 × HV_RPM_MAX 가 로거 자체 계산값 HV_RPM_Actual 과 같다 (드물게 몇 rpm 어긋난다)."""
    rec = E["stm32.rpm_recording"]
    f = pq.read_table(NORM / rec / "frames.parquet").to_pandas()
    m = pq.read_table(NORM / rec / "motor.parquet").to_pandas()
    d = (m["rpm"].round() - f["HV_RPM_Actual"]).abs()
    assert (d <= 1).mean() >= 0.995 and d.max() <= 10


@E.need
def test_stm32_truncated_bms_columns_are_restored():
    """하위 바이트만 남은 팩 전압 · 전류를 되살린 값이 에너지미터와 맞고, 전압 × 전류 = 전력이다."""
    b = pq.read_table(NORM / E["restored.stm32"] / "bms.parquet").to_pandas().dropna(subset=["pack_v", "current_a"])
    e = pq.read_table(NORM / E["restored.meter"] / "energy.parquet",
                      columns=["t_abs", "voltage_v", "current_a"]).to_pandas().sort_values("t_abs")
    e = e.set_index("t_abs").rolling("500ms", center=True).mean().reset_index()
    b["t_abs"] = b["t_abs"].astype(e["t_abs"].dtype)
    j = pd.merge_asof(b.sort_values("t_abs"), e, on="t_abs", direction="nearest", tolerance=pd.Timedelta("200ms"),
                      suffixes=("", "_em")).dropna(subset=["voltage_v"])
    assert len(j) > E["restored.matched_rows_min"]
    assert (j["pack_v"] - j["voltage_v"]).median() == E.approx("restored.bms_minus_meter_v")
    assert np.corrcoef(j["current_a"], j["current_a_em"])[0, 1] > E["restored.current_corr_min"]
    assert np.polyfit(j["current_a_em"], j["current_a"], 1)[0] == E.approx("restored.current_slope")
    assert ((j["pack_v"] * j["current_a"] / 1000 - j["power_kw"]).abs() < 0.2).mean() > 0.98      # 전압 × 전류 = 전력
    t = (j["t_abs"] - j["t_abs"].iloc[0]).dt.total_seconds().to_numpy()
    wh = lambda p: np.trapezoid(p, t) / 3.6                                                       # noqa: E731
    assert wh(j["power_kw"].to_numpy()) / wh((j["voltage_v"] * j["current_a_em"] / 1000).to_numpy()) == E.approx("restored.energy_ratio")


@E.need
def test_stm32_motor_temperature_is_plausible():
    m = pq.read_table(NORM / E["stm32.motor_temp_recording"] / "motor.parquet").to_pandas()
    lo, hi = E["stm32.motor_temp_c"]
    assert m["t_motor_c"].min() == pytest.approx(lo["value"], abs=lo["abs"])
    assert m["t_motor_c"].max() == pytest.approx(hi["value"], abs=hi["abs"])


# ---------------------------------------------------------------- 로거 시계

def test_stm32_short_logs_use_the_prior_clock_rate():
    al = align_table()
    prior = al.loc[al["clock_basis"] == "prior", "clock_f"]
    assert prior.eq(evo_stm32.CLOCK_F_PRIOR).all()


@E.need
def test_stm32_clock_runs_slow():
    """참 시간 = f × 로거 TIME. 길게 달린 로그에서 잰 f 가 한 범위로 모인다."""
    al = align_table()
    measured = al[al["clock_basis"] == "em_voltage"]["clock_f"]
    assert sorted(measured.index) == E["clock.measured"]
    assert measured.between(*E.range("clock.f")).all()
    assert (al.loc[E["clock.prior"], "clock_basis"] == "prior").all()


@E.need
def test_stm32_and_meter_share_a_power_cycle():
    """바로잡은 뒤에는 두 장비가 같이 켜지고 같이 꺼진다."""
    al = align_table()
    for rec, em in E["stm32_to_meter"].items():
        off_stm = al.loc[rec, "em_a_s"] + meta(rec)["time"]["duration_s"]       # 에너지미터 틱으로 본 STM32 의 마지막 행
        assert off_stm == pytest.approx(meta(em)["time"]["last_tick_ms"] / 1000, abs=E["clock.off_abs_s"]), rec
        lo, hi = E.range("clock.on_s")
        assert lo <= al.loc[rec, "em_a_s"] <= hi, rec
        assert al.loc[rec, "em_rms_s"] < E["clock.em_rms_max_s"]
    f = pq.read_table(NORM / E["clock.span_recording"] / "frames.parquet", columns=["t_rel_ms", "t_log_ms"]).to_pandas()
    assert f["t_log_ms"].iloc[-1] == E["clock.t_log_last_ms"]
    span = lambda c: f[c].iloc[-1] - f[c].iloc[0]                                 # noqa: E731
    assert span("t_rel_ms") / span("t_log_ms") == E.approx("clock.span_ratio")


@E.need
def test_monolith_and_meter_share_a_power_cycle():
    pc = clocks.power_cycles(EV)
    assert len(pc) == E["monolith_cycles.pairs"]
    assert pc["off_diff_s"].between(*E.range("monolith_cycles.off_diff_s")).all()
    assert pc["em_minus_monolith_s"].between(*E.range("monolith_cycles.em_minus_monolith_s")).all()
    assert E["monolith_cycles.not_in_monolith"] not in set(pc["energymeter"])


# ---------------------------------------------------------------- 정렬

@E.need
def test_alignment_matches_duration_and_soc_guess():
    al = align_table()
    for rec, em in E["stm32_to_meter"].items():
        assert al.loc[rec, "applied"] and al.loc[rec, "em_recording"] == em, rec
        assert al.loc[rec, "err_v"] < E["alignment.err_v_max"]


@E.need
def test_alignment_only_applies_where_confident():
    """짧은 · 평평한 로그는 보류해야 한다."""
    al = pd.read_csv(ALIGN)
    applied = al[al["applied"]]
    assert dict(zip(applied["recording_id"], applied["em_recording"])) == E["stm32_to_meter"]


def test_aligned_recordings_follow_log_order_without_overlap():
    al = pd.read_csv(ALIGN)
    applied = sorted(al.loc[al["applied"], "recording_id"])
    rows = sorted((meta(r)["time"]["t_abs_start"], meta(r)["time"]["duration_s"], r) for r in applied)
    starts = [(dt.datetime.fromisoformat(t), d, r) for t, d, r in rows]
    assert applied and [r for _, _, r in starts] == applied
    for (t1, d1, _), (t2, _, _) in zip(starts, starts[1:]):
        assert t1 + dt.timedelta(seconds=d1) <= t2


@E.need
def test_aligned_tables_have_t_abs_and_quality():
    m = pq.read_table(NORM / E["alignment.aligned_example"] / "motor.parquet").to_pandas()
    assert m["t_abs"].notna().all() and set(m["t_quality"]) == {"aligned_em"}
    b = pq.read_table(NORM / E["alignment.unaligned_example"] / "bms.parquet").to_pandas()
    assert b["t_abs"].isna().all() and set(b["t_quality"]) == {"none"}


def test_best_match_recovers_synthetic_lag():
    """합성 신호: 기울어진 전압 + 처짐 몇 개. 알려준 lag 를 ±1 s 로 되찾아야 한다."""
    rng = np.random.default_rng(0)
    ref_s = np.arange(2000)
    ref = 290 - ref_s * 0.003
    for k in rng.integers(0, 2000, 40):
        ref[k:k + 5] -= 15
    lag = 777
    sig_s = np.arange(600)
    sig = ref[sig_s + lag] + 4.0 + rng.normal(0, 0.2, 600)
    got = evo_stm32.best_match(ref_s, ref, sig_s, sig, offset=4.0)
    assert abs(got[0] - lag) <= 1 and got[3] < 0.5


# ---------------------------------------------------------------- Orion BMS Connect (Utility CSV)

def test_orion_filename_utc_matches_kst_time_column():
    """파일명은 UTC, Time 열은 KST — 같은 순간이어야 한다 (파일 생성과 첫 기록 사이 1 s 정도는 허용)."""
    recs = sorted(NORM.glob("*_orion"))
    assert recs and all(abs(meta(p.name)["time"]["filename_utc_diff_s"]) <= 2 for p in recs)


@E.need
def test_orion_recording_count():
    assert len(list(NORM.glob("*_orion"))) == E["orion.recordings"]


@E.need
def test_orion_endurance_golden():
    rec = E["orion.endurance"]
    m = meta(rec)
    assert m["time"]["duration_s"] == E["orion.endurance_s"]
    assert [m["summary"]["soc_start_pct"], m["summary"]["soc_end_pct"]] == E["orion.endurance_soc"]
    assert m["summary"]["temp_max_c"] == E["orion.endurance_temp_max_c"]
    b = pq.read_table(NORM / rec / "bms.parquet").to_pandas()
    assert b[["dcl_limited_temp", "dcl_limited_soc", "dcl_limited_cell_v", "errors_present"]].max().max() == 0     # BMS 가 출력을 제한한 적 없음


# ---------------------------------------------------------------- 시계 차이

@E.need
def test_clock_offsets_are_consistent():
    off = pd.read_csv(EV / "derived" / "clock_offsets.csv")
    em = off[off["device"] == "energymeter"]["offset_to_orion_s"]
    assert len(em) == E["clock_offsets.meter_rows"] and em.between(*E.range("clock_offsets.meter_s")).all()
    mono = off[(off["device"] == "monolith") & (off["corr"] > 0.9)]["offset_to_orion_s"]
    assert len(mono) >= 1 and mono.between(*E.range("clock_offsets.monolith_s")).all()
