"""엔진 연결부 — 서버 안에서 엔진을 부르는 곳은 여기 한 곳이다 (`사이트_설계.md` §3).

`LEF_SITE_ENGINE=fake` 이면 합성 세션, `real` 이면 lefdata.api. 나중에 데이터 공급원을 바꿔 꽂을 때도 여기만 바꾼다.
"""

from __future__ import annotations

from ..config import Settings
from .base import Engine, EngineError, check_file_id, check_sid

__all__ = ["Engine", "EngineError", "check_file_id", "check_sid", "make_engine"]


def make_engine(settings: Settings) -> Engine:
    if settings.engine == "real":
        from .real import RealEngine
        return RealEngine(settings.session_root, settings.data_root)
    from .fake import FakeEngine
    return FakeEngine()
