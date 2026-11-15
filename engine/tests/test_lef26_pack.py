"""LEF-26 새 펌웨어 로그의 BMS 프로파일 — 0x6B1 바이트 6-7 팩 전압 · 0x6B4 팩 전류 · 한계 · 0x8A 부호 (만든 로그로, 데이터 없이).

Orion 이 내는 0x6B1 은 판에 따라 바이트 7 이 체크섬이거나, 바이트 6-7 이 팩 순간 전압(빅 엔디언 0.1 V)이다 (26팀 CAN 정의 표).
레코딩의 체크섬 통과율로 가른다. 0x6B4 = 전류 한계 상태 · CCL · DCL (A) · 팩 전류 (int16 0.1 A, 방전 +).
"""

import json

import numpy as np
import pandas as pd
import pytest

from lefdata import catalog_build as cb, lef26
from test_lef26_newlog import DAY, F, _hms, cycle, write

SEG = (60.0, 30.0)                       # 켠 뒤 60 s 에 기록 켬, 30 s 기록


def amps(t_in):
    """기록 켬부터 t 초의 팩 전류 — 10 ~ 20 s 에 100 A (방전 한계 50 A 를 넘음), 22 ~ 24 s 에 회생 −20 A, 나머지 0.5 A."""
    return 100.0 if 10 <= t_in < 20 else -20.0 if 22 <= t_in < 24 else 0.5


def bms_lines(profile="pack_v", thermistor=True):
    """0x6B1 · 0x6B4 를 20 ms 마다. profile = pack_v (바이트 6-7 = 300.0 V) · checksum (바이트 7 = 체크섬). 0x8A 출력 전압도 같이."""
    out = []
    start, dur = SEG
    head = [156, 64, 20, 160, 152, 88]                                   # 셀 최고 4.0000 V · 20 °C · SOC 80 % · 셀 최저 3.9000 V
    for k in range(int(dur / 0.02) - 1):
        t_in = 0.005 + k * 0.02
        at = _hms((start + t_in) * F)
        six = head + ([11, 184] if profile == "pack_v" else [0, (sum(head)) & 0xFF])      # 3000 × 0.1 V / 체크섬
        cur = int(round(amps(t_in) * 10)) & 0xFFFF
        out += [f"{at},6b1,{','.join(map(str, six))},", f"{at},6b4,8,4,0,5,0,50,{cur >> 8},{cur & 0xFF},", f"{at},181,138,185,254,0,0,6,"]
        if thermistor and k % 50 == 0:                                   # 확장 ID 써미스터 모듈 프레임이 0x6B4 로 찍힌 줄 (로거 버릇)
            mod = [0, 15, 20, 17, 32, 3, 9]
            out.append(f"{at},6b4,{','.join(map(str, mod + [(sum(mod) + 0x39 + 8) & 0xFF]))},")
    return out


def log(profile="pack_v"):
    rows = cycle([SEG]) + bms_lines(profile)
    return sorted(rows, key=lambda ln: ln[:12])                          # 시각 순 (같은 시각은 LOG ON 이 먼저 — 안정 정렬)


def tables(tmp_path, profile="pack_v"):
    ev = write(tmp_path / "ev", log(profile))
    [row] = lef26.convert_event(ev)
    d = ev / "normalized" / f"{DAY}_r00"
    return row, {k: pd.read_parquet(d / f"{k}.parquet") for k in ("bms", "pack", "motor")}


def test_a_bms_without_checksum_gives_the_pack_voltage(tmp_path):
    row, t = tables(tmp_path)
    assert row["bms_profile"] == "pack_v"
    bms = t["bms"]
    assert bms["valid"].all() and np.allclose(bms["pack_v"], 300.0) and bms["cell_max_v"].iloc[0] == pytest.approx(4.0)
    pack = t["pack"]
    assert pack["valid"].all() and set(pack["limit_status"]) == {0x0804} and set(pack["dcl_a"]) == {50.0} and set(pack["ccl_a"]) == {5.0}
    assert set(pack["current_a"].round(1)) == {100.0, -20.0, 0.5}
    assert len(pack) == len(bms)                                         # 써미스터 모듈 프레임은 내용으로 빠졌다
    vout = t["motor"][t["motor"]["signal"] == "vout_raw"]["value"]
    assert set(vout) == {327.0}                                          # 바이트 1-2 int16 (−327) 의 부호를 뒤집은 값


def test_a_bms_with_checksum_keeps_the_old_reading(tmp_path):
    """같은 새 펌웨어라도 체크섬이 맞는 판(시험 로그) — 바이트 7 은 체크섬, 팩 전압 없음."""
    row, t = tables(tmp_path, "checksum")
    assert row["bms_profile"] == "checksum" and t["bms"]["valid"].all() and t["bms"]["pack_v"].isna().all()


def test_the_session_gets_battery_power_energy_and_a_dcl_warning(tmp_path):
    ev = tmp_path / "LEF-26" / f"{DAY}_test"
    write(ev, log())
    lef26.convert_event(ev)
    on = pd.Timestamp(f"{DAY} 10:00:00", tz="Asia/Seoul")
    a, b = on + pd.Timedelta(seconds=SEG[0]), on + pd.Timedelta(seconds=SEG[0] + SEG[1])
    (ev / cb.NOTES_NAME).write_text('[event]\ncar = "LEF-26"\nclock = "phone"\n[clocks.lef26_logger.recordings]\n'
                                    f'"{DAY}_r00" = "{on.isoformat()}"\n[[timed_session]]\nname = "시험"\ndiscipline = "미분류"\n'
                                    f'start = "{a.isoformat()}"\nend = "{b.isoformat()}"\n', encoding="utf-8")
    [s] = cb.build_event(ev)
    blocks = {x.key: x for x in s.blocks}
    bat = blocks["battery"]
    power = next(c for c in bat.series if c.key == "power_kw")
    assert np.nanmax(power.v) == pytest.approx(30.0, abs=0.1)              # 300 V × 100 A
    expect = 300 * (100 * 10 + 0.5 * (SEG[1] - 12) - 20 * 2) / 3600
    assert s.stats["net_wh"] == pytest.approx(expect, abs=1.5) and s.stats["i_max_a"] == 100
    assert {c.key for c in blocks["bms"].series} >= {"dcl_a", "ccl_a"}
    [warn] = [f for f in blocks["bms"].findings if f["code"] == "over_dcl"]
    (t0, t1), = warn["t_ranges"]
    assert t0 == pytest.approx(10.0, abs=0.1) and t1 == pytest.approx(20.0, abs=0.1)
