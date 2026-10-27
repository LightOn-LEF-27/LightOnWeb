"""25Evo 테스트위크의 에너지미터 · STM32 백업 로거 변환 · 시간 맞추기 검증 (값과 그 이유는 `evo_testweek_golden.toml`).

먼저 `lefdata energymeter --event <테스트위크>` → `lefdata monolith` → `lefdata evo-stm32 --event <테스트위크> --clock gps` 를 실행한다.
"""

import json

import numpy as np
import pandas as pd
import pyarrow.parquet as pq
import pytest

from expected import Expected
from lefdata import clocks, evo_stm32, paths

E = Expected("evo_testweek_golden")
EV = paths.LEF25EVO_EVENT
NORM = EV / "normalized"
ALIGN = EV / "derived" / "stm32_alignment.csv"

pytestmark = pytest.mark.skipif(not ALIGN.exists(),
                                reason="먼저 테스트위크 이벤트에 lefdata energymeter / monolith / evo-stm32 --clock gps 실행")


def meta(rec):
    return json.loads((NORM / rec / "meta.json").read_text(encoding="utf-8"))


def align_table():
    return pd.read_csv(ALIGN).set_index("recording_id")


# ---------------------------------------------------------------- 에너지미터

def test_em_checksums_all_pass():
    recs = sorted(p.name for p in NORM.glob("*_em"))
    assert recs and all(meta(r)["counts"]["checksum_fail"] == 0 for r in recs)


@E.need
def test_em_is_the_meter_log_the_report_was_built_on():
    recs = sorted(p.name for p in NORM.glob("*_em"))
    assert len(recs) == E["meter.recordings"]
    assert sorted({meta(r)["source"]["device_id"] for r in recs}) == E["meter.device_ids"]
    assert [r for r in recs if meta(r)["summary"]["hv_on"]] == E["meter.hv_on"]
    n = [meta(r)["counts"]["records"] for r in E["meter.report_files"]]
    assert n == E["meter.report_records"] and sum(n) == E["meter.report_total"]
    for rec, wh in E["meter.discharge_wh"].items():
        assert meta(rec)["summary"]["discharge_wh"] == pytest.approx(wh, abs=E["meter.discharge_abs_wh"]), rec


@E.need
def test_meter_and_monolith_share_a_power_cycle():
    pc = clocks.power_cycles(EV)
    assert len(pc) == E["power_cycles.pairs"]
    assert pc["off_diff_s"].between(*E.range("power_cycles.off_diff_s")).all()
    assert pc["em_minus_monolith_s"].between(*E.range("power_cycles.em_minus_monolith_s")).all()
    pairs = dict(zip(pc["energymeter"], pc["monolith"]))
    for em, mono in E["power_cycles.known"].items():
        assert pairs[em] == mono


# ---------------------------------------------------------------- STM32 백업 로거

def test_stm32_every_raw_row_is_kept():
    idx = pd.read_csv(NORM / "recordings_stm32.csv", encoding="utf-8-sig")
    raw = sum(len(pd.read_csv(p, comment="#")) for _, p in evo_stm32.source_files(EV))
    kept = sum(pq.ParquetFile(NORM / r / "frames.parquet").metadata.num_rows for r in idx["recording_id"])
    assert raw == kept == idx["rows"].sum()


@E.need
def test_stm32_two_card_sessions_and_two_formats():
    idx = pd.read_csv(NORM / "recordings_stm32.csv", encoding="utf-8-sig")
    assert len(idx) == E["stm32.recordings"] and idx["rows"].sum() == E["stm32.rows"]
    assert {f"{g}/{f}": n for (g, f), n in idx.groupby(["group", "format"]).size().to_dict().items()} == E["stm32.groups"]
    old = idx[idx["format"] == "old"]
    lo, hi = E.range("stm32.old_log_numbers")
    assert sorted(old["log_no"]) == list(range(lo, hi + 1)) and old["vdc_max_v"].isna().all()


@E.need
def test_stm32_is_the_can_logger_the_report_was_built_on():
    b = pq.read_table(NORM / E["report_log.recording"] / "bms.parquet").to_pandas()
    assert b["cell_min_v"].min() == E.approx("report_log.cell_min_v")
    assert b["temp_max_c"].max() == E["report_log.temp_max_c"]
    assert b["soc_pct"].iloc[-1] == E["report_log.soc_last_pct"]


def test_stm32_pairs_are_only_the_confident_ones():
    """짝(em_recording · gps_recording)은 em_ok · gps_ok 가 참일 때만 쓴다. 적용된 레코딩은 시각을 얻고, 아닌 것은 없다."""
    al = align_table()
    applied = al[al["applied"]]
    assert len(applied) and applied["gps_ok"].all()
    for rec in applied.index:
        assert meta(rec)["time"]["alignment"]["gps_recording"] == applied.loc[rec, "gps_recording"]
        m = pq.read_table(NORM / rec / "motor.parquet", columns=["t_abs", "t_quality"]).to_pandas()
        assert m["t_abs"].notna().all() and set(m["t_quality"]) <= {"aligned_gps", "aligned_em"}


@E.need
def test_stm32_aligns_to_monolith_by_speed():
    al = align_table()
    pairs = E["stm32_to_monolith"]
    assert dict(al.loc[al["applied"], "gps_recording"]) == pairs
    for rec in pairs:
        r = al.loc[rec]
        lo, hi = E.range("alignment.gps_a_s")
        assert lo <= r["gps_a_s"] <= hi, rec
        assert r["gps_rms_s"] < E["alignment.gps_rms_max_s"] and r["gps_windows"] >= 2 * max(r["gps_runner_up_windows"], 4), rec
    assert set(al.index[al["gps_ok"]]) == set(pairs)
    assert set(al.index[al["em_ok"]]) == set(E["alignment.em_ok"])
    stale = E["alignment.stale_em_candidate"]
    assert isinstance(al.loc[stale, "em_recording"], str) and not al.loc[stale, "em_ok"]
    m = pq.read_table(NORM / E["alignment.aligned_example"] / "motor.parquet").to_pandas()
    assert m["t_abs"].notna().all() and set(m["t_quality"]) == {"aligned_gps"}
    b = pq.read_table(NORM / E["alignment.unaligned_example"] / "bms.parquet").to_pandas()
    assert b["t_abs"].isna().all() and set(b["t_quality"]) == {"none"}


@E.need
def test_stm32_clock_rate_differs_between_power_cycles():
    """참 시간 = f × 로거 TIME. 레코딩마다 f 가 달라 하나의 상수로는 못 고친다."""
    al = align_table()
    f = al.loc[al["clock_basis"] == "gps_speed", "clock_f"]
    assert len(f) == E["clock.measured"] and f.between(*E.range("clock.f")).all()
    assert f.max() - f.min() > E["clock.spread_min"]
    both = al.loc[E["clock.both_devices"]]
    assert (both["em_f"] - both["gps_f"]).abs().max() < E["clock.both_f_diff_max"]
    stm, mono = E["clock.length_check"]
    assert meta(stm)["time"]["duration_s"] == pytest.approx(meta(mono)["time"]["duration_s"], abs=E["clock.length_abs_s"])


@E.need
def test_three_devices_agree_on_the_meter_clock_offset():
    """에너지미터 시계 − Monolith 시계: 전원 주기로 잰 값과, STM32 를 다리로(전압 · 속도) 잰 값이 같다."""
    al = align_table()
    pc = clocks.power_cycles(EV).set_index("energymeter")
    for rec in E["clock.both_devices"]:
        r = al.loc[rec]
        em0 = pd.Timestamp(meta(r["em_recording"])["time"]["t_abs_start"]) + pd.Timedelta(seconds=r["em_a_s"])
        mono0 = pd.Timestamp(meta(r["gps_recording"])["time"]["t_abs_start"]) + pd.Timedelta(seconds=r["gps_a_s"])
        assert (em0 - mono0).total_seconds() == pytest.approx(pc.loc[r["em_recording"], "em_minus_monolith_s"], abs=E["clock.offset_agree_abs_s"])


def test_clock_offsets_table_is_readable_when_empty():
    """기준 장비(Orion) 로그가 없으면 시계 차이표는 비지만 머리글은 남는다 (빈 파일이면 read_csv 가 실패한다)."""
    off = clocks.measure_event(EV)
    assert "offset_to_orion_s" in off.columns
    assert pd.read_csv(EV / "derived" / "clock_offsets.csv", encoding="utf-8-sig").columns.size


@E.need
def test_no_orion_in_this_event():
    assert clocks.measure_event(EV).empty is not E["orion_logs"]


@E.need
def test_startup_gap_grows_with_files_on_the_card():
    gap = {}
    for group, n in E["stm32.files_per_group"].items():
        for k in range(1, n + 1):
            t = pq.read_table(NORM / f"stm32_{group}_log{k:03d}" / "frames.parquet", columns=["t_log_ms"]).column(0).to_numpy()
            if len(t) >= 3:
                gap[(group, k)] = (t[1] - t[0]) / 1000
    n12, n3 = E["stm32.files_per_group"]["day12"], E["stm32.files_per_group"]["day3"]
    d12 = pd.Series({k: v for (g, k), v in gap.items() if g == "day12"})
    c2, c0 = np.polyfit(d12.index.to_numpy() ** 2, d12.to_numpy(), 1)
    assert c2 == E.approx("startup_gap.c2")
    assert np.corrcoef(d12.index.to_numpy() ** 2, d12.to_numpy())[0, 1] > E["startup_gap.corr_min"]
    assert d12.loc[n12] == E["startup_gap.day12_last_s"] and gap[("day3", n3)] == E["startup_gap.day3_last_s"]


@E.need
def test_restored_pack_power_matches_the_meter():
    """BMS 전력(BT_POW)을 적분한 값이 에너지미터와 같다."""
    rec = E["restored_power.recording"]
    al = align_table().loc[rec]
    b = pq.read_table(NORM / rec / "bms.parquet", columns=["t_rel_ms", "power_kw"]).to_pandas()
    e = pq.read_table(NORM / al["em_recording"] / "energy.parquet", columns=["t_rel_ms", "voltage_v", "current_a"]).to_pandas()
    t = b["t_rel_ms"].to_numpy() / 1000
    wh_bms = np.trapezoid(b["power_kw"].to_numpy(), t) / 3.6
    te = e["t_rel_ms"].to_numpy() / 1000
    wh_em = np.trapezoid((e["voltage_v"] * e["current_a"]).to_numpy() / 1000, te) / 3.6
    assert wh_bms == pytest.approx(wh_em, rel=E["restored_power.rel"])
    assert wh_em == E.approx("restored_power.meter_wh")
