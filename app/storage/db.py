from __future__ import annotations

import json
from datetime import date, datetime, timezone
from pathlib import Path

from sqlalchemy import Engine, create_engine, delete, inspect, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session, sessionmaker

from app.storage.models import (
    AIItemReview,
    AIItemScreen,
    Base,
    DAILY_EDITION_TIMEZONE,
    DailyEdition,
    DailyEditionReportEntry,
    FetchAttempt,
    IntelEvent,
    IntelEventItem,
    IntelEventStageDSnapshot,
    IntelItem,
    IntelRun,
    IntelRunItem,
    IntelRunStage,
    IntelRunStageAttempt,
    IntelRunStageTask,
)


# These tables are additive durable coordinator state.  ``init_db`` may create
# them in an otherwise complete Stage A/B database.  The DailyEdition upgrade
# is intentionally the one exception: it migrates only final historical daily
# reports, then physically removes superseded raw/intermediate data as requested.
STATE_TABLE_NAMES = frozenset(
    {
        "intel_run_stages",
        "intel_run_stage_tasks",
        "intel_run_stage_attempts",
    }
)
DAILY_EDITION_TABLE_NAMES = frozenset({"daily_editions", "daily_edition_report_entries"})
ADDITIVE_TABLE_NAMES = STATE_TABLE_NAMES | DAILY_EDITION_TABLE_NAMES


def create_engine_from_url(database_url: str, *, echo: bool = False) -> Engine:
    url = make_url(database_url)
    connect_args = {}
    if url.drivername.startswith("sqlite"):
        connect_args["check_same_thread"] = False
        _ensure_sqlite_parent(database_url)
    return create_engine(database_url, echo=echo, future=True, connect_args=connect_args)


def create_session_factory(engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(bind=engine, class_=Session, expire_on_commit=False, autoflush=False, future=True)


def init_db(engine: Engine) -> None:
    migrate_historical_reports = _needs_historical_report_migration(engine)
    if migrate_historical_reports:
        _prepare_historical_report_migration(engine)
    _assert_supported_schema(engine)
    Base.metadata.create_all(bind=engine)
    if migrate_historical_reports:
        _migrate_historical_daily_reports(engine)


def _prepare_historical_report_migration(engine: Engine) -> None:
    """Add only the temporary pointers needed to materialize old reports once."""

    inspector = inspect(engine)
    statements: list[str] = []
    if "intel_runs" in inspector.get_table_names():
        columns = {column["name"] for column in inspector.get_columns("intel_runs")}
        if "edition_id" not in columns:
            statements.append("ALTER TABLE intel_runs ADD COLUMN edition_id INTEGER")
    if "intel_items" in inspector.get_table_names():
        columns = {column["name"] for column in inspector.get_columns("intel_items")}
        if "build_id" not in columns:
            statements.append("ALTER TABLE intel_items ADD COLUMN build_id INTEGER")
    if "intel_events" in inspector.get_table_names():
        columns = {column["name"] for column in inspector.get_columns("intel_events")}
        if "build_id" not in columns:
            statements.append("ALTER TABLE intel_events ADD COLUMN build_id INTEGER")
    with engine.begin() as connection:
        for statement in statements:
            connection.execute(text(statement))


def _needs_historical_report_migration(engine: Engine) -> bool:
    """Whether a complete pre-workspace DB needs its one-way final-report migration."""

    inspector = inspect(engine)
    tables = set(inspector.get_table_names())
    required = {
        "sources",
        "intel_runs",
        "intel_items",
        "intel_events",
        "intel_event_items",
        "intel_event_stage_d_snapshots",
    }
    if not required <= tables:
        return False
    run_columns = {column["name"] for column in inspector.get_columns("intel_runs")}
    item_columns = {column["name"] for column in inspector.get_columns("intel_items")}
    event_columns = {column["name"] for column in inspector.get_columns("intel_events")}
    return (
        "edition_id" not in run_columns
        or "build_id" not in item_columns
        or "build_id" not in event_columns
    )


def _migrate_historical_daily_reports(engine: Engine) -> None:
    """Persist old publishable daily reports, then discard old build state.

    This migration is deliberately destructive *only* for data the new
    product contract no longer retains: raw items, AI projections, events,
    snapshots, tasks, attempts, fetch telemetry and runs.  Sources and the
    newly materialized date-addressed reports survive.
    """

    session_factory = create_session_factory(engine)
    with session_factory() as session:
        runs = list(
            session.scalars(
                select(IntelRun)
                .where(
                    IntelRun.status.in_(("completed", "completed_with_errors")),
                    IntelRun.partial.is_(False),
                )
                .order_by(IntelRun.finished_at.desc(), IntelRun.id.desc())
            ).all()
        )
        latest_by_date: dict[date, IntelRun] = {}
        for run in runs:
            edition_text = _migration_edition_date(run.scope_json, run.started_at)
            if edition_text is None:
                continue
            try:
                edition_date = date.fromisoformat(edition_text)
            except ValueError:
                continue
            latest_by_date.setdefault(edition_date, run)

        for edition_date, run in latest_by_date.items():
            snapshot_rows = list(
                session.execute(
                    select(IntelEventStageDSnapshot, IntelEvent)
                    .join(IntelEvent, IntelEvent.id == IntelEventStageDSnapshot.event_id)
                    .where(
                        IntelEventStageDSnapshot.run_id == int(run.id),
                        # Migration-only predicate for the pre-edition table.
                        text("intel_event_stage_d_snapshots.snapshot_key = :snapshot_key").bindparams(
                            snapshot_key=f"daily-{edition_date.isoformat()}"
                        ),
                        IntelEventStageDSnapshot.selected.is_(True),
                    )
                    .order_by(IntelEventStageDSnapshot.display_order.asc(), IntelEvent.id.asc())
                ).all()
            )
            # A completed report can legitimately contain zero selected
            # events.  It still needs a durable edition row so that no old
            # partial attempt becomes historical output later.
            edition = session.scalar(select(DailyEdition).where(DailyEdition.edition_date == edition_date))
            if edition is None:
                edition = DailyEdition(edition_date=edition_date)
                session.add(edition)
                session.flush()
            for entry in list(edition.report_entries):
                session.delete(entry)
            session.flush()
            for order, (snapshot, event) in enumerate(snapshot_rows, start=1):
                metadata = _public_historical_metadata(_json_object(snapshot.metadata_json))
                primary = session.get(IntelItem, event.primary_item_id) if event.primary_item_id is not None else None
                session.add(
                    DailyEditionReportEntry(
                        edition_id=int(edition.id),
                        event_key=str(event.event_key)[:512],
                        display_order=order,
                        title=str(metadata.get("display_title_zh") or event.title or "(untitled)"),
                        original_title=event.title,
                        summary=event.summary_cn,
                        url=event.canonical_url,
                        display_score=float(snapshot.display_score or event.display_score or 0.0),
                        topic=snapshot.topic or event.topic,
                        content_class=snapshot.content_class or event.content_class,
                        source_group=snapshot.source_group or event.source_group,
                        source_ids_json=event.source_ids_json or "[]",
                        source_refs_json="[]",
                        risk_flags_json=event.risk_flags_json or "[]",
                        keywords_json=event.keywords_json or "[]",
                        entities_json=event.entities_json or "[]",
                        metadata_json=json.dumps(metadata, ensure_ascii=False, sort_keys=True, default=str),
                        published_at=(primary.published_at if primary is not None else event.last_seen_at),
                    )
                )
            edition.draft_run_id = None
            edition.status = "published"
            edition.published_at = run.finished_at or run.started_at
            edition.error = None
        session.commit()

    with session_factory() as session:
        # Child tables must go first.  The final report table deliberately has
        # no foreign keys to any of these transient objects.
        for model in (
            IntelRunStageAttempt,
            IntelRunStageTask,
            IntelRunStage,
            IntelEventStageDSnapshot,
            IntelEventItem,
            AIItemScreen,
            AIItemReview,
            IntelRunItem,
            FetchAttempt,
            IntelEvent,
            IntelItem,
            IntelRun,
        ):
            session.execute(delete(model))
        session.commit()

    _rebuild_private_workspace_tables(engine)


def _rebuild_private_workspace_tables(engine: Engine) -> None:
    """Recreate the now-empty private build workspace with the new schema."""

    if not engine.dialect.name.startswith("sqlite"):
        raise RuntimeError(
            "DailyEdition migration requires rebuilding empty SQLite workspace tables; "
            "reinitialize or migrate this non-SQLite database explicitly."
        )
    with engine.connect() as connection:
        # SQLite ignores PRAGMA foreign_keys changes inside an active
        # transaction. Toggle it before the DDL transaction so deployments
        # that explicitly enable FK checks can still swap the now-empty
        # private workspace tables.
        connection.exec_driver_sql("PRAGMA foreign_keys=OFF")
        connection.commit()
        try:
            with connection.begin():
                for table_name in (
                    "intel_run_stage_attempts",
                    "intel_run_stage_tasks",
                    "intel_run_stages",
                    "intel_event_stage_d_snapshots",
                    "intel_event_items",
                    "ai_item_screens",
                    "ai_item_reviews",
                    "intel_run_items",
                    "fetch_attempts",
                    "intel_events",
                    "intel_items",
                    "intel_runs",
                ):
                    Base.metadata.tables[table_name].drop(bind=connection, checkfirst=True)
                for table_name in (
                    "intel_runs",
                    "intel_items",
                    "fetch_attempts",
                    "ai_item_screens",
                    "ai_item_reviews",
                    "intel_run_items",
                    "intel_events",
                    "intel_event_items",
                    "intel_event_stage_d_snapshots",
                    "intel_run_stages",
                    "intel_run_stage_tasks",
                    "intel_run_stage_attempts",
                ):
                    Base.metadata.tables[table_name].create(bind=connection)
        finally:
            connection.exec_driver_sql("PRAGMA foreign_keys=ON")
            connection.commit()


def _json_object(value: str | None) -> dict[str, object]:
    try:
        parsed = json.loads(value or "{}")
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}
    return dict(parsed) if isinstance(parsed, dict) else {}


def _public_historical_metadata(value: object) -> object:
    """Drop obsolete execution keys while importing a historical report."""

    if isinstance(value, dict):
        return {
            str(key): _public_historical_metadata(child)
            for key, child in value.items()
            if not _historical_execution_key(key)
        }
    if isinstance(value, list):
        return [_public_historical_metadata(child) for child in value]
    if isinstance(value, tuple):
        return [_public_historical_metadata(child) for child in value]
    return value


def _historical_execution_key(key: object) -> bool:
    normalized = str(key).strip().casefold()
    return (
        normalized == "run_id"
        or normalized.endswith("_run_id")
        or normalized == "snapshot_key"
        or normalized.endswith("_snapshot_key")
    )


def _migration_edition_date(scope_json: object, started_at: object) -> str | None:
    """Derive the report date while importing a pre-edition database."""

    scope: object = {}
    try:
        scope = json.loads(str(scope_json or "{}"))
    except (TypeError, ValueError, json.JSONDecodeError):
        scope = {}
    if isinstance(scope, dict):
        value = scope.get("edition_date")
        if value:
            try:
                return date.fromisoformat(str(value)).isoformat()
            except (TypeError, ValueError):
                pass
        reference = scope.get("reference_time")
    else:
        reference = None
    current = _migration_utc_datetime(reference) or _migration_utc_datetime(started_at)
    return current.astimezone(DAILY_EDITION_TIMEZONE).date().isoformat() if current is not None else None


def _migration_utc_datetime(value: object) -> datetime | None:
    if isinstance(value, datetime):
        return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)
    if isinstance(value, date):
        return datetime(value.year, value.month, value.day, tzinfo=timezone.utc)
    if value is None:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=timezone.utc)


def _assert_supported_schema(engine: Engine) -> None:
    """Reject unsupported schemas before normal daily-build execution.

    ``MetaData.create_all`` is intentionally create-only. A complete schema
    created by this metadata is accepted for idempotent startup. The only
    supported conversion is the one-way historical daily-report migration
    prepared before this validation for an eligible older database.
    """

    inspector = inspect(engine)
    existing = set(inspector.get_table_names())
    expected = set(Base.metadata.tables)
    if not existing:
        return

    # Ignore SQLite bookkeeping, but reject databases that are neither the
    # fresh schema nor an eligible one-way historical-report migration target.
    user_tables = {name for name in existing if not name.startswith("sqlite_")}
    # The report migration adds only the coordinator/report tables before
    # final published entries are materialized and private rows are purged.
    missing_tables = (expected - ADDITIVE_TABLE_NAMES) - user_tables
    extra_tables = user_tables - expected
    incompatible_columns: dict[str, set[str]] = {}
    for table_name in expected & user_tables:
        actual_columns = {column["name"] for column in inspector.get_columns(table_name)}
        expected_columns = set(Base.metadata.tables[table_name].columns.keys())
        missing_columns = expected_columns - actual_columns
        if missing_columns:
            incompatible_columns[table_name] = missing_columns

    if missing_tables or extra_tables or incompatible_columns:
        details: list[str] = []
        if missing_tables:
            details.append("missing tables=" + ",".join(sorted(missing_tables)))
        if extra_tables:
            details.append("unexpected tables=" + ",".join(sorted(extra_tables)))
        if incompatible_columns:
            details.extend(
                f"{table} missing columns={','.join(sorted(columns))}"
                for table, columns in sorted(incompatible_columns.items())
            )
        raise RuntimeError(
            "Existing database schema is unsupported by the daily-edition "
            "pipeline (no conversion is available): "
            + "; ".join(details)
            + ". Reinitialize the database explicitly."
        )


def _ensure_sqlite_parent(database_url: str) -> None:
    if database_url in {"sqlite://", "sqlite:///:memory:"}:
        return
    prefix = "sqlite:///"
    if not database_url.startswith(prefix):
        return
    path_text = database_url[len(prefix) :]
    if not path_text or path_text == ":memory:":
        return
    path = Path(path_text)
    if not path.is_absolute():
        path = Path.cwd() / path
    path.parent.mkdir(parents=True, exist_ok=True)
