"""설정 — 위험한 조합은 서버를 띄우기 전에 막는다."""

import pytest

from lefsite import config
from lefsite.config import ConfigError


def test_defaults_are_local_dev_with_fake_engine():
    s = config.load({})
    assert (s.engine, s.auth, s.host, s.base_url) == ("fake", "dev", "127.0.0.1", "http://127.0.0.1:8000")
    assert not s.https


@pytest.mark.parametrize("env", [
    {"LEF_SITE_HOST": "0.0.0.0"},                                   # 개발용 자동 로그인을 밖에 열기
    {"LEF_SITE_PUBLIC_URL": "https://data.example.org"},
    {"LEF_SITE_ENGINE": "nope"},
    {"LEF_SITE_AUTH": "password"},
    {"LEF_SITE_DEV_ROLE": "owner"},
    {"LEF_SITE_AUTH": "google"},                                     # 클라이언트 ID · 비밀 · 도메인 없음
    {"LEF_SITE_AUTH": "google", "LEF_SITE_GOOGLE_CLIENT_ID": "x", "LEF_SITE_GOOGLE_CLIENT_SECRET": "y",
     "LEF_SITE_ALLOWED_DOMAIN": "example.ac.kr", "LEF_SITE_HOST": "0.0.0.0", "LEF_SITE_PUBLIC_URL": "http://data.example.org"},  # 밖인데 http
])
def test_refuses_unsafe_settings(env):
    with pytest.raises(ConfigError):
        config.load(env)


def test_google_on_loopback_http_is_allowed_for_the_login_trial():
    s = config.load({"LEF_SITE_AUTH": "google", "LEF_SITE_GOOGLE_CLIENT_ID": "x", "LEF_SITE_GOOGLE_CLIENT_SECRET": "y",
                     "LEF_SITE_ALLOWED_DOMAIN": "@Example.ac.kr"})
    assert s.allowed_domain == "example.ac.kr"


def test_google_behind_https_gets_secure_cookie():
    s = config.load({"LEF_SITE_AUTH": "google", "LEF_SITE_GOOGLE_CLIENT_ID": "x", "LEF_SITE_GOOGLE_CLIENT_SECRET": "y",
                     "LEF_SITE_ALLOWED_DOMAIN": "example.ac.kr", "LEF_SITE_HOST": "0.0.0.0",
                     "LEF_SITE_PUBLIC_URL": "https://data.example.org/"})
    assert s.https and s.base_url == "https://data.example.org"
