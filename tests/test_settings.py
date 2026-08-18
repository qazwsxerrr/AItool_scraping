from __future__ import annotations

import sqlite3

import pytest

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


def test_settings_stage_d_has_own_overrides_and_review_fallback(monkeypatch):
    for name in (
        "AI_REVIEW_API_URL",
        "AI_REVIEW_API_KEY",
        "AI_REVIEW_MODEL",
        "AI_STAGE_D_API_URL",
        "AI_STAGE_D_API_KEY",
        "AI_STAGE_D_MODEL",
        "AI_STAGE_D_RETRIES",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("AI_REVIEW_API_URL", "https://review.example/v1")
    monkeypatch.setenv("AI_REVIEW_API_KEY", "review-secret")
    monkeypatch.setenv("AI_REVIEW_MODEL", "review-model")

    fallback = Settings.from_env(dotenv_path="/path/that/does/not/exist")
    assert fallback.ai_stage_d_api_url == "https://review.example/v1"
    assert fallback.ai_stage_d_api_key == "review-secret"
    assert fallback.ai_stage_d_model == "review-model"

    monkeypatch.setenv("AI_STAGE_D_API_URL", "https://editor.example/v1")
    monkeypatch.setenv("AI_STAGE_D_API_KEY", "editor-secret")
    monkeypatch.setenv("AI_STAGE_D_MODEL", "editor-model")
    monkeypatch.setenv("AI_STAGE_D_RETRIES", "3")
    dedicated = Settings.from_env(dotenv_path="/path/that/does/not/exist")
    assert dedicated.ai_stage_d_api_url == "https://editor.example/v1"
    assert dedicated.ai_stage_d_api_key == "editor-secret"
    assert dedicated.ai_stage_d_model == "editor-model"
    assert dedicated.ai_stage_d_retries == 3


def test_settings_read_category_taxonomy(monkeypatch):
    monkeypatch.delenv("AI_REVIEW_CATEGORIES", raising=False)
    monkeypatch.setenv("AI_REVIEW_CATEGORIES", "研究论文，产品与工具,研究论文")
    monkeypatch.setenv("AI_REVIEW_CATEGORY_MODE", "source")
    settings = Settings.from_env(dotenv_path="/path/that/does/not/exist")
    assert settings.ai_review_categories == ("研究论文", "产品与工具")
    assert settings.ai_review_category_mode == "source"


def test_init_db_rejects_incompatible_legacy_schema_without_backfill(tmp_path):
    path = tmp_path / "legacy.db"
    connection = sqlite3.connect(path)
    connection.execute("CREATE TABLE ai_item_reviews (id INTEGER PRIMARY KEY)")
    connection.commit()
    connection.close()
    with pytest.raises(RuntimeError, match="incompatible.*no migrations/backfill"):
        init_db(create_engine_from_url(f"sqlite:///{path}"))
