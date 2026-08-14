"""One-call provider adapter and per-item AI failure isolation."""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Iterable, Protocol

import httpx

from app.config.settings import Settings

from .guards import apply_deterministic_guards, infer_topic
from .models import RawIntelEnvelope, TriageResult
from .parser import strict_parse_triage
from .prompts import build_provider_payload


LOGGER = logging.getLogger(__name__)
AI_FAILURE_STATUS = "ai_failed"


class SupportsPost(Protocol):
    def post(self, url: str, *, headers: dict[str, str], json: dict[str, Any], **kwargs: Any): ...


class IntelTriageClient:
    """Analyze one :class:`RawIntelEnvelope` with exactly one HTTP request."""

    def __init__(
        self,
        *,
        api_url: str | None,
        api_key: str | None,
        model: str | None = None,
        api_style: str = "generic_json",
        timeout_seconds: float = 30.0,
        http_client: SupportsPost | None = None,
    ) -> None:
        self.api_url = api_url.rstrip("/") if api_url else None
        self.api_key = api_key
        self.model = model
        self.api_style = _normalize_api_style(api_style)
        self.timeout_seconds = timeout_seconds
        self._http_client = http_client

    @classmethod
    def from_settings(
        cls,
        settings: Settings,
        http_client: SupportsPost | None = None,
    ) -> "IntelTriageClient":
        return cls(
            api_url=settings.ai_review_api_url,
            api_key=settings.ai_review_api_key,
            model=settings.ai_review_model,
            api_style=settings.ai_review_api_style,
            timeout_seconds=settings.ai_review_timeout_seconds,
            http_client=http_client,
        )

    @property
    def is_configured(self) -> bool:
        return bool(self.api_url and self.api_key)

    def triage(self, envelope: RawIntelEnvelope | dict[str, Any]) -> TriageResult:
        item = envelope if isinstance(envelope, RawIntelEnvelope) else RawIntelEnvelope.model_validate(envelope)
        if not self.is_configured:
            raise RuntimeError("Intel triage API is not configured")
        response = self._post_once(self._endpoint_url(), build_provider_payload(item, model=self.model, api_style=self.api_style))
        try:
            data = response.json()
        except (TypeError, ValueError) as exc:
            raise ValueError("Intel triage API returned invalid JSON") from exc
        return strict_parse_triage(data, envelope=item)

    # ``analyze`` mirrors ItemAnalysisClient and makes adapters interchangeable.
    analyze = triage

    def triage_batch(self, envelopes: Iterable[RawIntelEnvelope | dict[str, Any]]) -> list[TriageResult]:
        return run_triage_batch(self, envelopes)

    def _post_once(self, url: str, payload: dict[str, Any]):
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        if self._http_client is not None:
            try:
                response = self._http_client.post(
                    url,
                    headers=headers,
                    json=payload,
                    timeout=self.timeout_seconds,
                )
            except TypeError:
                # Minimal fake clients used by tests often omit timeout.
                response = self._http_client.post(url, headers=headers, json=payload)
            raise_for_status = getattr(response, "raise_for_status", None)
            if callable(raise_for_status):
                raise_for_status()
            return response
        with httpx.Client(
            timeout=self.timeout_seconds,
            follow_redirects=True,
            http2=True,
            trust_env=True,
        ) as client:
            response = client.post(url, headers=headers, json=payload)
            raise_for_status = getattr(response, "raise_for_status", None)
            if callable(raise_for_status):
                raise_for_status()
            return response

    def _endpoint_url(self) -> str:
        if self.api_url is None:  # pragma: no cover - guarded by is_configured
            raise RuntimeError("Intel triage API is not configured")
        if self.api_style == "openai_chat" and not self.api_url.casefold().endswith("/chat/completions"):
            return f"{self.api_url}/chat/completions"
        if self.api_style == "openai_responses" and not self.api_url.casefold().endswith("/responses"):
            return f"{self.api_url}/responses"
        return self.api_url


TriageClient = IntelTriageClient


def _failure_result(item: RawIntelEnvelope, exc: BaseException) -> TriageResult:
    error_text = str(exc).strip() or exc.__class__.__name__
    return TriageResult(
        item_id=item.item_id,
        keep=False,
        topic=infer_topic(item),
        topics=[infer_topic(item)],
        summary_cn="",
        keywords=[],
        selection_score=0,
        novelty="unknown",
        paper_support={"is_paper": infer_topic(item) == "paper"},
        risk_flags=["ai_failed"],
        reason="provider_failure",
        confidence=0,
        content_class=item.source_content_class,
        source_group=item.source_group,
        status=AI_FAILURE_STATUS,
        error_code=exc.__class__.__name__,
        error_message=error_text,
        raw_response=None,
    )


def triage_item(
    client: Any,
    envelope: RawIntelEnvelope | dict[str, Any],
) -> TriageResult:
    """Run one triage call and convert any failure into an auditable result."""

    item = envelope if isinstance(envelope, RawIntelEnvelope) else RawIntelEnvelope.model_validate(envelope)
    try:
        analyzer = getattr(client, "triage", None) or getattr(client, "analyze", None)
        if not callable(analyzer):
            raise TypeError("AI triage client does not expose triage/analyze")
        value = analyzer(item)
        if isinstance(value, TriageResult):
            result = value.with_item(item)
        else:
            result = strict_parse_triage(value, envelope=item)
        return apply_deterministic_guards(result, item)
    except Exception as exc:
        LOGGER.warning("AI triage failed for item %s: %s", item.item_id, exc)
        return _failure_result(item, exc)


def safe_triage(client: Any, envelope: RawIntelEnvelope | dict[str, Any]) -> TriageResult:
    """Descriptive alias for :func:`triage_item`."""

    return triage_item(client, envelope)


def run_triage_batch(
    client: Any,
    envelopes: Iterable[RawIntelEnvelope | dict[str, Any]],
) -> list[TriageResult]:
    """Triage each item independently, preserving input order and isolation."""

    results: list[TriageResult] = []
    for envelope in envelopes:
        try:
            results.append(triage_item(client, envelope))
        except Exception as exc:  # validation failures must also be isolated
            try:
                item = envelope if isinstance(envelope, RawIntelEnvelope) else RawIntelEnvelope.model_validate(envelope)
            except Exception:
                # A malformed envelope has no safe source identity; retain an
                # auditable fallback row rather than aborting the batch.
                item = RawIntelEnvelope(
                    item_id=None,
                    source_id="unknown",
                    title="invalid raw intel envelope",
                    body_text="",
                )
            results.append(_failure_result(item, exc))
    return results


triage_items = run_triage_batch
isolate_ai_failures = run_triage_batch
run_triage_isolated = run_triage_batch
isolate_ai_failure = triage_item


def _normalize_api_style(value: Any) -> str:
    style = str(value or "generic_json").strip().casefold().replace("-", "_")
    aliases = {
        "responses": "openai_responses",
        "openai_response": "openai_responses",
        "openai_responses_api": "openai_responses",
        "chat": "openai_chat",
        "chat_completions": "openai_chat",
    }
    style = aliases.get(style, style)
    if style not in {"generic_json", "openai_chat", "openai_responses"}:
        raise ValueError("api_style must be generic_json, openai_chat, or openai_responses")
    return style


__all__ = [
    "AI_FAILURE_STATUS",
    "IntelTriageClient",
    "SupportsPost",
    "TriageClient",
    "isolate_ai_failures",
    "isolate_ai_failure",
    "run_triage_isolated",
    "run_triage_batch",
    "safe_triage",
    "triage_item",
    "triage_items",
]
