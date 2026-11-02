"""Monolith 실시간 수신기 — 브로커를 구독해 받은 메시지를 그대로 파일에 남긴다.

    LEF_RX_HOST=… LEF_RX_USER=… LEF_RX_PASSWORD=… LEF_RX_CHANNEL=… LEF_RX_DIR=… python receiver.py

해독은 하지 않는다 (받은 시각 · 토픽 · 원본 바이트만). 하루에 파일 하나, 한 줄에 메시지 하나.
장치로는 아무것도 보내지 않는다 — 이 파일에는 publish 가 없고, 읽기 전용 계정으로만 붙는다.
"""

from __future__ import annotations

import base64
import datetime as dt
import json
import logging
import os
import signal
import sys
import time
import uuid
from pathlib import Path

import paho.mqtt.client as mqtt
from paho.mqtt.enums import CallbackAPIVersion

# 장치 설정 토픽(d/cfg)에는 비밀번호가 평문으로 실리므로 받지 않는다
TOPICS = ("d", "d/can", "d/sl", "d/boot", "d/ver")
KST = dt.timezone(dt.timedelta(hours=9))    # 하루를 나누는 기준

log = logging.getLogger("receiver")


class Recorder:
    """하루에 파일 하나로 이어 쓴다. 줄마다 flush 해서 프로그램이 죽어도 받은 데까지는 남는다."""

    def __init__(self, root: Path):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.day: str | None = None
        self.f = None

    def _file(self, t_ns: int):
        day = dt.datetime.fromtimestamp(t_ns / 1e9, KST).strftime("%Y-%m-%d")
        if day != self.day:
            if self.f:
                self.f.close()
            self.f = open(self.root / f"{day}.jsonl", "a", encoding="ascii", newline="\n")
            self.day = day
        return self.f

    def _write(self, t_ns: int, row: dict) -> None:
        f = self._file(t_ns)
        f.write(json.dumps({"t_ns": t_ns, **row}, separators=(",", ":")) + "\n")
        f.flush()

    def message(self, t_ns: int, topic: str, payload: bytes, retain: bool) -> None:
        self._write(t_ns, {"topic": topic, "retain": bool(retain), "b64": base64.b64encode(payload).decode("ascii")})

    def event(self, t_ns: int, name: str) -> None:
        """수신기 쪽 접속 · 끊김. 장치가 끊긴 것과 수신기가 끊긴 것을 나중에 가르는 근거다."""
        self._write(t_ns, {"event": name})

    def close(self) -> None:
        if self.f:
            self.f.close()
            self.f = None


def read_day(path: Path):
    """파일을 다시 읽는다. 마지막 줄이 잘렸으면 건너뛴다."""
    with open(path, encoding="ascii") as f:
        for line in f:
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if "b64" in row:
                row["payload"] = base64.b64decode(row.pop("b64"))
            yield row


def subscriptions(channel: str) -> list[tuple[str, int]]:
    return [(f"{channel}/{t}", 0) for t in TOPICS]


def run(host: str, user: str, password: str, channel: str, root: Path, port: int = 443) -> None:
    rec = Recorder(root)
    # 같은 client ID 로 붙으면 먼저 붙은 쪽이 끊기므로 매번 새로 만든다
    c = mqtt.Client(CallbackAPIVersion.VERSION2, client_id=f"lef-rx-{uuid.uuid4().hex[:8]}",
                    protocol=mqtt.MQTTv5, transport="websockets")
    c.ws_set_options(path="/")
    c.tls_set()
    c.username_pw_set(user, password)
    c.reconnect_delay_set(min_delay=1, max_delay=30)

    def on_connect(client, _u, _f, rc, _p):
        log.info("접속: %s", rc)
        if rc.is_failure:
            return
        rec.event(time.time_ns(), "connected")
        client.subscribe(subscriptions(channel))

    def on_disconnect(_c, _u, _f, rc, _p):
        log.warning("끊김: %s", rc)
        rec.event(time.time_ns(), "disconnected")

    def on_message(_c, _u, msg):
        rec.message(time.time_ns(), msg.topic, msg.payload, msg.retain)

    c.on_connect, c.on_disconnect, c.on_message = on_connect, on_disconnect, on_message

    signal.signal(signal.SIGTERM, lambda *_: c.disconnect())
    c.connect_async(host, port, keepalive=30)
    try:
        c.loop_forever(retry_first_connection=True)
    except KeyboardInterrupt:
        c.disconnect()
    finally:
        rec.close()


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    names = ("LEF_RX_HOST", "LEF_RX_USER", "LEF_RX_PASSWORD", "LEF_RX_CHANNEL", "LEF_RX_DIR")
    missing = [k for k in names if not os.environ.get(k)]
    if missing:
        print(f"환경 변수가 없습니다: {', '.join(missing)}", file=sys.stderr)
        return 2
    host, user, password, channel, root = (os.environ[k] for k in names)
    run(host, user, password, channel, Path(root))
    return 0


if __name__ == "__main__":
    sys.exit(main())
