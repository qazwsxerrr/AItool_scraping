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
