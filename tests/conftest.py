from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _isolate_default_database_url(monkeypatch, tmp_path):
    """Keep CLI/default-settings tests away from a developer's real SQLite DB."""

    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'default-test.db'}")
