"""Filesystem-isolated daily build and audit workspaces.

The published database is deliberately not a build workspace.  Every
``edition_date`` owns a small directory next to it::

    data/editions/YYYY-MM-DD/
      draft.db  # one pending or retryable full rebuild
      audit.db  # the most recently published full rebuild

``draft.db`` holds raw fetch rows plus all A-D projections while a build is
in progress.  On a successful approval it replaces ``audit.db`` as one unit;
the published database receives only the compact final daily report.  A new
same-day rebuild replaces only ``draft.db`` until it is approved, keeping the
prior public report and its retained audit available for comparison.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import date, datetime
import os
from pathlib import Path
import sqlite3
import shutil
import time
from typing import Iterable
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.exc import OperationalError
from sqlalchemy.engine import make_url
from sqlalchemy.orm import selectinload

from app.config.settings import Settings
from app.storage.db import create_engine_from_url, create_session_factory, init_db
from app.storage.models import DailyEdition, DailyEditionReportEntry


@dataclass(frozen=True)
class DailyAuditPromotion:
    """Reversible replacement of one date's retained SQLite audit."""

    draft_path: Path
    audit_path: Path
    backup_path: Path | None = None


def normalize_draft_edition_date(value: date | str) -> str:
    if isinstance(value, datetime):
        raise ValueError("edition_date must use YYYY-MM-DD")
    if isinstance(value, date):
        return value.isoformat()
    try:
        return date.fromisoformat(str(value)).isoformat()
    except (TypeError, ValueError) as exc:
        raise ValueError("edition_date must use YYYY-MM-DD") from exc


def edition_workspace_path(database_url: str, edition_date: date | str) -> Path:
    """Return the deterministic workspace directory for one daily edition."""

    url = make_url(database_url)
    if not url.drivername.startswith("sqlite") or not url.database or url.database == ":memory:":
        raise ValueError(
            "daily draft workspaces require a file-backed SQLite DATABASE_URL; "
            "use sqlite:///... rather than an in-memory or server database"
        )
    normalized = normalize_draft_edition_date(edition_date)
    published_path = Path(url.database)
    return published_path.parent / "editions" / normalized


def draft_database_path(database_url: str, edition_date: date | str) -> Path:
    """Return the pending-build database path for one daily edition."""

    return edition_workspace_path(database_url, edition_date) / "draft.db"


def audit_database_path(database_url: str, edition_date: date | str) -> Path:
    """Return the retained published-audit database path for one edition."""

    return edition_workspace_path(database_url, edition_date) / "audit.db"


def draft_database_url(database_url: str, edition_date: date | str) -> str:
    """Build a SQLite URL that points to the isolated daily draft database."""

    url = make_url(database_url)
    path = draft_database_path(database_url, edition_date)
    return url.set(database=str(path)).render_as_string(hide_password=False)


def audit_database_url(database_url: str, edition_date: date | str) -> str:
    """Build a SQLite URL that points to a date's retained audit database."""

    url = make_url(database_url)
    path = audit_database_path(database_url, edition_date)
    return url.set(database=str(path)).render_as_string(hide_password=False)


def draft_settings(settings: Settings, edition_date: date | str) -> Settings:
    """Return settings identical to the public configuration except for storage."""

    return replace(settings, database_url=draft_database_url(settings.database_url, edition_date))


def audit_settings(settings: Settings, edition_date: date | str) -> Settings:
    """Return read-only/audit settings for a date's retained full build."""

    return replace(settings, database_url=audit_database_url(settings.database_url, edition_date))


def daily_draft_exists(settings: Settings, edition_date: date | str) -> bool:
    return draft_database_path(settings.database_url, edition_date).exists()


def daily_audit_exists(settings: Settings, edition_date: date | str) -> bool:
    return audit_database_path(settings.database_url, edition_date).exists()


def remove_daily_draft(settings: Settings, edition_date: date | str) -> None:
    """Remove exactly one pending build without touching its retained audit."""

    path = draft_database_path(settings.database_url, edition_date)
    _remove_sqlite_database(path)


def create_daily_draft(settings: Settings, edition_date: date | str) -> Settings:
    """Recreate a date's pending draft and seed only prior final history.

    No row in the published database is changed.  Stage C/D need the previous
    days' final entries for their duplicate checks, so those compact public
    rows are copied into the otherwise-empty ``draft.db``.  Existing
    ``audit.db`` for the same date is deliberately left in place until this
    new build is approved.
    """

    normalized = normalize_draft_edition_date(edition_date)
    target_settings = draft_settings(settings, normalized)
    # SQLite creates the database file itself, but not its parent directory.
    # A fresh installation has no ``data/editions/YYYY-MM-DD`` directory yet,
    # so create exactly this workspace parent before opening ``draft.db``.
    edition_workspace_path(settings.database_url, normalized).mkdir(
        parents=True,
        exist_ok=True,
    )
    remove_daily_draft(settings, normalized)

    target_engine = create_engine_from_url(target_settings.database_url)
    init_db(target_engine)
    target_factory = create_session_factory(target_engine)

    source_engine = create_engine_from_url(settings.database_url)
    source_factory = create_session_factory(source_engine)
    try:
        with source_factory() as source_session:
            rows = list(
                source_session.scalars(
                    select(DailyEdition)
                    .options(selectinload(DailyEdition.report_entries))
                    .where(
                        DailyEdition.edition_date < date.fromisoformat(normalized),
                        DailyEdition.published_at.is_not(None),
                    )
                    .order_by(DailyEdition.edition_date.asc(), DailyEdition.id.asc())
                ).all()
            )
    except OperationalError:
        # A first-ever run may point at a new published database.  The draft
        # has no history in that case; publication will initialize the public
        # report store explicitly.
        rows = []
    finally:
        source_engine.dispose()

    with target_factory() as target_session:
        for source_edition in rows:
            copied_edition = DailyEdition(
                edition_date=source_edition.edition_date,
                status="published",
                published_at=source_edition.published_at,
                error=None,
                created_at=source_edition.created_at,
                updated_at=source_edition.updated_at,
            )
            target_session.add(copied_edition)
            target_session.flush()
            _copy_report_entries(
                target_session,
                edition_id=int(copied_edition.id),
                entries=source_edition.report_entries,
            )
        target_session.commit()
    target_engine.dispose()
    return target_settings


def promote_daily_draft_to_audit(
    settings: Settings,
    edition_date: date | str,
) -> DailyAuditPromotion:
    """Move a complete pending build into the retained audit slot.

    The previous audit is kept as a rollback copy until the public output and
    final-report transaction both succeed.  The checkpointed draft is copied
    through a durable temporary file and then removed, so after publication
    there is no pending build, only the current date's retained audit.
    """

    normalized = normalize_draft_edition_date(edition_date)
    draft = draft_database_path(settings.database_url, normalized)
    audit = audit_database_path(settings.database_url, normalized)
    if not draft.exists():
        raise ValueError(f"daily draft database does not exist: {draft}")

    _prepare_sqlite_database_for_move(draft)
    if audit.exists():
        _prepare_sqlite_database_for_move(audit)
    audit.parent.mkdir(parents=True, exist_ok=True)

    backup: Path | None = None
    if audit.exists():
        backup = audit.parent / f".audit.previous-{uuid4().hex}.db"
    try:
        if backup is not None:
            _copy_sqlite_snapshot(audit, backup)
            _remove_sqlite_database(audit)
        _copy_sqlite_snapshot(draft, audit)
        _remove_sqlite_database(draft)
    except Exception:
        if audit.exists():
            _remove_sqlite_database(audit)
        if backup is not None and backup.exists():
            _copy_sqlite_snapshot(backup, audit)
            _remove_sqlite_database(backup)
        raise
    return DailyAuditPromotion(draft_path=draft, audit_path=audit, backup_path=backup)


def rollback_daily_audit(promotion: DailyAuditPromotion) -> None:
    """Restore the pending build and prior retained audit after a failure."""

    if promotion.audit_path.exists():
        if promotion.draft_path.exists():
            raise RuntimeError(
                f"cannot restore daily draft because its path already exists: {promotion.draft_path}"
            )
        _copy_sqlite_snapshot(promotion.audit_path, promotion.draft_path)
        _remove_sqlite_database(promotion.audit_path)
    if promotion.backup_path is not None and promotion.backup_path.exists():
        _copy_sqlite_snapshot(promotion.backup_path, promotion.audit_path)
        _remove_sqlite_database(promotion.backup_path)


def finalize_daily_audit(promotion: DailyAuditPromotion) -> None:
    """Discard the replaced audit only after public publication is durable."""

    if promotion.backup_path is not None:
        _remove_sqlite_database(promotion.backup_path)


def _prepare_sqlite_database_for_move(path: Path) -> None:
    """Checkpoint a SQLite file so its main DB is a self-contained snapshot."""

    try:
        connection = sqlite3.connect(path)
        try:
            connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        finally:
            connection.close()
    except sqlite3.DatabaseError as exc:
        raise RuntimeError(f"cannot checkpoint daily SQLite workspace {path}: {exc}") from exc
    _remove_sqlite_sidecars(path)


def _copy_sqlite_snapshot(source: Path, destination: Path) -> None:
    """Copy a checkpointed SQLite snapshot through a closed temporary file.

    Moving an open SQLite file with ``os.replace`` is reliable on native Linux
    but can expose an empty or stale inode on WSL's mounted Windows paths.
    Copying the closed snapshot, fsyncing it, and only then replacing the
    destination keeps the audit bytes observable before the operation returns.
    """

    if not source.exists():
        raise FileNotFoundError(source)
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.parent / f".{destination.name}.tmp-{uuid4().hex}"
    try:
        with source.open("rb") as source_file, temporary.open("wb") as target_file:
            shutil.copyfileobj(source_file, target_file, length=1024 * 1024)
            target_file.flush()
            os.fsync(target_file.fileno())
        os.replace(temporary, destination)
        _wait_for_replaced_file(destination)
    except Exception:
        if temporary.exists():
            temporary.unlink()
        raise


def _wait_for_replaced_file(path: Path) -> None:
    """Confirm a renamed audit file is visible before returning to callers.

    WSL mounted Windows directories can briefly delay directory-entry
    visibility after ``os.replace``.  The SQLite move itself is already
    complete; this bounded check only prevents the caller from observing a
    transient ``FileNotFoundError`` immediately after a successful publish.
    """

    for attempt in range(20):
        try:
            with path.open("rb"):
                return
        except FileNotFoundError:
            if attempt == 19:
                raise
            time.sleep(0.01)


def _remove_sqlite_database(path: Path) -> None:
    if path.exists():
        path.unlink()
    _remove_sqlite_sidecars(path)


def _remove_sqlite_sidecars(path: Path) -> None:
    for candidate in (Path(f"{path}-wal"), Path(f"{path}-shm"), Path(f"{path}-journal")):
        if candidate.exists():
            candidate.unlink()


def _copy_report_entries(
    session,
    *,
    edition_id: int,
    entries: Iterable[DailyEditionReportEntry],
) -> None:
    for entry in entries:
        session.add(
            DailyEditionReportEntry(
                edition_id=edition_id,
                event_key=entry.event_key,
                display_order=entry.display_order,
                title=entry.title,
                original_title=entry.original_title,
                summary=entry.summary,
                url=entry.url,
                display_score=entry.display_score,
                topic=entry.topic,
                content_class=entry.content_class,
                source_group=entry.source_group,
                source_ids_json=entry.source_ids_json,
                source_refs_json=entry.source_refs_json,
                risk_flags_json=entry.risk_flags_json,
                keywords_json=entry.keywords_json,
                entities_json=entry.entities_json,
                metadata_json=entry.metadata_json,
                published_at=entry.published_at,
                created_at=entry.created_at,
            )
        )
