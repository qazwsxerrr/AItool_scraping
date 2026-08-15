"""Transport adapter and per-item failure isolation for Stage A and B."""

from __future__ import annotations

import logging
from typing import Any, Iterable, Protocol

import httpx

from app.config.settings import Settings

from .guards import apply_analysis_guards, apply_screen_guard
from .models import AnalysisResult, RawIntelEnvelope, ScreenResult
from .parser import strict_parse_analysis, strict_parse_screen
from .prompts import build_analysis_provider_payload, build_screen_provider_payload, preflight_intel_triage_schemas


LOGGER = logging.getLogger(__name__)
SCREEN_FAILURE_STATUS = "screen_failed"
ANALYSIS_FAILURE_STATUS = "analysis_failed"


class SupportsPost(Protocol):
    def post(self, url: str, *, headers: dict[str, str], json: dict[str, Any], **kwargs: Any): ...


class IntelTriageClient:
    """Provider client exposing exactly the Stage A ``screen`` and Stage B ``analyze`` APIs."""

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
    def from_settings(cls, settings: Settings, http_client: SupportsPost | None = None) -> "IntelTriageClient":
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

    def screen(self, envelope: RawIntelEnvelope | dict[str, Any]) -> ScreenResult:
        item = _as_envelope(envelope)
        if not self.is_configured:
            raise RuntimeError("Intel screen API is not configured")
        preflight_intel_triage_schemas()
        response = self._post_once(self._endpoint_url(), build_screen_provider_payload(item, model=self.model, api_style=self.api_style))
        data = _response_json(response, stage="screen")
        return strict_parse_screen(data, envelope=item)

    def analyze(self, envelope: RawIntelEnvelope | dict[str, Any]) -> AnalysisResult:
        item = _as_envelope(envelope)
        if not self.is_configured:
            raise RuntimeError("Intel analysis API is not configured")
        preflight_intel_triage_schemas()
        response = self._post_once(self._endpoint_url(), build_analysis_provider_payload(item, model=self.model, api_style=self.api_style))
        data = _response_json(response, stage="analysis")
        return strict_parse_analysis(data, envelope=item)

    def screen_batch(self, envelopes: Iterable[RawIntelEnvelope | dict[str, Any]]) -> list[ScreenResult]:
        return run_screen_isolated(self, envelopes)

    def analyze_batch(self, envelopes: Iterable[RawIntelEnvelope | dict[str, Any]]) -> list[AnalysisResult]:
        return run_analysis_isolated(self, envelopes)

    def _post_once(self, url: str, payload: dict[str, Any]):
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        if self._http_client is not None:
            try:
                response = self._http_client.post(url, headers=headers, json=payload, timeout=self.timeout_seconds)
            except TypeError:
                response = self._http_client.post(url, headers=headers, json=payload)
        else:
            with httpx.Client(timeout=self.timeout_seconds, follow_redirects=True, http2=True, trust_env=True) as client:
                response = client.post(url, headers=headers, json=payload)
        raise_for_status = getattr(response, "raise_for_status", None)
        if callable(raise_for_status):
            raise_for_status()
        return response

    def _endpoint_url(self) -> str:
        if self.api_url is None:  # pragma: no cover - guarded by is_configured
            raise RuntimeError("Intel API is not configured")
        if self.api_style == "openai_chat" and not self.api_url.casefold().endswith("/chat/completions"):
            return f"{self.api_url}/chat/completions"
        if self.api_style == "openai_responses" and not self.api_url.casefold().endswith("/responses"):
            return f"{self.api_url}/responses"
        return self.api_url


def _response_json(response: Any, *, stage: str) -> Any:
    try:
        return response.json()
    except (TypeError, ValueError) as exc:
        raise ValueError(f"Intel {stage} API returned invalid JSON") from exc


def _as_envelope(value: RawIntelEnvelope | dict[str, Any] | Any) -> RawIntelEnvelope:
    return value if isinstance(value, RawIntelEnvelope) else RawIntelEnvelope.model_validate(value)


def _fallback_envelope(value: Any) -> RawIntelEnvelope:
    try:
        return _as_envelope(value)
    except Exception:
        return RawIntelEnvelope(item_id=None, source_id="unknown", title="invalid raw intel envelope", body_text="")


def _screen_failure(item: RawIntelEnvelope, exc: BaseException) -> ScreenResult:
    message = str(exc).strip() or exc.__class__.__name__
    return ScreenResult(
        item_id=item.item_id,
        decision="uncertain",
        reason_code="provider_failure",
        reason="Stage A provider call failed",
        confidence=0,
        risk_flags=["ai:screen_failed"],
        status=SCREEN_FAILURE_STATUS,
        error_code=exc.__class__.__name__,
        error_message=message,
        raw_response=None,
    )


def _analysis_failure(item: RawIntelEnvelope, exc: BaseException) -> AnalysisResult:
    message = str(exc).strip() or exc.__class__.__name__
    return AnalysisResult(
        item_id=item.item_id,
        topic="opinion",
        topics=["opinion"],
        summary_cn="",
        keywords=[],
        entities=[],
        selection_score=0,
        score_components={},
        paper_support={"is_paper": False},
        risk_flags=["ai:analysis_failed"],
        reason="Stage B provider call failed",
        confidence=0,
        source_content_class=item.source_content_class,
        source_group=item.source_group,
        status=ANALYSIS_FAILURE_STATUS,
        error_code=exc.__class__.__name__,
        error_message=message,
        raw_response=None,
    )


def screen_item(client: Any, envelope: RawIntelEnvelope | dict[str, Any]) -> ScreenResult:
    item = _fallback_envelope(envelope)
    try:
        method = getattr(client, "screen", None)
        if not callable(method):
            raise TypeError("AI client does not expose screen")
        value = method(item)
        result = value if isinstance(value, ScreenResult) else strict_parse_screen(value, envelope=item)
        return apply_screen_guard(result.with_item(item), item)
    except Exception as exc:
        LOGGER.warning("AI screen failed for item %s: %s", item.item_id, exc)
        return _screen_failure(item, exc)


def analyze_item(client: Any, envelope: RawIntelEnvelope | dict[str, Any]) -> AnalysisResult:
    item = _fallback_envelope(envelope)
    try:
        method = getattr(client, "analyze", None)
        if not callable(method):
            raise TypeError("AI client does not expose analyze")
        value = method(item)
        result = value if isinstance(value, AnalysisResult) else strict_parse_analysis(value, envelope=item)
        return apply_analysis_guards(result.with_item(item), item)
    except Exception as exc:
        LOGGER.warning("AI analysis failed for item %s: %s", item.item_id, exc)
        return _analysis_failure(item, exc)


def run_screen_isolated(client: Any, envelopes: Iterable[RawIntelEnvelope | dict[str, Any]]) -> list[ScreenResult]:
    results: list[ScreenResult] = []
    for envelope in envelopes:
        results.append(screen_item(client, envelope))
    return results


def run_analysis_isolated(client: Any, envelopes: Iterable[RawIntelEnvelope | dict[str, Any]]) -> list[AnalysisResult]:
    results: list[AnalysisResult] = []
    for envelope in envelopes:
        results.append(analyze_item(client, envelope))
    return results


safe_screen = screen_item
safe_analyze = analyze_item
screen_items = run_screen_isolated
analyze_items = run_analysis_isolated
isolate_screen_failure = screen_item
isolate_analysis_failure = analyze_item
isolate_screen_failures = run_screen_isolated
isolate_analysis_failures = run_analysis_isolated


def _normalize_api_style(value: Any) -> str:
    style = str(value or "generic_json").strip().casefold().replace("-", "_")
    style = {
        "responses": "openai_responses", "openai_response": "openai_responses", "openai_responses_api": "openai_responses",
        "chat": "openai_chat", "chat_completions": "openai_chat",
    }.get(style, style)
    if style not in {"generic_json", "openai_chat", "openai_responses"}:
        raise ValueError("api_style must be generic_json, openai_chat, or openai_responses")
    return style


__all__ = [
    "ANALYSIS_FAILURE_STATUS", "IntelTriageClient", "SCREEN_FAILURE_STATUS", "SupportsPost",
    "analyze_item", "analyze_items", "isolate_analysis_failure", "isolate_analysis_failures",
    "isolate_screen_failure", "isolate_screen_failures", "run_analysis_isolated", "run_screen_isolated",
    "safe_analyze", "safe_screen", "screen_item", "screen_items",
]
