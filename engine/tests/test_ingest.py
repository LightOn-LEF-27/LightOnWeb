"""파일 올리기 (`ingest`, 계약 §7 · E4) — 판별 · 넣기 · 변환 → 세션 · 작업 기록.

앞쪽은 만든 파일로 (데이터 없이): 에너지미터 · Monolith 형식을 바이트로 지어 판별 · 거절 · 같은 파일 다시 올리기 · 이름 겹침 · 자물쇠 · 오류 코드.
뒤쪽은 우리 25Evo 대회 파일로 (있을 때만): 원본마다 판별한 레코딩 이름이 변환된 것과 같고, 두 파일을 올리면 세션까지 나온다.
"""

import json

import numpy as np
import pandas as pd
import pytest

from lefdata import api, energymeter as em, ingest, monolith as mono, paths
from lefdata.api import EngineError

T0 = pd.Timestamp("2027-06-05 10:00:00", tz="Asia/Seoul")


def _xor_fill(block: np.ndarray) -> None:
    """에너지미터 체크섬 — 워드를 전부 XOR 하면 0 이 되게 체크섬 자리(2 ~ 3 바이트)를 채운다."""
    block[2:4] = 0
    block[2:4] = np.frombuffer(np.bitwise_xor.reduce(block.view("<u2")).tobytes(), "u1")


def meter_log(path, start=T0, n=3000, amps=50.0, device=(0x00450047, 0x34345118, 0x30323836)):
    """FSK 에너지미터 .log — 머리 32 B + 10 ms 마다 16 B 레코드 (전압 280 V · 전류 amps)."""
    head = np.zeros(32, "u1")
    head[0], head[1] = em.MAGIC, em.T_HEADER
    head[4:8] = np.frombuffer(np.uint32(0).tobytes(), "u1")
    head[8:20] = np.frombuffer(np.array(device, "<u4").tobytes(), "u1")
    head[20] = 1                                                            # 프로토콜 버전 (v1.8.0 앞 펌웨어)
    head[24:30] = [start.year - 2000, start.month, start.day, start.hour, start.minute, start.second]
    head[30:32] = np.frombuffer(np.uint16(start.microsecond // 1000).tobytes(), "u1")
    _xor_fill(head)
    rec = np.zeros(n, em.RECORD)
    rec["magic"], rec["type"], rec["t_ms"] = em.MAGIC, em.T_RECORD, np.arange(n) * 10
    rec["ch"][:, 0], rec["ch"][:, 1], rec["ch"][:, 2], rec["ch"][:, 3] = 2800, int(amps * 10), 1200, 2500
    raw = rec.view("u1").reshape(n, 16)
    for r in raw:
        _xor_fill(r)
    path.write_bytes(head.tobytes() + raw.tobytes())
    return path


def monolith_log(path, boots=1, n=64):
    """Monolith .log — 24 B 레코드 (BOOT 를 boots 개, 나머지는 SYSTEM). 체크섬은 monolith.checksum_ok 의 규칙."""
    raw = np.zeros((n, 24), "u1")
    raw[:, 0] = mono.MAGIC
    raw[:, 1] = mono.T_SYS
    raw[:boots, 1] = mono.T_BOOT
    raw[:, 4:8] = np.frombuffer((np.arange(n, dtype="<u4") * 10).tobytes(), "u1").reshape(n, 4)
    raw[:boots, 16:24] = np.frombuffer(np.uint64(int(T0.timestamp())).tobytes(), "u1")
    words = raw.view("<u4").reshape(n, 6).copy()
    words[:, 0] &= 0x0000FFFF
    x = np.bitwise_xor.reduce(words, axis=1)
    raw[:, 2:4] = np.frombuffer((((x & 0xFFFF) + (x >> 16)) & 0xFFFF).astype("<u2").tobytes(), "u1").reshape(n, 2)
    assert mono.checksum_ok(raw).all()
    path.write_bytes(raw.tobytes())
    return path


@pytest.fixture()
def roots(tmp_path):
    data, store = tmp_path / "data", tmp_path / "data" / "_sessions"
    data.mkdir()
    api.configure(store_root=store, data_root=data)
    yield data, store
    api.configure()


def test_detect_reads_kind_start_and_length_from_the_bytes(tmp_path):
    d = api.detect(meter_log(tmp_path / "아무 이름.log"))
    assert (d["kind"], d["start"], d["duration_s"], d["ok"], d["recording_id"]) == (
        "fsk_energymeter_log", "2027-06-05T10:00:00.000+09:00", 29.99, True, "2027-06-05_100000_em")
    m = api.detect(monolith_log(tmp_path / "m.log"))
    assert (m["kind"], m["start"], m["recording_id"]) == ("monolith_log", "2027-06-05T10:00:00.000+09:00", "2027-06-05_100000")
    (tmp_path / "x.txt").write_text("hello\n" * 100, encoding="utf-8")
    codes = []
    for call in (lambda: api.detect(tmp_path / "x.txt"), lambda: api.detect(monolith_log(tmp_path / "two.log", boots=2)),
                 lambda: api.detect(tmp_path / "없음.log")):
        with pytest.raises(EngineError) as e:
            call()
        codes.append(e.value.code)
    assert codes == ["unsupported_format", "corrupt_file", "bad_argument"]


def test_ingest_places_converts_and_records_the_job(roots, tmp_path):
    data, store = roots
    a = meter_log(tmp_path / "2027-06-05-10-00-00-000_미터.log")
    job = api.get_job(api.ingest([a], {"car": "LEF-26", "place_id": "testtrack", "title": "시험"}))
    assert job["state"] == "done" and job["event_id"] == "LEF-26/2027-06-05_testtrack" and job["progress"] == 1.0
    f = job["files"][0]
    assert f["status"] == "placed" and f["placed"] == "LEF-26/2027-06-05_testtrack/raw_energymeter/2027-06-05-10-00-00-000__.log"   # ASCII 이름
    assert f["recordings"] == ["2027-06-05_100000_em"] and job["recordings"] == ["2027-06-05_100000_em"]
    ev = data / job["event_id"]
    assert (ev / "normalized" / "2027-06-05_100000_em" / "energy.parquet").exists()
    assert 'sessions = "auto"' in (ev / ingest.NOTES_NAME).read_text(encoding="utf-8")
    assert [s["step"] for s in job["steps"]] == ["energymeter", "sessions"] and job["steps"][-1]["state"] == "skipped"   # GPS 가 없어 자동 세션 없음
    assert f["code"] == "no_session_match" and any("no_session_match" in w for w in job["warnings"])
    assert (data / ingest.UPLOADS / job["job_id"] / "log.txt").exists() and not (data / ingest.UPLOADS / ingest.LOCK).exists()

    again = api.get_job(api.ingest([a], {"car": "LEF-26", "event_id": job["event_id"]}))
    assert again["state"] == "done" and again["files"][0]["status"] == "duplicate" and again["steps"] == []
    b = meter_log(tmp_path / "other" / "2027-06-05-10-00-00-000_미터.log" if (tmp_path / "other").mkdir() is None else None, amps=80.0)
    moved = api.get_job(api.ingest([b], {"car": "LEF-26", "event_id": job["event_id"]}))["files"][0]
    assert moved["status"] == "placed" and moved["placed"] != f["placed"] and moved["placed"].endswith(".log")   # 이름이 겹치면 새 이름 (원본은 그대로)
    assert (data / f["placed"]).read_bytes() == a.read_bytes()


def test_ingest_rejects_what_it_cannot_place(roots, tmp_path):
    data, _ = roots
    lines = "".join(f"10:00:{k // 100:02d}.{k % 100 * 10:03d},IMU,1,2,3,4,5,6\n" for k in range(200))
    csv = tmp_path / "data.csv"
    csv.write_text(lines, encoding="utf-8")
    assert api.detect(csv)["kind"] == "lef26_logger_csv" and api.detect(csv)["car_guess"] == "LEF-26"
    job = api.get_job(api.ingest([csv], {"car": "LEF-25evo", "place_id": "x", "date": "2027-06-05"}))
    assert job["state"] == "failed" and job["files"][0]["code"] == "car_mismatch"
    job = api.get_job(api.ingest([csv], {"place_id": "x"}))                                       # 날짜가 없는 형식만 올렸다
    assert job["state"] == "failed" and "date" in job["errors"][0] and job["event_id"] is None
    (tmp_path / "x.txt").write_text("hello\n", encoding="utf-8")
    job = api.get_job(api.ingest([tmp_path / "x.txt"], {"car": "LEF-26", "place_id": "x", "date": "2027-06-05"}))
    assert job["state"] == "failed" and job["files"][0]["code"] == "unsupported_format"
    assert not (data / "LEF-26").exists() and not (data / "LEF-25evo").exists()                   # 넣은 것이 없으면 이벤트 폴더도 없다


def test_ingest_arguments_and_lock(roots, tmp_path):
    data, _ = roots
    a = meter_log(tmp_path / "a.log")
    codes = []
    for call in (lambda: api.ingest([tmp_path / "없음.log"], {"car": "LEF-26"}), lambda: api.ingest([a], {"driver": "누구"}),
                 lambda: api.ingest([], None), lambda: api.ingest([a], {"car": 26}), lambda: api.get_job("nope"),
                 lambda: api.get_job("j20270605-100000-abcd")):
        with pytest.raises(EngineError) as e:
            call()
        codes.append(e.value.code)
    assert codes == ["bad_argument"] * 4 + ["unknown_job"] * 2
    (data / ingest.UPLOADS).mkdir(parents=True, exist_ok=True)
    (data / ingest.UPLOADS / ingest.LOCK).write_text("j-other", encoding="utf-8")
    with pytest.raises(EngineError) as e:
        api.ingest([a], {"car": "LEF-26", "place_id": "x"})
    assert e.value.code == "busy"


def test_safe_names_are_ascii():
    assert ingest.safe_name("주행 1 (오전).log") == "1_.log" and ingest.safe_name("GOPR3331.MP4") == "GOPR3331.MP4"
    assert ingest.safe_name("../../etc/passwd") == "passwd" and ingest.safe_name("한글.csv") == "csv"


# ---------------------------------------------------------------- 우리 대회 파일 (있을 때만)

EVO = paths.LEF25EVO_FSK
need = pytest.mark.skipif(not (EVO / "raw_logger").exists(), reason="25Evo 대회 원본 없음")


@need
def test_detect_names_the_same_recordings_the_converters_made():
    """원본마다 판별한 레코딩 이름 · 시작 시각이 이미 변환된 레코딩의 것과 같다 (Monolith · 에너지미터 전부)."""
    for sub, pattern in (("raw_logger", "*.log"), ("raw_energymeter", "*.log")):
        for p in sorted((EVO / sub).glob(pattern)):
            if p.stat().st_size <= em.HEADER_SIZE:                     # 빈 파일 · 머리만 있는 파일 (전원만 켰다 끈 것)
                continue
            d = ingest.detect(p)
            if not d["ok"]:
                continue
            meta = json.loads((EVO / "normalized" / d["recording_id"] / "meta.json").read_text(encoding="utf-8"))
            assert pd.Timestamp(meta["time"]["t_abs_start"]) == pd.Timestamp(d["start"]) or sub == "raw_energymeter", p.name


@need
def test_two_real_files_become_a_session(roots):
    """Monolith 로그 + 에너지미터 로그를 새 이벤트에 올리면 손으로 돌릴 때의 순서대로 변환되고 세션이 나온다."""
    mono_log = EVO / "raw_logger" / "2026-08-29_093218.log"
    meter = next((EVO / "raw_energymeter").glob("2026-08-29-09-35-21*.log"))
    job = api.get_job(api.ingest([mono_log, meter], {"car": "LEF-25evo", "place_id": "ingesttest"}))
    assert job["state"] == "done", job["errors"]
    assert [s["step"] for s in job["steps"]] == ["monolith", "energymeter", "laps", "events", "evo-can", "clocks (전원 주기)", "em-laps", "sessions"]
    assert set(job["recordings"]) == {"2026-08-29_093218", "2026-08-29_093521_em"} and len(job["sessions"]) >= 1
    s = api.get_session(job["sessions"][0])
    blocks = {b["block"]: b for b in s["blocks"]}
    assert {"gps", "battery", "pedal", "bms"} <= set(blocks) and "Orion 팩 전류(CAN 0x447" in blocks["battery"]["checks"][0]
    assert all(f["sessions"] == job["sessions"][:1] for f in job["files"])


def test_a_session_lists_only_its_own_video_chunks(tmp_path):
    """세션의 영상 블록 원본은 그 녹화의 조각(같은 이름의 저해상도 사본 포함)뿐 — 폴더의 다른 녹화가 섞이면 올린 파일이 엉뚱한 세션에 '쓰임' 으로 잡힌다."""
    from types import SimpleNamespace
    from lefdata import catalog_build as cb
    raw = tmp_path / "raw_video"
    raw.mkdir()
    for n in ("GOPR0001.MP4", "GOPR0001.LRV", "GP010001.MP4", "GOPR0002.MP4"):
        (raw / n).write_bytes(b"x")
    r = SimpleNamespace(event_dir=tmp_path, notes={}, extra=lambda key: {},
                        meta={"source": {"logger": "GoPro 온보드 영상 (GPMF)", "files": ["GOPR0001.MP4", "GP010001.MP4"]}})
    assert [f.name for f in cb._video_block(r).raw] == ["GOPR0001.LRV", "GOPR0001.MP4", "GP010001.MP4"]
