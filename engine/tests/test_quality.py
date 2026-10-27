"""품질 검사 함수 자체의 검증 + 우리 데이터에 돌렸을 때의 골든 값 (`quality.toml`).

검사 도구도 틀릴 수 있다 (거짓 실패). 그래서 검사 함수도 만들어 낸 값으로 테스트한다 — 예: 주행 중에는 1 g 를 넘는 게 정상이라,
정지 구간 없이 중력 크기를 판정하면 안 된다.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pyarrow.parquet as pq
import pytest

from expected import Expected
from lefdata import paths, quality as q

E = Expected("quality")

# ---------------------------------------------------------------- 함수 자체


def test_Q1_in_range_counts_bad_values():
    f = q.in_range([1.0, 2.5, 3.7, 0.5, 9.9], 2.0, 4.3, "cell_v", "V")
    assert f.detail["bad"] == 3 and f.level == q.ERROR


def test_Q2_is_constant_catches_disconnected_sensor():
    assert q.is_constant(np.zeros(500), "ped").failed          # 센서가 안 꽂힌 채 기록하면 이렇다
    assert not q.is_constant(np.arange(500), "rpm").failed


def test_Q3_monotonic_time_respects_tolerance():
    t = [0, 1000, 2332, 2000, 3000]                            # 332 ms 역행
    assert q.monotonic_time(t, "x").level == q.ERROR
    assert q.monotonic_time(t, "x", tol_ms=1000).level == q.WARN
    assert q.monotonic_time([0, 1, 2], "x").level == q.OK


def test_Q4_gravity_skips_when_not_still():
    """주행 중에는 1 g 를 넘는 게 정상이라 판정하지 않는다 (거짓 실패 방지)."""
    n = 50
    f = q.gravity_magnitude(np.zeros(n), np.zeros(n), np.full(n, 1.3))
    assert f.level == q.OK and "부족" in f.message


def test_Q5_gravity_flags_scale_error():
    n = 500
    assert q.gravity_magnitude(np.zeros(n), np.zeros(n), np.full(n, 1.04)).level == q.WARN
    assert q.gravity_magnitude(np.zeros(n), np.zeros(n), np.full(n, 1.00)).level == q.OK


def test_Q6_no_overlap_detects_bad_clock_recovery():
    ok = [(0, 10), (20, 30), (40, 50)]
    bad = [(0, 25), (20, 30)]
    assert q.no_overlap(ok).level == q.OK
    assert q.no_overlap(bad).level == q.ERROR


def test_Q7_row_count_preserved():
    assert q.row_count_preserved(100, 100).level == q.OK
    f = q.row_count_preserved(5_000, 0, "PED")                 # 원본에는 있는데 변환하며 다 빠졌다
    assert f.failed and f.value == 5_000


def test_Q8_duplicate_rate():
    d = pd.DataFrame({"a": [1, 1, 1, 2], "b": [9, 9, 9, 8]})
    assert q.duplicate_rate(d).value == pytest.approx(2 / 3)


def test_Q9_summarize_puts_failures_first():
    out = q.summarize([q.Finding("b", q.OK, "-"), q.Finding("a", q.ERROR, "-")])
    assert out.index("FAIL") < out.index("OK")


# ---------------------------------------------------------------- 우리 데이터 골든 값

E25 = paths.LEF25EVO_EVENT / "normalized"
E26 = paths.LEF26_EVENT / "normalized"


@pytest.mark.skipif(not E25.exists(), reason="normalized/ 없음")
@E.need
def test_Q10_monolith_gravity_is_consistently_off():
    """IMU 배율 오차가 레코딩마다 일관된 범위에 있다 (우연한 흔들림이 아니다)."""
    offs = []
    for rec in sorted(p for p in E25.iterdir() if p.is_dir()):
        g, i = rec / "gps.parquet", rec / "imu.parquet"
        if not (g.exists() and i.exists()) or pq.ParquetFile(g).metadata.num_rows < 100:
            continue
        gd = pq.read_table(g).to_pandas()
        gd = gd[gd["fix"]]
        idf = pq.read_table(i).to_pandas()
        sp = np.interp(idf["t_rel_ms"], gd["t_rel_ms"], gd["speed_kmh"])
        f = q.gravity_magnitude(idf["ax_g"], idf["ay_g"], idf["az_g"], sp < 2.0)
        if f.value:
            offs.append(f.value)
    lo, hi = E.range("monolith_gravity.ratio")
    assert len(offs) >= E["monolith_gravity.recordings_at_least"]
    assert all(lo < v < hi for v in offs), offs


@pytest.mark.skipif(not E26.exists(), reason="normalized/ 없음")
@E.need
def test_Q11_lef26_time_backsteps_stay_under_one_second():
    """시계 점프를 이어 붙인 자리에 작은 역행이 남는다. 로거 시계로 1 초를 넘으면 복원이 틀린 것이다 (한계 · 이유는 기대값 파일)."""
    total, worst = 0, 0.0
    for rec in sorted(p for p in E26.iterdir() if p.is_dir()):
        for tbl in rec.glob("*.parquet"):
            if tbl.stem == "frames" or pq.ParquetFile(tbl).metadata.num_rows < 2:
                continue
            t = pq.read_table(tbl, columns=["t_rel_ms"]).to_pandas()["t_rel_ms"].to_numpy()
            d = np.diff(t)
            if (d < 0).any():
                total += int((d < 0).sum())
                worst = min(worst, float(d.min()))
    assert worst > E["lef26_backsteps.worst_ms_above"], f"로거 시계로 1 초 넘는 역행 {worst} ms — 시계 복원이 틀렸다"
    assert total <= E["lef26_backsteps.count_at_most"], f"역행 {total}건 (기준보다 늘었다)"


def _fake_recording(root, name, device, start, minutes):
    """검사용 가짜 레코딩: gps 표 하나와 meta.json."""
    import json

    import pandas as pd
    d = root / "normalized" / name
    d.mkdir(parents=True)
    t = pd.date_range(start, periods=minutes * 60, freq="s", tz="Asia/Seoul")
    pd.DataFrame({"t_rel_ms": np.arange(len(t)) * 1000, "t_abs": t, "fix": True,
                  "lat": 50.0, "lon": 8.0, "speed_kmh": 30.0, "course_deg": 90.0}).to_parquet(d / "gps.parquet")
    (d / "meta.json").write_text(json.dumps({"source": {"logger": device}}), encoding="utf-8")


def test_Q12_overlap_is_checked_per_device(tmp_path):
    """로거와 카메라가 같은 시각에 기록하는 것은 정상이다. 같은 장비끼리 겹칠 때만 시각 복원이 틀린 것이다."""
    _fake_recording(tmp_path, "log_1", "logger", "2027-01-01 09:00", 30)
    _fake_recording(tmp_path, "log_2", "logger", "2027-01-01 10:00", 30)
    _fake_recording(tmp_path, "cam_1", "camera", "2027-01-01 09:10", 10)     # 로거 레코딩 한가운데
    _fake_recording(tmp_path, "cam_2", "camera", "2027-01-01 10:05", 10)
    found = q.check_event(tmp_path)["_event"]
    assert len(found) == 2 and all(f.level == q.OK for f in found)
    assert {f.check for f in found} == {"overlap:recordings[logger]", "overlap:recordings[camera]"}

    _fake_recording(tmp_path, "log_3", "logger", "2027-01-01 09:20", 5)      # 같은 로거가 같은 시각에 두 번
    found = {f.check: f for f in q.check_event(tmp_path)["_event"]}
    assert found["overlap:recordings[logger]"].level == q.ERROR
    assert found["overlap:recordings[camera]"].level == q.OK
