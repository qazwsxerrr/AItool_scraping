from pathlib import Path

import yaml


def test_rsshub_local_compose_uses_env_file_for_x_credentials():
    compose_path = Path("rsshub-local/docker-compose.yml")
    assert compose_path.exists()

    compose = yaml.safe_load(compose_path.read_text(encoding="utf-8"))
    service = compose["services"]["rsshub"]

    assert service["image"] == "diygod/rsshub:latest"
    assert service["container_name"] == "rsshub"
    assert service["ports"] == ["1200:1200"]
    assert service["env_file"] == [".env"]
    assert service["environment"]["CACHE_TYPE"] == "memory"
    assert service["environment"]["CACHE_EXPIRE"] == "600"
    assert "TWITTER_AUTH_TOKEN" not in service["environment"]


def test_rsshub_local_env_example_documents_token_without_real_secret():
    env_example_path = Path("rsshub-local/.env.example")
    assert env_example_path.exists()

    content = env_example_path.read_text(encoding="utf-8")
    assert "TWITTER_AUTH_TOKEN=" in content
    assert "your_x_auth_token" in content
    assert "RSSHUB_BASE_URL" not in content
