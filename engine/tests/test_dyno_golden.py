"""섀시다이나모 기록 변환 · 로거 맞추기 · 다이나모 세션 — 우리 데이터 (값과 그 이유는 `dyno_golden.toml`).

먼저 `lefdata dyno --event <테스트위크>` → `lefdata sessions --event <테스트위크>` 를 실행한다.
"""

import json

import pandas as pd
import pytest

from expected import Expected
from lefdata import paths

E = Expected("dyno_golden")
CASES = ("evo", "lef26")
EVENTS = {"evo": paths.LEF25EVO_EVENT, "lef26": paths.LEF26_EVENT}     # 테스트위크 (다이나모가 있던 이벤트)


def event(case):
    return EVENTS[case]


def need(case):
    if not (event(case) / "derived" / "dyno_alignment.csv").exists():
        pytest.skip("먼저 lefdata dyno --event <테스트위크>")


def recordings(case):
    return pd.read_csv(event(case) / "normalized" / "recordings_dyno.csv", encoding="utf-8-sig")


def pairs(case):
    d = pd.read_csv(event(case) / "derived" / "dyno_alignment.csv", encoding="utf-8-sig")
    return d[d["accepted"].astype(bool)]


def sessions(case):
    path = paths.DATA_ROOT / "_sessions" / "index.json"
    if not path.exists():
        pytest.skip("먼저 lefdata sessions")
    ix = json.loads(path.read_text(encoding="utf-8"))
    rows = ix["sessions"] if isinstance(ix, dict) else ix
    ev = f"{event(case).parent.name}/{event(case).name}"
    return {s["sid"]: s for s in rows if s.get("event_id") == ev and s.get("discipline") in ("다이나모", "점검")
            and any(b["block"] == "dyno" for b in s.get("data_status", []))}


@E.need
@pytest.mark.parametrize("case", CASES)
def test_every_dyno_file_is_converted(case):
    need(case)
    r = recordings(case)
    want = E[f"{case}.recordings"]
    assert (list(r["recording_id"]) == want) if isinstance(want, list) else (len(r) == want)
    assert sorted(r.loc[r["dc_suspect"].astype(bool), "recording_id"]) == E[f"{case}.dc_suspect"]
    if case == "evo":
        assert list(r["rows"]) == E["evo.rows"]
        assert r["speed_max_kmh"].between(*E.range("evo.speed_max_kmh")).all()
    for rec in r["recording_id"]:                                     # 시간은 늘기만 한다
        m = json.loads((event(case) / "normalized" / rec / "meta.json").read_text(encoding="utf-8"))
        assert m["time"]["ts_monotonic"] and m["time"]["t_abs_start"]


@E.need
@pytest.mark.parametrize("case", CASES)
def test_untimed_logger_recordings_are_paired_with_dyno_files(case):
    need(case)
    p = pairs(case)
    assert p.groupby("recording_id").size().to_dict() == E[f"{case}.pairs"]
    assert not p.duplicated(["device", "dyno_recording"]).any()       # 다이나모 파일 하나 = 레코딩 하나
    if case == "evo":
        x = p.sort_values("t_rel_s")
        assert x["t_rel_s"].iloc[0] == E.approx("evo.log055_first_t_rel_s")
        assert x["t_rel_s"].iloc[-1] == E.approx("evo.log055_last_t_rel_s")
        assert (p["corr"] >= E["evo.pair_corr_min"]).all()
    else:
        line = p[p["recording_id"] == E["lef26.line_recording"]].set_index("dyno_recording")
        assert line.loc[E["lef26.spike_dyno"], "t_rel_s"] == E.approx("lef26.spike_t_rel_s")
        assert line["g"].iloc[0] == E.approx("lef26.line_g")
        assert p.loc[p["recording_id"] == E["lef26.pair_recording"], "t_rel_s"].iloc[0] == E.approx("lef26.pair_t_rel_s")
        assert set(p.loc[p["recording_id"] != E["lef26.line_recording"], "method"]) == {"notes"}


@E.need
def test_evo_dyno_sessions_carry_the_backup_logger():
    need("evo")
    ss = sessions("evo")
    assert len(ss) == E["evo.sessions"]
    s = ss[E["evo.first_sid"]]
    assert s["start"].startswith(E["evo.first_start"]) and s["blocks"]["have"] == E["evo.first_blocks"]


@E.need
def test_lef26_dyno_sessions():
    need("lef26")
    ss = sessions("lef26")
    assert len(ss) == E["lef26.sessions"]
    assert sum(s["discipline"] == "점검" and s["default_hidden"] for s in ss.values()) == E["lef26.checks"]
    spike = ss[E["lef26.spike_sid"]]
    assert spike["start"].startswith(E["lef26.spike_start"])
    assert [b for b in spike["blocks"]["have"]] == E["lef26.spike_blocks"]
    bad = ss[E["lef26.suspect_sid"]]
    assert "battery" in bad["blocks"]["warn"] and "net_wh" not in bad["stats"]
    assert {"motor", "imu"}.isdisjoint(ss[E["lef26.no_logger_sid"]]["blocks"]["have"])
