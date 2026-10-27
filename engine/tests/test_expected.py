"""기대값 파일 도우미(`tests/expected.py`) 자체의 검증 — 공개 저장소의 예시 파일로 돈다 (우리 데이터 없이)."""

import pytest

from expected import EXAMPLE_ROOT, Expected

E = Expected("example", EXAMPLE_ROOT)


def test_values_tolerances_and_ranges():
    assert E["circle.laps"] == 3 and E["circle.direction"] == "ccw"
    assert 251.0 == E.approx("circle.lap_m") and 252.0 != E.approx("circle.lap_m")      # abs 0.5
    assert E["circle.lap_m"] == 251.33                                                     # 값만 꺼내면 숫자
    assert E.range("circle.speed_kmh") == (30, 70)
    assert [c["lap"] for c in E.cases("circle.cases")] == [1, 2]
    assert 3.0 == E.approx("circle.laps")                                                  # 오차 없이 적힌 숫자


def test_a_missing_key_is_an_error_not_a_skip():
    """파일은 있는데 값이 없으면 실패다 — 조용히 건너뛰면 검사가 빠진 것을 모른다."""
    with pytest.raises(KeyError):
        E["circle.nope"]
    with pytest.raises(TypeError):
        E.range("circle.laps")


def test_a_missing_file_skips(tmp_path):
    none = Expected("not_there", tmp_path)
    assert not none.exists and none.cases("x") == []
    with pytest.raises(pytest.skip.Exception):
        none["anything"]


def test_unused_values_are_reported(tmp_path):
    (tmp_path / "t.toml").write_text('a = 1\nb = { value = 2.0, abs = 0.1 }\n[c]\nd = 3\ne = { min = 0, max = 1 }\n', encoding="utf-8")
    e = Expected("t", tmp_path)
    assert e["a"] == 1 and e.range("c.e") == (0, 1)
    assert e.unused() == ["b", "c.d"]
