"""Pure read/export stage for v2 intelligence records."""

from __future__ import annotations

import json
import hashlib
import os
import tempfile
from collections import Counter
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import or_, select
from sqlalchemy.orm import Session, joinedload, sessionmaker

from app.config.limits import DEFAULT_DAILY_REPORT_LIMIT
from app.config.settings import Settings
from app.domain.recency import RecentWindowDecision, recent_window_decision
from app.github.report import write_github_trending_report
from app.storage.db import create_engine_from_url, create_session_factory, init_db
from app.storage.repository import IntelRepository
from app.storage.models import IntelEvent, IntelEventItem, IntelEventStageDSnapshot, IntelItem, IntelRun, IntelRunItem
from app.storage.run_snapshot_summary import build_run_snapshot_summary


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
    snapshot_key: str = "latest"


def run_intel_export_job(
    *,
    session_factory: sessionmaker[Session],
    output_dir: str | Path = "output/intel",
    limit: int | None = DEFAULT_DAILY_REPORT_LIMIT,
    source_filter: str | None = None,
    content_class: str | None = None,
    dry_run: bool = False,
    github_report_dir: str | Path | None = None,
    snapshot_key: str | None = None,
    run_id: int | None = None,
    partial: bool = False,
    partial_reason: str | None = None,
) -> IntelExportResult:
    final_output = Path(output_dir)
    key = str(snapshot_key or "latest")
    effective_limit = _normalise_export_limit(limit)
    stage = None
    stage_task = None
    run: IntelRun | None = None
    run_snapshot_summary: dict[str, Any] | None = None
    watchlist_records: list[dict[str, Any]] = []
    owner = "intel-export"
    with session_factory() as session:
        try:
            repo = IntelRepository(session)
            run = session.get(IntelRun, int(run_id)) if run_id is not None else None
            # Public daily exports use the date-based Stage-D key.  The run
            # ID stays attached to database rows and stage tasks only.
            key = str(snapshot_key or (run.daily_snapshot_key if run is not None else None) or "latest")
            if run is not None and run_id is not None:
                _ensure_stage_d_ready(repo, int(run_id))
            if run is not None and not dry_run:
                artifact_output = _daily_output_dir(final_output, run)
                stage = repo.ensure_stage(
                    int(run_id),
                    "export",
                    metadata={"snapshot_key": key, "artifact_dir": str(artifact_output)},
                )
            snapshot_rows = _list_export_events(
                session,
                snapshot_key=key,
                limit=effective_limit,
                source_filter=source_filter,
                content_class=content_class,
                run_id=run_id,
                reference_time=run.reference_time if run is not None else None,
            )
            records = snapshot_rows
            if run is not None:
                watchlist_records = _list_watchlist_events(
                    session,
                    snapshot_key=key,
                    source_filter=source_filter,
                    content_class=content_class,
                    run_id=run_id,
                    reference_time=run.reference_time,
                )
            if run is not None and (run.partial or str(run.status).casefold() in {"failed", "partial"}):
                partial = True
                partial_reason = partial_reason or run.partial_reason or f"run_status:{run.status}"
            if stage is not None:
                input_fingerprint = _export_input_fingerprint(records, key)
                stage_task = repo.ensure_stage_task(
                    stage,
                    subject_type="run",
                    subject_id=int(run_id),
                    target_run_id=int(run_id),
                    input_fingerprint=input_fingerprint,
                    config_fingerprint="export-v1",
                )
                claimed = repo.claim_stage_task(
                    stage,
                    task_id=stage_task.id,
                    owner=owner,
                    force=True,
                    input_fingerprint=input_fingerprint,
                    config_fingerprint="export-v1",
                )
                if claimed is None:
                    raise RuntimeError("export stage is already running")
                stage_task = claimed
                # Make the export lease visible before filesystem work starts.
                session.commit()
            if run is not None:
                # The task is still running while files are written.  The
                # manifest describes the terminal artifact state that this
                # export invocation is about to commit, rather than exposing
                # that transient lease to static readers.
                run_snapshot_summary = build_run_snapshot_summary(
                    session,
                    run=run,
                    snapshot_key=key,
                    export_status="partial" if partial else "succeeded",
                    export_error=partial_reason if partial else None,
                )
        except Exception:
            session.rollback()
            raise

    artifact_output = _daily_output_dir(final_output, run) if run is not None else final_output
    jsonl_path = artifact_output / "intel_items.jsonl"
    markdown_path = artifact_output / "intel_digest.md"
    manifest_path = artifact_output / "manifest.json"
    legacy_pending_path = artifact_output / "intel_pending.jsonl"
    report_root = Path(github_report_dir) if github_report_dir else artifact_output / "github-trending"

    status_counts = dict(Counter(str(record.get("status") or "unknown") for record in records))
    failure_counts = {
        status: count
        for status, count in status_counts.items()
        if status in {"failed", "screen_failed", "analysis_failed"}
    }

    github_report_path: Path | None = None
    try:
        if not dry_run and (run_id is not None or not partial):
            jsonl_payload = "".join(json.dumps(record, ensure_ascii=False, default=str) + "\n" for record in records)
            markdown_payload = _markdown(
                records,
                edition_date=run.edition_date if run is not None else None,
                status_counts=status_counts,
                failure_counts=failure_counts,
                watchlist_records=watchlist_records,
                partial=partial,
                partial_reason=partial_reason,
            )
            payloads = {
                jsonl_path: jsonl_payload,
                markdown_path: markdown_payload,
            }
            if run is not None:
                payloads[manifest_path] = _manifest(
                    run=run,
                    records=records,
                    jsonl_payload=jsonl_payload,
                    watchlist_count=len(watchlist_records),
                    partial=partial,
                    partial_reason=partial_reason,
                    run_snapshot_summary=run_snapshot_summary,
                )
            _atomic_write_bundle(payloads)
            _remove_legacy_pending_artifacts(legacy_pending_path)
            github_report_path = write_github_trending_report(
                records,
                output_root=report_root,
                report_date=_edition_report_date(run),
            )
            # A partial/failed upstream run is auditable in its per-run
            # directory but must never replace the last successful digest.
            if run_id is not None and not partial:
                final_payloads = {
                    final_output / "intel_items.jsonl": jsonl_payload,
                    final_output / "intel_digest.md": markdown_payload,
                }
                _atomic_write_bundle(final_payloads)
                _remove_legacy_pending_artifacts(final_output / "intel_pending.jsonl")
        if stage_task is not None:
            with session_factory() as session:
                repo = IntelRepository(session)
                state_stage = repo.get_stage(int(run_id), "export") if run_id is not None else None
                state_task = repo.get_task(state_stage, subject_type="run", subject_id=int(run_id)) if state_stage else None
                if state_task is not None:
                    if partial:
                        repo.fail_stage_task(
                            state_task,
                            error_category="upstream",
                            error_code="partial_upstream",
                            error_message=partial_reason or "upstream stage is partial",
                            retryable=True,
                            owner=owner,
                        )
                    else:
                        repo.complete_stage_task(
                            state_task,
                            owner=owner,
                            result={"exported": len(records), "artifact_dir": str(artifact_output)},
                        )
                session.commit()
    except Exception as exc:
        if stage_task is not None:
            try:
                with session_factory() as session:
                    repo = IntelRepository(session)
                    state_stage = repo.get_stage(int(run_id), "export") if run_id is not None else None
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
        manifest_path=str(manifest_path) if run is not None else None,
        dry_run=dry_run,
        github_report_path=str(github_report_path) if github_report_path else None,
        status_counts=status_counts,
        failure_counts=failure_counts,
        partial=bool(partial),
        partial_reason=partial_reason,
        run_id=run_id,
        snapshot_key=key,
    )


def run_intel_export_from_settings(
    *,
    settings: Settings,
    output_dir: str | Path = "output/intel",
    limit: int | None = DEFAULT_DAILY_REPORT_LIMIT,
    source_filter: str | None = None,
    content_class: str | None = None,
    dry_run: bool = False,
    github_report_dir: str | Path | None = None,
    snapshot_key: str | None = None,
    run_id: int | None = None,
    partial: bool = False,
    partial_reason: str | None = None,
) -> IntelExportResult:
    database_url = _readable_database_url(settings.database_url, dry_run=dry_run)
    engine = create_engine_from_url(database_url)
    if not dry_run or database_url == "sqlite:///:memory:":
        init_db(engine)
    return run_intel_export_job(
        session_factory=create_session_factory(engine),
        output_dir=output_dir,
        limit=_normalise_export_limit(limit),
        source_filter=source_filter,
        content_class=content_class,
        dry_run=dry_run,
        github_report_dir=github_report_dir,
        snapshot_key=snapshot_key,
        run_id=run_id,
        partial=partial,
        partial_reason=partial_reason,
    )


def _daily_output_dir(final_output: Path, run: IntelRun) -> Path:
    """Return the mutable public bundle for one Asia/Shanghai daily edition.

    Run IDs remain immutable database/audit keys.  File artifacts intentionally
    group by the human-facing edition date so a same-day rerun atomically
    replaces the daily bundle rather than creating another ``runs/run-<id>``
    directory.
    """

    base = final_output.parent if final_output.name.casefold() == "intel" else final_output
    edition_date = run.edition_date
    if not edition_date:
        raise ValueError(f"run {run.id} has no valid edition_date")
    return base / "daily" / edition_date


def _edition_report_date(run: IntelRun | None) -> date | None:
    """Keep the optional GitHub report aligned with its daily bundle."""

    if run is None or not run.edition_date:
        return None
    try:
        return date.fromisoformat(run.edition_date)
    except ValueError:
        return None


def _ensure_stage_d_ready(repo: IntelRepository, run_id: int) -> None:
    """Block run-scoped export until Stage D has a complete terminal result.

    Legacy direct exports may not have a ``stage_d`` row at all; those remain
    compatible.  Once the row exists, however, a missing/failed/in-flight
    assessment or composition task must never create or replace artifacts.
    """

    stage = repo.get_stage(int(run_id), "stage_d")
    if stage is None:
        return
    tasks = repo.list_stage_tasks(stage, include_expired=True)
    blocking = [task for task in tasks if str(task.status) in _STAGE_D_BLOCKING_TASK_STATUSES]
    if str(stage.status) != "succeeded" or blocking:
        status = str(stage.status or "unknown")
        task_statuses = ",".join(str(task.status) for task in blocking) or "none"
        raise RuntimeError(
            f"stage_d_incomplete: run {int(run_id)} status={status} blocking_tasks={task_statuses}"
        )


def _manifest(
    *,
    run: IntelRun,
    records: list[dict[str, Any]],
    jsonl_payload: str,
    watchlist_count: int,
    partial: bool,
    partial_reason: str | None,
    run_snapshot_summary: dict[str, Any] | None,
) -> str:
    """Build the machine-readable metadata for a date-addressed export."""

    payload = {
        "schema_version": 5,
        "edition_date": run.edition_date,
        # Older runs did not persist a daily timezone.  Preserve their former
        # UTC interpretation; all newly-created runs explicitly store
        # Asia/Shanghai in ``scope_json``.
        "edition_timezone": str(run.scope.get("edition_timezone") or "UTC"),
        "artifact_status": "partial" if partial else "ready",
        "edition_status": "partial" if partial else "ready",
        "partial": bool(partial),
        "partial_reason": partial_reason,
        "reference_time": _date(run.reference_time),
        "selected_count": len(records),
        "watchlist_count": max(0, int(watchlist_count)),
        "stage_d_count": int((run_snapshot_summary or {}).get("funnel", {}).get("stage_d_total", 0)),
        "stage_d_shortlisted": int((run_snapshot_summary or {}).get("funnel", {}).get("stage_d_shortlisted", 0)),
        "funnel": (run_snapshot_summary or {}).get("funnel", {}),
        "stages": (run_snapshot_summary or {}).get("stages", {}),
        "failure_reasons": (run_snapshot_summary or {}).get("failure_reasons", []),
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
    snapshot_key: str,
) -> str:
    payload = {
        "snapshot_key": snapshot_key,
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


def _remove_legacy_pending_artifacts(*paths: Path) -> None:
    """Remove the retired audit artifact only after a new export succeeds."""

    for path in paths:
        try:
            path.unlink()
        except FileNotFoundError:
            continue


def _list_export_events(
    session: Session,
    *,
    snapshot_key: str,
    limit: int | None,
    source_filter: str | None,
    content_class: str | None,
    run_id: int | None = None,
    reference_time: datetime | None = None,
    selected: bool = True,
) -> list[dict[str, Any]]:
    """Return event records from one snapshot tier.

    The default remains selected-only for JSONL and all existing callers.  The
    export job uses ``selected=False`` only for the private watchlist appendix;
    it never feeds those rows into the public selected JSONL/API surfaces.
    """
    stmt = (
        select(IntelEventStageDSnapshot, IntelEvent)
        .join(IntelEvent, IntelEvent.id == IntelEventStageDSnapshot.event_id)
        .options(
            joinedload(IntelEvent.event_items).joinedload(IntelEventItem.item).joinedload(IntelItem.ai_screen),
            joinedload(IntelEvent.event_items).joinedload(IntelEventItem.item).joinedload(IntelItem.ai_review),
            joinedload(IntelEvent.event_items).joinedload(IntelEventItem.item).joinedload(IntelItem.source),
            joinedload(IntelEvent.event_items).joinedload(IntelEventItem.source),
        )
        .where(
            IntelEventStageDSnapshot.snapshot_key == snapshot_key,
            IntelEventStageDSnapshot.selected.is_(bool(selected)),
        )
        .order_by(IntelEventStageDSnapshot.display_order.asc(), IntelEvent.id.asc())
    )
    if run_id is not None:
        stmt = stmt.where(
            or_(
                IntelEventStageDSnapshot.run_id == int(run_id),
                IntelEventStageDSnapshot.run_id.is_(None),
            )
        )
    rows = list(session.execute(stmt).unique().all())
    records: list[dict[str, Any]] = []
    for snapshot, event in rows:
        freshness = _event_freshness(event, reference_time=reference_time)
        # A well-formed run-scoped event must still pass the frozen item-level
        # gate at export time.  Legacy events without a primary item retain
        # their prior compatibility behavior rather than being invented anew.
        if freshness is not None and not freshness.eligible:
            continue
        record = _serialize_event(snapshot, event, freshness=freshness)
        if content_class and record.get("content_class") != content_class:
            continue
        if source_filter:
            source_ids = record.get("source_ids") if isinstance(record.get("source_ids"), list) else []
            if source_filter not in source_ids:
                continue
        records.append(record)
        if limit is not None and len(records) >= limit:
            break
    return records


def _list_watchlist_events(
    session: Session,
    *,
    snapshot_key: str,
    source_filter: str | None,
    content_class: str | None,
    run_id: int | None = None,
    reference_time: datetime | None = None,
    limit: int = 10,
) -> list[dict[str, Any]]:
    """Return at most ``limit`` public watchlist rows in display order."""

    if limit <= 0:
        return []
    rows = _list_export_events(
        session,
        snapshot_key=snapshot_key,
        limit=None,
        source_filter=source_filter,
        content_class=content_class,
        run_id=run_id,
        reference_time=reference_time,
        selected=False,
    )
    watchlist: list[dict[str, Any]] = []
    for record in rows:
        metadata = record.get("metadata") if isinstance(record.get("metadata"), dict) else {}
        if str(metadata.get("editorial_tier") or "").casefold() != "watchlist":
            continue
        watchlist.append(record)
    watchlist.sort(
        key=lambda record: (
            _as_nonnegative_int(
                (record.get("metadata") or {}).get("watchlist_order")
                if isinstance(record.get("metadata"), dict)
                else None
            )
            or _as_nonnegative_int(record.get("display_order")),
            _as_nonnegative_int(record.get("event_id")),
        )
    )
    return watchlist[:limit]


def _serialize_event(
    snapshot: IntelEventStageDSnapshot,
    event: IntelEvent,
    *,
    freshness: RecentWindowDecision | None = None,
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
            }
        )
    risk_flags = _json_list(event.risk_flags_json)
    raw_metadata = _json(snapshot.metadata_json, {})
    metadata = _public_value(raw_metadata)
    display_title = raw_metadata.get("display_title_zh") if isinstance(raw_metadata, dict) else None
    display_title = str(display_title).strip() if display_title else None
    presentation = raw_metadata.get("source_presentation") if isinstance(raw_metadata, dict) else None
    labels = {
        "community_signal_pending_verification": ["社区线索 / 待核实"],
        "multi_community_signal_pending_verification": ["多源社区线索 / 待核实"],
    }.get(str(presentation), [])
    provenance_kind = "new" if event.new_in_run_id == snapshot.run_id else "repeat"
    return {
        "record_type": "intel_event",
        "stage": "stage_d",
        "event_id": event.id,
        "id": event.id,
        "display_order": snapshot.display_order,
        "selected": bool(snapshot.selected),
        "status": "selected" if snapshot.selected else "rejected",
        "display_score": float(snapshot.display_score or event.display_score or 0.0),
        "selection_score": float(snapshot.display_score or event.display_score or 0.0),
        "topic": snapshot.topic or event.topic,
        "topics": _json_list(event.topics_json),
        "content_class": snapshot.content_class or event.content_class,
        "source_group": snapshot.source_group or event.source_group or (source_groups[0] if source_groups else None),
        "source_groups": source_groups,
        "source_ids": source_ids,
        "title": display_title or event.title,
        "original_title": event.title,
        "summary": event.summary_cn,
        "summary_cn": event.summary_cn,
        "url": event.canonical_url,
        "canonical_url": event.canonical_url,
        "provenance": {
            "kind": provenance_kind,
        },
        "risk_flags": risk_flags,
        "reason": snapshot.reason,
        "metadata": metadata,
        "story_family_id": raw_metadata.get("story_family_id") if isinstance(raw_metadata, dict) else None,
        "family_position": raw_metadata.get("family_position") if isinstance(raw_metadata, dict) else None,
        "editorial_score": raw_metadata.get("editorial_score") if isinstance(raw_metadata, dict) else None,
        "presentation_labels": labels,
        "keywords": _json_list(event.keywords_json),
        "entities": _public_value(_json(event.entities_json, [])),
        "source_refs": source_refs,
        "member_statuses": sorted(set(member_statuses)),
        "screen_audit": screen_audit,
        "first_seen_at": _date(event.first_seen_at),
        "last_seen_at": _date(event.last_seen_at),
        "published_at": _date(primary_item.published_at) if primary_item is not None else None,
        "captured_at": _date(primary_item.captured_at) if primary_item is not None else None,
        "freshness": freshness.metadata() if freshness is not None else None,
    }


def _event_freshness(
    event: IntelEvent,
    *,
    reference_time: datetime | None,
) -> RecentWindowDecision | None:
    if reference_time is None:
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
    return recent_window_decision(
        primary,
        source=primary.source,
        reference_time=reference_time,
    )


def _markdown(
    records: list[dict[str, Any]],
    *,
    edition_date: str | None = None,
    status_counts: dict[str, int] | None = None,
    failure_counts: dict[str, int] | None = None,
    watchlist_records: list[dict[str, Any]] | None = None,
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
    if counts:
        lines.append("主题分类统计：" + "、".join(f"{key}={value}" for key, value in sorted(counts.items())))
        lines.append("")
    if status_counts:
        lines.append("状态统计：" + "、".join(f"{key}={value}" for key, value in sorted(status_counts.items())))
        lines.append("失败统计：" + ("、".join(f"{key}={value}" for key, value in sorted((failure_counts or {}).items())) or "无"))
        lines.append("")
    for index, record in enumerate(records, start=1):
        if record.get("record_type") == "intel_event":
            lines.extend(
                [
                    f"## {index}. {record.get('title') or '(untitled)'}",
                    (
                        f"- 事件：`{record.get('event_id')}` | 展示顺序：`{record.get('display_order')}` | "
                        f"display_score=`{record.get('display_score')}` | topic=`{record.get('topic')}`"
                    ),
                    (
                        f"- 类别：`{record.get('content_class')}` | 来源组：`{record.get('source_group') or '-'}`"
                        f" | 状态：`selected`"
                    ),
                    _event_time_line(record),
                    f"- 摘要：{record.get('summary_cn') or record.get('summary') or '暂无摘要'}",
                    f"- 风险：{', '.join(record.get('risk_flags') or []) or '无'}",
                    *([f"- 展示标签：{', '.join(record.get('presentation_labels') or [])}"] if record.get("presentation_labels") else []),
                    f"- 链接：{record.get('url') or '无'}",
                    "",
                ]
            )
            continue
    watchlist = list(watchlist_records or [])[:10]
    if watchlist:
        lines.extend(["## 候选观察", "", "以下条目已进入观察池，未计入日报精选。", ""])
        for index, record in enumerate(watchlist, start=1):
            metadata = record.get("metadata") if isinstance(record.get("metadata"), dict) else {}
            reason_codes = metadata.get("reason_codes") if isinstance(metadata.get("reason_codes"), list) else []
            reason = metadata.get("editorial_reason") or record.get("reason") or "暂无原因"
            if reason_codes:
                reason = f"{reason}（{', '.join(str(code) for code in reason_codes)}）"
            lines.extend(
                [
                    f"### {index}. {record.get('title') or '(untitled)' }",
                    f"- 摘要：{record.get('summary_cn') or record.get('summary') or '暂无摘要'}",
                    f"- 原因：{reason}",
                    f"- 链接：{record.get('url') or '无'}",
                    "",
                ]
            )
    return "\n".join(lines) + "\n"


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
    """Remove internal execution identifiers from exportable nested JSON."""

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
        or normalized == "snapshot_key"
        or normalized.endswith("_snapshot_key")
    )


def _normalise_export_limit(value: int | None) -> int:
    """Use the configured default while allowing explicit caller overrides."""

    if value is None:
        return DEFAULT_DAILY_REPORT_LIMIT
    try:
        return max(0, int(value))
    except (TypeError, ValueError, OverflowError):
        return DEFAULT_DAILY_REPORT_LIMIT


def _as_nonnegative_int(value: object) -> int:
    try:
        return max(0, int(value))
    except (TypeError, ValueError, OverflowError):
        return 0


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
