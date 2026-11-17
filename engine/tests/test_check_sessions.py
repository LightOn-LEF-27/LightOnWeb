"""점검 세션 (`[event] check_sessions = "auto"`, 2026-11-17) — 켜기만 하고 달리지 않은 기록을 묶어 숨김 세션으로 (만든 로그로, 데이터 없이).

켠 뒤 시각만 있는 로그 하나에 레코딩 넷: 달린 것(세션) · 1 초 간격의 짧은 켬 둘(→ 점검 하나) · 세션에 안 넣은 달린 것(점검이 아니다).
"""

import pandas as pd

from lefdata import catalog_build as cb, lef26, session_store
from test_lef26_newlog import DAY, cycle, write

ON = pd.Timestamp(f"{DAY} 10:00:00", tz="Asia/Seoul")
FAST = 5000                     # 0x30 원시값 — N-100 % 의 15 % (돈다). cycle 의 기본값은 3 % 남짓 (안 돈다)


def log(moving: set[int]):
    """레코딩 번호 moving 의 0x30 을 FAST 로 바꾼 로그."""
    rows, k = [], -1
    for line in cycle([(60.0, 40.0), (200.0, 5.0), (206.0, 2.0), (400.0, 30.0)]):
        if ",LOG ON," in line:
            k += 1
        if k in moving and ",181,48," in line:
            at = line.split(",", 1)[0]
            line = f"{at},181,48,{FAST & 0xFF},{FAST >> 8},1,"
        rows.append(line)
    return rows


def event(tmp_path, auto=True):
    ev = write(tmp_path / "LEF-26" / f"{DAY}_test", log({0, 3}))
    lef26.convert_event(ev)
    recs = sorted(p.name for p in (ev / "normalized").iterdir() if p.is_dir())
    notes = ('[event]\ncar = "LEF-26"\nclock = "phone"\n' + ('check_sessions = "auto"\n' if auto else "")
             + "[clocks.lef26_logger.recordings]\n" + "".join(f'"{r}" = "{ON.isoformat()}"\n' for r in recs)
             + f'[[timed_session]]\nrecording = "{recs[0]}"\nname = "주행"\ndiscipline = "짐카나"\n')
    (ev / cb.NOTES_NAME).write_text(notes, encoding="utf-8")
    return ev, recs


def test_short_power_ons_without_driving_become_one_hidden_check_session(tmp_path):
    ev, recs = event(tmp_path)
    ss = cb.build_event(ev)
    assert [s.discipline for s in ss] == ["짐카나", "점검"]                     # 달렸지만 세션에 안 넣은 r03 은 점검이 아니다
    chk = ss[1]
    assert chk.anchor == f"chk:{recs[1]}" and f"{recs[1]}, {recs[2]}" in chk.notes[0]          # 1 초 간격 둘을 하나로
    assert abs((chk.start - (ON + pd.Timedelta(seconds=200))).total_seconds()) < 1.0
    rows = [session_store.summary(s) for s in ss]
    assert [r["default_hidden"] for r in rows] == [False, True]


def test_without_the_flag_nothing_is_added(tmp_path):
    ev, _ = event(tmp_path, auto=False)
    assert [s.discipline for s in cb.build_event(ev)] == ["짐카나"]
