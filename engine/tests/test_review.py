"""데이터 넣기 확인 페이지 (`lefdata.review`) — 넣은 이벤트를 사람이 보고 고칠 것을 고르는 HTML (만든 로그로, 데이터 없이).

켠 뒤 시각만 있는 로그 두 레코딩 중 하나만 세션이 된다. 기준 시각은 추정(전원 켬 가정), 종목은 미분류 —
페이지의 '정해 줄 것' 에 셋 다 나오고, 레코딩은 세션 만들기와 같은 시계로 시간축에 놓인다.
"""

import pandas as pd

from lefdata import catalog_build as cb, lef26, review, session_store
from test_lef26_newlog import DAY, cycle, write

ON = pd.Timestamp(f"{DAY} 10:00:00", tz="Asia/Seoul")


def event(tmp_path):
    ev = write(tmp_path / "LEF-26" / f"{DAY}_test", cycle([(60.0, 40.0), (200.0, 5.0)]))
    lef26.convert_event(ev)
    recs = sorted(p.name for p in (ev / "normalized").iterdir() if p.is_dir())
    notes = ('[event]\ncar = "LEF-26"\nclock = "phone"\nclock_label = "추정 시각 (전원 켬 10:00 가정)"\n'
             "[clocks.lef26_logger.recordings]\n" + "".join(f'"{r}" = "{ON.isoformat()}"\n' for r in recs)
             + f'[[timed_session]]\nrecording = "{recs[0]}"\nname = "시험"\ndiscipline = "미분류"\n')
    (ev / cb.NOTES_NAME).write_text(notes, encoding="utf-8")
    store = tmp_path / "_sessions"
    for s in cb.build_event(ev):
        session_store.write_session(s, store, tmp_path)
    return ev, store, recs


def test_the_page_asks_about_the_estimated_clock_the_discipline_and_the_lone_recording(tmp_path):
    ev, store, recs = event(tmp_path)
    notes = cb.load_notes(ev)
    rs = review.read_recordings(ev, notes)
    ss = review.read_sessions(store, f"LEF-26/{ev.name}")
    review.link(rs, ss, notes)
    assert len(ss) == 1 and [r.sessions == [] for r in rs] == [False, True]
    first = next(r for r in rs if r.rid == recs[0])
    assert abs((first.start - (ON + pd.Timedelta(seconds=60))).total_seconds()) < 1.0      # 전원 켬 + 켠 뒤 시각 = 세션과 같은 시계
    tags = [d["tag"] for d in review.decisions(rs, ss, notes)]
    assert tags[:3] == ["추정", "미분류", "세션 없음"]
    page = review.build(ev, store).read_text(encoding="utf-8")
    assert "<title>" in page and recs[1] in page and "판과 데이터 맞음" not in page.split("<main>")[0]
    assert (ev / "derived" / "review" / "index.html").exists()


def test_multi_day_events_put_the_date_on_session_names():
    assert review._short("LEF-26_2026-08-14_S22") == "S22" and review._short("LEF-26_2026-08-14_S22", True) == "08-14 S22"
