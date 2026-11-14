"""LEF-26 새 펌웨어 로그 (`LOG ON` · `LOG OFF` · `STAT`, 시계 모델 v3) — 만든 로그로 (데이터 없이).

새 로그는 CAN 만 적고 (BAMOCAR 주기 프레임 10 ms), RTC 가 전원을 켤 때 00:00 에서 시작한다. 만든 로그의 RTC 는 참 시간보다 0.2 % 느리다 (F).
약속: 기록을 껐다 켠 곳이 레코딩 경계 · 시계 속도는 주기 프레임으로 잰다 (8월의 7 % 를 가정하지 않는다) · 절대 시각은 지어내지 않고
'켠 뒤 몇 초' 를 남긴다 → 세션은 일정표의 전원 켬에 그것을 더해 자른다.
"""

import json

import numpy as np
import pandas as pd
import pytest

from lefdata import catalog_build as cb, lef26, runsheet

DAY = "2027-06-05"
F = 0.998                       # 만든 RTC 의 속도 (참 1 s 에 0.998 s)


def _hms(rtc: float) -> str:
    ms = int(round(rtc * 1000))
    s, ms = divmod(ms, 1000)
    h, s = divmod(s, 3600)
    m, s = divmod(s, 60)
    return f"{h:02d}:{m:02d}:{s:02d}.{ms:03d}"


def cycle(segments, rtc0: float = 0.0, stat_lag: float = 2.2, rate: int = 900, stat: bool = True) -> list[str]:
    """전원 주기 하나. segments = [(켠 뒤 기록 켬까지 참 초, 기록한 참 초)]. RTC = rtc0 + 켠 뒤 참 초 × F.

    10 ms 마다 0x181 줄 셋 — 0x30 실제 속도 (k 에 따라 변함) · 0x23 Id 지령 (0) · 0x28 Id (−10 Num). STAT 은 1 초마다 (켠 뒤 stat_lag 부터 셈).
    """
    out = []
    for start, dur in segments:
        out.append(f"{_hms(rtc0 + start * F)},SYSTEM,LOG ON,4,23")
        for k in range(int(round(dur / 0.01))):
            true = start + k * 0.01
            at = _hms(rtc0 + true * F)
            speed = (-1000 - k) & 0xFFFF
            out += [f"{at},181,48,{speed & 0xFF},{speed >> 8},1,", f"{at},181,35,0,0,5,", f"{at},181,40,246,255,6,"]
            if stat and k % 100 == 50:
                out.append(f"{at},SYSTEM,STAT,{int(rate * (true - stat_lag))},50,0,0,0")
        out.append(f"{_hms(rtc0 + (start + dur) * F)},SYSTEM,LOG OFF")
    return out


def write(ev, rows: list[str], name: str = f"{DAY}_merged.csv"):
    (ev / "raw_merged").mkdir(parents=True, exist_ok=True)
    (ev / "raw_merged" / name).write_text("\n".join(rows) + "\n", encoding="utf-8")
    return ev


def meta(ev, rec: str) -> dict:
    return json.loads((ev / "normalized" / rec / "meta.json").read_text(encoding="utf-8"))


# 켠 뒤 60 s 에 20 s · 200 s 에 15 s 기록 / 다시 켜서 (RTC 가 다시 00:00) 30 s 에 12 s
TWO_BOOTS = cycle([(60.0, 20.0), (200.0, 15.0)]) + cycle([(30.0, 12.0)])


def test_log_on_splits_recordings_and_a_new_boot_is_found(tmp_path):
    ev = write(tmp_path / "ev", TWO_BOOTS)
    rows = lef26.convert_event(ev)
    assert [r["recording_id"] for r in rows] == [f"{DAY}_r00", f"{DAY}_r01", f"{DAY}_r02"]
    assert [r["power_cycle"] for r in rows] == [0, 0, 1]
    assert not any(r["time_trusted"] for r in rows) and all(r["t_abs_start"] is None for r in rows)   # 절대 시각은 지어내지 않는다
    assert [r["since_boot_s"] for r in rows] == pytest.approx([60.0, 200.0, 30.0], abs=0.01)
    assert {r["boot_basis"] for r in rows} == {"rtc_zero_stat"}
    m = meta(ev, f"{DAY}_r01")
    assert m["source"]["log_format"] == "log_on" and m["time"]["duration_s"] == pytest.approx(15.0, abs=0.02)
    assert m["time"]["boot"]["stat_zero_rtc_s"] == pytest.approx(2.2 * F, abs=0.02)


def test_clock_speed_comes_from_the_periodic_frames_not_the_old_seven_percent(tmp_path):
    """RTC 가 혼자 돌아도 8월 펌웨어의 '7 % 느림' 을 가정하지 않는다 — BAMOCAR 10 ms 주기 프레임의 로거 간격 9.98 ms → f = 0.998."""
    ev = write(tmp_path / "ev", TWO_BOOTS)
    rows = lef26.convert_event(ev)
    assert all(r["clock_mode"] == "free" for r in rows)
    assert [r["clock_f"] for r in rows] == pytest.approx([F] * 3, abs=2e-4)
    assert [r["clock_f_basis"] for r in rows] == ["can_period", "can_period", "can_period"]
    frames = pd.read_parquet(ev / "normalized" / f"{DAY}_r00" / "frames.parquet")
    assert frames["t_rel_ms"].iloc[-1] == pytest.approx(20_000, abs=20)            # 참 20 s (로거는 19.96 s 로 셈)
    assert frames["t_log_ms"].iloc[-1] == pytest.approx(20_000 * F, abs=20)


def test_a_short_recording_borrows_the_speed_of_the_others(tmp_path):
    """주기 프레임이 모자란 짧은 레코딩 (2 s) — 같은 파일의 다른 새 형식 레코딩을 모아 잰 속도. 그것도 없으면 사전값 (1.0)."""
    rows = lef26.convert_event(write(tmp_path / "a", cycle([(10.0, 2.0), (40.0, 20.0)])))
    assert [r["clock_f_basis"] for r in rows] == ["can_period_pooled", "can_period"] and rows[0]["clock_f"] == pytest.approx(F, abs=2e-4)
    [alone] = lef26.convert_event(write(tmp_path / "b", cycle([(10.0, 2.0)])))     # 2 s 로는 시계 상태도 못 가린다 — 그래도 7 % 는 아니다
    assert alone["clock_f"] == lef26.F_PRIOR_LOG_ON and alone["clock_f_basis"] in ("assumed", "none")


def test_boot_without_stat_lines_or_with_a_running_rtc(tmp_path):
    """STAT 줄이 없으면 이른 RTC 를 '켤 때 00:00' 으로 본다 (추정). RTC 가 이어 도는 로거면 STAT 직선이 켠 순간을 준다."""
    [no_stat] = lef26.convert_event(write(tmp_path / "a", cycle([(60.0, 10.0)], stat=False)))
    assert no_stat["boot_basis"] == "rtc_zero_assumed" and no_stat["since_boot_s"] == pytest.approx(60.0, abs=0.01)
    [running] = lef26.convert_event(write(tmp_path / "b", cycle([(60.0, 10.0)], rtc0=5000.0)))
    assert running["boot_basis"] == "stat" and running["since_boot_s"] == pytest.approx(60.0, abs=0.05)


def test_id_registers_are_decoded_and_stat_lines_keep_their_count(tmp_path):
    ev = write(tmp_path / "ev", cycle([(60.0, 10.0)]))
    lef26.convert_event(ev)
    motor = pd.read_parquet(ev / "normalized" / f"{DAY}_r00" / "motor.parquet")
    by = dict(tuple(motor.groupby("signal")))
    assert set(by) == {"n_act_pct", "id_cmd_a", "id_act_a"}
    assert by["id_act_a"]["value"].iloc[0] == pytest.approx(-10 * lef26.BAMOCAR_I_A_PER_NUM) and (by["id_cmd_a"]["value"] == 0).all()
    events = pd.read_parquet(ev / "normalized" / f"{DAY}_r00" / "events.parquet")["message"].tolist()
    assert events[0] == "LOG ON,4,23" and events[-1] == "LOG OFF" and events[1].startswith("STAT,") and events[1].endswith(",50,0,0,0")


def test_old_logs_keep_their_old_rules(tmp_path):
    """8월 펌웨어 로그 (CONFIG ON) — 0x23 · 0x28 줄이 섞여 있어도 풀지 않고, 혼자 돈 RTC 는 그대로 7 % 로 바로잡는다 (8월 결과를 바꾸지 않는다)."""
    rows = ["10:00:00.000,SYSTEM,CONFIG ON"] + [f"10:00:{k // 100:02d}.{k % 100 * 10:03d},181,40,246,255,6," for k in range(3000)]
    [row] = lef26.convert_event(write(tmp_path / "ev", rows))
    motor = pd.read_parquet(tmp_path / "ev" / "normalized" / f"{DAY}_r00" / "motor.parquet")
    assert motor.empty and row["clock_f"] == lef26.F_FREE and "power_cycle" not in row


def test_detect_says_it_is_the_new_format(tmp_path):
    ev = write(tmp_path / "ev", cycle([(60.0, 10.0)]))
    got = lef26.detect(ev / "raw_merged" / f"{DAY}_merged.csv")
    assert got["kind"] == "lef26_logger_csv" and any("새 펌웨어" in n for n in got["notes"])


# ---------------------------------------------------------------- 세션 — 일정표의 전원 켬 + 켠 뒤 몇 초

SHEET = """차,R번호,전원 켬,출발,도착,종목
LEF-26,1,10:00:00,10:00:59,10:01:21,연습
LEF-26,2,,10:03:19,10:03:36,연습
LEF-26,3,10:10:00,10:10:29,10:10:43,연습
"""


def event(tmp_path, sheet=SHEET, notes=""):
    ev = tmp_path / "LEF-26" / f"{DAY}_test"
    write(ev, TWO_BOOTS)
    lef26.convert_event(ev)
    (ev / cb.NOTES_NAME).write_text('[event]\ncar = "LEF-26"\nsessions = "auto"\n' + notes, encoding="utf-8")
    (ev / f"runsheet_{DAY}.csv").write_text(sheet, encoding="utf-8")
    return ev


def _rpm(s):
    mot = next(b for b in s.blocks if b.key == "motor")
    return next(c for c in mot.series if c.key == "rpm")


def test_each_power_cycle_takes_its_own_power_on(tmp_path):
    """두 번 켠 날 — 전원 주기마다 그 전원 켬에 '켠 뒤 몇 초' 를 더해 자른다. 기록은 출발 1 s 뒤에 시작한다 (일정표를 그렇게 적었다)."""
    report = {}
    sessions = cb.build_event(event(tmp_path), report)
    assert [s.run_no for s in sessions] == ["1", "2", "3"] and {s.split_by for s in sessions} == {"time"}
    for s, n in zip(sessions, (2000, 1500, 1200)):
        rpm = _rpm(s)
        assert rpm.t_s[0] == pytest.approx(1.0, abs=0.05) and len(rpm.t_s) == pytest.approx(n, abs=3)
        mot = next(b for b in s.blocks if b.key == "motor")
        assert any("전원 켬" in c and "켠 순간부터" in c for c in mot.checks)
    [p] = [p for p in report["runsheet"]["power_on"] if p.get("boot_relative")]
    assert p["pairs"] == 2 and p["unpaired_boots"] == [] and [b["power_on"] for b in p["boots"]] == ["10:00:00", "10:10:00"]
    assert any("전원 주기" in line for line in runsheet.check_text(report["runsheet"]))


def test_a_power_cycle_without_its_power_on_is_left_out(tmp_path):
    """두 번째 전원 켬을 안 적은 날 — 첫 전원 켬에 두 주기를 다 붙이지 않는다 (두 번째 주기는 그 뒤 주행과 겹치지 않는다). 남은 줄은 이유와 함께."""
    sheet = SHEET.replace("LEF-26,3,10:10:00,", "LEF-26,3,,")
    report = {}
    sessions = cb.build_event(event(tmp_path, sheet), report)
    assert [s.run_no for s in sessions] == ["1", "2"]
    [p] = [p for p in report["runsheet"]["power_on"] if p.get("boot_relative")]
    assert p["pairs"] == 1 and p["unpaired_boots"] == [f"raw_merged/{DAY}_merged.csv#001"]
    [left] = report["runsheet"]["unmatched_rows"]
    assert left["run_no"] == "3" and "기록이 남은 장비 없음" in left["why"]


def test_a_power_on_written_by_hand_for_one_recording(tmp_path):
    """일정표 없이 catalog_notes.toml 에 레코딩의 전원 켬을 시각으로 — 이벤트 하나의 offset_s 로는 이 레코딩을 옮기지 않는다."""
    notes = ('clock = "phone"\n[clocks.lef26_logger]\noffset_s = 3600.0\n[clocks.lef26_logger.recordings]\n'
             f'"{DAY}_r02" = "{DAY}T10:10:00"\n'
             f'[[timed_session]]\nname = "R3"\ndiscipline = "연습"\nstart = "{DAY}T10:10:29"\nend = "{DAY}T10:10:43"\n')
    ev = tmp_path / "LEF-26" / f"{DAY}_test"
    write(ev, TWO_BOOTS)
    lef26.convert_event(ev)
    (ev / cb.NOTES_NAME).write_text('[event]\ncar = "LEF-26"\n' + notes, encoding="utf-8")
    [s] = cb.build_event(ev)
    assert s.name == "R3" and _rpm(s).t_s[0] == pytest.approx(1.0, abs=0.05)
    assert all(b.key in ("motor", "bms", "thermistor") for b in s.blocks)
    without = notes.replace(f'[clocks.lef26_logger.recordings]\n"{DAY}_r02" = "{DAY}T10:10:00"\n', "")
    (ev / cb.NOTES_NAME).write_text('[event]\ncar = "LEF-26"\n' + without, encoding="utf-8")
    with pytest.raises(ValueError, match="기록이 남은 장비가 없다"):                    # offset_s 3600 s 로 옮겨 붙이지 않는다
        cb.build_event(ev)


def test_a_time_is_only_for_a_recording_that_counts_from_power_on():
    """[clocks.<시계>.recordings] 에 시각(전원 켬)을 적는 것은 켠 순간부터 세는 레코딩뿐 — 다른 장비는 더할 초."""
    clocks = cb._Clocks({"event": {"clock": "phone"}, "clocks": {"energymeter": {"recordings": {"em1": f"{DAY}T10:00:00"}}}})
    em = cb._Rec("em1", None, {}, "energymeter", "energymeter", 0.0, 10.0)
    with pytest.raises(ValueError, match="더할 초"):
        clocks.offset(em)
    log = cb._Rec("em1", None, {}, "lef26_logger", "energymeter", 60.0, 70.0, boot="x#000")
    assert clocks.offset(log) == pd.Timestamp(f"{DAY} 10:00:00", tz="Asia/Seoul").timestamp() and np.isfinite(clocks.offset(log))
