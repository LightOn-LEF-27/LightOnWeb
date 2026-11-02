"""수신기의 기록 형식과 안전 규칙을 확인한다 (브로커 없이)."""

import ast
from pathlib import Path

import pytest

pytest.importorskip("paho.mqtt")

import receiver as rx

NS = 1_000_000_000
# 2026-01-01 00:00 KST
MIDNIGHT = 1767193200 * NS


def test_message_round_trip_keeps_exact_bytes(tmp_path):
    r = rx.Recorder(tmp_path)
    body = bytes(range(256))
    r.message(MIDNIGHT, "x/d/can", body, False)
    r.close()
    row = next(rx.read_day(tmp_path / "2026-01-01.jsonl"))
    assert row["payload"] == body and row["topic"] == "x/d/can" and row["t_ns"] == MIDNIGHT and row["retain"] is False


def test_new_file_at_midnight_kst(tmp_path):
    r = rx.Recorder(tmp_path)
    r.message(MIDNIGHT - NS, "x/d", b"a", False)
    r.message(MIDNIGHT, "x/d", b"b", False)
    r.close()
    assert sorted(p.name for p in tmp_path.iterdir()) == ["2025-12-31.jsonl", "2026-01-01.jsonl"]


def test_appends_after_restart(tmp_path):
    for body in (b"a", b"b"):
        r = rx.Recorder(tmp_path)
        r.message(MIDNIGHT, "x/d", body, False)
        r.close()
    assert [x["payload"] for x in rx.read_day(tmp_path / "2026-01-01.jsonl")] == [b"a", b"b"]


def test_truncated_last_line_is_skipped(tmp_path):
    r = rx.Recorder(tmp_path)
    r.message(MIDNIGHT, "x/d", b"a", False)
    r.close()
    p = tmp_path / "2026-01-01.jsonl"
    p.write_text(p.read_text() + '{"t_ns":1,"topic":"x', encoding="ascii")
    assert len(list(rx.read_day(p))) == 1


def test_events_are_recorded(tmp_path):
    r = rx.Recorder(tmp_path)
    r.event(MIDNIGHT, "connected")
    r.close()
    assert next(rx.read_day(tmp_path / "2026-01-01.jsonl"))["event"] == "connected"


def test_does_not_subscribe_to_config_or_commands():
    topics = [t for t, _ in rx.subscriptions("x")]
    assert topics == ["x/d", "x/d/can", "x/d/sl", "x/d/boot", "x/d/ver"]
    assert not any(t.endswith("/cfg") or "/cmd" in t or "/set" in t or "#" in t or "+" in t for t in topics)


def test_refuses_to_start_without_settings(monkeypatch, capsys):
    for k in ("LEF_RX_HOST", "LEF_RX_USER", "LEF_RX_PASSWORD", "LEF_RX_CHANNEL", "LEF_RX_DIR"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("LEF_RX_HOST", "example.invalid")
    assert rx.main() == 2
    assert "LEF_RX_CHANNEL" in capsys.readouterr().err


def test_source_never_publishes():
    tree = ast.parse(Path(rx.__file__).read_text(encoding="utf-8"))
    calls = {n.func.attr for n in ast.walk(tree) if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)}
    assert "publish" not in calls
