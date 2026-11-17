"""코스 계측선 시도 (`lefdata.course_gates`) — 들어가고 나오는 선 · 코스 구역 · GPS 밀림 맞추기 (만든 궤적으로, 데이터 없이).

만든 코스: 북쪽 줄서기 → 선 (x 0 ~ 20, y = 0) 을 넘어 남쪽으로 → 남쪽에서 고리 (둘레 약 206 m) → 다시 선을 넘어 북쪽 → 북쪽 큰 고리.
"""

import numpy as np
import pytest

from lefdata import course_gates as cg

GATE = cg.Gate("짐카나", 0.0, 0.0, 20.0, 0.0)          # (0, 0) → (20, 0) (동쪽으로) 의 오른쪽 = 남쪽 (y < 0)


def path(*pts, step=1.0):
    """꼭짓점들을 step m 간격으로 잇는다."""
    out = [np.array(pts[0], float)]
    for a, b in zip(pts, pts[1:]):
        a, b = np.array(a, float), np.array(b, float)
        n = max(1, int(np.hypot(*(b - a)) / step))
        out += [a + (b - a) * k / n for k in range(1, n + 1)]
    return np.array(out)


def drive(points, kmh=20.0):
    xy = np.asarray(points, float)
    d = np.r_[0, np.cumsum(np.hypot(*np.diff(xy, axis=0).T))]
    t = d / (kmh / 3.6)
    return t, xy[:, 0], xy[:, 1], np.full(len(t), kmh)


QUEUE = [(10, 40), (10, 10), (10, 3)]
COURSE = [(10, 3), (10, -20), (40, -60), (-20, -60), (10, -20), (12, 3)]           # 남쪽 고리 → 다시 선 위로
LOOP = [(12, 3), (60, 40), (10, 80), (10, 40)]


def test_in_and_out_through_the_same_line_is_one_try():
    t, x, y, v = drive(path(*QUEUE, *COURSE[1:], *LOOP[1:], *QUEUE[1:], *COURSE[1:]))
    got = cg.find(t, x, y, v, [GATE])
    assert list(got["how"]) == ["line", "line"] and (got["course_no"] == 1).all()
    assert got["distance_m"].between(200, 240).all()                    # 남쪽 고리 둘레 약 206 m
    assert got["lap_time_s"].iloc[0] == pytest.approx(got["distance_m"].iloc[0] / (20 / 3.6), rel=0.05)


def test_creeping_across_the_line_in_the_queue_is_not_a_try():
    wiggle = [(10, 3), (10, -2), (10, 2), (10, -1)]                                  # 줄 서서 선을 넘나듦 (몇 m)
    t, x, y, v = drive(path(*QUEUE, *wiggle, (10, 3), (10, 40)))
    assert cg.find(t, x, y, v, [GATE]).empty


def test_a_try_entering_beside_the_line_is_caught_by_the_zone():
    """GPS 가 끊겨 선분 옆으로 들어갔다 나온 것 — 구역으로 잡고 how = zone."""
    side = [(10, 40), (45, 10), (45, -5), (40, -60), (-20, -60), (45, -5), (45, 10), (10, 40)]
    t, x, y, v = drive(path(*side))
    got = cg.find(t, x, y, v, [GATE])
    assert list(got["how"]) == ["zone"]


def test_two_courses_side_by_side_are_told_apart_by_zone_x():
    g1, g2 = cg.Gate("1", 0.0, 0.0, 20.0, 0.0, zone_x=(-1000, 60)), cg.Gate("2", 100.0, 0.0, 120.0, 0.0, zone_x=(60, 1000))
    east = [(110, 40), (110, 3), (110, -20), (140, -60), (80, -60), (110, -20), (112, 3), (110, 40)]
    t, x, y, v = drive(path(*east))
    assert list(cg.find(t, x, y, v, [g1, g2])["course_no"]) == [2]


def test_align_finds_a_whole_track_shift():
    t, x, y, v = drive(path(*QUEUE, *COURSE[1:], *LOOP[1:]))
    dx, dy, fit = cg.align(x + 7.0, y - 4.0, x, y)
    assert abs(dx + 7.0) <= cg.NEAR_M and abs(dy - 4.0) <= cg.NEAR_M and fit > 0.9           # 긴 직선은 NEAR_M 안에서 답이 여럿


def test_find_best_uses_the_shift_only_when_it_catches_more_tries_at_the_line():
    """선분 (0 ~ 10 m) 밖으로 18 m 밀린 기록 — 맞추면 선으로 잡히니 맞춘 쪽. 안 밀린 기록 — 그대로 (맞춘 쪽이 더 낫지 않으면 안 맞춘다)."""
    gate = cg.Gate("짐카나", 0.0, 0.0, 10.0, 0.0)
    pts = [(5, 40), (5, 3), (5, -20), (35, -60), (-25, -60), (5, -20), (6, 3), (40, 40)]
    t, x, y, v = drive(path(*pts))
    got, dx, dy, _ = cg.find_best(t, x + 18.0, y, v, [gate], x, y)
    assert abs(dx + 18.0) <= cg.NEAR_M and list(got["how"]) == ["line"]
    got, dx, dy, _ = cg.find_best(t, x, y, v, [gate], x, y)
    assert (dx, dy) == (0.0, 0.0) and list(got["how"]) == ["line"]
