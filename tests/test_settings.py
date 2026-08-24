from __future__ import annotations

import sqlite3

import pytest

from app.ai.skills.stage_c_agent.client import StageCAgentClient
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


def test_settings_stage_d_reuses_ai_review_configuration(monkeypatch):
    for name in ("AI_REVIEW_API_URL", "AI_REVIEW_API_KEY", "AI_REVIEW_MODEL", "REQUEST_RETRIES"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("AI_REVIEW_API_URL", "https://review.example/v1")
    monkeypatch.setenv("AI_REVIEW_API_KEY", "review-secret")
    monkeypatch.setenv("AI_REVIEW_MODEL", "review-model")
    monkeypatch.setenv("REQUEST_RETRIES", "3")
    settings = Settings.from_env(dotenv_path="/path/that/does/not/exist")
    client = StageDSelectionClient.from_settings(settings)
    assert client.model == "review-model"
    assert client.transport == "responses"
    assert client.is_configured is True
    assert client.max_retries == 3
    assert client.timeout_seconds >= 120


def test_settings_read_stage_c_and_d_search_budgets_and_tavily(monkeypatch):
    values = {
        "AI_STAGE_B_RESERVE_LIMIT": "17",
        "AI_STAGE_C_TIMEOUT_SECONDS": "240",
        "AI_STAGE_C_AGENT_MAX_TURNS": "96",
        "AI_STAGE_C_AGENT_MAX_TOOL_CALLS": "480",
        "AI_STAGE_C_AGENT_MAX_WEB_SEARCHES": "64",
        "AI_STAGE_D_MAX_WEB_SEARCHES": "9",
        "TAVILY_API_KEY": "test-tavily-key",
        "TAVILY_API_URL": "https://search.example/api",
    }
    for name, value in values.items():
        monkeypatch.setenv(name, value)
    settings = Settings.from_env(dotenv_path="/path/that/does/not/exist")
    assert settings.stage_b_reserve_limit == 17
    assert settings.stage_c_timeout_seconds == 240
    assert settings.stage_c_agent_max_turns == 96
    assert settings.stage_c_agent_max_tool_calls == 480
    assert settings.stage_c_agent_max_web_searches == 64
    assert settings.stage_d_max_web_searches == 9
    assert settings.tavily_api_key == "test-tavily-key"
    assert settings.tavily_api_url == "https://search.example/api"


def test_settings_stage_c_agent_budget_defaults(monkeypatch):
    for name in (
        "AI_STAGE_C_TIMEOUT_SECONDS",
        "AI_STAGE_C_AGENT_MAX_TURNS",
        "AI_STAGE_C_AGENT_MAX_TOOL_CALLS",
        "AI_STAGE_C_AGENT_MAX_WEB_SEARCHES",
    ):
        monkeypatch.delenv(name, raising=False)

    settings = Settings.from_env(dotenv_path="/path/that/does/not/exist")

    assert settings.stage_c_timeout_seconds == 120
    assert settings.stage_c_agent_max_turns == 32
    assert settings.stage_c_agent_max_tool_calls == 120
    assert settings.stage_c_agent_max_web_searches == 16


def test_stage_c_client_uses_its_dedicated_timeout():
    client = StageCAgentClient.from_settings(Settings(stage_c_timeout_seconds=180))

    assert client.timeout_seconds == 180


def test_settings_can_disable_stage_c_web_search(monkeypatch):
    monkeypatch.setenv("AI_STAGE_C_AGENT_MAX_WEB_SEARCHES", "0")
    settings = Settings.from_env(dotenv_path="/path/that/does/not/exist")
    assert settings.stage_c_agent_max_web_searches == 0


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
