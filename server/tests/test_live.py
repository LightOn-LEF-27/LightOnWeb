"""실시간 화면 API (계약 §12) — 수신 파일을 따라 읽고 엔진 상태를 그대로 돌려준다. 만들어 낸 메시지만 쓴다."""

import base64
import json
import struct

import pytest
from fastapi.testclient import TestClient

from lefsite.app import create_app
from lefsite.live import day_of

from .conftest import BASE, settings

pytest.importorskip("lefdata.live")

NOW = 1_794_000_000.0          # 고정 시계 (서버 시각)
VENUE = {"name": "test", "frame_id": "venue/test", "origin": {"lat0": 35.0, "lon0": 126.0},
         "gate": {"x": 0.0, "y": 0.0, "heading_deg": 90.0}, "half_width_m": 30.0, "line": {"x_m": [-50.0, 50.0], "y_m": [0.0, 0.0]}}


def rec(rtype: int, ts: int, pl: bytes) -> bytes:
    body = bytearray(struct.pack("<BBHI", 0xAE, rtype, 0, ts) + pl)
    x = 0
    for w in struct.unpack("<6I", bytes(body)):
        x ^= w
    struct.pack_into("<H", body, 2, ((x & 0xFFFF) + (x >> 16)) & 0xFFFF)
    return bytes(body)


def summary_frame(ts: int, power_kw: float) -> bytes:
    body = int(round(power_kw * 10)).to_bytes(2, "big", signed=True) + (39000).to_bytes(2, "big") + bytes([40, 35, 160])
    return rec(2, ts, struct.pack("<IBBBB", 0x446, 0, 0, 8, 0) + body + bytes([sum(body) & 0xFF]))


def lines(n: int, t_end: float, power_kw: float = 12.0) -> list[bytes]:
    out = []
    for k in range(n):
        t = t_end - (n - 1 - k) * 0.5
        dev = 10_000 + k * 500
        for topic, p in (("d", struct.pack("<II", dev, 0) + bytes(96)), ("d/can", summary_frame(dev - 100, power_kw))):
            out.append(json.dumps({"t_ns": int(t * 1e9), "topic": f"car/{topic}", "retain": False,
                                   "b64": base64.b64encode(p).decode()}).encode() + b"\n")
    return out


@pytest.fixture
def live_client(tmp_path):
    def make(**env) -> tuple[TestClient, object]:
        live_dir = tmp_path / "rx"
        live_dir.mkdir(exist_ok=True)
        app = create_app(settings(tmp_path, LEF_SITE_LIVE_DIR=str(live_dir), **env), start_live=False)
        feed = app.state.live
        feed.clock = lambda: NOW
        return TestClient(app, base_url=BASE), feed
    return make


def write(feed, chunks: list[bytes], mode: str = "ab") -> None:
    with open(feed.dir / f"{day_of(NOW)}.jsonl", mode) as f:
        f.write(b"".join(chunks))


def test_live_off_without_folder(client):
    r = client.get("/api/live/state")
    assert r.status_code == 404 and r.json()["error"]["code"] == "live_off"
    assert client.get("/api/live/track").json()["error"]["code"] == "live_off"


def test_state_follows_the_file_and_keeps_engine_shape(live_client):
    c, feed = live_client()
    assert c.get("/api/live/state").json()["link"]["status"] == "none"       # 파일이 아직 없다
    write(feed, lines(20, NOW - 0.1))
    assert feed.poll() == 40
    st = c.get("/api/live/state").json()
    assert st["contract"] == "0.16" and st["link"]["status"] == "ok"
    assert next(t for t in st["tiles"] if t["id"] == "battery.power_kw")["value"] == pytest.approx(12.0)
    assert feed.poll() == 0
    last = st["chart"]["t"][-1]
    assert c.get("/api/live/state", params={"since": last}).json()["chart"]["t"] == []


def test_partial_last_line_waits_for_the_rest(live_client):
    c, feed = live_client()
    one = lines(1, NOW)[0]
    write(feed, [one[:30]])
    assert feed.poll() == 0
    write(feed, [one[30:]])
    assert feed.poll() == 1


def test_bad_line_is_counted_not_fatal(live_client):
    c, feed = live_client()
    write(feed, [b"{not json\n"] + lines(2, NOW))
    assert feed.poll() == 5
    assert c.get("/api/live/state").json()["source"]["bad_rows"] == 1


def test_new_file_restarts_state(live_client):
    c, feed = live_client()
    write(feed, lines(10, NOW, power_kw=12.0))
    feed.poll()
    write(feed, lines(2, NOW, power_kw=20.0)[:2], mode="wb")              # 더 짧은 새 파일
    feed.poll()
    tile = next(t for t in c.get("/api/live/state").json()["tiles"] if t["id"] == "battery.power_kw")
    assert tile["value"] == pytest.approx(20.0)


def test_next_day_starts_empty(live_client):
    c, feed = live_client()
    write(feed, lines(10, NOW))
    feed.poll()
    feed.clock = lambda: NOW + 86_400
    feed.poll()
    assert c.get("/api/live/state").json()["link"]["status"] == "none"


def test_receiver_status_carries_over_to_the_next_day(live_client):
    c, feed = live_client()
    t0 = int((NOW - 86_400 * 2) * 1e9)                        # 이틀 전에 붙고, 어제 파일에는 사건이 없다
    (feed.dir / f"{day_of(NOW - 86_400 * 2)}.jsonl").write_bytes(
        json.dumps({"t_ns": t0, "event": "connected"}).encode() + b"\n")
    (feed.dir / f"{day_of(NOW - 86_400)}.jsonl").write_bytes(b"")
    feed.poll()
    st = c.get("/api/live/state").json()
    assert st["receiver"] == {"status": "connected", "since": pytest.approx(t0 / 1e9, abs=1e-3)} and st["events"] == []
    write(feed, [json.dumps({"t_ns": int(NOW * 1e9), "event": "disconnected"}).encode() + b"\n"])
    feed.poll()
    feed.clock = lambda: NOW + 86_400                          # 다음 날 — 오늘 끊긴 것이 이어진다
    feed.poll()
    assert c.get("/api/live/state").json()["receiver"]["status"] == "disconnected"


def test_track_needs_a_venue(live_client, tmp_path):
    c, _ = live_client()
    assert c.get("/api/live/track").json()["error"]["code"] == "no_venue"
    (tmp_path / "_venues").mkdir()
    (tmp_path / "_venues" / "test.json").write_text(json.dumps(VENUE), encoding="utf-8")
    c, _ = live_client(LEF_DATA_ROOT=str(tmp_path), LEF_SITE_LIVE_VENUE="test")
    t = c.get("/api/live/track").json()
    assert t["frame_id"] == "venue/test" and t["gate"]["heading_deg"] == 90.0


def test_wrong_venue_stops_the_server_at_start(tmp_path):
    with pytest.raises(FileNotFoundError):
        create_app(settings(tmp_path, LEF_SITE_LIVE_DIR=str(tmp_path), LEF_DATA_ROOT=str(tmp_path), LEF_SITE_LIVE_VENUE="nope"),
                   start_live=False)
