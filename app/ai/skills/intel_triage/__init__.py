"""Structured AI Intel Triage skill contract.

This module is the public, transport-neutral boundary used by later pipeline
waves.  It accepts one normalized raw item, performs one provider call when
requested, validates the response, and applies deterministic safety guards.
"""

from .guards import (
    apply_deterministic_guards,
    guard_paper_support,
    guard_triage_result,
    infer_topic,
)
from .models import (
    CONTENT_CLASS_TO_DEFAULT_TOPIC,
    INTEL_TOPIC_LABELS,
    INTEL_TOPICS,
    NOVELTY_STATUSES,
    PAPER_SUPPORT_LEVELS,
    SEVEN_TOPIC_TAXONOMY,
    TOPIC_INDUSTRY,
    TOPIC_MODEL,
    TOPIC_OPINION,
    TOPIC_PAPER,
    TOPIC_PRODUCT,
    TOPIC_PROJECT,
    TOPIC_TUTORIAL,
    TriageResult,
    TriageScores,
    PaperSupport,
    RawIntelEnvelope,
    normalize_content_class,
    normalize_topic,
)
from .normalize import (
    html_to_text,
    normalize_html,
    normalize_text,
    normalize_url,
)
from .parser import (
    parse_triage_response,
    parse_triage_result,
    strict_parse_triage,
    unwrap_provider_response,
)
from .prompts import (
    INTEL_TRIAGE_RESPONSE_SCHEMA,
    INTEL_TRIAGE_JSON_SCHEMA,
    INTEL_TRIAGE_SYSTEM_PROMPT,
    INTEL_TRIAGE_TASK,
    build_generic_triage_payload,
    build_openai_chat_triage_payload,
    build_openai_responses_triage_payload,
    build_provider_payload,
    build_triage_payload,
)
from .client import (
    AI_FAILURE_STATUS,
    IntelTriageClient,
    TriageClient,
    isolate_ai_failures,
    isolate_ai_failure,
    run_triage_batch,
    run_triage_isolated,
    safe_triage,
    triage_item,
    triage_items,
)

# Friendly aliases used by callers that prefer a descriptive name.
TOPIC_TAXONOMY = SEVEN_TOPIC_TAXONOMY
TRIAGE_TOPICS = INTEL_TOPICS
PAPER_SUPPORT_FIELDS = PaperSupport
RawIntelItem = RawIntelEnvelope

__all__ = [
    "AI_FAILURE_STATUS",
    "CONTENT_CLASS_TO_DEFAULT_TOPIC",
    "INTEL_TOPIC_LABELS",
    "INTEL_TOPICS",
    "INTEL_TRIAGE_JSON_SCHEMA",
    "INTEL_TRIAGE_RESPONSE_SCHEMA",
    "INTEL_TRIAGE_SYSTEM_PROMPT",
    "INTEL_TRIAGE_TASK",
    "NOVELTY_STATUSES",
    "PAPER_SUPPORT_FIELDS",
    "PAPER_SUPPORT_LEVELS",
    "PaperSupport",
    "RawIntelEnvelope",
    "RawIntelItem",
    "SEVEN_TOPIC_TAXONOMY",
    "TOPIC_INDUSTRY",
    "TOPIC_MODEL",
    "TOPIC_OPINION",
    "TOPIC_PAPER",
    "TOPIC_PRODUCT",
    "TOPIC_PROJECT",
    "TOPIC_TAXONOMY",
    "TOPIC_TUTORIAL",
    "TRIAGE_TOPICS",
    "TriageClient",
    "TriageResult",
    "TriageScores",
    "IntelTriageClient",
    "apply_deterministic_guards",
    "build_generic_triage_payload",
    "build_openai_chat_triage_payload",
    "build_openai_responses_triage_payload",
    "build_provider_payload",
    "build_triage_payload",
    "guard_paper_support",
    "guard_triage_result",
    "html_to_text",
    "infer_topic",
    "isolate_ai_failures",
    "isolate_ai_failure",
    "run_triage_isolated",
    "normalize_html",
    "normalize_text",
    "normalize_url",
    "normalize_content_class",
    "normalize_topic",
    "parse_triage_response",
    "parse_triage_result",
    "run_triage_batch",
    "safe_triage",
    "strict_parse_triage",
    "triage_item",
    "triage_items",
    "unwrap_provider_response",
]
