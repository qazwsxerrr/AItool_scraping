"""Unified one-call AI client for normalized intelligence items and daily stages."""

from __future__ import annotations

from typing import Any, Callable, Protocol, TypeVar

import httpx

from app.ai.prompts import (
    ITEM_ANALYSIS_RESPONSE_SCHEMA,
    ITEM_ANALYSIS_SYSTEM_PROMPT,
    ITEM_ANALYSIS_TASK,
    CLUSTER_RESPONSE_SCHEMA,
    CLUSTER_SYSTEM_PROMPT,
    CLUSTER_TASK,
    COMPOSE_SYSTEM_PROMPT,
    COMPOSE_TASK,
    EVENT_EDITORIAL_RESPONSE_SCHEMA,
    PROJECT_SUMMARY_SYSTEM_PROMPT,
    PROJECT_SUMMARY_TASK,
    TRIAGE_RESPONSE_SCHEMA,
    TRIAGE_SYSTEM_PROMPT,
    TRIAGE_TASK,
    build_generic_json_payload,
    build_openai_chat_payload,
    build_openai_responses_payload,
    build_stage_payload,
)
from app.ai.schemas import (
    COMMUNITY_SOCIAL,
    CONTENT_CLASSES,
    ContentClass,
    ClusterDecision,
    EventEditorialResponse,
    ItemAnalysisRequest,
    ItemAnalysisResponse,
    OFFICIAL_MODEL_COMPANY,
    PROJECT_SUMMARY_RESPONSE_SCHEMA,
    PROJECT_TOOL,
    StageCallResult,
    TriageResponse,
    parse_cluster_decision_response,
    parse_event_editorial_response,
    parse_item_analysis_response,
    parse_project_summary_response,
    parse_triage_response,
)
from app.config.settings import Settings


StageResultT = TypeVar("StageResultT")


class SupportsPost(Protocol):
    def post(self, url: str, *, headers: dict[str, str], json: dict[str, Any]): ...


class ItemAnalysisClient:
    """Analyze one item with exactly one provider request.

    Retries and multi-stage model work are intentionally absent from this
    boundary.  A job may record and retry a failed item later, but one invocation
    of :meth:`analyze` performs only one HTTP call.
    """

    def __init__(
        self,
        *,
        api_url: str | None,
        api_key: str | None,
        model: str | None = None,
        api_style: str = "generic_json",
        timeout_seconds: float = 30.0,
        http_client: SupportsPost | None = None,
        triage_model: str | None = None,
        cluster_model: str | None = None,
        compose_model: str | None = None,
    ) -> None:
        style = _normalize_api_style(api_style)
        self.api_url = api_url.rstrip("/") if api_url else None
        self.api_key = api_key
        self.model = model
        self.api_style = style
        self.timeout_seconds = timeout_seconds
        self._http_client = http_client
        # Stage-specific overrides are optional.  Keeping them on the same
        # client means URL, key, API style and timeout remain one provider
        # configuration rather than being copied per workflow.
        self.triage_model = triage_model or model
        self.cluster_model = cluster_model or model
        self.compose_model = compose_model or model

    @classmethod
    def from_settings(
        cls,
        settings: Settings,
        http_client: SupportsPost | None = None,
    ) -> "ItemAnalysisClient":
        """Build a client from the configured single-pass AI provider."""

        return cls(
            api_url=settings.ai_review_api_url,
            api_key=settings.ai_review_api_key,
            model=settings.ai_review_model,
            api_style=settings.ai_review_api_style,
            timeout_seconds=settings.ai_review_timeout_seconds,
            http_client=http_client,
            triage_model=getattr(settings, "ai_triage_model", None) or settings.ai_review_model,
            cluster_model=getattr(settings, "ai_cluster_model", None) or settings.ai_review_model,
            compose_model=getattr(settings, "ai_compose_model", None) or settings.ai_review_model,
        )

    @property
    def is_configured(self) -> bool:
        return bool(self.api_url and self.api_key)

    def analyze(self, request: ItemAnalysisRequest) -> ItemAnalysisResponse:
        if not self.is_configured:
            raise RuntimeError("Item analysis API is not configured")
        if not isinstance(request, ItemAnalysisRequest):
            raise TypeError("request must be an ItemAnalysisRequest")

        response = self._post_once(self._endpoint_url(), self._build_payload(request))
        try:
            data = response.json()
        except (TypeError, ValueError) as exc:
            raise ValueError("Item analysis API returned invalid JSON") from exc
        return parse_item_analysis_response(data, request.source_content_class)

    def summarize_project(self, request: ItemAnalysisRequest) -> ItemAnalysisResponse:
        """Run one GitHub project-summary request using the same schema/audit path."""

        if not self.is_configured:
            raise RuntimeError("Item analysis API is not configured")
        if not isinstance(request, ItemAnalysisRequest):
            raise TypeError("request must be an ItemAnalysisRequest")
        payload = self._build_payload(
            request,
            task=PROJECT_SUMMARY_TASK,
            system_prompt=PROJECT_SUMMARY_SYSTEM_PROMPT,
            response_schema=PROJECT_SUMMARY_RESPONSE_SCHEMA,
        )
        response = self._post_once(self._endpoint_url(), payload)
        try:
            data = response.json()
        except (TypeError, ValueError) as exc:
            raise ValueError("Item analysis API returned invalid JSON") from exc
        try:
            return parse_item_analysis_response(data, request.source_content_class)
        except ValueError as analysis_error:
            # The narrow contract may arrive directly, inside a generic
            # envelope, or inside an OpenAI-compatible choices message.  The
            # project parser already knows how to unwrap all of those shapes.
            try:
                return parse_project_summary_response(data)
            except ValueError:
                raise analysis_error

    def triage_item(self, item: Any) -> StageCallResult[TriageResponse]:
        """Run the structured triage stage and retain raw/parse errors."""

        return self._run_stage(
            stage="triage_item",
            input_data=item,
            model=self.triage_model,
            task=TRIAGE_TASK,
            system_prompt=TRIAGE_SYSTEM_PROMPT,
            response_schema=TRIAGE_RESPONSE_SCHEMA,
            parser=parse_triage_response,
        )

    def judge_cluster(
        self,
        left: Any,
        right: Any | None = None,
    ) -> StageCallResult[ClusterDecision]:
        """Judge whether two candidate signals refer to one event.

        Callers may pass one already-shaped mapping (for example with
        ``left``/``right`` keys), or pass the two candidates as separate
        arguments.  The model only suggests a decision; local code owns the
        confidence threshold and final merge operation.
        """

        input_data = left if right is None else {"left": left, "right": right}
        return self._run_stage(
            stage="judge_cluster",
            input_data=input_data,
            model=self.cluster_model,
            task=CLUSTER_TASK,
            system_prompt=CLUSTER_SYSTEM_PROMPT,
            response_schema=CLUSTER_RESPONSE_SCHEMA,
            parser=parse_cluster_decision_response,
        )

    def write_event(
        self,
        event: Any,
        evidence: Any | None = None,
    ) -> StageCallResult[EventEditorialResponse]:
        """Write citation-bearing event copy from an event and its evidence."""

        input_data = event if evidence is None else {"event": event, "evidence": evidence}
        result = self._run_stage(
            stage="write_event",
            input_data=input_data,
            model=self.compose_model,
            task=COMPOSE_TASK,
            system_prompt=COMPOSE_SYSTEM_PROMPT,
            response_schema=EVENT_EDITORIAL_RESPONSE_SCHEMA,
            parser=parse_event_editorial_response,
        )
        # If the caller supplied a structured evidence collection, enforce
        # that every cited ID points to one of those records.  The schema
        # already rejects empty evidence_ids; this additional local check
        # prevents a model from inventing an otherwise non-empty citation.
        if result.ok and evidence is not None and result.parsed is not None:
            known_ids = _extract_evidence_ids(evidence)
            if known_ids:
                unknown = sorted(
                    {
                        evidence_id
                        for fact in result.parsed.facts
                        for evidence_id in fact.evidence_ids
                        if evidence_id not in known_ids
                    }
                )
                if unknown:
                    return StageCallResult(
                        stage="write_event",
                        status="parse_error",
                        parsed=None,
                        raw=result.raw,
                        model=result.model,
                        error="Event editorial response cited unknown evidence_ids: " + ", ".join(unknown),
                    )
        return result

    def _run_stage(
        self,
        *,
        stage: str,
        input_data: Any,
        model: str | None,
        task: str,
        system_prompt: str,
        response_schema: dict[str, str],
        parser: Callable[[Any], StageResultT],
    ) -> StageCallResult[StageResultT]:
        """Execute one stage request and make every failure auditable."""

        if not self.is_configured:
            return StageCallResult(
                stage=stage,
                status="not_configured",
                model=model,
                error=f"{stage} AI API is not configured",
            )

        payload = build_stage_payload(
            input_data,
            model=model,
            task=task,
            system_prompt=system_prompt,
            response_schema=response_schema,
            api_style=self.api_style,
        )
        raw: Any | None = None
        try:
            response = self._post_once(self._endpoint_url(), payload)
        except httpx.HTTPStatusError as exc:
            return StageCallResult(
                stage=stage,
                status="http_error",
                model=model,
                error=f"{type(exc).__name__}: {exc}",
            )
        except Exception as exc:
            return StageCallResult(
                stage=stage,
                status="request_error",
                model=model,
                error=f"{type(exc).__name__}: {exc}",
            )

        try:
            raw = response.json()
        except Exception as exc:
            raw = getattr(response, "text", None)
            return StageCallResult(
                stage=stage,
                status="invalid_json",
                raw=raw,
                model=model,
                error=f"{type(exc).__name__}: provider returned invalid JSON",
            )

        try:
            parsed = parser(raw)
        except Exception as exc:
            return StageCallResult(
                stage=stage,
                status="parse_error",
                raw=raw,
                model=model,
                error=f"{type(exc).__name__}: {exc}",
            )
        return StageCallResult(stage=stage, status="success", parsed=parsed, raw=raw, model=model)

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
                # Small test/fake clients often expose only the minimal
                # ``post(url, headers, json)`` signature.
                response = self._http_client.post(url, headers=headers, json=payload)
        else:
            with httpx.Client(
                timeout=self.timeout_seconds,
                follow_redirects=True,
                http2=True,
                trust_env=True,
            ) as client:
                response = client.post(url, headers=headers, json=payload)
        response.raise_for_status()
        return response

    def _endpoint_url(self) -> str:
        if self.api_url is None:  # pragma: no cover - guarded by is_configured
            raise RuntimeError("Item analysis API is not configured")
        if self.api_style == "openai_chat" and not self.api_url.lower().endswith("/chat/completions"):
            return f"{self.api_url}/chat/completions"
        if self.api_style == "openai_responses" and not self.api_url.lower().endswith("/responses"):
            return f"{self.api_url}/responses"
        return self.api_url

    def _build_payload(
        self,
        request: ItemAnalysisRequest,
        *,
        task: str = ITEM_ANALYSIS_TASK,
        system_prompt: str = ITEM_ANALYSIS_SYSTEM_PROMPT,
        response_schema: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        if self.api_style == "openai_chat":
            return build_openai_chat_payload(
                request,
                model=self.model,
                task=task,
                system_prompt=system_prompt,
                response_schema=response_schema,
            )
        if self.api_style == "openai_responses":
            return build_openai_responses_payload(
                request,
                model=self.model,
                task=task,
                system_prompt=system_prompt,
                response_schema=response_schema,
            )
        return build_generic_json_payload(
            request,
            model=self.model,
            task=task,
            system_prompt=system_prompt,
            response_schema=response_schema,
        )

__all__ = [
    "COMMUNITY_SOCIAL",
    "CONTENT_CLASSES",
    "ContentClass",
    "ITEM_ANALYSIS_RESPONSE_SCHEMA",
    "ITEM_ANALYSIS_SYSTEM_PROMPT",
    "ITEM_ANALYSIS_TASK",
    "PROJECT_SUMMARY_SYSTEM_PROMPT",
    "PROJECT_SUMMARY_TASK",
    "PROJECT_SUMMARY_RESPONSE_SCHEMA",
    "TRIAGE_RESPONSE_SCHEMA",
    "CLUSTER_RESPONSE_SCHEMA",
    "EVENT_EDITORIAL_RESPONSE_SCHEMA",
    "TRIAGE_SYSTEM_PROMPT",
    "CLUSTER_SYSTEM_PROMPT",
    "COMPOSE_SYSTEM_PROMPT",
    "TRIAGE_TASK",
    "CLUSTER_TASK",
    "COMPOSE_TASK",
    "ItemAnalysisClient",
    "ItemAnalysisRequest",
    "ItemAnalysisResponse",
    "OFFICIAL_MODEL_COMPANY",
    "PROJECT_TOOL",
    "SupportsPost",
    "parse_item_analysis_response",
    "parse_project_summary_response",
    "parse_triage_response",
    "parse_cluster_decision_response",
    "parse_event_editorial_response",
    "StageCallResult",
]


def _normalize_api_style(value: Any) -> str:
    """Normalize provider style aliases while keeping one internal spelling."""

    style = str(value or "generic_json").strip().lower().replace("-", "_")
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


def _extract_evidence_ids(evidence: Any) -> set[str]:
    """Extract IDs from common evidence list/mapping shapes without guessing."""

    values: Any = evidence
    if isinstance(evidence, dict):
        if evidence.get("id") is not None or evidence.get("evidence_id") is not None:
            values = [evidence]
        else:
            values = evidence.get("evidence") or evidence.get("items") or evidence.get("records") or []
    if not isinstance(values, (list, tuple, set)):
        return set()
    result: set[str] = set()
    for value in values:
        if isinstance(value, dict):
            evidence_id = value.get("id") or value.get("evidence_id")
        else:
            evidence_id = getattr(value, "id", None) or getattr(value, "evidence_id", None)
        if isinstance(evidence_id, (str, int)) and str(evidence_id).strip():
            result.add(str(evidence_id).strip())
    return result
