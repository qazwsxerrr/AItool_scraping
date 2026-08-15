"""Durable Stage A (lightweight AI screening) orchestration.

The stage deliberately has no knowledge of Stage B.  It persists one task and
one immutable attempt per item, and a successful screen task is the only input
that the analysis stage is allowed to consume later.
"""

from __future__ import annotations

import hashlib
import json
import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping

import httpx
from pydantic import ValidationError
from sqlalchemy.orm import Session, sessionmaker

from app.ai.skills.intel_triage import (
    RawIntelEnvelope,
    ScreenResult,
    apply_screen_guard,
    preflight_intel_triage_schemas,
    strict_parse_screen,
)
from app.config.limits import DEFAULT_AI_REVIEW_LIMIT, DEFAULT_AI_REVIEW_CONCURRENCY, DEFAULT_AI_SCREEN_REJECT_THRESHOLD
from app.domain.models import SourceSpec
from app.storage.models import IntelItem, IntelRunStageTask
from app.storage.repository import IntelRepository

LOGGER = logging.getLogger(__name__)


@dataclass
class StageAScreenResult:
    run_id: int | None = None
    processed: int = 0
    screened: int = 0
    screened_out: int = 0
    screen_failed: int = 0
    skipped: int = 0
    partial: bool = False
    partial_reason: str | None = None
    item_ids: list[int] = field(default_factory=list)
    eligible_item_ids: list[int] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    @property
    def failed(self) -> int:
        return self.screen_failed

    @property
    def run_counts(self) -> dict[str, Any]:
        return {
            "processed": self.processed,
            "screened": self.screened,
            "screen_count": self.screened,
            "screened_out": self.screened_out,
            "screen_failed": self.screen_failed,
            "partial": self.partial,
            "partial_reason": self.partial_reason,
        }


@dataclass(frozen=True)
class _ItemContext:
    item_id: int
    input_fingerprint: str
    envelope: RawIntelEnvelope | None
    source_spec: SourceSpec | None
    structural_error: str | None = None


def run_stage_a_screen_job(
    *,
    session_factory: sessionmaker[Session],
    source_specs: Mapping[str, SourceSpec] | None = None,
    ai_client: Any | None = None,
    run_id: int | None = None,
    limit: int | None = DEFAULT_AI_REVIEW_LIMIT,
    ai_limit: int | None = None,
    source_filter: str | None = None,
    content_class: str | None = None,
    force: bool = False,
    retry_failed: bool = False,
    retry: bool | None = None,
    include_blocked: bool = False,
    item_ids: Iterable[int] | None = None,
    task_ids: Iterable[int] | None = None,
    screen_reject_threshold: int = DEFAULT_AI_SCREEN_REJECT_THRESHOLD,
    concurrency: int = DEFAULT_AI_REVIEW_CONCURRENCY,
    dry_run: bool = False,
    now: Any | None = None,
    owner: str = "stage-a",
    **_: Any,
) -> StageAScreenResult:
    """Run Stage A with durable per-item state.

    ``retry_failed`` only affects this stage.  In particular, this function
    never creates or invokes a Stage B task/provider call.
    """

    del now  # kept in the public job contract for compatibility
    max_workers = _bounded_concurrency(concurrency)
    if retry is not None:
        retry_failed = bool(retry)
    selected_limit = _normalise_limit(ai_limit if ai_limit is not None else limit)
    reject_threshold = _bounded_score(screen_reject_threshold, DEFAULT_AI_SCREEN_REJECT_THRESHOLD)
    result = StageAScreenResult(run_id=run_id)
    specs = dict(source_specs or {})

    # Local strict-schema validation is intentionally before any provider
    # request.  A malformed nested schema must fail fast for the whole stage.
    preflight_intel_triage_schemas()

    effective_run_id, contexts, explicit_cap = _prepare_scope(
        session_factory,
        run_id=run_id,
        source_specs=specs,
        source_filter=source_filter,
        content_class=content_class,
        limit=selected_limit,
        force=force,
        item_ids=item_ids,
        dry_run=dry_run,
    )
    result.run_id = effective_run_id
    result.partial = explicit_cap
    result.partial_reason = f"ai_limit:{selected_limit}" if explicit_cap else None
    result.item_ids = [context.item_id for context in contexts]
    result.processed = len(contexts)
    if dry_run:
        return result

    if effective_run_id is None:
        return result

    config_fingerprint = _config_fingerprint(
        stage="screen",
        model=getattr(ai_client, "model", None),
        reject_threshold=reject_threshold,
    )
    task_ids_by_item: dict[int, int] = {}
    requested_task_ids = {int(value) for value in task_ids} if task_ids is not None else None
    with session_factory() as session:
        repo = IntelRepository(session)
        existing_stage = repo.get_stage(effective_run_id, "screen")
        stage_force = force and item_ids is None
        stage = repo.ensure_stage(
            effective_run_id,
            "screen",
            config_fingerprint=config_fingerprint,
            # The storage API resets existing tasks for ``force``.  Do not
            # ask it to reset a just-created row before its auto-increment ID
            # has been flushed.
            force=stage_force if existing_stage is not None else False,
            metadata={"reject_threshold": reject_threshold},
        )
        if retry_failed:
            repo.retry_failed(stage, include_blocked=include_blocked)
        for context in contexts:
            existing = None
            if requested_task_ids is not None:
                existing = repo.get_task(stage, subject_type="item", subject_id=context.item_id)
                if existing is None or existing.id not in requested_task_ids:
                    continue
            task = repo.ensure_stage_task(
                stage,
                subject_type="item",
                subject_id=context.item_id,
                item_id=context.item_id,
                input_fingerprint=context.input_fingerprint,
                config_fingerprint=config_fingerprint,
                force=bool(force and ((item_ids is not None) or (requested_task_ids is not None and existing is not None))),
            )
            task_ids_by_item[context.item_id] = int(task.id)
        session.commit()

    # Provider calls are intentionally outside SQLAlchemy sessions.  Claim all
    # work first, run only the provider calls in bounded worker threads, and
    # keep every projection/task commit on this coordinator thread.
    claimed_contexts: list[tuple[_ItemContext, int]] = []
    for context in contexts:
        task_id = task_ids_by_item.get(context.item_id)
        if task_id is None:
            continue
        if not _claim_task(
            session_factory,
            task_id,
            owner=owner,
            input_fingerprint=context.input_fingerprint,
            config_fingerprint=config_fingerprint,
        ):
            result.skipped += 1
            # A reusable successful task is still eligible for Stage B.  Keep
            # it in the scope passed to the facade without making a provider
            # call again.
            if _task_is_eligible(session_factory, task_id):
                result.eligible_item_ids.append(context.item_id)
            continue
        claimed_contexts.append((context, task_id))

    def persist_outcome(
        context: _ItemContext,
        task_id: int,
        screen: ScreenResult,
        failure: BaseException | None,
    ) -> None:
        """Persist one completed outcome on the coordinator thread."""

        if failure is not None or screen.status == "screen_failed":
            retryable, category, code, message = _classify_provider_failure(
                failure,
                code=screen.error_code,
                message=screen.error_message,
            )
            if _persist_screen_failure(
                session_factory,
                task_id,
                item_id=context.item_id,
                run_id=effective_run_id,
                screen=screen,
                owner=owner,
                model=getattr(ai_client, "model", None),
                retryable=retryable,
                error_category=category,
                error_code=code,
                error_message=message,
            ):
                result.screen_failed += 1
                result.errors.append(f"intel_item_id={context.item_id}: {message}")
            else:
                result.errors.append(f"intel_item_id={context.item_id}: screen persistence failed")
            return

        try:
            with session_factory() as session:
                repo = IntelRepository(session)
                repo.save_screen(
                    context.item_id,
                    screen,
                    run_id=effective_run_id,
                    model=getattr(ai_client, "model", None),
                    status="success",
                )
                if screen.decision == "reject" and screen.confidence >= reject_threshold:
                    repo.set_item_status(context.item_id, "screened_out", run_id=effective_run_id)
                    result.screened_out += 1
                else:
                    # Keep the legacy projection in ``new`` until Stage B.
                    repo.set_item_status(context.item_id, "new", run_id=effective_run_id)
                    result.eligible_item_ids.append(context.item_id)
                task = session.get(IntelRunStageTask, task_id)
                if task is None:
                    raise RuntimeError(f"stage A task {task_id} disappeared")
                repo.complete_stage_task(
                    task,
                    owner=owner,
                    result_ref={"projection": "AIItemScreen", "item_id": context.item_id},
                    result=screen.model_dump(mode="json"),
                    raw_response=screen.raw_response,
                    metadata={"decision": screen.decision, "confidence": screen.confidence},
                )
                session.commit()
            result.screened += 1
        except Exception as exc:
            # Persistence failures are item-local and must not abort the batch.
            result.screen_failed += 1
            result.errors.append(f"intel_item_id={context.item_id}: screen persistence failed: {exc}")
            _mark_persistence_failure(session_factory, task_id, owner=owner, message=str(exc))
            LOGGER.exception("Stage A persistence failed for intel item %s", context.item_id)

    for context, task_id in claimed_contexts:
        if context.structural_error is not None:
            persist_outcome(
                context,
                task_id,
                _structural_screen(context.item_id, context.structural_error),
                None,
            )

    provider_contexts = [
        (context, task_id)
        for context, task_id in claimed_contexts
        if context.structural_error is None
    ]
    if provider_contexts:
        with ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="intel-stage-a") as executor:
            futures = {
                executor.submit(
                    _screen_provider_outcome,
                    ai_client,
                    context,
                    reject_threshold,
                ): (context, task_id)
                for context, task_id in provider_contexts
            }
            for future in as_completed(futures):
                context, task_id = futures[future]
                screen, failure = future.result()
                persist_outcome(context, task_id, screen, failure)

    with session_factory() as session:
        repo = IntelRepository(session)
        stage = repo.get_stage(effective_run_id, "screen")
        if stage is not None:
            repo.refresh_stage_status(stage)
            session.commit()
    return result


# Descriptive aliases used by callers that name stages instead of jobs.
run_stage_a = run_stage_a_screen_job
run_screen_stage = run_stage_a_screen_job
run_stage_a_job = run_stage_a_screen_job
run_stage_a_screen = run_stage_a_screen_job
StageAResult = StageAScreenResult


def _prepare_scope(
    session_factory: sessionmaker[Session],
    *,
    run_id: int | None,
    source_specs: Mapping[str, SourceSpec],
    source_filter: str | None,
    content_class: str | None,
    limit: int | None,
    force: bool,
    item_ids: Iterable[int] | None,
    dry_run: bool,
) -> tuple[int | None, list[_ItemContext], bool]:
    """Freeze/resolve the item scope and build detached provider envelopes."""

    explicit_cap = limit is not None
    requested_ids = {int(value) for value in item_ids} if item_ids is not None else None
    with session_factory() as session:
        repo = IntelRepository(session)
        if run_id is None:
            # The compatibility facade has no explicit fetch run.  Create a
            # durable run and attach the currently selected pending items.
            candidates = repo.list_pending_items(
                limit=None,
                source_id=source_filter,
                content_class=content_class,
                force=force,
                stage="screen",
            )
            if limit is not None:
                candidates = candidates[:limit]
            if dry_run:
                return None, [_context_from_item(item, source_specs) for item in candidates], explicit_cap
            run = repo.start_run(run_type="ai_review", source_ids=[item.source_id for item in candidates])
            for item in candidates:
                repo.record_run_item(run.id, item.id, source_id=item.source_id, role="fetched", status="new")
            session.commit()
            run_id = int(run.id)
        else:
            candidates = repo.list_run_items(run_id, role="fetched")
            if not candidates:
                candidates = repo.list_pending_items(
                    limit=None,
                    source_id=source_filter,
                    content_class=content_class,
                    force=force,
                    run_id=run_id,
                    stage="screen",
                )
            if source_filter:
                candidates = [item for item in candidates if item.source_id == source_filter]
            if content_class:
                candidates = [item for item in candidates if item.content_class == content_class]
            if requested_ids is not None:
                candidates = [item for item in candidates if int(item.id) in requested_ids]
            if limit is not None:
                candidates = candidates[:limit]
        if dry_run:
            return run_id, [_context_from_item(item, source_specs) for item in candidates], explicit_cap
        contexts = [_context_from_item(item, source_specs) for item in candidates]
        return run_id, contexts, explicit_cap


def _context_from_item(item: IntelItem, source_specs: Mapping[str, SourceSpec]) -> _ItemContext:
    input_fingerprint = _item_fingerprint(item)
    source_spec = source_specs.get(item.source_id)
    if source_spec is None:
        source_spec = _spec_from_row(item.source)
    if not _structurally_valid(item):
        return _ItemContext(
            item_id=int(item.id),
            input_fingerprint=input_fingerprint,
            envelope=None,
            source_spec=source_spec,
            structural_error="item failed the structural prefilter",
        )
    try:
        envelope = _item_to_envelope(item, source_spec)
    except Exception as exc:
        return _ItemContext(
            item_id=int(item.id),
            input_fingerprint=input_fingerprint,
            envelope=None,
            source_spec=source_spec,
            structural_error=str(exc)[:4000] or exc.__class__.__name__,
        )
    return _ItemContext(int(item.id), input_fingerprint, envelope, source_spec)


def _call_screen(client: Any, envelope: RawIntelEnvelope | None, *, reject_threshold: int) -> ScreenResult:
    if envelope is None:
        raise ValueError("raw intel envelope is missing")
    method = getattr(client, "screen", None)
    if not callable(method):
        raise TypeError("AI client does not expose screen")
    value = method(envelope)
    if isinstance(value, ScreenResult):
        parsed = value.with_item(envelope)
    else:
        parsed = strict_parse_screen(value, envelope=envelope, reject_threshold=reject_threshold)
    return apply_screen_guard(parsed.with_item(envelope), envelope, reject_threshold=reject_threshold)


def _screen_provider_outcome(
    client: Any,
    context: _ItemContext,
    reject_threshold: int,
) -> tuple[ScreenResult, BaseException | None]:
    """Execute one provider call without touching SQLAlchemy state."""

    try:
        return _call_screen(client, context.envelope, reject_threshold=reject_threshold), None
    except BaseException as exc:  # isolate one provider failure from its batch
        return _screen_failure(context.item_id, exc), exc


def _screen_failure(item_id: int, exc: BaseException) -> ScreenResult:
    message = str(exc).strip() or exc.__class__.__name__
    return ScreenResult(
        item_id=item_id,
        decision="uncertain",
        reason_code="provider_failure",
        reason="Stage A provider call failed",
        confidence=0,
        risk_flags=["ai:screen_failed"],
        status="screen_failed",
        error_code=getattr(exc, "error_code", None) or exc.__class__.__name__,
        error_message=message[:4000],
    )


def _structural_screen(item_id: int, error: str) -> ScreenResult:
    return ScreenResult(
        item_id=item_id,
        decision="reject",
        reason_code="structural_invalid",
        reason=error[:4000],
        confidence=100,
        risk_flags=["prefilter:structural_invalid"],
        raw_response={"prefilter": "structural_invalid", "error": error},
    )


def _persist_screen_failure(
    session_factory: sessionmaker[Session],
    task_id: int,
    *,
    item_id: int,
    run_id: int,
    screen: ScreenResult,
    owner: str,
    model: str | None,
    retryable: bool,
    error_category: str,
    error_code: str,
    error_message: str,
) -> bool:
    try:
        with session_factory() as session:
            repo = IntelRepository(session)
            repo.save_screen(
                item_id,
                screen,
                run_id=run_id,
                model=model,
                status="screen_failed",
                error_message=error_message,
            )
            repo.set_item_status(item_id, "screen_failed", run_id=run_id)
            task = session.get(IntelRunStageTask, task_id)
            if task is None:
                raise RuntimeError(f"stage A task {task_id} disappeared")
            repo.fail_stage_task(
                task,
                owner=owner,
                error_category=error_category,
                error_code=error_code,
                error_message=error_message,
                retryable=retryable,
                raw_response=screen.raw_response,
            )
            session.commit()
        return True
    except Exception:
        LOGGER.exception("Stage A failure persistence failed for intel item %s", item_id)
        return False


def _mark_persistence_failure(session_factory: sessionmaker[Session], task_id: int, *, owner: str, message: str) -> None:
    try:
        with session_factory() as session:
            repo = IntelRepository(session)
            task = session.get(IntelRunStageTask, task_id)
            if task is not None:
                repo.fail_stage_task(
                    task,
                    owner=owner,
                    error_category="persistence",
                    error_code="persistence_failed",
                    error_message=message[:4000],
                    retryable=False,
                )
                session.commit()
    except Exception:
        LOGGER.exception("Unable to persist Stage A item-local failure for task %s", task_id)


def _claim_task(
    session_factory: sessionmaker[Session],
    task_id: int,
    *,
    owner: str,
    input_fingerprint: str,
    config_fingerprint: str,
) -> bool:
    try:
        with session_factory() as session:
            repo = IntelRepository(session)
            task = repo.claim_stage_task(
                task_id=task_id,
                stage_id=session.get(IntelRunStageTask, task_id).stage_id if session.get(IntelRunStageTask, task_id) else None,
                owner=owner,
                input_fingerprint=input_fingerprint,
                config_fingerprint=config_fingerprint,
            )
            if task is None:
                return False
            session.commit()
            return True
    except Exception:
        LOGGER.exception("Unable to claim Stage A task %s", task_id)
        return False


def _task_is_eligible(session_factory: sessionmaker[Session], task_id: int) -> bool:
    try:
        with session_factory() as session:
            task = session.get(IntelRunStageTask, task_id)
            if task is None or task.status != "succeeded":
                return False
            data = task.result
            return str(data.get("decision", "")).casefold() in {"pass", "uncertain"}
    except Exception:
        return False


def _classify_provider_failure(
    exc: BaseException | None,
    *,
    code: str | None,
    message: str | None,
) -> tuple[bool, str, str, str]:
    """Apply the frozen 429/timeout/5xx retry policy conservatively."""

    response = getattr(exc, "response", None) if exc is not None else None
    status = getattr(response, "status_code", None) if response is not None else None
    if status is None and exc is not None:
        status = getattr(exc, "status_code", None)
    try:
        status_int = int(status) if status is not None else None
    except (TypeError, ValueError):
        status_int = None
    text = " ".join(str(value or "") for value in (code, message, exc)).casefold()
    transient_text = any(
        token in text
        for token in (
            "429",
            "rate limit",
            "ratelimit",
            "5xx",
            "500",
            "502",
            "503",
            "504",
            "service unavailable",
            "bad gateway",
        )
    )
    if status_int == 429 or (status_int is not None and status_int >= 500) or transient_text:
        return True, "provider_retryable", str(status_int or code or "provider_retryable")[:128], str(message or exc or "provider unavailable")[:4000]
    if isinstance(exc, (httpx.TimeoutException, TimeoutError)) or any(token in text for token in ("timeout", "timed out", "temporarily unavailable", "rate limit", "ratelimit")):
        return True, "provider_retryable", str(code or (status_int or "timeout"))[:128], str(message or exc or "provider timeout")[:4000]
    if status_int is not None and 400 <= status_int < 500:
        if any(token in text for token in ("validation", "schema", "invalid json", "missing required")):
            category = "schema"
        else:
            category = "provider_auth" if status_int in {401, 403} or "auth" in text else "provider_blocked"
        return False, category, str(status_int)[:128], str(message or exc or "provider request blocked")[:4000]
    if isinstance(exc, (ValidationError, ValueError, TypeError, json.JSONDecodeError)) or any(token in text for token in ("validation", "schema", "invalid json", "missing required")):
        return False, "schema", str(code or (exc.__class__.__name__ if exc else "schema_error"))[:128], str(message or exc or "provider schema validation failed")[:4000]
    # Unknown provider failures are blocked unless they carry an explicit
    # transient hint.  This prevents hidden retry loops for permanent errors.
    return False, "provider_blocked", str(code or (exc.__class__.__name__ if exc else "provider_error"))[:128], str(message or exc or "provider request failed")[:4000]


def _item_to_envelope(item: IntelItem, spec: SourceSpec) -> RawIntelEnvelope:
    source = item.source
    return RawIntelEnvelope(
        item_id=item.id,
        source_id=item.source_id,
        source_name=source.name if source is not None else spec.name,
        source_group=spec.source_group or (source.source_group if source is not None else None),
        source_subtype=spec.source_subtype or (source.source_subtype if source is not None else None),
        source_role=spec.source_role or (source.source_role if source is not None else None),
        source_tier=spec.tier or (source.tier if source is not None else None),
        source_content_class=spec.content_class or item.content_class or "community_social",
        external_id=item.external_id,
        content_hash=item.content_hash,
        title=item.title,
        url=item.canonical_url,
        published_at=item.published_at,
        captured_at=item.captured_at,
        summary=item.summary,
        body_text=item.content_text or item.summary,
        metrics=_json_dict(item.metrics_json),
        raw_payload=_json_dict(item.raw_payload_json),
    )


def _spec_from_row(row: Any) -> SourceSpec:
    if row is None:
        return SourceSpec.model_validate(
            {"id": "unknown", "name": "unknown", "transport": "feed", "url": "https://invalid.local/", "content_class": "community_social"}
        )
    data: dict[str, Any] = {
        "id": row.id,
        "name": row.name,
        "transport": row.transport,
        "url": row.url,
        "enabled": row.enabled,
        "priority": row.priority,
        "fetch_interval": row.fetch_interval,
        "default_limit": row.default_limit,
        "source_group": row.source_group,
        "source_subtype": row.source_subtype,
        "source_role": row.source_role,
        "spam_risk": row.spam_risk,
        "quality_weight": row.quality_weight,
        "content_class": row.content_class,
        "selection_policy": _json_dict(row.selection_policy_json),
    }
    if row.transport in {"feed", "rsshub"}:
        data["feed"] = {"format": row.feed_format or "rss", "adapter": row.feed_adapter or "generic"}
    elif row.transport == "github":
        github: dict[str, Any] = {"mode": row.github_mode or "search"}
        for name in ("query", "sort", "order", "pushed_days", "period"):
            value = getattr(row, f"github_{name}", None)
            if value is not None:
                github[name] = value
        data["github"] = github
    return SourceSpec.model_validate(data)


def _structurally_valid(item: IntelItem) -> bool:
    return bool(str(item.source_id or "").strip() and str(item.title or "").strip() and str(item.content_hash or "").strip())


def _item_fingerprint(item: IntelItem) -> str:
    value = item.content_hash or "|".join(str(part or "") for part in (item.source_id, item.title, item.canonical_url, item.summary, item.content_text))
    return hashlib.sha256(value.encode("utf-8", errors="ignore")).hexdigest()


def _config_fingerprint(*, stage: str, model: Any, reject_threshold: int) -> str:
    payload = {"stage": stage, "model": str(model or ""), "reject_threshold": int(reject_threshold)}
    return hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()


def _bounded_score(value: Any, default: int) -> int:
    try:
        return max(0, min(100, int(value)))
    except (TypeError, ValueError):
        return default


def _bounded_concurrency(value: Any) -> int:
    try:
        return max(1, min(4, int(value)))
    except (TypeError, ValueError):
        return 4


def _normalise_limit(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return max(1, int(value))
    except (TypeError, ValueError):
        return None


def _json_dict(value: str | None) -> dict[str, Any]:
    if not value:
        return {}
    try:
        parsed = json.loads(value)
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}
    return dict(parsed) if isinstance(parsed, Mapping) else {}


__all__ = [
    "StageAScreenResult",
    "StageAResult",
    "run_stage_a_screen_job",
    "run_stage_a_screen",
    "run_stage_a",
    "run_stage_a_job",
    "run_screen_stage",
]
