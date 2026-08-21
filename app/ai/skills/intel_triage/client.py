"""Responses-only client and per-item failure isolation for Stage A/B."""

from __future__ import annotations

import logging
from typing import Any, Iterable, Mapping

from app.ai.responses import ResponsesClient, SupportsPost
from app.config.settings import Settings

from .guards import apply_analysis_guards, apply_screen_guard
from .models import AnalysisResult, RawIntelEnvelope, ScreenResult
from .parser import strict_parse_analysis, strict_parse_screen
from .prompts import build_analysis_provider_payload, build_screen_provider_payload, preflight_intel_triage_schemas


LOGGER = logging.getLogger(__name__)
SCREEN_FAILURE_STATUS = "screen_failed"
ANALYSIS_FAILURE_STATUS = "analysis_failed"


class IntelTriageResponseParseError(ValueError):
    """A provider returned a response that failed local schema parsing."""

    def __init__(self, message: str, *, raw_response: Mapping[str, Any] | None) -> None:
        self.raw_response = dict(raw_response) if isinstance(raw_response, Mapping) else None
        super().__init__(message)


class IntelTriageClient:
    """The sole Responses transport used by Stage A and Stage B."""

    transport = "responses"

    def __init__(
        self,
        *,
        api_url: str | None,
        api_key: str | None,
        model: str | None = None,
        timeout_seconds: float = 30.0,
        http_client: SupportsPost | None = None,
    ) -> None:
        self.model = model
        self._responses = ResponsesClient(
            api_url=api_url,
            api_key=api_key,
            model=model,
            timeout_seconds=timeout_seconds,
            http_client=http_client,
        )

    @classmethod
    def from_settings(cls, settings: Settings, http_client: SupportsPost | None = None) -> "IntelTriageClient":
        return cls(
            api_url=settings.ai_review_api_url,
            api_key=settings.ai_review_api_key,
            model=settings.ai_review_model,
            timeout_seconds=settings.ai_review_timeout_seconds,
            http_client=http_client,
        )

    @property
    def is_configured(self) -> bool:
        return self._responses.is_configured

    def screen(self, envelope: RawIntelEnvelope | dict[str, Any]) -> ScreenResult:
        item = _as_envelope(envelope)
        preflight_intel_triage_schemas()
        response = self._responses.create(build_screen_provider_payload(item, model=self.model))
        try:
            return strict_parse_screen(response, envelope=item)
        except Exception as exc:
            raise IntelTriageResponseParseError(
                f"Stage A response failed schema validation: {exc}",
                raw_response=response,
            ) from exc

    def analyze(self, envelope: RawIntelEnvelope | dict[str, Any]) -> AnalysisResult:
        item = _as_envelope(envelope)
        preflight_intel_triage_schemas()
        response = self._responses.create(build_analysis_provider_payload(item, model=self.model))
        try:
            return strict_parse_analysis(response, envelope=item)
        except Exception as exc:
            raise IntelTriageResponseParseError(
                f"Stage B response failed schema validation: {exc}",
                raw_response=response,
            ) from exc

    def screen_batch(self, envelopes: Iterable[RawIntelEnvelope | dict[str, Any]]) -> list[ScreenResult]:
        return run_screen_isolated(self, envelopes)

    def analyze_batch(self, envelopes: Iterable[RawIntelEnvelope | dict[str, Any]]) -> list[AnalysisResult]:
        return run_analysis_isolated(self, envelopes)


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
        raw_response=_raw_response_from_exception(exc),
    )


def _analysis_failure(item: RawIntelEnvelope, exc: BaseException) -> AnalysisResult:
    message = str(exc).strip() or exc.__class__.__name__
    return AnalysisResult(
        item_id=item.item_id,
        topic="technology_insight",
        topics=["technology_insight"],
        summary_cn="",
        keywords=[],
        entities=[],
        b1_priority=0,
        score_components={},
        status=ANALYSIS_FAILURE_STATUS,
        error_code=exc.__class__.__name__,
        error_message=message,
        raw_response=_raw_response_from_exception(exc),
    )


def screen_item(client: Any, envelope: RawIntelEnvelope | dict[str, Any]) -> ScreenResult:
    item = _fallback_envelope(envelope)
    try:
        method = getattr(client, "screen", None)
        if not callable(method):
            raise TypeError("AI client does not expose screen")
        value = method(item)
        try:
            result = value if isinstance(value, ScreenResult) else strict_parse_screen(value, envelope=item)
        except Exception as exc:
            raise IntelTriageResponseParseError(
                f"Stage A response failed schema validation: {exc}",
                raw_response=value if isinstance(value, Mapping) else None,
            ) from exc
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
        try:
            result = value if isinstance(value, AnalysisResult) else strict_parse_analysis(value, envelope=item)
        except Exception as exc:
            raise IntelTriageResponseParseError(
                f"Stage B response failed schema validation: {exc}",
                raw_response=value if isinstance(value, Mapping) else None,
            ) from exc
        return apply_analysis_guards(result.with_item(item), item)
    except Exception as exc:
        LOGGER.warning("AI analysis failed for item %s: %s", item.item_id, exc)
        return _analysis_failure(item, exc)


def run_screen_isolated(client: Any, envelopes: Iterable[RawIntelEnvelope | dict[str, Any]]) -> list[ScreenResult]:
    return [screen_item(client, envelope) for envelope in envelopes]


def run_analysis_isolated(client: Any, envelopes: Iterable[RawIntelEnvelope | dict[str, Any]]) -> list[AnalysisResult]:
    return [analyze_item(client, envelope) for envelope in envelopes]


def _raw_response_from_exception(exc: BaseException) -> dict[str, Any] | None:
    value = getattr(exc, "raw_response", None)
    return dict(value) if isinstance(value, Mapping) else None


__all__ = [
    "ANALYSIS_FAILURE_STATUS",
    "IntelTriageClient",
    "IntelTriageResponseParseError",
    "SCREEN_FAILURE_STATUS",
    "SupportsPost",
    "analyze_item",
    "run_analysis_isolated",
    "run_screen_isolated",
    "screen_item",
]
