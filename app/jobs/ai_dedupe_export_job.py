from __future__ import annotations

import argparse
import json
import re
import unicodedata
from dataclasses import dataclass
from datetime import datetime
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any, Iterable, Protocol
from urllib.parse import urlsplit, urlunsplit

import httpx

from app.config.settings import Settings


MERGE_DECISIONS = {"exact_duplicate", "same_entity_same_event"}
GROUP_DECISIONS = {"same_entity_different_event", "same_family", "related_topic"}
VALID_DECISIONS = MERGE_DECISIONS | GROUP_DECISIONS | {"unrelated", "conflict_or_uncertain"}
VALID_POLICIES = {
    "keep_highest_score_add_other_links",
    "group_only_keep_separate_events",
    "keep_all",
    "keep_all_with_warning",
}
FAMILY_PATTERNS: tuple[tuple[str, str], ...] = (
    (r"\bqwen\b|qwen\d", "qwen"),
    (r"\bmimo\b", "mimo"),
    (r"\bgemma\b", "gemma"),
    (r"\bllama[.\-_ ]?cpp\b|\bllamacpp\b", "llama.cpp"),
    (r"\bclaude[ \-_]?code\b", "claude code"),
    (r"\bmcp\b|model context protocol", "mcp"),
)
LEVEL_RANK = {"S": 5, "A": 4, "B": 3, "C": 2, "D": 1}


class DedupeClient(Protocol):
    @property
    def is_configured(self) -> bool: ...

    def decide(self, records: list[dict[str, Any]]) -> "AIDedupeDecision": ...


@dataclass(frozen=True)
class AIDedupeDecision:
    group_label: str
    decision: str
    canonical_candidate_id: int
    members: list[int]
    merge_policy: str
    reason: str
    confidence: float


@dataclass(frozen=True)
class DedupeExportResult:
    input_count: int
    output_count: int
    audit_count: int
    cleaned_markdown_path: Path
    cleaned_jsonl_path: Path
    audit_jsonl_path: Path


class AIDedupeClient:
    def __init__(
        self,
        *,
        api_url: str | None,
        api_key: str | None,
        model: str | None = None,
        api_style: str = "generic_json",
        timeout_seconds: float = 60.0,
        http_client: Any | None = None,
    ) -> None:
        self.api_url = api_url.rstrip("/") if api_url else None
        self.api_key = api_key
        self.model = model
        self.api_style = api_style
        self.timeout_seconds = timeout_seconds
        self._http_client = http_client

    @classmethod
    def from_settings(cls, settings: Settings, http_client: Any | None = None) -> "AIDedupeClient":
        return cls(
            api_url=settings.ai_verify_api_url or settings.ai_review_api_url,
            api_key=settings.ai_verify_api_key or settings.ai_review_api_key,
            model=settings.ai_verify_model or settings.ai_review_model,
            api_style=settings.ai_verify_api_style or settings.ai_review_api_style,
            timeout_seconds=settings.ai_verify_timeout_seconds or settings.ai_review_timeout_seconds,
            http_client=http_client,
        )

    @property
    def is_configured(self) -> bool:
        return bool(self.api_url and self.api_key)

    def decide(self, records: list[dict[str, Any]]) -> AIDedupeDecision:
        if not self.is_configured:
            raise RuntimeError("AI dedupe API is not configured")
        payload = self._build_payload(records)
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        url = self._endpoint_url()
        if self._http_client is not None:
            response = self._http_client.post(url, headers=headers, json=payload)
        else:
            with httpx.Client(timeout=self.timeout_seconds, follow_redirects=True) as client:
                response = client.post(url, headers=headers, json=payload)
        response.raise_for_status()
        return _parse_ai_decision(response.json(), fallback_members=[_candidate_id(item) for item in records])

    def _endpoint_url(self) -> str:
        assert self.api_url is not None
        if self.api_style == "openai_chat" and not self.api_url.endswith("/chat/completions"):
            return f"{self.api_url}/chat/completions"
        return self.api_url

    def _build_payload(self, records: list[dict[str, Any]]) -> dict[str, Any]:
        candidates = [_candidate_for_ai(record) for record in records]
        schema = {
            "group_label": "string",
            "decision": "exact_duplicate | same_entity_same_event | same_entity_different_event | same_family | related_topic | unrelated | conflict_or_uncertain",
            "canonical_candidate_id": "integer",
            "members": "array<integer>",
            "merge_policy": "keep_highest_score_add_other_links | group_only_keep_separate_events | keep_all | keep_all_with_warning",
            "reason": "string",
            "confidence": "number 0.0-1.0",
        }
        if self.api_style == "openai_chat":
            return {
                "model": self.model,
                "messages": [
                    {
                        "role": "system",
                        "content": (
                            "你是推荐日报去重判定器。只返回 JSON，不要 Markdown。"
                            "你只判断候选组是否应合并，不要重写正文。"
                            "必须返回字段：group_label, decision, canonical_candidate_id, members, merge_policy, reason, confidence。"
                            "decision 只能是 exact_duplicate, same_entity_same_event, same_entity_different_event, "
                            "same_family, related_topic, unrelated, conflict_or_uncertain。"
                            "Qwen3.6-27B 和 Qwen3.7-Max 不能合并，只能 same_family。"
                            "Claude Code sandboxing 和 Claude Code auto mode 不能合并，只能 same_entity_different_event。"
                            "llama.cpp 的不同版本更新不能直接删除，只能 same_entity_different_event 或 same_family 分组展示。"
                            "final_keep=true 和 final_keep=false 混合时，除非确为同一事件且证据很强，否则不要合并。"
                        ),
                    },
                    {
                        "role": "user",
                        "content": json.dumps(
                            {"candidates": candidates, "response_schema": schema},
                            ensure_ascii=False,
                            default=str,
                        ),
                    },
                ],
                "response_format": {"type": "json_object"},
                "temperature": 0.0,
            }
        return {
            "model": self.model,
            "task": "ai_tool_intel_recommendation_dedupe",
            "candidates": candidates,
            "response_schema": schema,
        }


def run_ai_dedupe_export_job(
    *,
    input_path: str | Path,
    output_dir: str | Path = "output",
    ai_client: DedupeClient | None = None,
    no_ai: bool = False,
    dry_run: bool = False,
    min_ai_confidence: float = 0.75,
    max_block_size: int = 8,
) -> DedupeExportResult:
    input_path = Path(input_path)
    output_path = Path(output_dir)
    output_path.mkdir(parents=True, exist_ok=True)
    records = _read_jsonl(input_path)
    for record in records:
        _ensure_clean_fields(record)

    audit_rows: list[dict[str, Any]] = []
    current_records = list(records)

    strong_blocks = _build_strong_blocks(current_records, max_block_size=max_block_size)
    current_records = _apply_blocks(
        current_records,
        strong_blocks,
        audit_rows=audit_rows,
        ai_client=ai_client,
        no_ai=no_ai,
        dry_run=dry_run,
        min_ai_confidence=min_ai_confidence,
        phase="strong",
    )

    family_blocks = _build_family_blocks(current_records, max_block_size=max_block_size)
    current_records = _apply_blocks(
        current_records,
        family_blocks,
        audit_rows=audit_rows,
        ai_client=ai_client,
        no_ai=no_ai,
        dry_run=dry_run,
        min_ai_confidence=min_ai_confidence,
        phase="family",
    )

    current_records.sort(key=_sort_key)
    suffix = _output_suffix(input_path)
    cleaned_jsonl_path = output_path / f"recommendations_cleaned_{suffix}.jsonl"
    cleaned_markdown_path = output_path / f"recommendations_cleaned_{suffix}.md"
    audit_jsonl_path = output_path / f"dedupe_audit_{suffix}.jsonl"
    _write_jsonl(cleaned_jsonl_path, current_records)
    _write_jsonl(audit_jsonl_path, audit_rows)
    _write_markdown(cleaned_markdown_path, current_records)
    return DedupeExportResult(
        input_count=len(records),
        output_count=len(current_records),
        audit_count=len(audit_rows),
        cleaned_markdown_path=cleaned_markdown_path,
        cleaned_jsonl_path=cleaned_jsonl_path,
        audit_jsonl_path=audit_jsonl_path,
    )


def _apply_blocks(
    records: list[dict[str, Any]],
    blocks: list[dict[str, Any]],
    *,
    audit_rows: list[dict[str, Any]],
    ai_client: DedupeClient | None,
    no_ai: bool,
    dry_run: bool,
    min_ai_confidence: float,
    phase: str,
) -> list[dict[str, Any]]:
    by_id = {_candidate_id(record): record for record in records}
    removed: set[int] = set()
    for block_index, block in enumerate(blocks, start=1):
        ids = [candidate_id for candidate_id in block["candidate_ids"] if candidate_id in by_id and candidate_id not in removed]
        if len(ids) < 2:
            continue
        members = [by_id[candidate_id] for candidate_id in ids]
        decision, error = _decide_block(members, ai_client=ai_client, no_ai=no_ai, phase=phase, reasons=block["reasons"])
        target_ids = [candidate_id for candidate_id in _unique(decision.members) if candidate_id in by_id and candidate_id not in removed]
        target_members = [by_id[candidate_id] for candidate_id in target_ids]
        applied = False
        action = "keep_all"
        if _should_merge(target_members, decision, min_ai_confidence=min_ai_confidence) and not dry_run:
            merged = _merge_records(target_members, decision)
            primary_id = _candidate_id(merged)
            by_id[primary_id] = merged
            for candidate_id in target_ids:
                if candidate_id != primary_id:
                    removed.add(candidate_id)
            applied = True
            action = "merged"
        elif decision.decision in GROUP_DECISIONS or dry_run:
            for record in target_members:
                _annotate_group_record(record, decision)
            action = "dry_run" if dry_run and decision.decision in MERGE_DECISIONS else "grouped"
        elif decision.decision == "conflict_or_uncertain":
            for record in target_members:
                _annotate_group_record(record, decision)
            action = "manual_review"

        audit_rows.append(
            {
                "block_id": f"{phase}-{block_index}",
                "phase": phase,
                "candidate_ids": ids,
                "titles": [str(item.get("title") or "") for item in members],
                "blocking_reasons": block["reasons"],
                "group_label": decision.group_label,
                "decision": decision.decision,
                "canonical_candidate_id": decision.canonical_candidate_id,
                "members": decision.members,
                "merge_policy": decision.merge_policy,
                "reason": decision.reason,
                "confidence": decision.confidence,
                "applied": applied,
                "action": action,
                "error": error,
            }
        )
    return [record for record in by_id.values() if _candidate_id(record) not in removed]


def _decide_block(
    members: list[dict[str, Any]],
    *,
    ai_client: DedupeClient | None,
    no_ai: bool,
    phase: str,
    reasons: list[str],
) -> tuple[AIDedupeDecision, str | None]:
    if no_ai:
        return _apply_guardrails(_heuristic_decision(members, phase=phase, reasons=reasons), members), None
    if ai_client is None:
        ai_client = AIDedupeClient.from_settings(Settings.from_env())
    try:
        decision = _sanitize_decision(ai_client.decide(members), fallback_members=[_candidate_id(item) for item in members])
        by_id = {_candidate_id(item): item for item in members}
        decision_members = [by_id[candidate_id] for candidate_id in decision.members if candidate_id in by_id]
        return _apply_guardrails(decision, decision_members or members), None
    except Exception as exc:
        return (
            AIDedupeDecision(
                group_label=_default_group_label(members),
                decision="conflict_or_uncertain",
                canonical_candidate_id=_candidate_id(_select_primary(members)),
                members=[_candidate_id(item) for item in members],
                merge_policy="keep_all_with_warning",
                reason="AI dedupe failed; conservatively kept all records.",
                confidence=0.0,
            ),
            str(exc),
        )


def _apply_guardrails(decision: AIDedupeDecision, members: list[dict[str, Any]]) -> AIDedupeDecision:
    """Correct unsafe merge decisions for known high-risk families/events."""
    aliases = {_entity_key(record) for record in members}
    family = _shared_family(members)

    if {"qwen3.6-27b", "qwen3.7-max"}.issubset(aliases) and decision.decision in MERGE_DECISIONS:
        return _replace_decision(
            decision,
            new_decision="same_family",
            merge_policy="group_only_keep_separate_events",
            group_label="qwen",
            reason=f"{decision.reason} Guardrail: Qwen3.6-27B and Qwen3.7-Max are distinct model releases.",
        )

    claude_events = {_claude_code_event(record) for record in members}
    claude_events.discard(None)
    if len(claude_events) >= 2 and {"sandboxing", "auto_mode"}.issubset(claude_events):
        return _replace_decision(
            decision,
            new_decision="same_entity_different_event",
            merge_policy="group_only_keep_separate_events",
            group_label="claude code",
            reason=f"{decision.reason} Guardrail: Claude Code sandboxing and auto mode are separate events.",
        )

    if family == "llama.cpp" and decision.decision in MERGE_DECISIONS and not _is_safe_exact_same_event(members):
        return _replace_decision(
            decision,
            new_decision="same_entity_different_event",
            merge_policy="group_only_keep_separate_events",
            group_label="llama.cpp",
            reason=f"{decision.reason} Guardrail: llama.cpp items with different update titles are grouped, not deleted.",
        )

    return decision


def _replace_decision(
    original: AIDedupeDecision,
    *,
    new_decision: str,
    merge_policy: str,
    group_label: str,
    reason: str,
) -> AIDedupeDecision:
    return AIDedupeDecision(
        group_label=group_label,
        decision=new_decision,
        canonical_candidate_id=original.canonical_candidate_id,
        members=original.members,
        merge_policy=merge_policy,
        reason=reason,
        confidence=original.confidence,
    )


def _heuristic_decision(members: list[dict[str, Any]], *, phase: str, reasons: list[str]) -> AIDedupeDecision:
    primary = _select_primary(members)
    if len({bool(item.get("final_keep")) for item in members}) > 1:
        decision = "conflict_or_uncertain"
        policy = "keep_all_with_warning"
        confidence = 0.0
    elif phase == "family":
        decision = "same_family"
        policy = "group_only_keep_separate_events"
        confidence = 0.65
    elif any(reason.startswith(("source_url:", "link:")) for reason in reasons):
        decision = "exact_duplicate"
        policy = "keep_highest_score_add_other_links"
        confidence = 0.9
    else:
        decision = "same_entity_same_event"
        policy = "keep_highest_score_add_other_links"
        confidence = 0.8
    return AIDedupeDecision(
        group_label=_default_group_label(members),
        decision=decision,
        canonical_candidate_id=_candidate_id(primary),
        members=[_candidate_id(item) for item in members],
        merge_policy=policy,
        reason=f"Rule-only dedupe decision from blocking reasons: {', '.join(reasons)}",
        confidence=confidence,
    )


def _should_merge(
    members: list[dict[str, Any]],
    decision: AIDedupeDecision,
    *,
    min_ai_confidence: float,
) -> bool:
    if decision.decision not in MERGE_DECISIONS:
        return False
    if decision.confidence < min_ai_confidence:
        return False
    keep_values = {bool(item.get("final_keep")) for item in members}
    if len(keep_values) > 1 and decision.confidence < min_ai_confidence:
        return False
    return True


def _merge_records(members: list[dict[str, Any]], decision: AIDedupeDecision) -> dict[str, Any]:
    primary = dict(_select_primary(members))
    member_ids = sorted({_candidate_id(item) for item in members})
    primary["merged_candidate_ids"] = member_ids
    primary["merged_titles"] = _unique(str(item.get("title") or "") for item in members)
    primary["source_links"] = _unique(link for item in members for link in _source_links(item))
    primary["evidence_domains"] = _unique(domain for item in members for domain in _list_value(item.get("evidence_domains")))
    primary["risk_flags"] = _unique(flag for item in members for flag in _list_value(item.get("risk_flags")))
    primary["dedupe_decision"] = decision.decision
    primary["dedupe_reason"] = decision.reason
    primary["dedupe_confidence"] = decision.confidence
    primary["dedupe_group_label"] = decision.group_label
    primary["dedupe_member_ids"] = member_ids
    primary["supporting_sources"] = [
        {
            "candidate_id": _candidate_id(item),
            "title": item.get("title"),
            "url": item.get("url"),
            "links": item.get("links") or {},
            "final_keep": item.get("final_keep"),
            "recommendation_level": item.get("recommendation_level"),
            "final_score": item.get("final_score"),
            "credibility_score": item.get("credibility_score"),
        }
        for item in members
        if _candidate_id(item) != _candidate_id(primary)
    ]
    return primary


def _annotate_group_record(record: dict[str, Any], decision: AIDedupeDecision) -> None:
    _ensure_clean_fields(record)
    if _decision_rank(str(record.get("dedupe_decision") or "unrelated")) > _decision_rank(decision.decision):
        return
    record["dedupe_decision"] = decision.decision
    record["dedupe_reason"] = decision.reason
    record["dedupe_confidence"] = decision.confidence
    record["dedupe_group_label"] = decision.group_label
    record["dedupe_member_ids"] = sorted(set(decision.members))


def _decision_rank(decision: str) -> int:
    return {
        "exact_duplicate": 5,
        "same_entity_same_event": 5,
        "same_entity_different_event": 3,
        "same_family": 2,
        "related_topic": 1,
        "conflict_or_uncertain": 1,
        "unrelated": 0,
    }.get(decision, 0)


def _ensure_clean_fields(record: dict[str, Any]) -> None:
    record.setdefault("merged_candidate_ids", [_candidate_id(record)])
    record.setdefault("merged_titles", [str(record.get("title") or "")])
    record.setdefault("source_links", _source_links(record))
    record.setdefault("evidence_domains", _list_value(record.get("evidence_domains")))
    record.setdefault("risk_flags", _list_value(record.get("risk_flags")))
    record.setdefault("dedupe_decision", "unrelated")
    record.setdefault("dedupe_reason", "")
    record.setdefault("dedupe_confidence", 0.0)
    record.setdefault("dedupe_group_label", _entity_label(record))
    record.setdefault("dedupe_member_ids", [_candidate_id(record)])
    record.setdefault("supporting_sources", [])


def _build_strong_blocks(records: list[dict[str, Any]], *, max_block_size: int) -> list[dict[str, Any]]:
    buckets: dict[str, set[int]] = {}
    reasons: dict[str, set[str]] = {}
    for record in records:
        candidate_id = _candidate_id(record)
        for key in _strong_keys(record):
            buckets.setdefault(key, set()).add(candidate_id)
            reasons.setdefault(key, set()).add(key)
    for left_index, left in enumerate(records):
        for right in records[left_index + 1 :]:
            if _title_similarity(left, right) >= 0.86:
                key = f"title:{min(_candidate_id(left), _candidate_id(right))}:{max(_candidate_id(left), _candidate_id(right))}"
                buckets.setdefault(key, set()).update({_candidate_id(left), _candidate_id(right)})
                reasons.setdefault(key, set()).add("title_similarity")
    return _blocks_from_buckets(buckets, reasons, max_block_size=max_block_size)


def _build_family_blocks(records: list[dict[str, Any]], *, max_block_size: int) -> list[dict[str, Any]]:
    buckets: dict[str, set[int]] = {}
    reasons: dict[str, set[str]] = {}
    for record in records:
        family = _family_key(record)
        if not family:
            continue
        key = f"family:{family}"
        buckets.setdefault(key, set()).add(_candidate_id(record))
        reasons.setdefault(key, set()).add(key)
    return _blocks_from_buckets(buckets, reasons, max_block_size=max_block_size)


def _blocks_from_buckets(
    buckets: dict[str, set[int]],
    reasons: dict[str, set[str]],
    *,
    max_block_size: int,
) -> list[dict[str, Any]]:
    raw_blocks = [(key, ids) for key, ids in buckets.items() if len(ids) >= 2]
    raw_blocks.sort(key=lambda item: (len(item[1]), item[0]))
    blocks: list[dict[str, Any]] = []
    seen: set[tuple[int, ...]] = set()
    for key, ids in raw_blocks:
        ordered = sorted(ids)
        chunks = [ordered[index : index + max_block_size] for index in range(0, len(ordered), max_block_size)]
        for chunk in chunks:
            if len(chunk) < 2:
                continue
            frozen = tuple(chunk)
            if frozen in seen:
                continue
            seen.add(frozen)
            blocks.append({"candidate_ids": chunk, "reasons": sorted(reasons.get(key, {key}))})
    return blocks


def _strong_keys(record: dict[str, Any]) -> list[str]:
    keys: list[str] = []
    source_url = _normalize_url(record.get("url"))
    if source_url:
        keys.append(f"source_url:{source_url}")
    links = record.get("links") if isinstance(record.get("links"), dict) else {}
    for name in ("official", "github", "huggingface", "producthunt"):
        url = _normalize_url(links.get(name))
        if url:
            keys.append(f"link:{name}:{url}")
    entity_key = _entity_key(record)
    if entity_key:
        keys.append(f"entity:{entity_key}")
    return keys


def _entity_key(record: dict[str, Any]) -> str:
    text = str(record.get("entity_name") or record.get("title") or "")
    alias = _known_entity_alias(text)
    if alias:
        return alias
    return _normalize_text(text)


def _family_key(record: dict[str, Any]) -> str | None:
    text = f"{record.get('entity_name') or ''} {record.get('title') or ''}"
    normalized = _normalize_text(text)
    for pattern, family in FAMILY_PATTERNS:
        if re.search(pattern, normalized, flags=re.IGNORECASE):
            return family
    return None


def _shared_family(records: list[dict[str, Any]]) -> str | None:
    families = {_family_key(record) for record in records}
    families.discard(None)
    if len(families) == 1:
        return next(iter(families))
    return None


def _claude_code_event(record: dict[str, Any]) -> str | None:
    text = _normalize_text(f"{record.get('entity_name') or ''} {record.get('title') or ''} {record.get('summary_cn') or ''}")
    if "claude code" not in text:
        return None
    if re.search(r"\bsandbox(ing|ed)?\b|permission prompts?|secure|security", text):
        return "sandboxing"
    if re.search(r"\bauto[- ]?mode\b|autonomous|skip permissions?|auto accept", text):
        return "auto_mode"
    return None


def _is_safe_exact_same_event(records: list[dict[str, Any]]) -> bool:
    source_urls = {_normalize_url(record.get("url")) for record in records}
    source_urls.discard(None)
    if len(source_urls) == 1:
        return True
    titles = {_normalize_text(str(record.get("title") or "")) for record in records}
    titles.discard("")
    return len(titles) == 1


def _known_entity_alias(text: str) -> str | None:
    normalized = _normalize_text(text)
    if re.search(r"qwen\s*3[.\- ]*6\s*[- ]*27\s*b|qwen3[.\- ]*6[- ]*27b", normalized):
        return "qwen3.6-27b"
    if re.search(r"qwen\s*3[.\- ]*7\s*[- ]*max|qwen3[.\- ]*7[- ]*max", normalized):
        return "qwen3.7-max"
    if re.search(r"mimo\s*[- ]*v?\s*2[.\- ]*5\s*[- ]*coder|mimo-v2[.\- ]*5-coder", normalized):
        return "mimo-v2.5-coder"
    if re.search(r"gemma\s*[- ]*4|gemma4", normalized):
        return "gemma-4"
    if re.search(r"llama[.\-_ ]?cpp|llamacpp", normalized):
        return "llama.cpp"
    return None


def _title_similarity(left: dict[str, Any], right: dict[str, Any]) -> float:
    a = _normalize_text(str(left.get("title") or ""))
    b = _normalize_text(str(right.get("title") or ""))
    if not a or not b:
        return 0.0
    return SequenceMatcher(None, a, b).ratio()


def _select_primary(records: list[dict[str, Any]]) -> dict[str, Any]:
    return sorted(records, key=_quality_key, reverse=True)[0]


def _quality_key(record: dict[str, Any]) -> tuple:
    return (
        1 if record.get("final_keep") else 0,
        LEVEL_RANK.get(str(record.get("recommendation_level") or "D").upper(), 0),
        _int_value(record.get("rerank_score")),
        _int_value(record.get("final_score")),
        _int_value(record.get("credibility_score")),
        -_int_value(record.get("spam_risk_score")),
        -_candidate_id(record),
    )


def _sort_key(record: dict[str, Any]) -> tuple:
    key = _quality_key(record)
    return tuple(-value if isinstance(value, int) else value for value in key)


def _candidate_for_ai(record: dict[str, Any]) -> dict[str, Any]:
    return {
        "candidate_id": _candidate_id(record),
        "title": record.get("title"),
        "entity_name": record.get("entity_name"),
        "entity_type": record.get("entity_type"),
        "url": record.get("url"),
        "links": record.get("links") or {},
        "final_keep": record.get("final_keep"),
        "recommendation_level": record.get("recommendation_level"),
        "rerank_score": record.get("rerank_score"),
        "final_score": record.get("final_score"),
        "credibility_score": record.get("credibility_score"),
        "spam_risk_score": record.get("spam_risk_score"),
        "category": record.get("category"),
        "summary_cn": record.get("summary_cn"),
        "risk_flags": _list_value(record.get("risk_flags")),
        "evidence_domains": _list_value(record.get("evidence_domains")),
    }


def _parse_ai_decision(data: dict[str, Any], *, fallback_members: list[int]) -> AIDedupeDecision:
    result = _unwrap_response(data)
    return _sanitize_decision(_decision_from_mapping(result, fallback_members=fallback_members), fallback_members=fallback_members)


def _decision_from_mapping(data: dict[str, Any], *, fallback_members: list[int]) -> AIDedupeDecision:
    members = _int_list(data.get("members")) or fallback_members
    canonical = _int_value(data.get("canonical_candidate_id")) or members[0]
    confidence = _float_value(data.get("confidence"))
    return AIDedupeDecision(
        group_label=str(data.get("group_label") or "duplicate candidates"),
        decision=str(data.get("decision") or "conflict_or_uncertain"),
        canonical_candidate_id=canonical,
        members=members,
        merge_policy=str(data.get("merge_policy") or "keep_all_with_warning"),
        reason=str(data.get("reason") or ""),
        confidence=max(0.0, min(confidence, 1.0)),
    )


def _sanitize_decision(decision: AIDedupeDecision, *, fallback_members: list[int]) -> AIDedupeDecision:
    normalized_decision = decision.decision if decision.decision in VALID_DECISIONS else "conflict_or_uncertain"
    normalized_policy = decision.merge_policy if decision.merge_policy in VALID_POLICIES else "keep_all_with_warning"
    members = [item for item in decision.members if item in set(fallback_members)] or fallback_members
    canonical = decision.canonical_candidate_id if decision.canonical_candidate_id in set(members) else members[0]
    return AIDedupeDecision(
        group_label=decision.group_label or "duplicate candidates",
        decision=normalized_decision,
        canonical_candidate_id=canonical,
        members=members,
        merge_policy=normalized_policy,
        reason=decision.reason,
        confidence=max(0.0, min(float(decision.confidence), 1.0)),
    )


def _unwrap_response(data: dict[str, Any]) -> dict[str, Any]:
    if isinstance(data.get("result"), dict):
        return data["result"]
    choices = data.get("choices")
    if isinstance(choices, list) and choices:
        message = choices[0].get("message") if isinstance(choices[0], dict) else None
        content = message.get("content") if isinstance(message, dict) else None
        if isinstance(content, str):
            parsed = json.loads(_strip_json_fence(content))
            if isinstance(parsed, dict):
                return parsed
    return data


def _strip_json_fence(value: str) -> str:
    text = value.strip()
    if text.startswith("```"):
        lines = text.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].startswith("```"):
            lines = lines[:-1]
        return "\n".join(lines).strip()
    return text


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as file:
        for line in file:
            if line.strip():
                records.append(json.loads(line))
    return records


def _write_jsonl(path: Path, records: Iterable[dict[str, Any]]) -> None:
    with path.open("w", encoding="utf-8") as file:
        for record in records:
            file.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")


def _write_markdown(path: Path, records: list[dict[str, Any]]) -> None:
    lines = [
        f"# AI 工具情报去重日报 - {datetime.now().date().isoformat()}",
        "",
        f"- 导出时间：{datetime.now().isoformat(timespec='seconds')}",
        f"- 去重后条目数：{len(records)}",
        "",
    ]
    sections = [
        ("今日强推荐", lambda row: row.get("final_keep") and row.get("recommendation_level") in {"S", "A"}),
        ("值得关注", lambda row: row.get("final_keep") and row.get("recommendation_level") == "B"),
        ("仅归档", lambda row: not row.get("final_keep") and row.get("recommendation_level") in {"B", "C"}),
        ("被剔除的高风险内容", lambda row: not row.get("final_keep") and row.get("recommendation_level") == "D"),
    ]
    used: set[int] = set()
    for title, predicate in sections:
        section_rows = [row for row in records if predicate(row) and _candidate_id(row) not in used]
        lines.extend([f"## {title}", ""])
        if not section_rows:
            lines.extend(["无。", ""])
            continue
        for index, row in enumerate(section_rows, start=1):
            used.add(_candidate_id(row))
            lines.extend(_markdown_record(index, row))
    remainder = [row for row in records if _candidate_id(row) not in used]
    if remainder:
        lines.extend(["## 其他", ""])
        for index, row in enumerate(remainder, start=1):
            lines.extend(_markdown_record(index, row))
    path.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")


def _markdown_record(index: int, row: dict[str, Any]) -> list[str]:
    source_links = _list_value(row.get("source_links"))
    supporting_sources = row.get("supporting_sources") if isinstance(row.get("supporting_sources"), list) else []
    lines = [
        f"### {index}. {row.get('entity_name') or row.get('title') or 'Untitled'}",
        "",
        f"- 标题：{row.get('title') or ''}",
        f"- 推荐分：`{row.get('final_score')}` / `{row.get('recommendation_level')}`",
        f"- 可信度：`{row.get('credibility_score')}`；垃圾风险：`{row.get('spam_risk_score')}`",
        f"- 去重决策：`{row.get('dedupe_decision')}`；置信度：`{row.get('dedupe_confidence')}`",
        f"- 去重原因：{row.get('dedupe_reason') or ''}",
        f"- 合并候选：`{row.get('merged_candidate_ids')}`",
        f"- 证据域名：{', '.join(_list_value(row.get('evidence_domains'))) or '无'}",
        f"- 风险标签：{', '.join(_list_value(row.get('risk_flags'))) or '无'}",
        f"- 摘要：{row.get('summary_cn') or ''}",
        f"- 链接：{row.get('url') or ''}",
    ]
    if source_links:
        lines.append("- 来源链接：")
        lines.extend([f"  - {link}" for link in source_links])
    if supporting_sources:
        lines.append("- 补充来源：")
        for source in supporting_sources:
            lines.append(f"  - [{source.get('candidate_id')}] {source.get('title')} — {source.get('url')}")
    lines.append("")
    return lines


def _output_suffix(input_path: Path) -> str:
    stem = input_path.stem
    if stem.startswith("recommendations_"):
        suffix = stem.removeprefix("recommendations_")
    else:
        suffix = datetime.now().strftime("%Y%m%d_%H%M%S")
    return suffix


def _source_links(record: dict[str, Any]) -> list[str]:
    links = [record.get("url")]
    link_map = record.get("links") if isinstance(record.get("links"), dict) else {}
    links.extend(link_map.get(key) for key in ("source", "official", "github", "huggingface", "producthunt"))
    return _unique(str(link) for link in links if link)


def _default_group_label(records: list[dict[str, Any]]) -> str:
    families = [_family_key(record) for record in records]
    for family in families:
        if family:
            return family
    return _entity_label(_select_primary(records))


def _entity_label(record: dict[str, Any]) -> str:
    return str(record.get("entity_name") or record.get("title") or f"candidate-{_candidate_id(record)}")


def _candidate_id(record: dict[str, Any]) -> int:
    return int(record.get("candidate_id"))


def _normalize_text(value: str) -> str:
    text = unicodedata.normalize("NFKC", value).lower()
    text = text.replace("千问", "qwen")
    text = re.sub(r"[\u2010-\u2015_/]+", "-", text)
    text = re.sub(r"[^a-z0-9.+#\-\s]+", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def _normalize_url(value: Any) -> str | None:
    if not value:
        return None
    try:
        parts = urlsplit(str(value).strip())
    except ValueError:
        return None
    if not parts.scheme or not parts.netloc:
        return None
    path = parts.path.rstrip("/")
    return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), path, "", ""))


def _list_value(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, list):
        return [str(item) for item in value if str(item)]
    return [str(value)] if str(value) else []


def _unique(values: Iterable[Any]) -> list:
    result: list[Any] = []
    seen: set[str] = set()
    for value in values:
        if value is None:
            continue
        key = json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
        if key in seen:
            continue
        seen.add(key)
        result.append(value)
    return result


def _int_value(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _float_value(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _int_list(value: Any) -> list[int]:
    if not isinstance(value, list):
        return []
    result: list[int] = []
    for item in value:
        try:
            result.append(int(item))
        except (TypeError, ValueError):
            continue
    return result


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="AI dedupe post-processing for recommendations_*.jsonl")
    parser.add_argument("--input", required=True, dest="input_path", help="Path to recommendations_*.jsonl")
    parser.add_argument("--output-dir", default="output", help="Directory for cleaned Markdown/JSONL and audit JSONL")
    parser.add_argument("--no-ai", action="store_true", help="Use conservative rule-only decisions without calling AI")
    parser.add_argument("--dry-run", action="store_true", help="Write decisions/audit but do not apply merges")
    parser.add_argument("--min-ai-confidence", type=float, default=0.75, help="Minimum AI confidence to apply merges")
    parser.add_argument("--max-block-size", type=int, default=8, help="Maximum candidates per AI dedupe block")
    args = parser.parse_args(argv)
    client = None if args.no_ai else AIDedupeClient.from_settings(Settings.from_env())
    result = run_ai_dedupe_export_job(
        input_path=args.input_path,
        output_dir=args.output_dir,
        ai_client=client,
        no_ai=args.no_ai,
        dry_run=args.dry_run,
        min_ai_confidence=args.min_ai_confidence,
        max_block_size=args.max_block_size,
    )
    print(f"input={result.input_count} output={result.output_count} audit={result.audit_count}")
    print(f"markdown={result.cleaned_markdown_path}")
    print(f"jsonl={result.cleaned_jsonl_path}")
    print(f"audit={result.audit_jsonl_path}")


if __name__ == "__main__":
    main()
