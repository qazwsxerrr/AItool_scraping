"""Pure read/export stage for v2 intelligence records."""

from __future__ import annotations

import json
import hashlib
import os
import shutil
import tempfile
from uuid import uuid4
from collections import Counter
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session, joinedload, sessionmaker

from app.ai.skills.intel_triage.normalize import normalize_text
from app.ai.skills.stage_d_selection import (
    STAGE_D_SELECTION_SCHEMA_VERSION,
    strict_parse_stage_d_selection,
)
from app.config.limits import DEFAULT_DAILY_REPORT_LIMIT
from app.config.settings import Settings
from app.domain.recency import StageAFreshnessDecision, stage_a_time_decision
from app.github.report import write_github_trending_report
from app.storage.db import create_engine_from_url, create_session_factory, init_db
from app.storage.repository import IntelRepository
from app.storage.models import IntelEvent, IntelEventItem, IntelItem, IntelRun
from app.storage.daily_build_summary import build_daily_build_summary


_STAGE_D_BLOCKING_TASK_STATUSES = frozenset(
    {"pending", "running", "failed", "retry_waiting", "blocked"}
)


@dataclass(frozen=True)
class IntelExportResult:
    exported: int
    jsonl_path: str
    markdown_path: str
    manifest_path: str | None = None
    dry_run: bool = False
    github_report_path: str | None = None
    status_counts: dict[str, int] = field(default_factory=dict)
    failure_counts: dict[str, int] = field(default_factory=dict)
    partial: bool = False
    partial_reason: str | None = None
    run_id: int | None = None
    # Internal hand-off to the daily publisher.  This is never serialized to
    # the public manifest/CLI, but avoids re-reading a build after its files
    # have been staged.
    records: tuple[dict[str, Any], ...] = field(default_factory=tuple, repr=False)


@dataclass(frozen=True)
class DailyBundlePromotion:
    """Reversible directory-level replacement of one public daily bundle."""

    staging_dir: Path
    final_dir: Path
    backup_dir: Path | None = None


def run_intel_export_job(
    *,
    session_factory: sessionmaker[Session],
    output_dir: str | Path = "output/intel",
    limit: int | None = DEFAULT_DAILY_REPORT_LIMIT,
    dry_run: bool = False,
    github_report_dir: str | Path | None = None,
    run_id: int,
    artifact_dir: str | Path | None = None,
    artifact_reference_dir: str | Path | None = None,
    allow_partial: bool = False,
) -> IntelExportResult:
    final_output = Path(output_dir)
    effective_limit = _normalise_export_limit(limit)
    stage = None
    stage_task = None
    run: IntelRun
    build_summary: dict[str, Any] | None = None
    owner = "intel-export"
    with session_factory() as session:
        try:
            repo = IntelRepository(session)
            run = session.get(IntelRun, int(run_id))
            if run is None or run.edition_id is None:
                raise ValueError("export requires the current daily edition build")
            stage_d_selection = _load_stage_d_selection(repo, int(run_id))
            artifact_output = Path(artifact_dir) if artifact_dir is not None else _daily_output_dir(final_output, run)
            artifact_reference = (
                Path(artifact_reference_dir)
                if artifact_reference_dir is not None
                else artifact_output
            )
            records = _list_export_events(
                session,
                selection=stage_d_selection,
                limit=effective_limit,
                run_id=run_id,
                edition_date=run.edition_date,
                reference_time=run.reference_time,
            )
            # ``partial`` is a retained audit label for explicit downstream
            # truncation/failure states.  A source fetch warning is not a
            # publication gate and is handled through fetch metadata.
            fetch_stage = repo.get_stage(int(run_id), "fetch")
            fetch_metadata = fetch_stage.metadata_dict if fetch_stage is not None else {}
            try:
                fetch_failed_count = int(fetch_metadata.get("failed") or 0)
            except (TypeError, ValueError):
                fetch_failed_count = 0
            source_warning = (
                str(run.partial_reason or "").casefold().startswith("fetch_failed_sources:")
                or bool(fetch_metadata.get("failed_sources"))
                or fetch_failed_count > 0
            )
            is_partial_build = (
                not source_warning
                and (bool(run.partial) or str(run.status).casefold() in {"failed", "partial"})
            )
            if is_partial_build and not allow_partial:
                raise RuntimeError(f"daily build is not publishable: {run.partial_reason or run.status}")
            stage = repo.ensure_stage(
                int(run_id),
                "export",
                metadata={"artifact_dir": str(artifact_reference)},
            )
            input_fingerprint = _export_input_fingerprint(records, int(run_id))
            stage_task = repo.ensure_stage_task(
                stage,
                subject_type="run",
                subject_id=int(run_id),
                target_run_id=int(run_id),
                input_fingerprint=input_fingerprint,
                config_fingerprint="export-v2",
            )
            claimed = repo.claim_stage_task(
                stage,
                task_id=stage_task.id,
                owner=owner,
                force=True,
                input_fingerprint=input_fingerprint,
                config_fingerprint="export-v2",
            )
            if claimed is None:
                raise RuntimeError("export stage is already running")
            stage_task = claimed
            # Make the export lease visible before filesystem work starts.
            session.commit()
            # The task is still running while files are written. The manifest
            # describes the terminal artifact state about to be published.
            build_summary = build_daily_build_summary(
                session,
                run=run,
                export_status="succeeded",
            )
        except Exception:
            session.rollback()
            raise

    artifact_output = Path(artifact_dir) if artifact_dir is not None else _daily_output_dir(final_output, run)
    jsonl_path = artifact_output / "intel_items.jsonl"
    markdown_path = artifact_output / "intel_digest.md"
    manifest_path = artifact_output / "manifest.json"
    report_root = Path(github_report_dir) if github_report_dir else artifact_output / "github-trending"

    status_counts = dict(Counter(str(record.get("status") or "unknown") for record in records))
    failure_counts = {
        status: count
        for status, count in status_counts.items()
        if status in {"failed", "screen_failed", "analysis_failed"}
    }

    github_report_path: Path | None = None
    try:
        if not dry_run:
            jsonl_payload = "".join(json.dumps(record, ensure_ascii=False, default=str) + "\n" for record in records)
            markdown_payload = _markdown(
                records,
                edition_date=run.edition_date,
                status_counts=status_counts,
                failure_counts=failure_counts,
                source_warnings=(build_summary or {}).get("source_warnings", []),
                partial=is_partial_build,
                partial_reason=run.partial_reason or run.error if is_partial_build else None,
            )
            payloads = {
                jsonl_path: jsonl_payload,
                markdown_path: markdown_payload,
            }
            payloads[manifest_path] = _manifest(
                run=run,
                records=records,
                jsonl_payload=jsonl_payload,
                partial=is_partial_build,
                partial_reason=run.partial_reason or run.error if is_partial_build else None,
                build_summary=build_summary,
            )
            _atomic_write_bundle(payloads)
            github_report_path = write_github_trending_report(
                records,
                output_root=report_root,
                report_date=_edition_report_date(run),
            )
        if stage_task is not None:
            with session_factory() as session:
                repo = IntelRepository(session)
                state_stage = repo.get_stage(int(run_id), "export")
                state_task = repo.get_task(state_stage, subject_type="run", subject_id=int(run_id)) if state_stage else None
                if state_task is not None:
                    repo.complete_stage_task(
                        state_task,
                        owner=owner,
                        result={"exported": len(records), "artifact_dir": str(artifact_reference)},
                    )
                session.commit()
    except Exception as exc:
        if stage_task is not None:
            try:
                with session_factory() as session:
                    repo = IntelRepository(session)
                    state_stage = repo.get_stage(int(run_id), "export")
                    state_task = repo.get_task(state_stage, subject_type="run", subject_id=int(run_id)) if state_stage else None
                    if state_task is not None and state_task.status == "running":
                        repo.fail_stage_task(
                            state_task,
                            error_category="io",
                            error_code="export_failed",
                            error_message=str(exc),
                            retryable=True,
                            owner=owner,
                        )
                    session.commit()
            except Exception:
                pass
        raise

    return IntelExportResult(
        exported=len(records),
        jsonl_path=str(jsonl_path),
        markdown_path=str(markdown_path),
        manifest_path=str(manifest_path),
        dry_run=dry_run,
        github_report_path=str(github_report_path) if github_report_path else None,
        status_counts=status_counts,
        failure_counts=failure_counts,
        partial=is_partial_build,
        partial_reason=run.partial_reason or run.error if is_partial_build else None,
        run_id=run_id,
        records=tuple(records),
    )


def run_intel_export_from_settings(
    *,
    settings: Settings,
    output_dir: str | Path = "output/intel",
    limit: int | None = DEFAULT_DAILY_REPORT_LIMIT,
    dry_run: bool = False,
    github_report_dir: str | Path | None = None,
    run_id: int,
    artifact_dir: str | Path | None = None,
    artifact_reference_dir: str | Path | None = None,
    allow_partial: bool = False,
) -> IntelExportResult:
    database_url = _readable_database_url(settings.database_url, dry_run=dry_run)
    engine = create_engine_from_url(database_url)
    try:
        if not dry_run or database_url == "sqlite:///:memory:":
            init_db(engine)
        return run_intel_export_job(
            session_factory=create_session_factory(engine),
            output_dir=output_dir,
            limit=_normalise_export_limit(limit),
            dry_run=dry_run,
            github_report_dir=github_report_dir,
            run_id=run_id,
            artifact_dir=artifact_dir,
            artifact_reference_dir=artifact_reference_dir,
            allow_partial=allow_partial,
        )
    finally:
        # Daily publication moves the completed SQLite draft into its
        # date-level audit slot immediately after export.  Dispose the export
        # engine so it cannot keep a stale SQLite file handle open.
        engine.dispose()


def daily_output_dir_for_run(output_dir: str | Path, run: IntelRun) -> Path:
    """Public helper used by the pipeline's success-only publisher."""

    return _daily_output_dir(Path(output_dir), run)


def draft_output_dir_for_run(output_dir: str | Path, run: IntelRun) -> Path:
    """Return the persistent, human-readable artifact directory for a draft."""

    return _draft_output_dir(Path(output_dir), run)


def create_daily_bundle_staging_dir(output_dir: str | Path, run: IntelRun) -> Path:
    """Create an adjacent temporary directory safe to rename into place."""

    final_dir = daily_output_dir_for_run(output_dir, run)
    return _create_bundle_staging_dir(final_dir, run_id=int(run.id))


def create_draft_bundle_staging_dir(output_dir: str | Path, run: IntelRun) -> Path:
    """Create an adjacent staging directory for one persistent draft bundle."""

    final_dir = draft_output_dir_for_run(output_dir, run)
    return _create_bundle_staging_dir(final_dir, run_id=int(run.id))


def _create_bundle_staging_dir(final_dir: Path, *, run_id: int) -> Path:
    final_dir.parent.mkdir(parents=True, exist_ok=True)
    return Path(tempfile.mkdtemp(prefix=f".{final_dir.name}.build-{int(run_id)}-", dir=str(final_dir.parent)))


def promote_daily_bundle(*, staging_dir: str | Path, final_dir: str | Path) -> DailyBundlePromotion:
    """Atomically replace a whole date directory while retaining one rollback copy."""

    staging = Path(staging_dir)
    final = Path(final_dir)
    if not staging.is_dir():
        raise ValueError(f"daily staging directory does not exist: {staging}")
    final.parent.mkdir(parents=True, exist_ok=True)
    backup: Path | None = None
    if final.exists():
        backup = final.parent / f".{final.name}.previous-{uuid4().hex}"
        os.replace(final, backup)
    try:
        os.replace(staging, final)
    except Exception:
        if backup is not None and backup.exists() and not final.exists():
            os.replace(backup, final)
        raise
    return DailyBundlePromotion(staging_dir=staging, final_dir=final, backup_dir=backup)


def rollback_daily_bundle(promotion: DailyBundlePromotion) -> None:
    """Restore the prior public bundle after a failed database publication."""

    if promotion.final_dir.exists():
        if promotion.staging_dir.exists():
            shutil.rmtree(promotion.staging_dir)
        os.replace(promotion.final_dir, promotion.staging_dir)
    if promotion.backup_dir is not None and promotion.backup_dir.exists():
        os.replace(promotion.backup_dir, promotion.final_dir)


def finalize_daily_bundle(promotion: DailyBundlePromotion) -> None:
    """Discard the rollback copy only after the new report is durable."""

    if promotion.backup_dir is not None and promotion.backup_dir.exists():
        shutil.rmtree(promotion.backup_dir)


def _daily_output_dir(final_output: Path, run: IntelRun) -> Path:
    """Return the mutable public bundle for one Asia/Shanghai daily edition.

    File artifacts group only by their public edition date, so a same-day
    rebuild atomically replaces the existing daily directory.
    """

    base = final_output.parent if final_output.name.casefold() == "intel" else final_output
    edition_date = run.edition_date
    if not edition_date:
        raise ValueError(f"run {run.id} has no valid edition_date")
    return base / "daily" / edition_date


def _draft_output_dir(output_root: Path, run: IntelRun) -> Path:
    """Return the review-only projection for the current database draft."""

    edition_date = run.edition_date
    if not edition_date:
        raise ValueError(f"run {run.id} has no valid edition_date")
    return output_root / "draft" / edition_date


def _edition_report_date(run: IntelRun) -> date:
    """Keep the optional GitHub report aligned with its daily bundle."""

    if not run.edition_date:
        raise ValueError("daily build is missing its edition date")
    try:
        return date.fromisoformat(run.edition_date)
    except ValueError as exc:
        raise ValueError("daily build has an invalid edition date") from exc


def _load_stage_d_selection(repo: IntelRepository, run_id: int) -> list[dict[str, Any]]:
    """Return the validated ordered Stage-D subset for this build."""

    stage = repo.get_stage(int(run_id), "stage_d")
    if stage is None:
        raise RuntimeError("stage_d_incomplete: Stage D has not run")
    tasks = repo.list_stage_tasks(stage, include_expired=True)
    blocking = [task for task in tasks if str(task.status) in _STAGE_D_BLOCKING_TASK_STATUSES]
    if str(stage.status) != "succeeded" or blocking:
        status = str(stage.status or "unknown")
        task_statuses = ",".join(str(task.status) for task in blocking) or "none"
        raise RuntimeError(
            f"stage_d_incomplete: run {int(run_id)} status={status} blocking_tasks={task_statuses}"
        )
    run_tasks = [
        task
        for task in tasks
        if task.subject_type == "run" and task.subject_id == str(int(run_id))
    ]
    if len(run_tasks) != 1 or run_tasks[0].status != "succeeded":
        raise RuntimeError("stage_d_incomplete: Stage D run task is missing or incomplete")
    result = run_tasks[0].result
    if not isinstance(result, dict) or result.get("schema_version") != STAGE_D_SELECTION_SCHEMA_VERSION:
        raise RuntimeError("stage_d_incomplete: Stage D result uses an unsupported schema")
    candidate_event_ids = result.get("candidate_event_ids")
    if not isinstance(candidate_event_ids, list):
        raise RuntimeError("stage_d_incomplete: Stage D result is missing candidate_event_ids")
    try:
        parsed = strict_parse_stage_d_selection(
            {
                "schema_version": result.get("schema_version"),
                "selected": result.get("selected"),
                "unselected": result.get("unselected", []),
            },
            candidate_event_ids=candidate_event_ids,
            max_selected=len(candidate_event_ids),
        )
    except (TypeError, ValueError) as exc:
        raise RuntimeError(f"stage_d_incomplete: invalid Stage D selection: {exc}") from exc
    return [row.model_dump(mode="json") for row in parsed.selected]


def _manifest(
    *,
    run: IntelRun,
    records: list[dict[str, Any]],
    jsonl_payload: str,
    partial: bool,
    partial_reason: str | None,
    build_summary: dict[str, Any] | None,
) -> str:
    """Build the machine-readable metadata for a date-addressed export."""

    payload = {
        "schema_version": 7,
        "edition_date": run.edition_date,
        "edition_timezone": "Asia/Shanghai",
        "artifact_status": "partial" if partial else "ready",
        "edition_status": "partial" if partial else "ready",
        "partial": bool(partial),
        "partial_reason": partial_reason,
        "reference_time": _date(run.reference_time),
        "selected_count": len(records),
        "stage_d_count": int((build_summary or {}).get("funnel", {}).get("stage_d_total", 0)),
        "stage_d_candidate_count": int((build_summary or {}).get("funnel", {}).get("stage_d_candidate_count", 0)),
        "funnel": (build_summary or {}).get("funnel", {}),
        "stages": (build_summary or {}).get("stages", {}),
        "source_warnings": (build_summary or {}).get("source_warnings", []),
        "failure_reasons": (build_summary or {}).get("failure_reasons", []),
        "artifacts": {
            "items_jsonl": "intel_items.jsonl",
            "digest_markdown": "intel_digest.md",
            "github_trending_dir": "github-trending",
        },
        "jsonl_sha256": hashlib.sha256(jsonl_payload.encode("utf-8")).hexdigest(),
        "jsonl_bytes": len(jsonl_payload.encode("utf-8")),
    }
    return json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"


def _export_input_fingerprint(
    records: list[dict[str, Any]],
    run_id: int,
) -> str:
    payload = {
        "build_id": int(run_id),
        "events": [int(record.get("event_id")) for record in records if record.get("event_id") is not None],
    }
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _atomic_write_bundle(payloads: dict[Path, str]) -> None:
    """Write a group of text artifacts without truncating existing outputs."""

    temporary: list[tuple[str, Path]] = []
    try:
        for path, value in payloads.items():
            path.parent.mkdir(parents=True, exist_ok=True)
            fd, name = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent), text=True)
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(value)
                handle.flush()
                os.fsync(handle.fileno())
            temporary.append((name, path))
        for name, path in temporary:
            os.replace(name, path)
    finally:
        for name, _ in temporary:
            try:
                os.unlink(name)
            except FileNotFoundError:
                pass


def _list_export_events(
    session: Session,
    *,
    selection: list[dict[str, Any]],
    limit: int | None,
    run_id: int,
    edition_date: str | None = None,
    reference_time: datetime | None = None,
) -> list[dict[str, Any]]:
    """Serialize the validated Stage-D subset without applying another gate."""

    if not selection:
        return []
    selected_event_ids = [int(row["event_id"]) for row in selection]
    stmt = (
        select(IntelEvent)
        .options(
            joinedload(IntelEvent.event_items).joinedload(IntelEventItem.item).joinedload(IntelItem.ai_screen),
            joinedload(IntelEvent.event_items).joinedload(IntelEventItem.item).joinedload(IntelItem.ai_review),
            joinedload(IntelEvent.event_items).joinedload(IntelEventItem.item).joinedload(IntelItem.source),
            joinedload(IntelEvent.event_items).joinedload(IntelEventItem.source),
            joinedload(IntelEvent.evidence),
        )
        .where(
            IntelEvent.build_id == int(run_id),
            IntelEvent.id.in_(selected_event_ids),
        )
    )
    by_id = {
        int(event.id): event
        for event in session.scalars(stmt).unique().all()
    }
    missing = [event_id for event_id in selected_event_ids if event_id not in by_id]
    if missing:
        raise RuntimeError(f"stage_d_incomplete: selected events are missing from this build: {missing}")
    records: list[dict[str, Any]] = []
    for display_order, decision in enumerate(selection, start=1):
        event = by_id[int(decision["event_id"])]
        freshness = _event_stage_a_freshness(
            event,
            edition_date=edition_date,
            reference_time=reference_time,
        )
        record = _serialize_event(
            event,
            decision=decision,
            display_order=display_order,
            freshness=freshness,
        )
        records.append(record)
        if limit is not None and len(records) >= limit:
            break
    return records


def _serialize_event(
    event: IntelEvent,
    *,
    decision: dict[str, Any],
    display_order: int,
    freshness: StageAFreshnessDecision | None = None,
) -> dict[str, Any]:
    source_ids = _json_list(event.source_ids_json)
    source_groups = _json_list(event.source_groups_json)
    source_refs: list[dict[str, Any]] = []
    screen_audit: list[dict[str, Any]] = []
    member_statuses: list[str] = []
    primary_item: IntelItem | None = None
    for relation in event.event_items:
        if relation.source_id and relation.source_id not in source_ids:
            source_ids.append(relation.source_id)
        if relation.source_group and relation.source_group not in source_groups:
            source_groups.append(relation.source_group)
        item = relation.item
        if relation.is_primary and item is not None:
            primary_item = item
        source = relation.source or (item.source if item is not None else None)
        if item is not None:
            member_statuses.append(item.status)
            screen = item.ai_screen
            if screen is not None:
                screen_audit.append(
                    {
                        "item_id": item.id,
                        "decision": screen.decision,
                        "reason_code": screen.reason_code,
                        "reason": screen.reason,
                        "confidence": screen.confidence,
                        "risk_flags": _json_list(screen.risk_flags_json),
                        "status": screen.status,
                    }
                )
        raw_summary = (
            item.ai_review.summary_cn
            if item is not None and item.ai_review is not None and item.ai_review.summary_cn
            else (item.summary if item is not None else None)
        )
        ai_summary = normalize_text(raw_summary) if raw_summary else None
        content_text = normalize_text(item.content_text) if item is not None and item.content_text else None
        source_refs.append(
            {
                "item_id": item.id if item is not None else relation.item_id,
                "source_id": relation.source_id,
                "source_name": source.name if source is not None else None,
                "source_group": relation.source_group or (source.source_group if source is not None else None),
                "source_url": relation.source_url or (item.canonical_url if item is not None else None),
                "title": relation.source_title or (item.title if item is not None else None),
                "match_type": relation.match_type,
                "match_confidence": relation.match_confidence,
                "is_primary": bool(relation.is_primary),
                "lineage": _public_value(_json(relation.lineage_json, {})),
                "ai_summary": ai_summary,
                "source_summary": normalize_text(item.summary) if item is not None and item.summary else None,
                "content_text": content_text,
                "content_depth": item.content_depth if item is not None else None,
            }
        )
    risk_flags = _json_list(event.risk_flags_json)
    verification_refs = _verification_refs(event)
    novelty = str(event.novelty_status or "unknown").casefold()
    provenance_kind = novelty if novelty in {"new", "updated", "repeat", "unknown"} else "unknown"
    reason_code = str(decision.get("reason_code") or "selected")
    reason = str(decision.get("reason") or "").strip()
    metadata = {
        "reason_code": reason_code,
        "reason": reason,
        "provenance": {"kind": provenance_kind},
    }
    return {
        "record_type": "intel_event",
        "stage": "stage_d",
        # Stable public history identity.  It is deliberately distinct from
        # the audit-workspace event id, which never appears in public output.
        "event_key": event.event_key,
        "event_id": event.id,
        "id": event.id,
        "display_order": int(display_order),
        "selected": True,
        "status": "selected",
        "display_score": float(event.display_score or 0.0),
        "topic": event.topic,
        "topics": _json_list(event.topics_json),
        "content_class": event.content_class,
        "source_group": event.source_group or (source_groups[0] if source_groups else None),
        "source_groups": source_groups,
        "source_ids": source_ids,
        "title": event.title,
        "original_title": event.title,
        "summary": event.summary_cn,
        "summary_cn": event.summary_cn,
        "url": event.canonical_url,
        "canonical_url": event.canonical_url,
        "provenance": {
            "kind": provenance_kind,
        },
        "risk_flags": risk_flags,
        "reason": reason,
        "reason_code": reason_code,
        "metadata": metadata,
        "keywords": _json_list(event.keywords_json),
        "entities": _public_value(_json(event.entities_json, [])),
        "source_refs": source_refs,
        # These are deliberately separate from original input sources: they
        # document optional C-agent web verification and never replace source
        # attribution for the event itself.
        "verification_refs": verification_refs,
        "review_state": event.review_state,
        "resolution_confidence": int(event.resolution_confidence or 0),
        "member_statuses": sorted(set(member_statuses)),
        "screen_audit": screen_audit,
        "first_seen_at": _date(event.first_seen_at),
        "last_seen_at": _date(event.last_seen_at),
        "published_at": _date(primary_item.published_at) if primary_item is not None else None,
        "captured_at": _date(primary_item.captured_at) if primary_item is not None else None,
        # Retain the public audit field, but never use it as an Export gate.
        "freshness": freshness.metadata() if freshness is not None else None,
    }


def _event_stage_a_freshness(
    event: IntelEvent,
    *,
    edition_date: str | None,
    reference_time: datetime | None,
) -> StageAFreshnessDecision | None:
    """Reproduce Stage A metadata for export audit without filtering rows."""

    if not edition_date or reference_time is None:
        return None
    primary = next(
        (
            relation.item
            for relation in event.event_items
            if relation.is_primary and relation.item is not None
        ),
        None,
    )
    if primary is None:
        return None
    return stage_a_time_decision(
        primary,
        source=primary.source,
        reference_time=reference_time,
        edition_date=edition_date,
    )


def _markdown(
    records: list[dict[str, Any]],
    *,
    edition_date: str | None = None,
    status_counts: dict[str, int] | None = None,
    failure_counts: dict[str, int] | None = None,
    source_warnings: list[dict[str, Any]] | None = None,
    partial: bool = False,
    partial_reason: str | None = None,
) -> str:
    counts: dict[str, int] = {}
    for record in records:
        key = str(record.get("topic_category") or record.get("content_class") or "未分类")
        counts[key] = counts.get(key, 0) + 1
    title = f"# AI 日报 · {edition_date}" if edition_date else "# AI 情报导出"
    lines = [title, "", f"保留条目：{len(records)}", ""]
    if partial:
        lines.extend([f"运行状态：partial（{partial_reason or 'explicit_ai_limit'}）", ""])
    warning_line = _source_warning_markdown_line(source_warnings)
    if warning_line:
        lines.extend([warning_line, ""])
    if counts:
        lines.append("主题分类统计：" + "、".join(f"{key}={value}" for key, value in sorted(counts.items())))
        lines.append("")
    if status_counts:
        lines.append("状态统计：" + "、".join(f"{key}={value}" for key, value in sorted(status_counts.items())))
        lines.append("失败统计：" + ("、".join(f"{key}={value}" for key, value in sorted((failure_counts or {}).items())) or "无"))
        lines.append("")
    for index, record in enumerate(records, start=1):
        if record.get("record_type") == "intel_event":
            event_lines = [
                f"## {index}. {record.get('title') or '(untitled)'}",
                (
                    f"- 事件：`{record.get('event_id')}` | 展示顺序：`{record.get('display_order')}` | "
                    f"display_score=`{record.get('display_score')}` | topic=`{record.get('topic')}`"
                ),
                (
                    f"- 类别：`{record.get('content_class')}` | 来源：{_primary_source_markdown(record)}"
                    f" | 状态：`selected`"
                ),
                _event_time_line(record),
                f"- 摘要：{record.get('summary_cn') or record.get('summary') or '暂无摘要'}",
                f"- 选稿依据：{record.get('reason') or '未记录'}",
                f"- 风险：{', '.join(record.get('risk_flags') or []) or '无'}",
                _related_links_markdown_line(record),
            ]
            verification_line = _verification_markdown_line(record)
            if verification_line:
                event_lines.append(verification_line)
            event_lines.append("")
            lines.extend(event_lines)
            continue
    return "\n".join(lines) + "\n"


def _source_warning_markdown_line(source_warnings: list[dict[str, Any]] | None) -> str | None:
    """Render source fetch failures as a non-blocking publication warning."""

    if not isinstance(source_warnings, list):
        return None
    labels: list[str] = []
    for row in source_warnings:
        if not isinstance(row, dict):
            continue
        source_id = str(row.get("source_id") or "").strip()
        if not source_id:
            continue
        status = str(row.get("status") or "failed").strip()
        label = f"{source_id}（{status}）"
        if label not in labels:
            labels.append(label)
    return "- 来源警告：" + "、".join(labels) if labels else None


def _event_time_line(record: dict[str, Any]) -> str:
    freshness = record.get("freshness") if isinstance(record.get("freshness"), dict) else {}
    age = freshness.get("age_hours")
    try:
        age_text = f"{float(age):.1f}h" if age is not None else "-"
    except (TypeError, ValueError):
        age_text = "-"
    return (
        f"- 时间：published_at=`{record.get('published_at') or '-'}` | "
        f"basis=`{freshness.get('time_basis') or '-'}` | age=`{age_text}`"
    )


def _source_refs(record: dict[str, Any]) -> list[dict[str, Any]]:
    refs = record.get("source_refs")
    return [ref for ref in refs if isinstance(ref, dict)] if isinstance(refs, list) else []


def _primary_source_ref(refs: list[dict[str, Any]]) -> dict[str, Any] | None:
    return next((ref for ref in refs if bool(ref.get("is_primary"))), refs[0] if refs else None)


def _source_name(ref: dict[str, Any] | None, record: dict[str, Any]) -> str:
    if ref is not None:
        for key in ("source_name", "source_id"):
            value = str(ref.get(key) or "").strip()
            if value:
                return value
    source_ids = record.get("source_ids")
    if isinstance(source_ids, list):
        for value in source_ids:
            text = str(value or "").strip()
            if text:
                return text
    return "未标注来源"


def _source_url(ref: dict[str, Any] | None, record: dict[str, Any], *, fallback_to_record: bool = False) -> str:
    if ref is not None:
        value = str(ref.get("source_url") or "").strip()
        if value:
            return value
    if fallback_to_record:
        return str(record.get("url") or record.get("canonical_url") or "").strip()
    return ""


def _markdown_link(label: str, url: str) -> str:
    safe_label = label.replace("[", "\\[").replace("]", "\\]")
    return f"[{safe_label}]({url})" if url else safe_label


def _primary_source_markdown(record: dict[str, Any]) -> str:
    refs = _source_refs(record)
    primary = _primary_source_ref(refs)
    return _markdown_link(_source_name(primary, record), _source_url(primary, record, fallback_to_record=True))


def _related_source_label(ref: dict[str, Any], record: dict[str, Any], *, duplicate_name: bool) -> str:
    name = _source_name(ref, record)
    title = " ".join(str(ref.get("title") or "").split())
    if not duplicate_name or not title or title == name:
        return name
    return f"{name}：{title[:100]}{'…' if len(title) > 100 else ''}"


def _related_links_markdown_line(record: dict[str, Any]) -> str:
    refs = _source_refs(record)
    primary = _primary_source_ref(refs)
    candidates: list[tuple[dict[str, Any], str, str]] = []
    seen_urls: set[str] = set()
    for ref in refs:
        if ref is primary:
            continue
        url = _source_url(ref, record)
        if not url or url in seen_urls:
            continue
        seen_urls.add(url)
        candidates.append((ref, url, _source_name(ref, record)))
    if not candidates:
        return "- 相关链接：无"
    name_counts = Counter(name for _, _, name in candidates)
    links = [
        _markdown_link(
            _related_source_label(ref, record, duplicate_name=name_counts[name] > 1),
            url,
        )
        for ref, url, name in candidates
    ]
    return "- 相关链接：" + "；".join(links)


def _verification_refs(event: IntelEvent) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str]] = set()
    for evidence in list(getattr(event, "evidence", ()) or ()):
        if str(evidence.status or "").strip().casefold() != "verified":
            continue
        url = str(evidence.final_url or evidence.url or "").strip()
        host = str(evidence.host or "").strip()
        claim = str(evidence.verification_claim or "").strip()
        key = (url, host, claim)
        if not url or key in seen:
            continue
        seen.add(key)
        rows.append(
            {
                "url": url,
                "host": host or None,
                "title": str(evidence.title or "").strip() or None,
                "excerpt": str(evidence.excerpt or "").strip()[:600] or None,
                "claim": claim or None,
                "status": str(evidence.status or "recorded").strip(),
                "fetched_at": _date(evidence.fetched_at),
            }
        )
    return rows


def _verification_markdown_line(record: dict[str, Any]) -> str | None:
    refs = record.get("verification_refs")
    if not isinstance(refs, list) or not refs:
        return None
    verified_labels: list[str] = []
    for ref in refs[:6]:
        if not isinstance(ref, dict):
            continue
        if str(ref.get("status") or "").strip().casefold() != "verified":
            continue
        label = str(ref.get("title") or ref.get("host") or ref.get("url") or "复核来源").strip()
        url = str(ref.get("url") or "").strip()
        value = f"[{label}]({url})" if url else label
        if value not in verified_labels:
            verified_labels.append(value)
    if not verified_labels:
        return None
    return "- 复核依据：" + "；".join(verified_labels)


def _json(value: str | None, default: Any) -> Any:
    if not value:
        return default
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return default


def _json_list(value: str | None) -> list[str]:
    raw = _json(value, [])
    if isinstance(raw, str):
        raw = [raw]
    if not isinstance(raw, (list, tuple, set)):
        return []
    return [str(item) for item in raw if item is not None and str(item).strip()]


def _public_value(value: Any) -> Any:
    """Remove hidden build identifiers from exportable nested JSON."""

    if isinstance(value, dict):
        return {
            str(key): _public_value(child)
            for key, child in value.items()
            if not _is_internal_execution_key(key)
        }
    if isinstance(value, list):
        return [_public_value(child) for child in value]
    if isinstance(value, tuple):
        return [_public_value(child) for child in value]
    return value


def _is_internal_execution_key(key: object) -> bool:
    normalized = str(key).strip().casefold()
    return (
        normalized == "run_id"
        or normalized.endswith("_run_id")
    )


def _normalise_export_limit(value: int | None) -> int:
    """Use the configured default while allowing explicit caller overrides."""

    if value is None:
        return DEFAULT_DAILY_REPORT_LIMIT
    try:
        return max(0, int(value))
    except (TypeError, ValueError, OverflowError):
        return DEFAULT_DAILY_REPORT_LIMIT


def _date(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


def _readable_database_url(database_url: str, *, dry_run: bool) -> str:
    if not dry_run or not database_url.startswith("sqlite:///"):
        return database_url
    path_text = database_url[len("sqlite:///") :]
    if path_text in {":memory:", ""}:
        return "sqlite:///:memory:"
    path = Path(path_text)
    if not path.is_absolute():
        path = Path.cwd() / path
    return database_url if path.exists() else "sqlite:///:memory:"
