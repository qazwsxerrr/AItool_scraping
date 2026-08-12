from __future__ import annotations

from app.config.settings import Settings


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


def test_settings_stage_models_fall_back_to_review_model(monkeypatch):
    for name in (
        "AI_REVIEW_MODEL",
        "AI_TRIAGE_MODEL",
        "AI_CLUSTER_MODEL",
        "AI_COMPOSE_MODEL",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("AI_REVIEW_MODEL", "review-model")

    settings = Settings.from_env(dotenv_path="/path/that/does/not/exist")

    assert settings.ai_review_model == "review-model"
    assert settings.ai_triage_model == "review-model"
    assert settings.ai_cluster_model == "review-model"
    assert settings.ai_compose_model == "review-model"


def test_settings_stage_models_allow_independent_overrides(monkeypatch):
    monkeypatch.setenv("AI_REVIEW_MODEL", "review-model")
    monkeypatch.setenv("AI_TRIAGE_MODEL", "triage-model")
    monkeypatch.setenv("AI_CLUSTER_MODEL", "cluster-model")
    monkeypatch.setenv("AI_COMPOSE_MODEL", "compose-model")

    settings = Settings.from_env(dotenv_path="/path/that/does/not/exist")

    assert settings.ai_triage_model == "triage-model"
    assert settings.ai_cluster_model == "cluster-model"
    assert settings.ai_compose_model == "compose-model"
