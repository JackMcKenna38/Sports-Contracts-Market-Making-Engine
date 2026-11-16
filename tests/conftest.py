from __future__ import annotations

import json
import os
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import create_engine

from mm.config import Config, load_config
from mm.storage import schema
from mm.storage.store import Store

FIXTURES = Path(__file__).parent / "fixtures"

# Set to run the suite against a shared server database (e.g. PostgreSQL) instead of a
# per-test SQLite file. Every table in it is dropped before each test.
TEST_DATABASE_URL = os.environ.get("MM_TEST_DATABASE_URL")


def fixture_json(name: str) -> Any:
    assert "synthetic" in name, "test fixtures must be labelled synthetic"
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


@pytest.fixture
def cfg(tmp_path: Path) -> Config:
    if TEST_DATABASE_URL:
        engine = create_engine(TEST_DATABASE_URL)
        schema.metadata.drop_all(engine)
        engine.dispose()
    base = load_config()
    paths = base.paths.model_copy(
        update={
            "data_dir": tmp_path / "data",
            "reports_dir": tmp_path / "reports",
            "database_url": TEST_DATABASE_URL or f"sqlite:///{(tmp_path / 'test.db').as_posix()}",
        }
    )
    http = base.http.model_copy(
        update={"min_interval_seconds": 0.0, "backoff_base_seconds": 0.001, "max_retries": 2}
    )
    return base.model_copy(update={"paths": paths, "http": http})


@pytest.fixture
def store(cfg: Config) -> Iterator[Store]:
    st = Store(cfg.paths.database_url)
    st.init()
    yield st
    st.close()
