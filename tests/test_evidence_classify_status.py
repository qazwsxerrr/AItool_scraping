from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app.evidence.classifier import EvidenceClassification
from app.jobs.evidence_classify_job import run_evidence_classify_job
from app.storage.db import create_engine_from_url, create_session_factory, init_db
from app.storage.models import CandidateItem, EvidenceItem, NormalizedItem, RawItem, Source
from app.storage.repository import EvidenceItemRepository


def test_evidence_classify_skips_completed_items(monkeypatch, tmp_path):
    session_factory = _seed_two_fetched_evidence(tmp_path / "skip_completed.db")
    calls: list[int] = []

    def fake_classify(evidence: EvidenceItem) -> EvidenceClassification:
        calls.append(evidence.id)
        return EvidenceClassification("support", 78, [], ["direct_support"])

    monkeypatch.setattr("app.jobs.evidence_classify_job.classify_evidence", fake_classify)

    result = run_evidence_classify_job(session_factory=session_factory, limit=20)

    with session_factory() as session:
        pending = session.query(EvidenceItem).filter_by(url="https://example.com/pending").one()
        completed = session.query(EvidenceItem).filter_by(url="https://example.com/completed").one()

    assert result.processed == 1
    assert calls == [pending.id]
    assert pending.classify_status == "completed"
    assert completed.classify_status == "completed"


def test_fetch_success_resets_previous_classification_state(tmp_path):
    session_factory = _seed_one_evidence(tmp_path / "fetch_reset.db", classify_status="completed")
    old_time = datetime(2026, 5, 1, 8, 0, tzinfo=timezone.utc)

    with session_factory() as session:
        evidence = session.query(EvidenceItem).one()
        evidence.classified_at = old_time
        evidence.classify_error = "old error"
        evidence.classification_version = "rules_old"
        evidence.updated_at = old_time
        session.commit()
        evidence_id = evidence.id

    with session_factory() as session:
        EvidenceItemRepository(session).update_fetch_result(
            evidence_id=evidence_id,
            http_status=200,
            final_url="https://example.com/fresh",
            url_validation_status="reachable",
            fetched_title="Fresh Evidence",
            fetched_description="fresh description",
            fetched_text_preview="fresh install usage evidence",
            raw_payload={"provider": "http", "version": 2},
            fetch_status="completed",
            fetch_error=None,
        )
        session.commit()

    with session_factory() as session:
        evidence = session.get(EvidenceItem, evidence_id)
        assert evidence.classify_status == "pending"
        assert evidence.classified_at is None
        assert evidence.classify_error is None
        assert evidence.classification_version == "rules_old"
        assert evidence.updated_at.replace(tzinfo=timezone.utc) > old_time


def test_evidence_classify_success_records_status_time_and_version(monkeypatch, tmp_path):
    session_factory = _seed_one_evidence(tmp_path / "classify_success.db", classify_status="pending")

    def fake_classify(evidence: EvidenceItem) -> EvidenceClassification:
        return EvidenceClassification("support", 91, [], ["direct_support"])

    monkeypatch.setattr("app.jobs.evidence_classify_job.classify_evidence", fake_classify)

    result = run_evidence_classify_job(session_factory=session_factory, limit=10, classification_version="rules_v2")

    assert result.processed == 1
    assert result.updated == 1
    assert result.failed == 0
    with session_factory() as session:
        evidence = session.query(EvidenceItem).one()
        assert evidence.supports_claim == "support"
        assert evidence.classify_status == "completed"
        assert evidence.classified_at is not None
        assert evidence.classify_error is None
        assert evidence.classification_version == "rules_v2"
        assert evidence.updated_at is not None


def test_evidence_classify_recomputes_completed_item_when_force_is_enabled(monkeypatch, tmp_path):
    session_factory = _seed_one_evidence(tmp_path / "classify_force.db", classify_status="completed")
    calls: list[int] = []

    def fake_classify(evidence: EvidenceItem) -> EvidenceClassification:
        calls.append(evidence.id)
        return EvidenceClassification("support", 88, [], ["forced_recheck"])

    monkeypatch.setattr("app.jobs.evidence_classify_job.classify_evidence", fake_classify)

    result = run_evidence_classify_job(
        session_factory=session_factory,
        limit=10,
        force=True,
        classification_version="rules_v1",
    )

    assert result.processed == 1
    assert calls


def test_evidence_classify_recomputes_completed_item_when_version_changes(monkeypatch, tmp_path):
    session_factory = _seed_one_evidence(tmp_path / "classify_version_change.db", classify_status="completed")

    def fake_classify(evidence: EvidenceItem) -> EvidenceClassification:
        return EvidenceClassification("support", 89, [], ["version_recheck"])

    monkeypatch.setattr("app.jobs.evidence_classify_job.classify_evidence", fake_classify)

    result = run_evidence_classify_job(
        session_factory=session_factory,
        limit=10,
        classification_version="rules_v2",
    )

    assert result.processed == 1
    with session_factory() as session:
        evidence = session.query(EvidenceItem).one()
        assert evidence.classification_version == "rules_v2"


def test_evidence_classify_failure_records_retryable_error(monkeypatch, tmp_path):
    session_factory = _seed_one_evidence(tmp_path / "classify_failed.db", classify_status="pending")

    def broken_classify(evidence: EvidenceItem) -> EvidenceClassification:
        raise RuntimeError("bad evidence payload")

    monkeypatch.setattr("app.jobs.evidence_classify_job.classify_evidence", broken_classify)

    result = run_evidence_classify_job(session_factory=session_factory, limit=10)

    assert result.processed == 1
    assert result.updated == 0
    assert result.failed == 1
    with session_factory() as session:
        evidence = session.query(EvidenceItem).one()
        assert evidence.classify_status == "failed"
        assert "bad evidence payload" in evidence.classify_error
        assert evidence.updated_at is not None


def _seed_two_fetched_evidence(db_path):
    session_factory = _session_factory(db_path)
    with session_factory() as session:
        candidate = _seed_candidate(session)
        session.add(
            EvidenceItem(
                candidate_item_id=candidate.id,
                evidence_type="official_page",
                url="https://example.com/completed",
                title="Completed",
                snippet="already classified",
                source_domain="example.com",
                supports_claim="support",
                confidence=80,
                retrieval_score=80,
                evidence_confidence=80,
                raw_payload="{}",
                fetch_status="completed",
                classify_status="completed",
                classified_at=datetime.now(timezone.utc),
            )
        )
        session.add(
            EvidenceItem(
                candidate_item_id=candidate.id,
                evidence_type="official_page",
                url="https://example.com/pending",
                title="Pending",
                snippet="needs classification",
                source_domain="example.com",
                supports_claim="unknown",
                confidence=30,
                retrieval_score=70,
                evidence_confidence=30,
                raw_payload="{}",
                fetch_status="completed",
                classify_status="pending",
            )
        )
        session.commit()
    return session_factory


def _seed_one_evidence(db_path, *, classify_status: str):
    session_factory = _session_factory(db_path)
    with session_factory() as session:
        candidate = _seed_candidate(session)
        session.add(
            EvidenceItem(
                candidate_item_id=candidate.id,
                evidence_type="official_page",
                url="https://example.com/evidence",
                title="Evidence",
                snippet="install usage quickstart",
                source_domain="example.com",
                supports_claim="unknown",
                confidence=30,
                retrieval_score=70,
                evidence_confidence=30,
                raw_payload="{}",
                fetch_status="completed",
                classify_status=classify_status,
            )
        )
        session.commit()
    return session_factory


def _session_factory(db_path):
    engine = create_engine_from_url(f"sqlite:///{db_path}")
    init_db(engine)
    return create_session_factory(engine)


def _seed_candidate(session) -> CandidateItem:
    source = Source(
        id="source_test",
        name="Source Test",
        type="atom",
        url="https://example.com/feed.xml",
        enabled=True,
        priority=1,
        fetch_interval=3600,
        parser_type="feedparser",
        source_group="official_blog",
        source_subtype="fixed",
    )
    session.add(source)
    session.flush()
    raw = RawItem(
        source_id=source.id,
        external_id="raw-1",
        title="Example tool released",
        link="https://example.com/post",
        raw_payload="{}",
        content_hash="hash-1",
        status="normalized",
    )
    session.add(raw)
    session.flush()
    normalized = NormalizedItem(
        raw_item_id=raw.id,
        title=raw.title,
        body_text="Example tool released with install usage docs.",
        url=raw.link,
        language="en",
        dedupe_key=f"dedupe-{db_safe_now()}",
    )
    session.add(normalized)
    session.flush()
    candidate = CandidateItem(
        normalized_item_id=normalized.id,
        source_group="official_blog",
        source_subtype="fixed",
        candidate_score=90,
        matched_keywords='["ai"]',
        status="kept",
    )
    session.add(candidate)
    session.flush()
    return candidate


def db_safe_now() -> str:
    return datetime.now(timezone.utc).isoformat() + str(timedelta(microseconds=1))
