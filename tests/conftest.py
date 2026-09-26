from __future__ import annotations

import uuid
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from readhorizon.analysis.llm import DemoClient
from readhorizon.config import Settings
from readhorizon.db import Store
from readhorizon.main import create_app

SAMPLES = Path(__file__).resolve().parent.parent / "samples"


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(
        db_path=tmp_path / "test.db",
        llm_mode="demo",
        model="claude-opus-5",
        effort="high",
        retention_days=180,
        nl_cert_key=None,
        max_upload_mb=2,
        fulltext_char_limit=200_000,
    )


@pytest.fixture
def store(settings: Settings) -> Store:
    s = Store(settings.db_path)
    yield s
    s.close()


@pytest.fixture
def client(settings: Settings, store: Store) -> TestClient:
    app = create_app(settings=settings, llm=DemoClient(), store=store)
    return TestClient(app)


@pytest.fixture
def workspace() -> dict[str, str]:
    return {"X-Workspace-Id": str(uuid.uuid4())}


@pytest.fixture
def seosi_text() -> str:
    return (SAMPLES / "works" / "서시_윤동주.txt").read_text(encoding="utf-8")
