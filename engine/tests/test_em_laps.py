"""GPS 랩에 에너지미터를 붙이는 `em_laps` 검증.

앞쪽은 만들어 낸 데이터로 도는 단위 테스트, 뒤쪽은 실제 데이터의 골든 값이다 (값은 `em_laps.toml`).
골든은 `lefdata energymeter` · 랩 분할(`gopro --laps`) · `lefdata em-laps` 가 끝나 있어야 돈다.
"""

import json

import numpy as np
import pandas as pd
import pytest

from expected import Expected
from lefdata import em_laps as el, paths

T0 = 1_700_000_000.0        # GPS 시각(UTC 초)의 시작
LAT0, LON0 = 50.0, 8.0      # 아무 곳 (평면 근사가 되는 위도면 된다)
EM_FROM = 20.0              # 미터는 GPS 레코딩이 시작되고 이만큼 뒤에 켜진다
MASS_KG = 300.0


# ---------------------------------------------------------------- 단위 테스트

def speed(t, periods=(23.0, 7.3, 41.0, 13.7)):
    """만들어 낸 주행의 속도(m/s). 주기가 서로 맞물리지 않아 같은 모양이 되풀이되지 않는다."""
    amps = (5.0, 3.0, 2.0, 1.5)
    return 14 + sum(a * np.sin(2 * np.pi * t / p) for a, p in zip(amps, periods))


def synthetic(offset_s=87.4, f=1.0, duration=400.0, meter_periods=None):
    """같은 주행을 본 GPS 레코딩(18 Hz, 동쪽으로 직진)과 에너지미터 레코딩(100 Hz).

    미터 시계는 offset_s 앞서 있고, 미터가 1 초 가는 동안 GPS 는 f 초 간다. meter_periods 를 주면 미터는 다른 주행을 본다.
    """
    fine = np.arange(0.0, duration, 0.001)
    v = speed(fine)
    tg = np.arange(0.0, duration, 1 / 18.0)
    lon = LON0 + np.interp(tg, fine, np.cumsum(v) * 0.001) / (111_320.0 * np.cos(np.radians(LAT0)))
    track = el.Track("gps", tg, T0 + tg, *el.drive_power(T0 + tg, np.full(len(tg), LAT0), lon))

    t_em = np.arange(0.0, (duration - EM_FROM - 5.0) / f, 0.01)
    seen = v if meter_periods is None else speed(fine, meter_periods)
    kw = np.clip(MASS_KG * seen * (np.gradient(seen, 0.001) + 0.3), 0, None) / 1000.0
    volt = np.full(len(t_em), 300.0)
    meter = el.make_meter("em", T0 + EM_FROM + offset_s, t_em, volt, np.interp(EM_FROM + f * t_em, fine, kw) * 1000.0 / volt)
    return meter, track


def test_align_recovers_the_clock_offset():
    """전력과 GPS 위치만으로 '미터 시계가 87.4 s 앞서 있다' 를 찾는다."""
    meter, track = synthetic(offset_s=87.4)
    al = el.align(meter, track)
    assert al.accepted
    assert al.em_minus_gps_s == pytest.approx(87.4, abs=0.05)
    assert al.f == pytest.approx(1.0, abs=1e-4)
    assert al.to_em(al.to_gps([50.0, 300.0])) == pytest.approx([50.0, 300.0], abs=1e-6)


def test_align_recovers_the_clock_rate_too():
    """시계 속도가 0.4 % 다르면 400 초 사이에 1.4 초가 벌어진다. offset 하나로는 양 끝이 맞지 않는다."""
    meter, track = synthetic(offset_s=-60.0, f=1.004)
    al = el.align(meter, track)
    assert al.accepted
    assert al.f == pytest.approx(1.004, abs=2e-4)
    for t_em in (30.0, 340.0):
        assert float(al.to_gps(t_em)) == pytest.approx(T0 + EM_FROM + 1.004 * t_em, abs=0.1)


def test_align_does_not_accept_a_different_drive():
    """닮은 곳이 없는 두 파형을 억지로 맞추지 않는다."""
    meter, track = synthetic(meter_periods=(19.0, 5.9, 37.0, 11.3))
    al = el.align(meter, track)
    assert al is None or not al.accepted


def test_segment_energy_splits_discharge_and_regen_and_adds_up():
    """300 V 에서 100 A 를 10 초 쓰고 20 A 를 5 초 돌려받는다. 구간을 어디서 자르든 합은 같다."""
    t = np.arange(0.0, 15.0, 0.01)
    meter = el.make_meter("em", T0, t, np.full(len(t), 300.0), np.where(t < 10.0, 100.0, -20.0))
    whole = el.segment_energy(meter, 0.0, 15.0, distance_m=500.0)
    assert whole["discharge_wh"] == pytest.approx(300 * 100 * 10 / 3600, abs=0.2)
    assert whole["regen_wh"] == pytest.approx(300 * 20 * 5 / 3600, abs=0.2)
    assert whole["net_wh"] == pytest.approx(whole["discharge_wh"] - whole["regen_wh"], abs=0.11)
    assert whole["wh_per_km"] == pytest.approx(whole["net_wh"] / 0.5, abs=0.3)
    assert (whole["p_max_kw"], whole["p500_max_kw"], whole["i_min_a"]) == (30.0, 30.0, -20.0)
    assert whole["i_rms_a"] == pytest.approx(np.sqrt((100 ** 2 * 10 + 20 ** 2 * 5) / 15), abs=0.2)
    parts = [el.segment_energy(meter, a, b) for a, b in ((0.0, 7.303), (7.303, 15.0))]
    assert sum(p["discharge_wh"] for p in parts) == pytest.approx(whole["discharge_wh"], abs=0.11)
    assert sum(p["regen_wh"] for p in parts) == pytest.approx(whole["regen_wh"], abs=0.11)


def test_segment_outside_the_meter_recording_is_left_blank():
    """미터가 켜지기 전에 시작한 구간에는 반쪽짜리 에너지를 적지 않는다."""
    meter, track = synthetic()
    al = el.align(meter, track)
    half = el.summarize_segment([al], track, 0.0, 2 * EM_FROM)
    assert half["cover_share"] == pytest.approx(0.5, abs=0.01) and half["em_recording"] == "em"
    assert "discharge_wh" not in half
    full = el.summarize_segment([al], track, 100.0, 160.0, distance_m=900.0)
    assert full["cover_share"] == 1.0 and full["discharge_wh"] > 0
    assert full["em_t_start_s"] == pytest.approx(100.0 - EM_FROM, abs=0.05)
    assert abs(full["em_lag_s"]) <= 0.04 and full["em_corr"] > 0.98


def test_segment_lag_shows_a_line_that_is_off():
    """직선이 0.3 s 틀려 있으면 구간별 검증이 그만큼을 내놓는다 (전체 맞추기와 독립인 확인)."""
    meter, track = synthetic()
    al = el.align(meter, track)
    al.a += 0.3                          # 미터의 같은 순간을 0.3 s 늦은 GPS 시각에 놓는다 → 전력은 직선이 말한 것보다 늦게 있다
    lag, corr = el.segment_lag(al, T0 + 100.0, T0 + 160.0)
    assert lag == pytest.approx(0.3, abs=0.04) and corr > 0.98


# ---------------------------------------------------------------- 골든 (실제 데이터 — em_laps.toml)

E = Expected("em_laps")
RACE = paths.LEF26_FSK
race = pytest.mark.skipif(not (RACE / "derived" / "lap_energy.csv").exists(), reason="대회 이벤트에 lefdata em-laps 결과 없음")
week = pytest.mark.skipif(not (paths.LEF25EVO_EVENT / "derived" / "power_cycles.csv").exists()
                          or not (paths.LEF25EVO_EVENT / "normalized" / "recordings_energymeter.csv").exists(),
                          reason="25Evo 테스트위크 normalized/ · power_cycles.csv 없음")


def _derived(name):
    return pd.read_csv(RACE / "derived" / name, encoding="utf-8-sig")


@race
def test_meter_recordings_take_the_car_name_from_the_folder():
    """에너지미터 변환기는 다른 차를 위해 먼저 만들었다. 차량 이름은 폴더에서 읽어야 한다."""
    for p in (RACE / "normalized").glob("*_em"):
        meta = json.loads((p / "meta.json").read_text(encoding="utf-8"))
        assert meta["car"] == RACE.parent.name and meta["counts"]["checksum_fail"] == 0


@race
def test_race_alignment_is_a_clean_line():
    """맞은 창이 전부 한 직선 위에 있다 (틀린 짝이 섞이지 않았다)."""
    al = _derived("em_alignment.csv")
    assert len(al) and al["accepted"].all() and (al["inliers"] == al["windows"]).all()


@race
@E.need
def test_race_meter_clock_offset():
    """두 레코딩을 따로 맞췄는데 같은 값이 나온다 (같은 RTC). 시계 속도는 1 이다."""
    al = _derived("em_alignment.csv")
    assert len(al) == E["race_alignment.recordings"]
    assert al["em_minus_gps_s"].between(*E.range("race_alignment.em_minus_gps_s")).all()
    assert abs(al["em_minus_gps_s"].iloc[0] - al["em_minus_gps_s"].iloc[1]) < E["race_alignment.pair_diff_max_s"]
    assert (al["windows"] > E["race_alignment.windows_min"]).all()
    assert (al["f"] - 1).abs().max() < E["race_alignment.f_err_max"]
    assert al["rms_s"].max() < E["race_alignment.rms_max_s"]


@race
def test_race_every_lap_is_fully_covered():
    lap = _derived("lap_energy.csv")
    assert len(lap) and (lap["cover_share"] == 1.0).all()


@race
@E.need
def test_race_every_lap_has_energy_and_sits_on_the_line():
    lap = _derived("lap_energy.csv")
    assert len(lap) == E["race_laps.laps"]
    assert lap["em_lag_s"].abs().max() <= E["race_laps.lag_max_s"]
    assert lap["em_corr"].min() > E["race_laps.corr_min"]
    best = lap.loc[lap["lap_time_s"].idxmin()]
    assert [best["run"], best["lap"]] == E["race_laps.best"]
    assert best["net_wh"] == E.approx("race_laps.best_net_wh")
    assert {str(k): v for k, v in lap.groupby("run")["net_wh"].median().to_dict().items()} == E.approx("race_laps.median_net_wh")
    assert lap.loc[lap["run"] == 1, "i_max_a"].iloc[1:].between(*E.range("race_laps.run1_i_max_a")).all()
    assert lap.loc[lap["run"] == 2, "i_max_a"].max() < E["race_laps.run2_i_max_below_a"]


@race
def test_race_laps_do_not_cover_the_pit_laps():
    """계측 랩의 방전 합은 주행 구간 전체보다 작다 (나가는 랩 · 들어오는 랩이 남는다)."""
    run, lap = _derived("run_energy.csv").set_index("run"), _derived("lap_energy.csv")
    assert (lap.groupby("run")["discharge_wh"].sum() < run["discharge_wh"]).all()


@race
@E.need
def test_race_run_energy_adds_up_to_the_recording():
    run = _derived("run_energy.csv").set_index("run")
    al = _derived("em_alignment.csv").set_index("em_recording")
    lo, hi = E.range("race_runs.idle_wh")
    for _, r in run.iterrows():
        assert lo <= al.loc[r["em_recording"], "discharge_wh"] - r["discharge_wh"] < hi       # 서 있는 동안 쓴 것
    assert run["net_wh"].sum() == E.approx("race_runs.net_total_wh")
    assert {str(k): v for k, v in run["net_wh"].to_dict().items()} == E.approx("race_runs.net_wh")
    assert run["p500_max_kw"].max() == E.approx("race_runs.p500_max_kw")


@week
@E.need
def test_evo_week_alignment_agrees_with_power_cycles():
    """다른 차 · 다른 GPS · 다른 방법 — 전원이 꺼진 순간으로 짝지은 시계 차(`clocks.power_cycles`)와 같다."""
    found = {(a.meter.rec, a.track.rec): a for a in el.align_event(paths.LEF25EVO_EVENT) if a.accepted}
    cycles = pd.read_csv(paths.LEF25EVO_EVENT / "derived" / "power_cycles.csv", encoding="utf-8-sig").set_index(["energymeter", "monolith"])
    assert set(found) == {tuple(p) for p in E["week.pairs"]}
    for key, al in found.items():
        assert al.em_minus_gps_s == pytest.approx(cycles.loc[key, "em_minus_monolith_s"], abs=E["week.agree_abs_s"])
        assert al.f == pytest.approx(1.0, abs=E["week.f_err_max"])
