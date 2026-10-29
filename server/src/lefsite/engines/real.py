"""진짜 연결부 — 분석 엔진 `lefdata.api` 를 그대로 부른다 (같은 프로세스, HTTP 층 없음 — 계약 §9-1).

엔진은 `pip install -e ../engine` 으로 같은 가상환경에 넣는다. 엔진 안쪽(모듈 · 폴더 · parquet 열 이름)은 보지 않고 api 만 부른다.
"""

from __future__ import annotations

from functools import wraps
from pathlib import Path

from .base import EngineError, check_file_id, check_sid


def _translate(fn):
    """lefdata.api.EngineError → 사이트의 EngineError (모양은 같다 — 계약 §8)."""

    @wraps(fn)
    def call(self, *args, **kwargs):
        try:
            return fn(self, *args, **kwargs)
        except self._api.EngineError as e:
            raise EngineError(e.code, e.message, e.detail) from e

    return call


class RealEngine:
    kind = "real"

    def __init__(self, session_root: Path | None = None, data_root: Path | None = None):
        try:
            from lefdata import api
        except ImportError as e:  # 엔진을 깔지 않은 가상환경
            raise RuntimeError("LEF_SITE_ENGINE=real 인데 lefdata 가 없다 — server 가상환경에서 pip install -e ../engine") from e
        self._api = api
        api.configure(store_root=session_root, data_root=data_root)

    @_translate
    def engine_info(self) -> dict:
        return self._api.engine_info()

    @_translate
    def list_sessions(self, car=None, event_id=None, discipline=None, limit=None, offset=0) -> list[dict]:
        return self._api.list_sessions(car=car, event_id=event_id, discipline=discipline, limit=limit, offset=offset)

    @_translate
    def get_session(self, sid: str) -> dict:
        return self._api.get_session(check_sid(sid))

    @_translate
    def list_channels(self, sid: str) -> list[dict]:
        return self._api.list_channels(check_sid(sid))

    @_translate
    def get_laps(self, sid: str) -> dict:
        return self._api.get_laps(check_sid(sid))

    @_translate
    def get_quality(self, sid: str) -> list[dict]:
        return self._api.get_quality(check_sid(sid))

    @_translate
    def open_file(self, file_id: str) -> dict:
        return self._api.open_file(check_file_id(file_id))

    @_translate
    def resolve_session(self, sid: str) -> dict:
        return self._api.resolve_session(check_sid(sid))
