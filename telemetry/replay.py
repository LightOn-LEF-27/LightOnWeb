"""재생 — 수신기와 같은 모양의 줄을 지금 시각으로 수신 폴더에 써 넣는다. 차 없이 실시간 화면을 만들고 연습한다 (계약 §12-7).

    python replay.py --dir <수신 폴더> --sd <Monolith .log> [--start 300] [--duration 600] [--speed 1] [--drop 60:75]
    python replay.py --dir <수신 폴더> --jsonl <받은 기록 .jsonl> [--speed 1]

--sd    SD 의 전체 기록에 Monolith 펌웨어가 서버로 보낼 때의 규칙을 그대로 돌린다 (펌웨어 v2.3 mqtt.c · can.c):
        보내기 주기(intv)마다 `/d` (그 순간의 장치 ms + GPS · 자이로 · 아날로그 · 디지털의 가장 최근 레코드),
        시스템 메시지 묶음 `/d/sl`, CAN 은 ID 마다 "마지막으로 보낸 지 intv 가 지났으면 그때 들어온 프레임 하나" 를 모은 `/d/can`.
--jsonl 받은 그대로 다시 (시각만 지금으로 옮긴다). 장치 설정 토픽(d/cfg)은 비밀번호가 실려 있어 건너뛴다.

모든 줄에 "replay": true 가 붙는다 — 화면이 "재생 중" 띠를 띄운다. 이 도구는 브로커에 붙지 않는다 (파일만 쓴다).
"""

from __future__ import annotations

import argparse
import base64
import datetime as dt
import json
import struct
import sys
import time
from pathlib import Path
from typing import Iterable, Iterator

REC = 24
T_BOOT, T_CAN, T_GPS, T_ANALOG, T_DIGITAL, T_GYRO, T_SYS, T_USER = range(1, 9)
LOGBUF_ORDER = (T_GPS, T_GYRO, T_ANALOG, T_DIGITAL)      # /d 의 레코드 4칸 순서 (main.h LOGBUF_POS)
KST = dt.timezone(dt.timedelta(hours=9))
CAN_BATCH, SL_BATCH = 128, 32
TOPIC = "replay"


def checksum(rec: bytes) -> int:
    """Monolith 레코드 체크섬 — 체크섬 칸을 0 으로 둔 u32 6개의 XOR 을 16비트로 접는다."""
    words = list(struct.unpack("<6I", rec))
    words[0] &= 0x0000FFFF
    x = 0
    for w in words:
        x ^= w
    return ((x & 0xFFFF) + (x >> 16)) & 0xFFFF


def valid(rec: bytes) -> bool:
    return len(rec) == REC and rec[0] == 0xAE and struct.unpack_from("<H", rec, 2)[0] == checksum(rec)


def retime(rec: bytes, ts: int) -> bytes:
    """레코드의 장치 시각을 바꾸고 체크섬을 다시 맞춘다."""
    b = bytearray(rec)
    struct.pack_into("<I", b, 4, ts)
    struct.pack_into("<H", b, 2, 0)
    struct.pack_into("<H", b, 2, checksum(bytes(b)))
    return bytes(b)


def read_sd(path: Path) -> list[bytes]:
    data = path.read_bytes()
    return [r for r in (data[i:i + REC] for i in range(0, len(data) - REC + 1, REC)) if valid(r)]


def ts_of(rec: bytes) -> int:
    return struct.unpack_from("<I", rec, 4)[0]


def simulate(records: Iterable[bytes], intv_ms: int = 500, loop_ms: int = 504) -> Iterator[tuple[int, str, bytes]]:
    """SD 레코드(시각순) → 펌웨어가 서버로 보냈을 메시지 (장치 ms, 토픽 끝, 본문). loop_ms = 보내기 주기 (설정값 + 보내는 데 든 시간)."""
    recs = sorted((r for r in records if r[1] != T_BOOT), key=ts_of)
    if not recs:
        return
    latest: dict[int, bytes] = {}
    last_sent: dict[tuple[int, int], int] = {}
    can_q: list[bytes] = []
    sl_q: list[bytes] = []
    i = 0
    loop = ts_of(recs[0]) + loop_ms
    end = ts_of(recs[-1])
    while loop <= end + loop_ms:
        while i < len(recs) and ts_of(recs[i]) <= loop:
            r = recs[i]
            i += 1
            kind, ts = r[1], ts_of(r)
            if kind in LOGBUF_ORDER:
                latest[kind] = r
            elif kind == T_CAN:
                key = (struct.unpack_from("<I", r, 8)[0], r[12])
                if ts - last_sent.get(key, -10**9) >= intv_ms:
                    last_sent[key] = ts
                    can_q.append(r)
            elif kind in (T_SYS, T_USER):
                sl_q.append(r)
        body = struct.pack("<II", loop, 0) + b"".join(latest.get(k, bytes(REC)) for k in LOGBUF_ORDER)
        yield loop, "d", body
        if sl_q:
            yield loop, "d/sl", b"".join(sl_q[:SL_BATCH])
            del sl_q[:SL_BATCH]
        while can_q:
            yield loop, "d/can", b"".join(can_q[:CAN_BATCH])
            del can_q[:CAN_BATCH]
        loop += loop_ms


def scale(records: list[bytes], speed: float, start_ms: int | None = None, end_ms: int | None = None) -> list[bytes]:
    """구간을 자르고 장치 시각을 speed 배로 줄인다 (체크섬은 다시 맞춘다)."""
    if not records:
        return records
    t0 = ts_of(min(records, key=ts_of))
    out = []
    for r in records:
        ts = ts_of(r)
        if (start_ms is not None and ts < t0 + start_ms) or (end_ms is not None and ts > t0 + end_ms):
            continue
        out.append(r if speed == 1 else retime(r, round(t0 + (ts - t0) / speed)))
    return out


def from_sd(path: Path, start_s: float = 0.0, duration_s: float | None = None, speed: float = 1.0, delay_s: float = 0.25,
            drops: list[tuple[float, float]] = (), intv_ms: int = 500, loop_ms: int = 504) -> Iterator[tuple[float, dict]]:
    """→ (지금부터 몇 초 뒤, 줄). 끊김 구간(재생 시작부터의 초)에 든 메시지는 보내지 않는다."""
    recs = scale(read_sd(path), speed, round(start_s * 1000), None if duration_s is None else round((start_s + duration_s) * 1000))
    first = None
    for dev, sub, body in simulate(recs, intv_ms, loop_ms):
        first = dev if first is None else first
        at = (dev - first) / 1000.0 + delay_s
        if any(a <= at < b for a, b in drops):
            continue
        yield at, {"topic": f"{TOPIC}/{sub}", "retain": False, "b64": base64.b64encode(body).decode("ascii"), "replay": True}


def from_jsonl(path: Path, speed: float = 1.0) -> Iterator[tuple[float, dict]]:
    t0 = None
    with open(path, encoding="ascii") as f:
        for line in f:
            try:
                row = json.loads(line)
            except ValueError:
                continue
            if str(row.get("topic", "")).endswith("/d/cfg"):
                continue
            t0 = row["t_ns"] if t0 is None else t0
            out = {k: v for k, v in row.items() if k != "t_ns"}
            out["replay"] = True
            yield (row["t_ns"] - t0) / 1e9 / speed, out


def emit(rows: Iterable[tuple[float, dict]], live_dir: Path, wait: bool = True, clock=time.time, sleep=time.sleep) -> int:
    """줄을 지금 시각으로 오늘 파일(KST 날짜)에 이어 쓴다. wait 이면 제 시각까지 기다린다 → 쓴 줄 수."""
    live_dir.mkdir(parents=True, exist_ok=True)
    start = clock()
    n = 0
    f = day = None
    try:
        for at, row in rows:
            t = start + at
            if wait:
                d = t - clock()
                if d > 0:
                    sleep(d)
            name = dt.datetime.fromtimestamp(t, KST).strftime("%Y-%m-%d")
            if name != day:
                if f:
                    f.close()
                f = open(live_dir / f"{name}.jsonl", "a", encoding="ascii", newline="\n")
                day = name
            f.write(json.dumps({"t_ns": int(t * 1e9), **row}, separators=(",", ":")) + "\n")
            f.flush()
            n += 1
    finally:
        if f:
            f.close()
    return n


def parse_drop(text: str) -> tuple[float, float]:
    a, b = text.split(":")
    return float(a), float(b)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dir", type=Path, required=True, help="수신 폴더 (사이트의 LEF_SITE_LIVE_DIR)")
    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument("--sd", type=Path, help="Monolith SD 로그 (.log)")
    src.add_argument("--jsonl", type=Path, help="수신기가 받은 기록 (.jsonl)")
    ap.add_argument("--start", type=float, default=0.0, help="SD 기록의 몇 초부터 (기본 처음)")
    ap.add_argument("--duration", type=float, help="몇 초 동안 (기본 끝까지)")
    ap.add_argument("--speed", type=float, default=1.0, help="빠르게 (2 = 두 배)")
    ap.add_argument("--delay", type=float, default=0.25, help="전달 시간 (초)")
    ap.add_argument("--drop", type=parse_drop, action="append", default=[], help="끊김 구간 — 재생 시작부터 A:B 초 (여러 번)")
    ap.add_argument("--no-wait", action="store_true", help="기다리지 않고 한꺼번에 쓴다 (시각은 앞당겨 적힌다)")
    a = ap.parse_args(argv)
    if a.speed <= 0:
        ap.error("--speed 는 0 보다 커야 합니다")
    rows = (from_sd(a.sd, a.start, a.duration, a.speed, a.delay, a.drop) if a.sd else from_jsonl(a.jsonl, a.speed))
    try:
        n = emit(rows, a.dir, wait=not a.no_wait)
    except KeyboardInterrupt:
        print("멈춤", file=sys.stderr)
        return 130
    print(f"{n}줄 씀 → {a.dir}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
