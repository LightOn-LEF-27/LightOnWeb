"""25Evo 차량 CAN 해독 (`evo_can`) — Monolith 가 받은 프레임 → 가속페달 · Orion · 모터 컨트롤러 표.

앞쪽은 만들어 낸 프레임을 인코딩해 다시 푸는 단위 테스트 (부호 · 체크섬 · 배율 · 같은 순번끼리 잇기).
뒤쪽은 우리 대회 데이터 — 서로 다른 프레임이 같은 물리량을 같게 말하는지(보편)와 개수 · 범위(골든, 값은 `evo_can.toml`).
"""

import json
from types import SimpleNamespace

import numpy as np
import pandas as pd
import pytest

from expected import Expected
from lefdata import catalog_build as cb, evo_can as ec, lef26, paths

E = Expected("evo_can")


def _be(v, n=2, signed=False):
    return int(v).to_bytes(n, "big", signed=signed)


def _le(v, n=2, signed=False):
    return int(v).to_bytes(n, "little", signed=signed)


def _frame(body7: bytes, extra: int = 0) -> np.ndarray:
    """7 바이트 + 체크섬(합 + extra 의 하위 8비트)."""
    return np.frombuffer(body7 + bytes([(sum(body7) + extra) & 0xFF]), dtype=np.uint8)


def test_orion_current_and_power_are_signed_and_checked():
    """공식 DBC 는 팩 전류 · 전력을 부호 없음으로 적지만 회생은 음수다 — 0x8000 이상을 음수로 읽는다."""
    d = np.stack([_frame(_be(2873) + _be(-123, signed=True) + bytes(3)), _frame(_be(2873) + _be(2345) + bytes(3))])
    vi = ec.decode_orion_vi(d)
    assert vi["pack_v"].tolist() == pytest.approx([287.3, 287.3]) and vi["current_a"].tolist() == pytest.approx([-12.3, 234.5])
    assert vi["checksum_ok"].all()
    bad = d.copy()
    bad[0, 7] ^= 1
    assert ec.decode_orion_vi(bad)["checksum_ok"].tolist() == [False, True]
    s = ec.decode_orion_summary(np.stack([_frame(_be(-39, signed=True) + _be(41028) + bytes([30, 251, 192]))]))
    assert s.iloc[0][["power_kw", "cell_min_v", "temp_max_c", "temp_min_c", "soc_pct"]].tolist() == pytest.approx([-3.9, 4.1028, 30, -5, 96.0])


def test_orion_stream_flips_the_current_and_checks_with_the_id():
    """0x6D0 은 방전을 − 로 보낸다 (방전 + 로 뒤집는다). 체크섬에 ID + 8 이 들어간다."""
    body = bytes([40]) + _be(-1000, signed=True) + _be(2860) + _be(31)
    d = np.stack([_frame(body, ec.ORION_STREAM + 8)])
    st = ec.decode_orion_stream(d)
    assert st.iloc[0][["slot", "current_a", "pack_v", "slot_raw"]].tolist() == pytest.approx([40, 100.0, 286.0, 31])
    assert st["checksum_ok"].all() and not ec.decode_orion_stream(np.stack([_frame(body)]))["checksum_ok"].any()
    sig = ec.stream_signals(st.assign(t_s=[1.0]))
    assert sig["temp_max_c"][1].tolist() == [31.0]
    neg = ec.decode_orion_stream(np.stack([_frame(bytes([41]) + _be(0) + _be(2860) + _be(-3, signed=True), ec.ORION_STREAM + 8)]))
    assert ec.stream_signals(neg.assign(t_s=[0.0]))["temp_min_c"][1].tolist() == [-3.0]          # 온도 슬롯은 부호 있음


def test_apps_frames_are_little_endian_and_join_by_counter(tmp_path):
    """0x7A0 · 0x7A1 · 0x7A2 — 리틀 엔디언, 같은 순번끼리 한 줄로. 토크 명령은 부호 있는 값."""
    rows, t0 = [], pd.Timestamp("2027-06-05 10:00", tz="Asia/Seoul")
    for k in range(6):
        ms = 50 * k
        pedal = 100 * k                                                        # 0.1 % 단위 → 0 ~ 50 %
        for cid, data, dt in ((0x7A0, _le(600 + k) + _le(450) + _le(pedal) + bytes([0x0F, k]), 0),
                              (0x7A1, _le(pedal) + _le(pedal + 3) + _le(-int(pedal * 32.767), signed=True) + bytes([0, k]), 2),
                              (0x7A2, _le(0x10 if k == 5 else 0) + _le(0) + _le(k) + bytes([0, k]), 4)):
            rows.append({"t_rel_ms": ms + dt, "t_abs": t0 + pd.Timedelta(milliseconds=ms + dt), "kind": "CAN", "can_id": float(cid),
                         "payload": _le(cid, 4) + bytes([0, 0, 8, 0]) + data, "valid": True, "dup": False})
    pd.DataFrame(rows).to_parquet(tmp_path / "frames.parquet", index=False)
    a = ec.decode_recording(tmp_path)["apps"]
    assert len(a) == 6 and a["pedal_pct"].tolist() == pytest.approx([0, 10, 20, 30, 40, 50])
    assert a["apps2_pct"].tolist() == pytest.approx(np.array([0, 10, 20, 30, 40, 50]) + 0.3)
    cmd = ec.TORQUE_SIGN * a["torque_cmd_raw"] / ec.TORQUE_FULL * 100
    assert cmd.tolist() == pytest.approx(a["pedal_pct"].tolist(), abs=0.01)                     # 전진 명령이 음수 → 가속 + 로
    s = ec.summarize({"apps": a})["apps"]
    assert s["counter_missed"] == 0 and s["valid_share"] == 1.0 and ec.fault_names(int(a["status"].max())) == ["DIFF_FAULT"]


def _bam_row(t_ms: int, reg: int, raw: int, n: int) -> dict:
    """0x181 프레임 한 줄 — 레지스터 번호 + 값 (n = 값 바이트 수. 부호는 2 바이트일 때만)."""
    body = bytes([reg]) + int(raw).to_bytes(n, "little", signed=n == 2)
    return {"t_rel_ms": t_ms, "t_abs": pd.Timestamp("2027-06-05 10:00", tz="Asia/Seoul") + pd.Timedelta(milliseconds=t_ms), "kind": "CAN", "can_id": float(ec.BAMOCAR),
            "payload": _le(ec.BAMOCAR, 4) + bytes([0, 0, n + 2, 0]) + body.ljust(8, b"\0"), "valid": True, "dup": False}     # 길이 = 번호 + 값 + 뒤 1 바이트


def test_bamocar_values_have_two_lengths_and_keep_the_sign(tmp_path):
    """DLC 4 = 부호 있는 16비트 (전진 회전수는 음수), DLC 6 = 부호 없는 32비트 (최상위 비트가 켜진 상태값도 양수)."""
    rows = [_bam_row(0, ec.REG_SPEED, -16384, 2), _bam_row(10, ec.REG_STATUS, (1 << 31) | (1 << 14) | 1, 4), _bam_row(20, ec.REG_VDC, 15000, 2),
            _bam_row(30, ec.REG_ERROR, 1 << ec.ERROR_VOLTAGE_MAX, 4), _bam_row(40, ec.REG_T_IGBT, 19000, 2), _bam_row(50, ec.REG_T_MOTOR, 11080, 2)]
    pd.DataFrame(rows).to_parquet(tmp_path / "frames.parquet", index=False)
    b = ec.decode_recording(tmp_path)["bamocar"]
    assert b["raw"].tolist() == [-16384, (1 << 31) | (1 << 14) | 1, 15000, 1 << ec.ERROR_VOLTAGE_MAX, 19000, 11080]
    sig = ec.bamocar_signals(b, "t_rel_ms")
    assert sig["rpm"][1][0] == pytest.approx(-16384 / 32767 * ec.LOGGER_N100_RPM)
    assert sig["vdc_v"][1][0] == pytest.approx(15000 * lef26.BAMOCAR_VDC_V_PER_NUM)
    assert sig["enabled"][1].tolist() == [1.0] and sig["ready"][1].tolist() == [1.0]
    assert sig["t_motor_c"][1][0] == pytest.approx(30.0)                                        # 표의 한 점 (11080 Num = 30 ℃)
    assert np.isfinite(sig["t_igbt_c"][1][0])


def test_bamocar_length_outside_the_two_formats_is_kept_empty():
    d = np.zeros((1, 8), np.uint8)
    d[0, 0] = ec.REG_VDC
    b = ec.decode_bamocar(d, np.array([3]))
    assert b["reg"].tolist() == [ec.REG_VDC] and b["raw"].isna().all() and not ec.bamocar_signals(b.assign(t_s=[0.0]))


def test_bamocar_hv_off_is_not_a_fault_but_overvoltage_is(tmp_path):
    """HV 를 꺼 두면 '전원 전압 없음' 비트가 늘 켜져 있다 — 고장으로 세지 않는다. 상위 16비트는 경고."""
    low, high, warn = 1 << ec.ERROR_VOLTAGE_MIN, 1 << ec.ERROR_VOLTAGE_MAX, 1 << 24
    assert ec.bamocar_error_bits(np.array([low, high | low, warn])).tolist() == [0, high, 0]
    assert ec.bamocar_error_names(high) == ["Mains Voltage Max. Limit"] and ec.bamocar_warning_names(warn) == ["Vout Saturation Max. Limit"]
    b = pd.DataFrame([_bam_row(0, ec.REG_ERROR, low, 4), _bam_row(100, ec.REG_ERROR, high | low, 4), _bam_row(200, ec.REG_ERROR, warn, 4), _bam_row(300, ec.REG_VDC, 18000, 2)])
    b.to_parquet(tmp_path / "frames.parquet", index=False)
    s = ec.summarize(ec.decode_recording(tmp_path))["bamocar"]
    assert s["overvoltage_frames"] == 1 and s["errors_any"] == ["Mains Voltage Max. Limit"] and s["warnings_any"] == ["Vout Saturation Max. Limit"]
    assert s["regs"] == {"0x8F": 3, "0xEB": 1} and s["vdc_max_v"] == pytest.approx(18000 * lef26.BAMOCAR_VDC_V_PER_NUM, abs=0.05)


def _motor_run():
    """_can_motor_block 이 쓰는 만큼만 있는 세션 구간 — 60 s, 랩 하나."""
    t = np.arange(0.0, 60.0, 0.1)
    return SimpleNamespace(rec="rec", laps=pd.DataFrame({"t_start_s": [0.0], "t_end_s": [60.0]}), ts=t, gps=pd.DataFrame({"speed_kmh": np.abs(30 * np.sin(t / 5))}),
                           where=lambda p: p, extra=lambda k: {})


def _motor_table(error_raw: int = 1 << ec.ERROR_VOLTAGE_MIN, regs=(ec.REG_SPEED, ec.REG_VDC, ec.REG_T_IGBT, ec.REG_T_MOTOR, ec.REG_STATUS, ec.REG_ERROR)):
    t = np.arange(0.0, 60.0, 0.1)
    speed = (-30 * np.sin(t / 5) * 250).astype(np.int64)
    val = {ec.REG_SPEED: speed, ec.REG_VDC: np.full(len(t), 15000), ec.REG_T_IGBT: np.full(len(t), 19000), ec.REG_T_MOTOR: np.full(len(t), 11080),
           ec.REG_STATUS: np.full(len(t), (1 << 14) | 1), ec.REG_ERROR: np.where((t >= 20) & (t < 21), error_raw, 1 << ec.ERROR_VOLTAGE_MIN)}
    return pd.concat([pd.DataFrame({"t_s": t, "reg": r, "raw": pd.array(val[r], dtype="Int64")}) for r in regs], ignore_index=True)


def test_motor_block_uses_the_same_channel_names_as_the_backup_logger():
    """값을 주는 장비가 바뀌어도 사이트가 보는 이름은 같다 (STM32 로거의 t_igbt_c · t_motor_c · vdc_v · rpm). HV 를 꺼 둔 비트만 있으면 경고 없음."""
    r = _motor_run()
    oc = pd.DataFrame({"t_s": [0.0, 60.0], "pack_v": [271.0, 271.0], "current_a": [0.0, 0.0], "checksum_ok": [True, True]})
    b = cb._can_motor_block(r, _motor_table(), oc)
    assert b.key == "motor" and not b.findings and {c.key for c in b.channels} == {"t_igbt_c", "t_motor_c", "vdc_v", "rpm", "enabled", "ready"}
    rpm = next(c for c in b.channels if c.key == "rpm")
    assert rpm.v.max() > 0 and rpm.sign == "전진 +" and "로거" in rpm.derivation          # 기록 부호를 뒤집어 전진 + · 실제 rpm 이라고 하지 않는다
    assert next(c for c in b.channels if c.key == "ready").levels == [{"value": 0, "label": "꺼짐"}, {"value": 1, "label": "켜짐"}]
    assert r.laps["motor_t_max_c"].iloc[0] == pytest.approx(30.0) and "inverter_t_max_c" in r.laps
    assert "오류: 없음" in b.facts[-1] and "GPS 를 +0.0 s" in b.checks[0]
    assert b.checks[1] == f"DC 버스 − Orion 팩 전압 {15000 * lef26.BAMOCAR_VDC_V_PER_NUM - 271.0:+.1f} V (전류 3 A 미만인 순간의 가운데값)"


def test_motor_block_warns_with_the_moment_of_a_controller_error():
    b = cb._can_motor_block(_motor_run(), _motor_table(1 << ec.ERROR_VOLTAGE_MAX), None)
    f = b.findings[0]
    assert f["code"] == "inverter_fault" and "Mains Voltage Max. Limit" in f["message"] and b.status == cb.WARN
    assert f["t_ranges"][0][0] == pytest.approx(20.0, abs=0.11) and f["t_ranges"][0][1] == pytest.approx(21.0, abs=0.11)


def test_status_only_records_make_no_motor_block():
    assert cb._can_motor_block(_motor_run(), _motor_table(regs=(ec.REG_STATUS, ec.REG_ERROR)), None) is None


# ---------------------------------------------------------------- 우리 대회 데이터

REC = paths.LEF25EVO_FSK / "normalized" / "2026-08-29_105716"
need = pytest.mark.skipif(not (REC / "apps.parquet").exists(), reason="25Evo 대회 lefdata evo-can 결과 없음")


@need
def test_race_frames_agree_with_each_other():
    """서로 다른 프레임이 같은 물리량을 같게 말한다 — 0x447 전류 ↔ 0x6D0 전류, 0x446 전력 ↔ 0x447 V × I, 토크 명령 ↔ 페달."""
    ap, oc, st = (pd.read_parquet(REC / f"{n}.parquet") for n in ("apps", "orion_can", "orion_stream"))
    assert oc["checksum_ok"].all() and st["checksum_ok"].all() and oc["summary_ok"].fillna(True).all()
    i = np.interp(oc["t_rel_ms"], st["t_rel_ms"], st["current_a"])
    assert np.corrcoef(oc["current_a"], i)[0, 1] > 0.99
    ok = oc["power_kw"].notna()
    assert np.corrcoef(oc.loc[ok, "power_kw"], oc.loc[ok, "pack_v"] * oc.loc[ok, "current_a"] / 1000)[0, 1] > 0.99
    assert ap["pedal_pct"].between(0, 100).all() and (ap["torque_cmd_raw"] <= 0).all()          # 이 차는 전진 명령이 음수
    cmd = ec.TORQUE_SIGN * ap["torque_cmd_raw"] / ec.TORQUE_FULL * 100
    assert np.corrcoef(cmd, ap["pedal_pct"])[0, 1] > 0.99
    assert (ap["apps1_pct"] - ap["apps2_pct"]).abs().max() < 10                                  # 규정의 비타당 기준 (10 %) 안


@need
@E.need
def test_race_can_golden():
    meta = json.loads((REC / "meta.json").read_text(encoding="utf-8"))["can_decoded"]
    assert {k: v["rows"] for k, v in meta.items()} == E["race.rows"]
    assert meta["apps"]["counter_missed"] == E["race.apps_counter_missed"] and meta["apps"]["fault_latched_any"] == E["race.apps_faults"]
    oc = pd.read_parquet(REC / "orion_can.parquet")
    assert [float(oc["soc_pct"].max()), float(oc["soc_pct"].min())] == E["race.soc_range"]
    assert meta["thermistor"]["thermistors"] == E["race.thermistors"] and meta["orion_can"]["boot_rows"] == E["race.orion_boot_rows"]


@need
def test_race_thermistor_values_lie_between_the_pack_low_and_high():
    """써미스터 하나씩 방송의 값은 같은 프레임의 최저 ~ 최고 안 (해독이 맞다), 모듈 요약은 체크섬이 맞는다.
    Orion 의 0x446 최고 온도(켠 직후 뺀 것)와 모듈 최고는 1 ℃ 안 (같은 써미스터를 본다)."""
    th = pd.read_parquet(REC / "thermistor.parquet")
    one = th[th["source"] == "single"]
    assert ((one["lowest_c"] <= one["temp_c"]) & (one["temp_c"] <= one["highest_c"])).all() and not one["fault"].any()
    assert th.loc[th["source"] == "module_to_bms", "checksum_ok"].all()
    oc = pd.read_parquet(REC / "orion_can.parquet").dropna(subset=["temp_max_c"]).sort_values("t_rel_ms")
    near = pd.merge_asof(oc[["t_rel_ms", "temp_max_c"]], one[["t_rel_ms", "highest_c"]].sort_values("t_rel_ms"), on="t_rel_ms", direction="nearest", tolerance=200)
    assert (near["temp_max_c"] - near["highest_c"]).abs().max() <= 1


need_bam = pytest.mark.skipif(not (REC / "bamocar.parquet").exists(), reason="25Evo 대회 lefdata evo-can 결과 없음 (bamocar)")


@need_bam
@E.need
def test_race_bamocar_registers_and_scale():
    """요청한 레지스터가 다 오고, DC 버스 전압이 BMS 팩 전압과, 회전수가 GPS 속도와 같은 크기로 맞는다 — 배율 · 레지스터 뜻의 근거."""
    b = pd.read_parquet(REC / "bamocar.parquet")
    assert [f"0x{r:02X}" for r in sorted(b["reg"].unique())] == E["bamocar.registers"] and b["raw"].notna().all()
    eb = b[b["reg"] == ec.REG_VDC]
    oc = pd.read_parquet(REC / "orion_can.parquet")
    pack, cur = (np.interp(eb["t_rel_ms"], oc["t_rel_ms"], oc[k]) for k in ("pack_v", "current_a"))
    calm = (pack > 200) & (np.abs(cur) < 3) & (eb["raw"].to_numpy(float) > 10000)
    assert np.median(pack[calm] / eb["raw"].to_numpy(float)[calm]) == E.approx("bamocar.vdc_v_per_num_vs_orion")
    assert np.median(eb["raw"].to_numpy(float)[calm] * lef26.BAMOCAR_VDC_V_PER_NUM - pack[calm]) == E.approx("bamocar.dc_minus_pack_v")
    run = paths.LEF25EVO_FSK / "normalized" / E["bamocar.speed_recording"]
    n = pd.read_parquet(run / "bamocar.parquet")
    n = n[n["reg"] == ec.REG_SPEED]
    g = pd.read_parquet(run / "gps.parquet")
    g = g[g["valid"]]
    sp = np.interp(n["t_rel_ms"], g["t_rel_ms"], g["speed_kmh"])
    fast = sp > 15
    x = np.abs(n["raw"].to_numpy(float))[fast]
    assert (x * sp[fast]).sum() / (sp[fast] ** 2).sum() == E.approx("bamocar.raw_per_kmh")


@E.need
def test_server_only_recordings_read_like_sd_logs():
    """서버 DB 에서 되살린 로그 — 기존 변환기가 그대로 읽고, 트랙을 달린 레코딩은 랩이 나뉜다."""
    ev = paths.DATA_ROOT / "LEF-25evo" / E["server_only.event"]
    if not (ev / "derived" / "laps.csv").exists():
        pytest.skip("서버에서 옮긴 이벤트 없음")
    rec = pd.read_csv(ev / "normalized" / "recordings.csv", encoding="utf-8-sig")
    assert len(rec) == E["server_only.recordings"] and rec["invalid"].sum() == 0
    laps = pd.read_csv(ev / "derived" / "laps.csv", encoding="utf-8-sig")
    assert laps["recording_id"].unique().tolist() == [E["server_only.track_recording"]] and len(laps) == E["server_only.laps"]
    assert laps["distance_m"].between(*E["server_only.lap_distance_m"]).all()                   # 2 Hz 로도 랩 거리는 코스 길이 안쪽에 모인다


@need_bam
@E.need
def test_race_overvoltage_only_in_the_two_kill_switch_recordings():
    """DC 버스 과전압 오류는 전원을 끊으며 달린 두 레코딩에만 있다. 나머지 레코딩의 오류 표는 비어 있다."""
    got = {}
    for m in sorted((paths.LEF25EVO_FSK / "normalized").glob("*/meta.json")):
        s = json.loads(m.read_text(encoding="utf-8")).get("can_decoded", {}).get("bamocar")
        if s and s["errors_any"]:
            assert s["errors_any"] == ["Mains Voltage Max. Limit"] and s["overvoltage_frames"] > 0, m.parent.name
            got[m.parent.name] = s["vdc_max_v"]
    assert got == E["bamocar.overvoltage_vdc_max_v"]


def test_unfiltered_dc_bus_0x66_is_its_own_channel_with_the_same_scale(tmp_path):
    """0x66 (필터 없는 DC 버스) 는 0xEB 와 같은 배율로 풀고, 이름을 나눠 둔다 (25Evo 는 0xEB 가 vdc_v)."""
    rows = [_bam_row(0, ec.REG_VDC, 15000, 2), _bam_row(5, ec.REG_VDC_FAST, 15500, 2), _bam_row(10, ec.REG_VDC_FAST, 16000, 2)]
    pd.DataFrame(rows).to_parquet(tmp_path / "frames.parquet", index=False)
    tables = ec.decode_recording(tmp_path)
    sig = ec.bamocar_signals(tables["bamocar"], "t_rel_ms")
    assert sig["vdc_v"][1].tolist() == pytest.approx([15000 * lef26.BAMOCAR_VDC_V_PER_NUM])
    assert sig["vdc_fast_v"][1].tolist() == pytest.approx([15500 * lef26.BAMOCAR_VDC_V_PER_NUM, 16000 * lef26.BAMOCAR_VDC_V_PER_NUM])
    s = ec.summarize(tables)["bamocar"]
    assert s["regs"] == {"0x66": 2, "0xEB": 1} and s["vdc_fast_max_v"] == pytest.approx(16000 * lef26.BAMOCAR_VDC_V_PER_NUM, abs=0.05)


def test_motor_block_carries_the_unfiltered_dc_bus_when_it_is_requested():
    fast = pd.DataFrame({"t_s": np.arange(0.0, 60.0, 0.005), "reg": ec.REG_VDC_FAST, "raw": pd.array(np.full(12000, 15200), dtype="Int64")})
    blk = cb._can_motor_block(_motor_run(), pd.concat([_motor_table(), fast], ignore_index=True), None)
    ch = {c.key: c for c in blk.channels}
    assert "vdc_fast_v" in ch and len(ch["vdc_fast_v"].t_s) == 12000 and not any("10 Hz" in c for c in ch["vdc_fast_v"].cautions)
    assert "vdc_fast_v" not in {c.key for c in cb._can_motor_block(_motor_run(), _motor_table(), None).channels}


def test_torque_and_flux_current_flip_to_drive_plus_and_give_phase_current():
    """0x27 · 0x28 (Iq · Id, 요청할 때만) → motor.iq_a · id_a 는 구동 + (기록은 구동할 때 −), 둘이 있으면 상전류 크기."""
    t = np.arange(0.0, 60.0, 0.01)
    cur = pd.concat([pd.DataFrame({"t_s": t, "reg": r, "raw": pd.array(np.full(len(t), v), dtype="Int64")})
                     for r, v in ((ec.REG_IQ, -300), (ec.REG_ID, -400))], ignore_index=True)
    sig = ec.bamocar_signals(cur)
    assert sig["iq_a"][1][0] == pytest.approx(-300 * lef26.BAMOCAR_I_A_PER_NUM)
    ch = {c.key: c for c in cb._can_motor_block(_motor_run(), pd.concat([_motor_table(), cur], ignore_index=True), None).channels}
    assert ch["iq_a"].v[0] == pytest.approx(300 * lef26.BAMOCAR_I_A_PER_NUM) and ch["id_a"].v[0] == pytest.approx(400 * lef26.BAMOCAR_I_A_PER_NUM)
    assert np.nanmax(ch["i_phase_a"].v) == pytest.approx(500 * lef26.BAMOCAR_I_A_PER_NUM)
    assert not any("10 Hz" in c for c in ch["iq_a"].cautions)


# ---------------------------------------------------------------- Orion 이 켜진 직후 · 써미스터 확장 모듈

def _can_row(t_ms: int, cid: int, data: bytes) -> dict:
    """Monolith CAN 레코드 한 줄 (id u32 · 확장 · 원격 · 길이 · 예약 · 데이터 8)."""
    return {"t_rel_ms": t_ms, "t_abs": pd.Timestamp("2027-06-05 10:00", tz="Asia/Seoul") + pd.Timedelta(milliseconds=t_ms), "kind": "CAN",
            "can_id": float(cid), "payload": _le(cid, 4) + bytes([int(cid > 0x7FF), 0, 8, 0]) + data, "valid": True, "dup": False}


def _summary(cell: float, hi: int, lo: int) -> bytes:
    return bytes(_frame(_be(0) + _be(round(cell * 1e4)) + bytes([hi & 0xFF, lo & 0xFF, 190])))


def test_orion_boot_defaults_are_blanked_at_power_on_and_after_a_restart():
    """켠 직후 (레코딩 처음 · 오래 끊긴 뒤) 최고 · 최저 25 / 25 가 짧게 이어지면 기본값. 기록 중간의 25 / 25 · 오래 이어지는 25 / 25 는 실제 값."""
    t = np.arange(40) * 96.0
    t[20:] += 18_000                                                        # 18 s 끊겼다 다시 옴 = Orion 이 다시 켜짐
    hi = np.array([25] * 10 + [14] * 5 + [25] * 2 + [14] * 3 + [25] * 9 + [14] * 11)
    lo = np.array([25] * 10 + [11] * 5 + [25] * 2 + [11] * 3 + [25] * 9 + [11] * 11)
    boot = ec.orion_boot(t, hi, lo)
    assert boot.tolist() == [True] * 10 + [False] * 10 + [True] * 9 + [False] * 11
    assert not ec.orion_boot(np.arange(30) * 96.0, np.full(30, 25), np.full(30, 25)).any()          # 25 ℃ 로 오래 = 실제 온도
    cell = np.r_[3.7, np.full(39, 4.0)]
    s = ec.mark_orion_boot(pd.DataFrame({"t_rel_ms": t, "temp_max_c": hi.astype(float), "temp_min_c": lo.astype(float), "cell_min_v": cell}))
    assert s["temp_max_c"].isna().sum() == 19 and s["temp_max_c"].iloc[15] == 25 and s["orion_boot"].sum() == 19
    assert np.isnan(s["cell_min_v"].iloc[0]) and s["cell_min_v"].iloc[1:].notna().all()             # 첫 프레임의 튄 최저 셀만 뺀다


def test_recording_keeps_pack_values_but_not_boot_temperatures(tmp_path):
    """켠 직후 구간: orion_can 의 팩 전압 · 전류는 그대로, 온도는 빈 값. 같은 구간의 0x6D0 슬롯도 세션 값에서 뺀다."""
    rows = []
    for k in range(15):
        ms = 96 * k
        hot = 25 if k < 10 else 31
        rows.append(_can_row(ms, ec.ORION_SUMMARY, _summary(4.1, hot, 25 if k < 10 else 29)))
        rows.append(_can_row(ms + 2, ec.ORION_VI, bytes(_frame(_be(2870) + _be(0) + bytes(3)))))
        rows.append(_can_row(ms + 4, ec.ORION_STREAM, bytes(_frame(bytes([40]) + _be(0) + _be(2870) + _be(hot), ec.ORION_STREAM + 8))))
    pd.DataFrame(rows).to_parquet(tmp_path / "frames.parquet", index=False)
    t = ec.decode_recording(tmp_path)
    oc, st = t["orion_can"], t["orion_stream"]
    assert oc["orion_boot"].tolist() == [True] * 10 + [False] * 5 and oc["pack_v"].tolist() == pytest.approx([287.0] * 15)
    assert oc["temp_max_c"].isna().sum() == 10 and oc["temp_max_c"].dropna().tolist() == [31.0] * 5
    assert st["orion_boot"].sum() == 10 and ec.stream_signals(st, "t_rel_ms")["temp_max_c"][1].tolist() == [31.0] * 5
    assert ec.summarize(t)["orion_can"]["boot_rows"] == 10


def test_thermistor_module_frames_decode_like_the_lef26_table(tmp_path):
    """0x1838F380 = 써미스터 하나 (번호 2 바이트 · 값 · 모듈 안 번호 · 최저 · 최고 · 가장 뜨거운 · 차가운 번호, 체크섬 없음),
    0x1839F380 = 모듈 → BMS 요약 (체크섬 = 합 + 0x39 + 8). 온도는 부호 있는 8비트."""
    one = [bytes([0, 12, 17, 12, 14, 17, 22, 10]), bytes([0, 10, (-3) & 0xFF, 10 | 0x80, (-3) & 0xFF, 17, 22, 10])]
    body = bytes([0, 14, 17, 15, 40, 22, 10])
    rows = [_can_row(0, ec.THERM_SINGLE, one[0]), _can_row(100, ec.THERM_SINGLE, one[1]),
            _can_row(150, ec.THERM_MODULE, body + bytes([(sum(body) + ec.THERM_MODULE_CHECK) & 0xFF])),
            _can_row(250, ec.THERM_MODULE, body + bytes([0]))]
    pd.DataFrame(rows).to_parquet(tmp_path / "frames.parquet", index=False)
    tables = ec.decode_recording(tmp_path)
    th = tables["thermistor"]
    assert th["source"].tolist() == ["single", "single", "module_to_bms", "module_to_bms"]
    assert th["thermistor_id"].iloc[:2].tolist() == [12, 10] and th["temp_c"].iloc[:2].tolist() == [17, -3] and th["lowest_c"].iloc[1] == -3
    assert th["fault"].tolist() == [False, True, False, False] and th["hottest_id"].iloc[0] == 22 and th["coldest_id"].iloc[0] == 10
    assert th["average_c"].iloc[2] == 15 and th["enabled"].iloc[2] == 40 and th["checksum_ok"].iloc[2:].tolist() == [True, False]
    assert set(lef26.THERMISTOR_INT) - {"thermistor_id"} <= set(th.columns)              # LEF-26 thermistor 표와 같은 열 이름
    s = ec.summarize(tables)["thermistor"]
    assert s == {"rows": 4, "single_rows": 2, "thermistors": 2, "module_checksum_fail": 1, "fault_rows": 1}
    pd.DataFrame(rows[:2]).to_parquet(tmp_path / "frames.parquet", index=False)           # 하나씩 방송만 있는 레코딩도 같은 열
    only = ec.decode_recording(tmp_path)["thermistor"]
    assert {"average_c", "enabled", "checksum_ok"} <= set(only.columns) and only["average_c"].isna().all()


def test_thermistor_block_gives_the_spread_and_each_thermistor_rise():
    """써미스터 하나씩 → 팩 최고 · 최저 · 차 채널, 랩마다 차 최대, 써미스터마다 처음 → 끝 (내보낼 표)."""
    t = np.arange(0.0, 60.0, 0.1)
    tid = np.arange(len(t)) % 3
    temp = 15 + tid + (t > 30)                                               # 30 s 뒤 모두 1 ℃ 오름
    hi, lo = 17 + (t > 30), 15 + (t > 30)
    th = pd.DataFrame({"t_s": t, "source": "single", "thermistor_id": pd.array(tid, dtype="Int64"), "temp_c": pd.array(temp, dtype="Int64"),
                       "lowest_c": lo, "highest_c": hi, "hottest_id": 2, "coldest_id": 0, "fault": False})
    r = _motor_run()
    b = cb._can_thermistor_block(r, th)
    ch = {c.key: c for c in b.channels}
    assert b.key == "thermistor" and set(ch) == {"highest_c", "lowest_c", "spread_c"} and np.allclose(ch["spread_c"].v, 2) and ch["spread_c"].derived
    assert r.laps["cell_t_spread_c"].iloc[0] == 2 and b.checks == ["써미스터 값이 같은 프레임의 팩 최저 ~ 최고 안 100.0%"]
    per = b.exports[0].table
    assert per["thermistor_id"].tolist() == [0, 1, 2] and per["rise_c"].tolist() == [1, 1, 1] and per["below_hot_c"].tolist() == [2, 1, 0]
    assert any("써미스터 3개" in f and "0.3 s" in f for f in b.facts)
    assert cb._can_thermistor_block(r, th.assign(fault=True)) is None                    # 고장 표시만 있으면 블록 없음
