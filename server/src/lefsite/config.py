"""설정 — 전부 환경 변수에서 읽는다 (`server/.env` 가 있으면 먼저 읽는다, 이미 있는 환경 변수가 이긴다).

로컬 PC → 서버로 옮길 때 이 값들만 바뀌게 만든다. 이름 · 기본값은 `server/.env.example` 와 같다.
"""

from __future__ import annotations

import ipaddress
import os
import secrets
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

from dotenv import load_dotenv

SERVER_DIR = Path(__file__).resolve().parents[2]
REPO_DIR = SERVER_DIR.parent
LOCAL_DIR = SERVER_DIR / ".local"          # 앱 DB · 쿠키 서명 키 (git 에 넣지 않는다)

ENGINES = ("fake", "real")
AUTH_MODES = ("dev", "google")
ROLES = ("admin", "lead", "member")


class ConfigError(Exception):
    """설정이 빠졌거나 같이 쓰면 안 되는 값 — 서버를 띄우지 않는다."""


@dataclass(frozen=True)
class Settings:
    engine: str = "fake"                    # fake = 합성 세션 · real = lefdata.api
    session_root: Path | None = None        # 엔진 세션 저장소 (None = 엔진 기본값)
    data_root: Path | None = None           # 엔진 데이터 폴더 (None = 엔진 기본값)
    host: str = "127.0.0.1"
    port: int = 8000
    public_url: str = ""                    # 브라우저가 여는 주소 (Google 로그인 돌아올 곳의 앞부분). 빈 값 = http://host:port
    auth: str = "dev"                       # dev = 개발용 자동 로그인 (127.0.0.1 에서만) · google
    dev_email: str = "dev@localhost"
    dev_name: str = "개발용"
    dev_role: str = "admin"
    secret_key: str = ""                    # 로그인 쿠키 서명 키. 빈 값 = .local/secret_key (없으면 만든다)
    session_max_age_s: int = 7 * 24 * 3600
    db_path: Path = LOCAL_DIR / "site.db"
    allowed_domain: str = ""                # Google 로그인을 받을 메일 도메인 (hd 값과 메일 끝을 둘 다 본다)
    google_client_id: str = ""
    google_client_secret: str = ""
    web_dist: Path = REPO_DIR / "web" / "dist"

    @property
    def base_url(self) -> str:
        return (self.public_url or f"http://{self.host}:{self.port}").rstrip("/")

    @property
    def https(self) -> bool:
        return self.base_url.startswith("https://")


def _path(v: str | None) -> Path | None:
    return Path(v).expanduser() if v else None


def is_loopback(host: str) -> bool:
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def load(env: dict[str, str] | None = None, dotenv: bool = True, validate: bool = True) -> Settings:
    """환경 변수 → Settings. 테스트는 env 를 직접 준다. validate=False 는 명단 명령처럼 서버를 띄우지 않을 때."""
    if env is None:
        if dotenv:
            load_dotenv(SERVER_DIR / ".env", override=False)
        env = dict(os.environ)
    g = lambda k, d="": env.get(k, d).strip()  # noqa: E731
    s = Settings(
        engine=g("LEF_SITE_ENGINE", "fake"),
        session_root=_path(g("LEF_SESSION_ROOT")),
        data_root=_path(g("LEF_DATA_ROOT")),
        host=g("LEF_SITE_HOST", "127.0.0.1"),
        port=int(g("LEF_SITE_PORT", "8000")),
        public_url=g("LEF_SITE_PUBLIC_URL"),
        auth=g("LEF_SITE_AUTH", "dev"),
        dev_email=g("LEF_SITE_DEV_EMAIL", "dev@localhost").lower(),
        dev_name=g("LEF_SITE_DEV_NAME", "개발용"),
        dev_role=g("LEF_SITE_DEV_ROLE", "admin"),
        secret_key=g("LEF_SITE_SECRET_KEY"),
        session_max_age_s=int(g("LEF_SITE_SESSION_MAX_AGE_S", str(7 * 24 * 3600))),
        db_path=_path(g("LEF_SITE_DB")) or LOCAL_DIR / "site.db",
        allowed_domain=g("LEF_SITE_ALLOWED_DOMAIN").lower().lstrip("@"),
        google_client_id=g("LEF_SITE_GOOGLE_CLIENT_ID"),
        google_client_secret=g("LEF_SITE_GOOGLE_CLIENT_SECRET"),
        web_dist=_path(g("LEF_SITE_WEB_DIST")) or REPO_DIR / "web" / "dist",
    )
    if validate:
        check(s)
    return s


def check(s: Settings) -> None:
    """같이 쓰면 위험한 설정을 막는다 — 띄운 뒤에 알게 되면 늦다."""
    if s.engine not in ENGINES:
        raise ConfigError(f"LEF_SITE_ENGINE 은 {' · '.join(ENGINES)} 중 하나: {s.engine!r}")
    if s.auth not in AUTH_MODES:
        raise ConfigError(f"LEF_SITE_AUTH 는 {' · '.join(AUTH_MODES)} 중 하나: {s.auth!r}")
    if s.dev_role not in ROLES:
        raise ConfigError(f"LEF_SITE_DEV_ROLE 은 {' · '.join(ROLES)} 중 하나: {s.dev_role!r}")
    public_host = urlsplit(s.base_url).hostname or ""
    if s.auth == "dev" and not (is_loopback(s.host) and is_loopback(public_host)):
        # 개발용 자동 로그인은 누구나 관리자로 들어오게 한다 — 이 PC 밖에서 닿으면 안 된다
        raise ConfigError(f"LEF_SITE_AUTH=dev 는 127.0.0.1 에서만 쓴다 (host {s.host!r}, 주소 {s.base_url!r}) — 밖에 열려면 google")
    if s.auth == "google":
        missing = [k for k, v in (("LEF_SITE_GOOGLE_CLIENT_ID", s.google_client_id), ("LEF_SITE_GOOGLE_CLIENT_SECRET", s.google_client_secret),
                                  ("LEF_SITE_ALLOWED_DOMAIN", s.allowed_domain)) if not v]
        if missing:
            raise ConfigError(f"LEF_SITE_AUTH=google 에 필요한 값이 없음: {', '.join(missing)} (server/.env)")
        if not s.https and not is_loopback(public_host):
            # 로그인 쿠키에 Secure 를 붙일 수 없는 주소 — 밖에서는 https 로만
            raise ConfigError(f"밖에 여는 주소는 https 여야 한다: {s.base_url!r}")


def secret_key(s: Settings) -> str:
    """로그인 쿠키 서명 키. 설정에 없으면 .local/secret_key 를 읽고, 그것도 없으면 만들어 둔다 (서버를 다시 켜도 로그인이 풀리지 않게)."""
    if s.secret_key:
        return s.secret_key
    path = LOCAL_DIR / "secret_key"
    if path.exists():
        return path.read_text(encoding="ascii").strip()
    LOCAL_DIR.mkdir(parents=True, exist_ok=True)
    key = secrets.token_urlsafe(48)
    path.write_text(key, encoding="ascii")
    return key
