from pathlib import Path


def test_rsshub_runner_reads_root_env_only():
    runner_path = Path("scripts/start_rsshub.sh")
    assert runner_path.exists()

    content = runner_path.read_text(encoding="utf-8")
    assert 'PROJECT_ROOT=$(cd -- "$SCRIPT_DIR/.." && pwd)' in content
    assert 'export PORT="$RSSHUB_PORT"' in content
    assert "RSSHUB_LOCAL_PROXY_URI" not in content
    assert "RSSHUB_PROXY_URI" not in content
    assert "127.0.0.1:2080" not in content
    assert 'echo "RSSHub proxy: ${PROXY_URI:-disabled}"' in content


def test_root_env_example_declares_local_rsshub_settings():
    content = Path(".env.example").read_text(encoding="utf-8")

    assert "RSSHUB_PORT=1200" in content
    assert "PROXY_URI=http://127.0.0.1:2080" in content
    assert "RSSHUB_LOCAL_PROXY_URI" not in content
    assert "RSSHUB_PROXY_URI" not in content
