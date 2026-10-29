"""명령 — 서버 띄우기 · 명단 관리.

    python -m lefsite                     서버 (LEF_SITE_HOST:PORT, 기본 127.0.0.1:8000)
    python -m lefsite serve --reload      코드를 고치면 다시 띄운다 (개발용)
    python -m lefsite users               명단 보기
    python -m lefsite users add <메일> --role member|lead|admin [--name 이름] [--note 메모]
    python -m lefsite users disable <메일> · enable <메일>
    python -m lefsite logins              최근 로그인 시도 (Google 로그인 시험 때 거절 이유 확인)

명단 관리 화면은 아직 없다 — 그동안 이 명령으로 넣는다 (관리자 PC 에서).
"""

from __future__ import annotations

import argparse
import sys

from . import config
from .config import ROLES, ConfigError
from .db import Db


def _db() -> Db:
    db = Db(config.load(validate=False).db_path)
    db.migrate()
    return db


def _print_users(rows: list[dict]) -> None:
    if not rows:
        print("(명단이 비어 있다)")
    for u in rows:
        print(f"{'켜짐' if u['active'] else '꺼짐'}  {u['role']:<6}  {u['email']:<40}  {u['name'] or '-':<12}  "
              f"마지막 로그인 {u['last_login_at'] or '-'}  {u['note']}")


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):    # Windows 콘솔(cp949)이 못 찍는 글자(— 등)에서 멈추지 않게
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(errors="replace")
    p = argparse.ArgumentParser(prog="lefsite", description="LightOn 데이터 사이트 서버")
    sub = p.add_subparsers(dest="cmd")
    serve = sub.add_parser("serve", help="서버 띄우기 (기본)")
    serve.add_argument("--reload", action="store_true", help="코드를 고치면 다시 띄운다 (개발용)")
    users = sub.add_parser("users", help="명단 보기 · 고치기")
    usub = users.add_subparsers(dest="ucmd")
    add = usub.add_parser("add", help="명단에 넣기 (있으면 역할 · 이름을 고친다)")
    add.add_argument("email")
    add.add_argument("--role", choices=ROLES, default="member")
    add.add_argument("--name")
    add.add_argument("--note")
    for name in ("disable", "enable"):
        usub.add_parser(name, help="끄기 (졸업 · 탈퇴)" if name == "disable" else "다시 켜기").add_argument("email")
    sub.add_parser("logins", help="최근 로그인 시도")
    a = p.parse_args(argv)

    if a.cmd in (None, "serve"):
        import uvicorn
        try:
            s = config.load()
        except ConfigError as e:
            print(f"설정 오류: {e}", file=sys.stderr)
            return 2
        print(f"LightOn Data — {s.base_url} (엔진 {s.engine} · 로그인 {s.auth})")
        uvicorn.run("lefsite.app:create_app", factory=True, host=s.host, port=s.port, reload=getattr(a, "reload", False),
                    reload_dirs=[str(config.SERVER_DIR / "src")] if getattr(a, "reload", False) else None)
        return 0

    db = _db()
    if a.cmd == "logins":
        for r in db.recent_logins():
            print(f"{r['at']}  {r['result']:<16}  {r['email'] or '-'}")
        return 0
    if a.ucmd == "add":
        try:
            u = db.put_user(a.email, a.role, name=a.name, note=a.note)
        except ValueError as e:
            print(e, file=sys.stderr)
            return 2
        _print_users([u])
    elif a.ucmd in ("disable", "enable"):
        if not db.set_active(a.email, a.ucmd == "enable"):
            print(f"명단에 없다: {a.email}", file=sys.stderr)
            return 1
        _print_users([db.get_user(a.email)])
    else:
        _print_users(db.list_users())
    return 0


if __name__ == "__main__":
    sys.exit(main())
