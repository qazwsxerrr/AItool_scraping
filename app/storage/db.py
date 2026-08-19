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
# reports, then physically removes legacy raw/intermediate data as requested.
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
    migrate_legacy_daily_workspace = _needs_daily_workspace_migration(engine)
    _upgrade_intel_run_edition_date(engine)
    _upgrade_daily_workspace_columns(engine)
    _assert_fresh_or_compatible_schema(engine)
    Base.metadata.create_all(bind=engine)
    _ensure_intel_run_edition_date_index(engine)
    if migrate_legacy_daily_workspace:
        _migrate_legacy_daily_workspace(engine)


def _upgrade_intel_run_edition_date(engine: Engine) -> None:
    """Add and backfill the indexed daily label on an otherwise complete DB.

    The project normally rejects arbitrary legacy schemas rather than trying
    to repair them.  This is intentionally narrower: only a complete current
    schema missing *exactly* the additive ``intel_runs.edition_date`` column
    is upgraded.  That keeps existing local databases usable without changing
    run IDs, foreign keys, or historical audit rows.
    """

    inspector = inspect(engine)
    if "intel_runs" not in inspector.get_table_names():
        return
    actual = {column["name"] for column in inspector.get_columns("intel_runs")}
    if "edition_date" in actual:
        return

    with engine.begin() as connection:
        connection.execute(text("ALTER TABLE intel_runs ADD COLUMN edition_date DATE"))
        rows = connection.execute(
            text(
                "SELECT id, scope_json, started_at "
                "FROM intel_runs WHERE edition_date IS NULL"
            )
        ).mappings()
        updates = [
            {"id": int(row["id"]), "edition_date": value}
            for row in rows
            if (value := _legacy_edition_date(row.get("scope_json"), row.get("started_at"))) is not None
        ]
        if updates:
            connection.execute(
                text("UPDATE intel_runs SET edition_date = :edition_date WHERE id = :id"),
                updates,
            )


def _needs_daily_workspace_migration(engine: Engine) -> bool:
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


def _upgrade_daily_workspace_columns(engine: Engine) -> None:
    """Add the private workspace pointers before ORM-backed migration runs."""

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
    if not statements:
        return
    with engine.begin() as connection:
        for statement in statements:
            connection.execute(text(statement))


def _ensure_intel_run_edition_date_index(engine: Engine) -> None:
    """Create the new date index for both fresh and additive-upgrade DBs."""

    inspector = inspect(engine)
    if "intel_runs" not in inspector.get_table_names():
        return
    columns = {column["name"] for column in inspector.get_columns("intel_runs")}
    if "edition_date" not in columns:
        return
    table = Base.metadata.tables["intel_runs"]
    index = next((value for value in table.indexes if value.name == "ix_intel_runs_edition_date"), None)
    if index is None:
        return
    with engine.begin() as connection:
        index.create(bind=connection, checkfirst=True)


def _migrate_legacy_daily_workspace(engine: Engine) -> None:
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
                    IntelRun._edition_date.is_not(None),
                    IntelRun.status.in_(("completed", "completed_with_errors")),
                    IntelRun.partial.is_(False),
                )
                .order_by(IntelRun._edition_date.desc(), IntelRun.finished_at.desc(), IntelRun.id.desc())
            ).all()
        )
        latest_by_date: dict[date, IntelRun] = {}
        for run in runs:
            if run._edition_date is not None:
                latest_by_date.setdefault(run._edition_date, run)

        for edition_date, run in latest_by_date.items():
            snapshot_rows = list(
                session.execute(
                    select(IntelEventStageDSnapshot, IntelEvent)
                    .join(IntelEvent, IntelEvent.id == IntelEventStageDSnapshot.event_id)
                    .where(
                        IntelEventStageDSnapshot.run_id == int(run.id),
                        IntelEventStageDSnapshot.snapshot_key == run.daily_snapshot_key,
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
                metadata = _json_object(snapshot.metadata_json)
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
    """Recreate empty item/event tables with build-scoped unique constraints."""

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
                Base.metadata.tables["intel_events"].drop(bind=connection, checkfirst=True)
                Base.metadata.tables["intel_items"].drop(bind=connection, checkfirst=True)
                Base.metadata.tables["intel_items"].create(bind=connection)
                Base.metadata.tables["intel_events"].create(bind=connection)
        finally:
            connection.exec_driver_sql("PRAGMA foreign_keys=ON")
            connection.commit()


def _json_object(value: str | None) -> dict[str, object]:
    try:
        parsed = json.loads(value or "{}")
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}
    return dict(parsed) if isinstance(parsed, dict) else {}


def _legacy_edition_date(scope_json: object, started_at: object) -> str | None:
    """Derive a Shanghai daily label for a pre-column run row."""

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
    current = _as_utc_datetime(reference) or _as_utc_datetime(started_at)
    return current.astimezone(DAILY_EDITION_TIMEZONE).date().isoformat() if current is not None else None


def _as_utc_datetime(value: object) -> datetime | None:
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


def _assert_fresh_or_compatible_schema(engine: Engine) -> None:
    """Reject pre-Stage-A databases instead of mutating them in place.

    ``MetaData.create_all`` is intentionally create-only.  Existing local
    SQLite files from the old single-call triage pipeline are not migrated or
    backfilled; callers must explicitly remove/reinitialize those files.  A
    complete schema created by this metadata is accepted for idempotent
    startup.  The only migration exception is the one-column, additive run
    edition upgrade performed before this compatibility check.
    """

    inspector = inspect(engine)
    existing = set(inspector.get_table_names())
    expected = set(Base.metadata.tables)
    if not existing:
        return

    # Ignore SQLite's internal bookkeeping tables, but reject any user table
    # set that is not the fresh contract.  This catches both old schemas with
    # missing Stage A/B columns and partial databases that would otherwise
    # fail later on the first query.
    user_tables = {name for name in existing if not name.startswith("sqlite_")}
    # Existing databases from before resumable stages are accepted when their
    # complete legacy contract is present.  Missing coordinator tables are
    # additive and will be created by ``create_all`` below.
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
            "Existing database schema is incompatible with the fresh Stage A/B "
            "intelligence schema (no migrations/backfill are supported): "
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
