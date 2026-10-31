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
    assert rpm.v.min() < 0 and rpm.sign == "전진할 때 −" and "로거" in rpm.derivation                    # 실제 rpm 이라고 하지 않는다
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
