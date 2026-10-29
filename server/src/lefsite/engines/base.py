"""엔진 연결부의 약속 — 진짜(lefdata.api)와 가짜(합성 세션)가 같은 함수 · 같은 모양을 낸다.

함수 이름 · 인자 · 돌려주는 JSON 모양의 원본은 인터페이스 문서(`분석엔진_인터페이스.md`)다. 여기는 그중 사이트가 지금 부르는 것만 적는다.
화면이 늘면 (그래프 F5 · 랩 표 F6 …) 여기에 함수를 더하고 두 연결부에 같이 만든다.
"""

from __future__ import annotations

import re
from typing import Protocol

# 계약 §3 의 sid 모양. 엔진도 다시 보지만, 주소에서 받은 값은 엔진에 넘기기 전에 거른다
SID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
FILE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}/[A-Za-z0-9_./-]{1,256}$")   # <sid>/files/x.csv · <sid>/raw/3

# 계약 §8 의 오류 code → HTTP 상태
HTTP_STATUS = {
    "unknown_session": 404, "unknown_channel": 404, "unknown_file": 404, "unknown_job": 404,
    "session_retired": 410,
    "bad_argument": 400, "frame_mismatch": 400,
    "unsupported_format": 415, "corrupt_file": 422, "no_session_match": 422,
    "not_implemented": 501,
    "internal": 500,
}


class EngineError(Exception):
    """계약 §8 의 오류 — 진짜 엔진의 lefdata.api.EngineError 도 이것으로 바꿔 던진다."""

    def __init__(self, code: str, message: str, detail=None):
        super().__init__(message)
        self.code, self.message, self.detail = code, message, detail

    @property
    def status(self) -> int:
        return HTTP_STATUS.get(self.code, 500)

    def to_json(self) -> dict:
        return {"error": {"code": self.code, "message": self.message, "detail": self.detail}}


def check_sid(sid: str) -> str:
    if not isinstance(sid, str) or not SID.match(sid) or ".." in sid:
        raise EngineError("unknown_session", f"세션 ID 가 올바르지 않음: {sid!r}")
    return sid


def check_file_id(file_id: str) -> str:
    if not isinstance(file_id, str) or not FILE_ID.match(file_id) or ".." in file_id or "//" in file_id:
        raise EngineError("unknown_file", f"파일 ID 가 올바르지 않음: {file_id!r}")
    return file_id


class Engine(Protocol):
    """사이트가 부르는 엔진 함수 (계약 번호). 돌려주는 값은 전부 JSON 으로 바꿀 수 있다."""

    kind: str  # "fake" · "real" — 화면 아래 상태 줄에 보인다

    def engine_info(self) -> dict: ...                                     # F1

    def list_sessions(self, car: str | None = None, event_id: str | None = None, discipline: str | None = None,
                      limit: int | None = None, offset: int = 0) -> list[dict]: ...  # F2

    def get_session(self, sid: str) -> dict: ...                           # F3

    def list_channels(self, sid: str) -> list[dict]: ...                   # F4

    def get_laps(self, sid: str) -> dict: ...                              # F6

    def get_quality(self, sid: str) -> list[dict]: ...                     # F8

    def open_file(self, file_id: str) -> dict: ...                         # F12 — path 는 서버 안에서만 쓴다

    def resolve_session(self, sid: str) -> dict: ...                       # F15 (v0.9)
