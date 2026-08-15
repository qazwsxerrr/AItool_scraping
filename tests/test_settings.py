from __future__ import annotations

from app.config.settings import Settings
from app.storage.db import create_engine_from_url, init_db


def test_settings_read_canonical_rsshub_name(monkeypatch):
    values = {
        "RSSHUB_BASE_URL": "http://127.0.0.1:1200/",
    }
    names = {
        "RSSHUB_BASE_URL",
    }
    for name in names:
        monkeypatch.delenv(name, raising=False)
    for name, value in values.items():
        monkeypatch.setenv(name, value)

    settings = Settings.from_env(dotenv_path="/path/that/does/not/exist")

    assert settings.rsshub_base_url == "http://127.0.0.1:1200"


def test_settings_read_ai_review_model(monkeypatch):
    monkeypatch.delenv("AI_REVIEW_MODEL", raising=False)
    monkeypatch.setenv("AI_REVIEW_MODEL", "review-model")

    settings = Settings.from_env(dotenv_path="/path/that/does/not/exist")

    assert settings.ai_review_model == "review-model"


def test_settings_read_category_taxonomy(monkeypatch):
    monkeypatch.delenv("AI_REVIEW_CATEGORIES", raising=False)
    monkeypatch.setenv("AI_REVIEW_CATEGORIES", "研究论文，产品与工具,研究论文")
    monkeypatch.setenv("AI_REVIEW_CATEGORY_MODE", "source")
    settings = Settings.from_env(dotenv_path="/path/that/does/not/exist")
    assert settings.ai_review_categories == ("研究论文", "产品与工具")
    assert settings.ai_review_category_mode == "source"


def test_init_db_adds_topic_category_to_existing_schema(tmp_path):
    import sqlite3

    path = tmp_path / "legacy.db"
    connection = sqlite3.connect(path)
    connection.execute("CREATE TABLE ai_item_reviews (id INTEGER PRIMARY KEY)")
    connection.commit()
    connection.close()
    init_db(create_engine_from_url(f"sqlite:///{path}"))
    connection = sqlite3.connect(path)
    columns = {row[1] for row in connection.execute("PRAGMA table_info(ai_item_reviews)")}
    connection.close()
    assert "topic_category" in columns
