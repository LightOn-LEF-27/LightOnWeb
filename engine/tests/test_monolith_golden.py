"""Monolith 변환 결과 검증.

형식에서 늘 참인 것(레코드 수 = 파일 크기 ÷ 24, 체크섬, 파일명 = 첫 시각, 표 나누기, 시간 단조)은 어느 Monolith 로그에도 돈다.
답을 아는 값(골든)은 기대값 파일 `monolith_golden.toml` 에 있다. 먼저 `lefdata monolith` 로 변환한 뒤 실행한다.
"""

import csv
import json
import re

import pyarrow.compute as pc
import pyarrow.parquet as pq
import pytest

from expected import Expected
from lefdata import paths

E = Expected("monolith_golden")
NORM = paths.LEF25EVO_EVENT / "normalized"
RAW = paths.LEF25EVO_EVENT / "raw_logger"

pytestmark = pytest.mark.skipif(not NORM.exists(), reason="normalized/ 없음 — 먼저 lefdata monolith 실행")


def rec(rec_id):
    return NORM / rec_id


def meta(rec_id):
    return json.loads((rec(rec_id) / "meta.json").read_text(encoding="utf-8"))


def table(rec_id, name):
    return pq.read_table(rec(rec_id) / f"{name}.parquet")


MONOLITH_ID = re.compile(r"\d{4}-\d{2}-\d{2}_\d{6}")      # 같은 폴더에 에너지미터(_em)·STM32(stm32_) 레코딩도 있다


def all_ids():
    return sorted(p.name for p in NORM.iterdir() if p.is_dir() and MONOLITH_ID.fullmatch(p.name))


def labelled_ids():
    """담당자가 종목 이름을 붙여 준 레코딩."""
    rows = csv.DictReader((NORM / "recordings.csv").open(encoding="utf-8-sig"))
    return sorted(r["recording_id"] for r in rows if r["label"])


def test_every_raw_file_becomes_a_recording():
    assert len(all_ids()) == len(list(RAW.glob("*.log")))


@E.need
def test_recording_count():
    assert len(all_ids()) == E["recordings"]
    assert len(labelled_ids()) == E["labelled"]


@E.need
def test_G1_total_frames():
    frames = lambda ids: sum(pq.ParquetFile(rec(r) / "frames.parquet").metadata.num_rows for r in ids)      # noqa: E731
    assert frames(labelled_ids()) == E["frames_labelled"]
    assert frames(all_ids()) == E["frames_all"]


@E.need
def test_unlabelled_recordings_are_standby_or_short():
    """이름이 없는 레코딩은 대부분 서 있던 기록이다 — 종목 주행은 이름을 붙인 것에 다 있다."""
    fast = []
    for r in sorted(set(all_ids()) - set(labelled_ids())):
        g = table(r, "gps").filter(pc.field("fix"))
        if g.num_rows and pc.max(g.column("speed_kmh")).as_py() > E["unlabelled_moving_kmh"]:
            fast.append(r)
    assert fast == E["unlabelled_moving"]
    has_can = any(meta(r)["counts"]["by_kind"].get("CAN") for r in all_ids())
    assert has_can is not E["no_can"]


def test_G1_frames_equal_file_records_per_recording():
    for r in all_ids():
        m = meta(r)
        assert m["counts"]["frames"] == m["source"]["file_bytes"] // 24
        assert m["source"]["tail_bytes_ignored"] == 0


def test_G2_no_checksum_failures():
    for r in all_ids():
        v = table(r, "frames").column("valid")
        assert pc.all(v).as_py(), r


@E.need
def test_G3_G4_top_speeds():
    for rid, kmh in E["top_speed"].items():
        g = table(rid, "gps").filter(pc.field("fix"))
        assert round(pc.max(g.column("speed_kmh")).as_py(), 2) == kmh, rid


@E.need
def test_G5_gravity_on_z_axis():
    az = table(E["gravity.recording"], "imu").column("az_g")
    assert pc.mean(az).as_py() == E.approx("gravity.az_mean_g")


@E.need
def test_G6_all_track_sessions_at_same_site():
    for r in all_ids():
        g = table(r, "gps").filter(pc.field("fix"))
        if g.num_rows == 0:
            continue
        for col in ("lat", "lon"):
            target, x = E[f"site.{col}"], g.column(col)
            assert abs(pc.approximate_median(x).as_py() - target) < E["site.median_tol_deg"], (r, col)
            assert pc.max(pc.abs(pc.subtract(x, target))).as_py() < E["site.max_tol_deg"], (r, col)


@E.need
def test_G7_dyno_indoor_has_no_gps():
    assert table(E["dyno_no_gps"], "gps").num_rows == 0


def test_G8_abs_time_start_matches_filename():
    for r in all_ids():
        m = meta(r)
        assert m["time"]["filename_time_match"], r
        f = table(r, "frames")
        first = f.column("t_abs")[0].as_py()
        assert first.strftime("%Y-%m-%d_%H%M%S") == r


def test_signal_tables_partition_frames():
    """frames 의 종류별 개수와 신호 테이블 행 수가 정확히 맞아야 한다 (빠지거나 겹친 행 없음)."""
    for r in all_ids():
        k = meta(r)["counts"]["by_kind"]
        assert table(r, "gps").num_rows == k.get("GPS", 0)
        assert table(r, "imu").num_rows == k.get("GYROSCOPE", 0)
        assert table(r, "analog").num_rows == k.get("ANALOG", 0)
        assert table(r, "events").num_rows == k.get("SYSTEM", 0) + k.get("USER_EVENT", 0)


def test_time_is_monotonic_within_recording():
    for r in all_ids():
        assert meta(r)["time"]["ts_monotonic"], r
