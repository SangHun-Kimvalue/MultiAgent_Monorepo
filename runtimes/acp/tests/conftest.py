"""tests/conftest.py — 공통 픽스처."""
from __future__ import annotations


import pytest

from acp.config import LivenessConfig
from acp.store import SessionStore


@pytest.fixture
def tmp_store(tmp_path):
    """임시 DB 경로를 사용하는 SessionStore."""
    db = str(tmp_path / "test.db")
    log = str(tmp_path / "events.jsonl")
    store = SessionStore(db, log)
    yield store
    store.close()


@pytest.fixture
def default_cfg() -> LivenessConfig:
    return LivenessConfig(idle_threshold=120.0, hold_threshold=300.0, stale_ttl=1800.0)
