"""앱 DB — 사이트 자체의 상태만 둔다 (사용자 명단 · 역할 · 로그인 · 내려받기 기록). 주행 데이터는 넣지 않는다 (엔진이 읽는다).

로컬은 SQLite 파일 하나 (`LEF_SITE_DB`, 기본 `server/.local/site.db` — git 에 넣지 않는다).
세션을 가리킬 때는 sid 로만 적는다 (계약 §3-1 — sid 는 안 바뀐다).
"""

from __future__ import annotations

import sqlite3
from contextlib import closing, contextmanager
from datetime import datetime, timezone
from pathlib import Path

from .config import ROLES

# 판마다 한 덩어리. 판을 올릴 때는 끝에 더하기만 한다 (PRAGMA user_version 이 몇 번째까지 했는지 기억한다)
MIGRATIONS = [
    """
    CREATE TABLE users (
        email         TEXT PRIMARY KEY,                  -- 소문자
        name          TEXT NOT NULL DEFAULT '',
        role          TEXT NOT NULL CHECK (role IN ('admin', 'lead', 'member')),
        active        INTEGER NOT NULL DEFAULT 1,        -- 졸업 · 탈퇴는 지우지 않고 끈다
        note          TEXT NOT NULL DEFAULT '',
        created_at    TEXT NOT NULL,
        updated_at    TEXT NOT NULL,
        last_login_at TEXT
    );
    CREATE TABLE logins (
        id     INTEGER PRIMARY KEY,
        at     TEXT NOT NULL,
        email  TEXT NOT NULL,
        result TEXT NOT NULL                             -- ok · 거절 이유 code (auth.REFUSALS)
    );
    CREATE TABLE downloads (
        id      INTEGER PRIMARY KEY,
        at      TEXT NOT NULL,
        email   TEXT NOT NULL,
        file_id TEXT NOT NULL,
        sid     TEXT,
        name    TEXT NOT NULL DEFAULT ''
    );
    """,
]


def now() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


class Db:
    def __init__(self, path: Path):
        self.path = Path(path)

    @contextmanager
    def connect(self):
        """작업 하나마다 연결 하나 (서버는 여러 스레드에서 부른다). with 안에서 끝나면 커밋."""
        with closing(sqlite3.connect(self.path)) as con:
            con.row_factory = sqlite3.Row
            con.execute("PRAGMA foreign_keys = ON")
            with con:
                yield con

    def migrate(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as con:
            done = con.execute("PRAGMA user_version").fetchone()[0]
            for i, sql in enumerate(MIGRATIONS[done:], done + 1):
                con.executescript(sql)
                con.execute(f"PRAGMA user_version = {i}")

    # ---------------------------------------------------------------- 사용자 명단

    def get_user(self, email: str) -> dict | None:
        with self.connect() as con:
            row = con.execute("SELECT * FROM users WHERE email = ?", (email.lower(),)).fetchone()
        return dict(row) if row else None

    def list_users(self) -> list[dict]:
        with self.connect() as con:
            return [dict(r) for r in con.execute("SELECT * FROM users ORDER BY active DESC, role, email")]

    def put_user(self, email: str, role: str, name: str | None = None, active: bool = True, note: str | None = None) -> dict:
        """명단에 넣거나 고친다. name · note 가 None 이면 있던 값을 그대로 둔다."""
        if role not in ROLES:
            raise ValueError(f"역할은 {' · '.join(ROLES)} 중 하나: {role!r}")
        email, t = email.strip().lower(), now()
        if "@" not in email:
            raise ValueError(f"메일 주소가 아님: {email!r}")
        with self.connect() as con:
            con.execute(
                """INSERT INTO users (email, name, role, active, note, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(email) DO UPDATE SET role = excluded.role, active = excluded.active, updated_at = excluded.updated_at,
                       name = COALESCE(?, users.name), note = COALESCE(?, users.note)""",
                (email, name or "", role, int(active), note or "", t, t, name, note))
        return self.get_user(email)

    def set_active(self, email: str, active: bool) -> bool:
        with self.connect() as con:
            cur = con.execute("UPDATE users SET active = ?, updated_at = ? WHERE email = ?", (int(active), now(), email.lower()))
        return cur.rowcount == 1

    def record_login(self, email: str, result: str, name: str | None = None) -> None:
        with self.connect() as con:
            con.execute("INSERT INTO logins (at, email, result) VALUES (?, ?, ?)", (now(), email.lower(), result))
            if result == "ok":
                con.execute("UPDATE users SET last_login_at = ?, name = CASE WHEN ? != '' THEN ? ELSE name END WHERE email = ?",
                            (now(), name or "", name or "", email.lower()))

    def recent_logins(self, limit: int = 50) -> list[dict]:
        with self.connect() as con:
            return [dict(r) for r in con.execute("SELECT * FROM logins ORDER BY id DESC LIMIT ?", (limit,))]
