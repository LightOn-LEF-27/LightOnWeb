"""재생 — 펌웨어의 보내기 규칙 · 시각 옮기기 · 끊김 넣기 · 비밀 토픽 건너뛰기 (만들어 낸 기록만)."""

import base64
import json
import struct

import pytest

import replay as rp


def rec(rtype: int, ts: int, pl: bytes = bytes(16)) -> bytes:
    b = bytearray(struct.pack("<BBHI", 0xAE, rtype, 0, ts) + pl)
    struct.pack_into("<H", b, 2, rp.checksum(bytes(b)))
    return bytes(b)


def can(ts: int, can_id: int) -> bytes:
    return rec(rp.T_CAN, ts, struct.pack("<IBBBB", can_id, 0, 0, 8, 0) + bytes(8))


def sd_file(tmp_path, recs) -> str:
    p = tmp_path / "x.log"
    p.write_bytes(rec(rp.T_BOOT, 0) + b"".join(recs) + b"\x00" * 7)      # 끝의 잘린 조각은 버린다
    return p


def test_checksum_matches_engine():
    lefdata = pytest.importorskip("lefdata.monolith")
    import numpy as np
    rs = [rec(rp.T_GPS, 1234), can(99, 0x446), rp.retime(can(99, 0x446), 5)]
    assert lefdata.checksum_ok(np.frombuffer(b"".join(rs), np.uint8).reshape(-1, 24)).all()


def test_can_is_thinned_per_id_and_logbuf_carries_latest():
    recs = [can(t, 0x446) for t in range(0, 3000, 100)] + [can(t, 0x447) for t in range(50, 3000, 100)]
    recs += [rec(rp.T_GPS, t) for t in range(0, 3000, 100)]
    msgs = list(rp.simulate(recs))
    d = [m for m in msgs if m[1] == "d"]
    assert [m[0] for m in d][:3] == [504, 1008, 1512]
    last_gps = rp.ts_of(d[0][2][8:32])
    assert last_gps == 500                                       # 보내는 순간 가장 최근 GPS
    sent = [struct.unpack_from("<I", r, 8)[0] for m in msgs if m[1] == "d/can" for r in (m[2][i:i + 24] for i in range(0, len(m[2]), 24))]
    ts = {cid: [rp.ts_of(r) for m in msgs if m[1] == "d/can" for r in (m[2][i:i + 24] for i in range(0, len(m[2]), 24))
                if struct.unpack_from("<I", r, 8)[0] == cid] for cid in (0x446, 0x447)}
    assert ts[0x446] == list(range(0, 3000, 500)) and ts[0x447] == list(range(50, 3000, 500))
    assert sent.count(0x446) == 6


def test_can_batches_of_128():
    recs = [can(10, 0x100 + k) for k in range(300)]
    sizes = [len(m[2]) // 24 for m in rp.simulate(recs) if m[1] == "d/can"]
    assert sizes == [128, 128, 44]


def test_speed_rescales_device_time_and_keeps_checksums(tmp_path):
    p = sd_file(tmp_path, [rec(rp.T_GPS, t) for t in range(1000, 11000, 100)])
    rows = list(rp.from_sd(p, speed=2.0, delay_s=0.0))
    ats = [at for at, r in rows if r["topic"].endswith("/d")]
    assert ats[-1] == pytest.approx(10.0 / 2, abs=0.6)
    body = base64.b64decode(rows[-1][1]["b64"])
    assert rp.valid(body[8:32])


def test_start_duration_and_drop(tmp_path):
    p = sd_file(tmp_path, [rec(rp.T_GPS, t) for t in range(0, 60_000, 100)])
    rows = list(rp.from_sd(p, start_s=10, duration_s=20, drops=[(5.0, 10.0)]))
    ats = [at for at, r in rows]
    assert max(ats) < 20.5 and not any(5.0 <= a < 10.0 for a in ats)
    assert all(r["replay"] is True and r["topic"].startswith("replay/") for _, r in rows)


def test_jsonl_skips_config_topic_and_shifts_time(tmp_path):
    src = tmp_path / "in.jsonl"
    rows = [{"t_ns": 10**18, "topic": "car/d", "retain": False, "b64": "AA=="},
            {"t_ns": 10**18 + 5 * 10**8, "topic": "car/d/cfg", "retain": False, "b64": "c2VjcmV0"},
            {"t_ns": 10**18 + 10**9, "event": "connected"}]
    src.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="ascii")
    out = list(rp.from_jsonl(src, speed=2.0))
    assert [r.get("topic", r.get("event")) for _, r in out] == ["car/d", "connected"]
    assert out[1][0] == pytest.approx(0.5)


def test_emit_writes_today_file_with_timestamps(tmp_path):
    now = 1_794_000_000.0
    n = rp.emit([(0.0, {"topic": "replay/d", "b64": "AA==", "replay": True}), (1.5, {"event": "connected", "replay": True})],
                tmp_path, wait=False, clock=lambda: now)
    files = list(tmp_path.glob("*.jsonl"))
    lines = [json.loads(x) for x in files[0].read_text().splitlines()]
    assert n == 2 and len(files) == 1 and lines[1]["t_ns"] == int((now + 1.5) * 1e9)


def test_replayed_sd_drives_the_live_engine(tmp_path):
    live = pytest.importorskip("lefdata.live")
    summ = (1234).to_bytes(2, "big") + (39000).to_bytes(2, "big") + bytes([40, 35, 160])
    frame = struct.pack("<IBBBB", 0x446, 0, 0, 8, 0) + summ + bytes([sum(summ) & 0xFF])
    p = sd_file(tmp_path, [rec(rp.T_CAN, t, frame) for t in range(0, 20_000, 100)])
    L = live.Live()
    base = 1_794_000_000.0
    last = 0.0
    for at, row in rp.from_sd(p):
        L.feed({"t_ns": int((base + at) * 1e9), **row})
        last = at
    st = L.state(base + last)
    tile = next(t for t in st["tiles"] if t["id"] == "battery.power_kw")
    assert st["source"]["replay"] is True and st["link"]["status"] == "ok" and tile["value"] == pytest.approx(123.4)
