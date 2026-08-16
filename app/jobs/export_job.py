"""Pure read/export stage for v2 intelligence records."""

from __future__ import annotations

import json
import hashlib
import os
import tempfile
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime
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
from app.storage.models import IntelEvent, IntelEventItem, IntelEventRankingSnapshot, IntelItem, IntelRun, IntelRunItem


@dataclass(frozen=True)
class IntelExportResult:
    exported: int
    jsonl_path: str
    markdown_path: str
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
    key = str(snapshot_key or (f"run-{int(run_id)}" if run_id is not None else "latest"))
    run_output = _run_output_dir(final_output, run_id)
    artifact_output = run_output if run_id is not None else final_output
    effective_limit = _normalise_export_limit(limit)
    jsonl_path = artifact_output / "intel_items.jsonl"
    markdown_path = artifact_output / "intel_digest.md"
    legacy_pending_path = artifact_output / "intel_pending.jsonl"
    report_root = Path(github_report_dir) if github_report_dir else artifact_output / "github-trending"
    stage = None
    stage_task = None
    owner = "intel-export"
    with session_factory() as session:
        try:
            repo = IntelRepository(session)
            run = session.get(IntelRun, int(run_id)) if run_id is not None else None
            if run is not None and not dry_run:
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
        except Exception:
            session.rollback()
            raise

    status_counts = dict(Counter(str(record.get("status") or "unknown") for record in records))
    failure_counts = {
        status: count
        for status, count in status_counts.items()
        if status in {"failed", "screen_failed", "analysis_failed"}
    }

    github_report_path: Path | None = None
    try:
        if not dry_run and (run_id is not None or not partial):
            payloads = {
                jsonl_path: "".join(json.dumps(record, ensure_ascii=False, default=str) + "\n" for record in records),
                markdown_path: _markdown(
                    records,
                    status_counts=status_counts,
                    failure_counts=failure_counts,
                    partial=partial,
                    partial_reason=partial_reason,
                ),
            }
            _atomic_write_bundle(payloads)
            _remove_legacy_pending_artifacts(legacy_pending_path)
            github_report_path = write_github_trending_report(records, output_root=report_root)
            # A partial/failed upstream run is auditable in its per-run
            # directory but must never replace the last successful digest.
            if run_id is not None and not partial:
                final_payloads = {
                    final_output / "intel_items.jsonl": payloads[jsonl_path],
                    final_output / "intel_digest.md": payloads[markdown_path],
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


def _run_output_dir(final_output: Path, run_id: int | None) -> Path:
    if run_id is None:
        return final_output
    base = final_output.parent if final_output.name.casefold() == "intel" else final_output
    return base / "runs" / f"run-{int(run_id)}"


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
) -> list[dict[str, Any]]:
    """Return selected event records from the requested snapshot only."""
    stmt = (
        select(IntelEventRankingSnapshot, IntelEvent)
        .join(IntelEvent, IntelEvent.id == IntelEventRankingSnapshot.event_id)
        .options(
            joinedload(IntelEvent.event_items).joinedload(IntelEventItem.item).joinedload(IntelItem.ai_screen),
            joinedload(IntelEvent.event_items).joinedload(IntelEventItem.item).joinedload(IntelItem.ai_review),
            joinedload(IntelEvent.event_items).joinedload(IntelEventItem.item).joinedload(IntelItem.source),
            joinedload(IntelEvent.event_items).joinedload(IntelEventItem.source),
        )
        .where(
            IntelEventRankingSnapshot.snapshot_key == snapshot_key,
            IntelEventRankingSnapshot.selected.is_(True),
        )
        .order_by(IntelEventRankingSnapshot.rank.asc(), IntelEvent.id.asc())
    )
    if run_id is not None:
        stmt = stmt.where(
            or_(
                IntelEventRankingSnapshot.run_id == int(run_id),
                IntelEventRankingSnapshot.run_id.is_(None),
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


def _serialize_event(
    snapshot: IntelEventRankingSnapshot,
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
                "lineage": _json(relation.lineage_json, {}),
            }
        )
    risk_flags = _json_list(event.risk_flags_json)
    metadata = _json(snapshot.metadata_json, {})
    provenance_kind = "new" if event.new_in_run_id == snapshot.run_id else "repeat"
    return {
        "record_type": "intel_event",
        "stage": "editorial_rank",
        "event_id": event.id,
        "id": event.id,
        "rank": snapshot.rank,
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
        "title": event.title,
        "summary": event.summary_cn,
        "summary_cn": event.summary_cn,
        "url": event.canonical_url,
        "canonical_url": event.canonical_url,
        "provenance": {
            "kind": provenance_kind,
            "new_in_run_id": event.new_in_run_id,
            "snapshot_run_id": snapshot.run_id,
        },
        "risk_flags": risk_flags,
        "reason": snapshot.reason,
        "metadata": metadata,
        "keywords": _json_list(event.keywords_json),
        "entities": _json(event.entities_json, []),
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
    status_counts: dict[str, int] | None = None,
    failure_counts: dict[str, int] | None = None,
    partial: bool = False,
    partial_reason: str | None = None,
) -> str:
    counts: dict[str, int] = {}
    for record in records:
        key = str(record.get("topic_category") or record.get("content_class") or "未分类")
        counts[key] = counts.get(key, 0) + 1
    lines = ["# AI 情报导出", "", f"保留条目：{len(records)}", ""]
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
                        f"- 事件：`{record.get('event_id')}` | 排名：`{record.get('rank')}` | "
                        f"display_score=`{record.get('display_score')}` | topic=`{record.get('topic')}`"
                    ),
                    (
                        f"- 类别：`{record.get('content_class')}` | 来源组：`{record.get('source_group') or '-'}`"
                        f" | 状态：`selected`"
                    ),
                    _event_time_line(record),
                    f"- 摘要：{record.get('summary_cn') or record.get('summary') or '暂无摘要'}",
                    f"- 风险：{', '.join(record.get('risk_flags') or []) or '无'}",
                    f"- 链接：{record.get('url') or '无'}",
                    "",
                ]
            )
            continue
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
