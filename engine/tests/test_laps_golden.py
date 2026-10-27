"""랩 분할 검증 — 대회 리포트에 적힌 랩 수 · 주행 시간이 다시 나와야 한다 (값은 기대값 파일 `laps_golden.toml`).

먼저 `lefdata monolith` 로 변환한 뒤 실행한다.
"""

import pytest

from expected import Expected
from lefdata import laps, paths

E = Expected("laps_golden")
NORM = paths.LEF25EVO_EVENT / "normalized"
pytestmark = [pytest.mark.skipif(not NORM.exists(), reason="normalized/ 없음"), E.need]


@pytest.fixture(scope="module")
def result():
    return laps.run_event(paths.LEF25EVO_EVENT)


def _laps(result, rid):
    return result[1][result[1]["recording_id"] == rid]


@pytest.mark.parametrize("case", E.cases("endurance_race"), ids=lambda c: c["recording"])
def test_endurance_race_matches_the_report(result, case):
    runs = result[0][result[0]["recording_id"] == case["recording"]]
    assert len(runs) == 1
    assert len(_laps(result, case["recording"])) == case["laps"]
    assert runs["duration_s"].iloc[0] == pytest.approx(case["duration_s"], abs=E["duration_abs_s"])


def test_other_endurance_sessions(result):
    for rid, n in E["other_endurance"].items():
        assert len(_laps(result, rid)) == n, rid


def test_lap_length_is_consistent(result):
    """같은 코스를 도는 랩은 길이 · 시간이 한 범위에 든다 (계측선 통과를 놓치면 2 배가 된다)."""
    L = result[1][result[1]["label"].isin(E["lap_range.labels"])]
    assert len(L)
    assert L["distance_m"].between(*E.range("lap_range.distance_m")).all()
    assert L["lap_time_s"].between(*E.range("lap_range.lap_time_s")).all()


def test_no_laps_without_gps_or_circuit(result):
    known = {p.name for p in NORM.iterdir() if p.is_dir()}
    for rid in E["no_laps"]:
        assert rid in known and _laps(result, rid).empty, rid             # 있는 레코딩인데 랩이 없다
