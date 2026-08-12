from __future__ import annotations

from app.config.settings import Settings
from app.jobs import fetch_job


def test_fetch_client_profiles_separate_external_proxy_from_local_rsshub(monkeypatch):
    calls = []

    class FakeClient:
        def __init__(self, **kwargs):
            calls.append(kwargs)

        def close(self):
            pass

    monkeypatch.setattr(fetch_job.httpx, "Client", FakeClient)
    settings = Settings(request_timeout_seconds=12, user_agent="test-agent")

    fetch_job._build_http_client(settings, trust_env=True)
    fetch_job._build_http_client(settings, trust_env=False)

    assert calls[0]["trust_env"] is True
    assert calls[0]["http2"] is True
    assert calls[1]["trust_env"] is False
    assert calls[1]["http2"] is True
