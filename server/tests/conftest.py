"""테스트는 가짜 연결부 · 임시 DB 로만 돈다 — 진짜 데이터 · server/.local 을 건드리지 않는다."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from lefsite import config
from lefsite.app import create_app

BASE = "http://127.0.0.1:8000"


def settings(tmp_path, **env) -> config.Settings:
    base = {"LEF_SITE_ENGINE": "fake", "LEF_SITE_SECRET_KEY": "test-secret", "LEF_SITE_DB": str(tmp_path / "site.db"),
            "LEF_SITE_WEB_DIST": str(tmp_path / "no-dist")}
    return config.load({**base, **env})


@pytest.fixture
def make_client(tmp_path):
    def make(**env) -> TestClient:
        return TestClient(create_app(settings(tmp_path, **env)), base_url=BASE)
    return make


@pytest.fixture
def client(make_client) -> TestClient:
    return make_client()
