from __future__ import annotations

import sqlite3

import pytest

from app.ai.skills.stage_d_selection.client import StageDSelectionClient
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


def test_settings_read_stage_c_input_min_score(monkeypatch):
    monkeypatch.delenv("AI_STAGE_C_INPUT_MIN_SCORE", raising=False)
    monkeypatch.setenv("AI_STAGE_C_INPUT_MIN_SCORE", "72")

    settings = Settings.from_env(dotenv_path="/path/that/does/not/exist")

    assert settings.ai_stage_c_input_min_score == 72


def test_settings_stage_c_input_min_score_defaults_to_60(monkeypatch):
    monkeypatch.delenv("AI_STAGE_C_INPUT_MIN_SCORE", raising=False)

    settings = Settings.from_env(dotenv_path="/path/that/does/not/exist")

    assert settings.ai_stage_c_input_min_score == 60


def test_settings_stage_d_reuses_ai_review_configuration(monkeypatch):
    for name in ("AI_REVIEW_API_URL", "AI_REVIEW_API_KEY", "AI_REVIEW_MODEL", "AI_REVIEW_API_STYLE", "REQUEST_RETRIES"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("AI_REVIEW_API_URL", "https://review.example/v1")
    monkeypatch.setenv("AI_REVIEW_API_KEY", "review-secret")
    monkeypatch.setenv("AI_REVIEW_MODEL", "review-model")
    monkeypatch.setenv("AI_REVIEW_API_STYLE", "openai_responses")
    monkeypatch.setenv("REQUEST_RETRIES", "3")
    settings = Settings.from_env(dotenv_path="/path/that/does/not/exist")
    client = StageDSelectionClient.from_settings(settings)
    assert client.api_url == "https://review.example/v1"
    assert client.api_key == "review-secret"
    assert client.model == "review-model"
    assert client.api_style == "openai_responses"
    assert client.max_retries == 3
    assert client.timeout_seconds >= 120


def test_settings_read_category_taxonomy(monkeypatch):
    monkeypatch.delenv("AI_REVIEW_CATEGORIES", raising=False)
    monkeypatch.setenv("AI_REVIEW_CATEGORIES", "论文，产品与工具,观点")
    settings = Settings.from_env(dotenv_path="/path/that/does/not/exist")
    assert settings.ai_review_categories == (
        "开发生态",
        "模型发布",
        "产品应用",
        "行业动态",
        "技术与洞察",
        "前瞻与传闻",
    )


def test_init_db_rejects_incompatible_legacy_schema_without_backfill(tmp_path):
    path = tmp_path / "legacy.db"
    connection = sqlite3.connect(path)
    connection.execute("CREATE TABLE ai_item_reviews (id INTEGER PRIMARY KEY)")
    connection.commit()
    connection.close()
    with pytest.raises(RuntimeError, match="unsupported.*no conversion"):
        init_db(create_engine_from_url(f"sqlite:///{path}"))
