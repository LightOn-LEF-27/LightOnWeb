"""실시간 화면 (계약 §12, v0.16) — 수신기가 쓰는 오늘 파일을 따라 읽어 엔진(lefdata.live)에 넘기고, 엔진이 낸 상태를 그대로 돌려준다.

    수신기 (telemetry/receiver.py, 다른 리눅스 계정) → <LEF_SITE_LIVE_DIR>/<YYYY-MM-DD>.jsonl (KST)
    → 이 모듈의 스레드가 0.2 s 마다 새 줄을 읽어 Live.feed → GET /api/live/state · /api/live/track

다시 켜면 오늘 파일을 처음부터 읽어 그날 상태(끊김 · 랩 · 사건 · 그래프)를 되살린다. 날짜가 바뀌면 새 파일 · 새 상태로 넘어간다.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import threading
import time
from pathlib import Path
from typing import Callable

from fastapi import APIRouter, Depends

from .auth import User, require
from .config import Settings
from .engines import EngineError

log = logging.getLogger("lefsite.live")

KST = dt.timezone(dt.timedelta(hours=9))    # 수신기가 하루를 나누는 기준과 같다
POLL_S = 0.2
FEED_BATCH = 2000                            # 하루치를 처음 읽을 때 화면 요청이 오래 막히지 않게 잠금을 나눠 잡는다


def day_of(now: float) -> str:
    return dt.datetime.fromtimestamp(now, KST).strftime("%Y-%m-%d")


class LiveFeed:
    """오늘 수신 파일 하나를 따라가는 엔진 상태. poll 은 한 스레드에서만, state · track 은 어느 스레드에서나."""

    def __init__(self, live_dir: Path, make: Callable[[], object], clock: Callable[[], float] = time.time):
        self.dir, self.make, self.clock = Path(live_dir), make, clock
        self.lock = threading.Lock()
        self.day: str | None = None
        self.live = make()
        self.pos = 0
        self.buf = b""
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def poll(self) -> int:
        """새 줄을 읽어 넘긴다 → 넘긴 줄 수."""
        day = day_of(self.clock())
        if day != self.day:
            with self.lock:
                self.live = self.make()
            self.day, self.pos, self.buf = day, 0, b""
        path = self.dir / f"{day}.jsonl"
        try:
            size = path.stat().st_size
        except FileNotFoundError:
            return 0
        if size < self.pos:                       # 파일이 새로 만들어졌다 — 처음부터
            with self.lock:
                self.live = self.make()
            self.pos, self.buf = 0, b""
        if size == self.pos:
            return 0
        with open(path, "rb") as f:
            f.seek(self.pos)
            chunk = f.read(size - self.pos)
        self.pos += len(chunk)
        lines = (self.buf + chunk).split(b"\n")
        self.buf = lines.pop()                    # 아직 다 쓰지 않은 마지막 줄
        rows = []
        for line in lines:
            if not line.strip():
                continue
            try:
                rows.append(json.loads(line))
            except ValueError:
                rows.append({})                   # 엔진이 깨진 줄로 센다
        for i in range(0, len(rows), FEED_BATCH):
            with self.lock:
                for r in rows[i:i + FEED_BATCH]:
                    self.live.feed(r)
        return len(rows)

    def state(self, since: float | None = None) -> dict:
        with self.lock:
            return self.live.state(self.clock(), since)

    def track(self) -> dict | None:
        with self.lock:
            return self.live.track()

    def start(self) -> None:
        if self._thread is None:
            self._thread = threading.Thread(target=self._run, name="lefsite-live", daemon=True)
            self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self.poll()
            except Exception:                    # 수신 파일 하나 때문에 사이트가 죽지 않게 — 기록만 남기고 계속 본다
                log.exception("실시간 수신 파일 읽기 실패")
                time.sleep(2.0)
            self._stop.wait(POLL_S)


def make_feed(settings: Settings) -> LiveFeed | None:
    """설정에 수신 폴더가 없으면 None — 화면은 '실시간 수신 꺼짐' 을 띄운다."""
    if settings.live_dir is None:
        return None
    try:
        from lefdata import live
    except ImportError as e:
        raise RuntimeError("LEF_SITE_LIVE_DIR 를 줬는데 lefdata 가 없다 — server 가상환경에서 pip install -e ../engine") from e
    car, venue, rules, data_root = settings.live_car, settings.live_venue or None, settings.live_rules, settings.data_root
    live.Live(car=car, venue=venue, rules=rules, data_root=data_root)     # 경기장 · 문턱 파일이 잘못됐으면 띄울 때 바로 멈춘다
    return LiveFeed(settings.live_dir, lambda: live.Live(car=car, venue=venue, rules=rules, data_root=data_root))


def router(feed: LiveFeed | None) -> APIRouter:
    r = APIRouter(prefix="/api/live")

    def need() -> LiveFeed:
        if feed is None:
            raise EngineError("live_off", "실시간 수신 꺼짐 — 서버 설정에 수신 폴더(LEF_SITE_LIVE_DIR) 없음")
        return feed

    @r.get("/state")
    def state(since: float | None = None, user: User = Depends(require("view"))) -> dict:
        """화면 상태 (계약 §12-4) — 0.5 s 마다. since = 화면이 가진 그래프의 마지막 t (그 뒤 줄만 받는다)."""
        return need().state(since)

    @r.get("/track")
    def track(user: User = Depends(require("view"))) -> dict:
        """경기장 기준선 · 계측선 (계약 §12-5)."""
        t = need().track()
        if t is None:
            raise EngineError("no_venue", "경기장이 정해지지 않음 — 서버 설정 LEF_SITE_LIVE_VENUE")
        return t

    return r
