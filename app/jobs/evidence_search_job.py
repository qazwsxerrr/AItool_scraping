from __future__ import annotations

import logging
from dataclasses import dataclass, field

from sqlalchemy.orm import Session, sessionmaker

from app.config.settings import Settings
from app.pipeline.evidence import (
    build_direct_evidence_seeds,
    build_evidence_queries,
    classify_evidence_type,
    extract_domain,
)
from app.search.tavily_client import TavilyClient
from app.storage.db import create_engine_from_url, create_session_factory, init_db
from app.storage.repository import EvidenceItemRepository, ExtractedClaimRepository

LOGGER = logging.getLogger(__name__)


@dataclass
class EvidenceSearchJobResult:
    processed: int = 0
    inserted: int = 0
    skipped: int = 0
    failed: int = 0
    errors: list[str] = field(default_factory=list)


def run_evidence_search_job(
    *,
    session_factory: sessionmaker[Session],
    client: TavilyClient,
    limit: int | None = 30,
) -> EvidenceSearchJobResult:
    result = EvidenceSearchJobResult()
    if not client.is_configured:
        raise RuntimeError("Tavily API is not configured")

    with session_factory() as session:
        claim_repo = ExtractedClaimRepository(session)
        evidence_repo = EvidenceItemRepository(session)
        claims = claim_repo.list_pending_for_evidence_search(limit=limit)
        for claim in claims:
            result.processed += 1
            try:
                candidate = claim.candidate_item
                normalized = candidate.normalized_item
                for seed in build_direct_evidence_seeds(
                    source_url=normalized.url,
                    official_url=claim.official_url,
                    github_url=claim.github_url,
                    huggingface_url=claim.huggingface_url,
                    producthunt_url=claim.producthunt_url,
                ):
                    inserted = evidence_repo.insert_if_new(
                        candidate_item_id=claim.candidate_item_id,
                        url=seed.url,
                        evidence_type=seed.evidence_type,
                        title=seed.title,
                        snippet=seed.snippet,
                        source_domain=extract_domain(seed.url),
                        supports_claim="unknown",
                        confidence=seed.confidence,
                        raw_payload=seed.raw_payload,
                    )
                    if inserted.inserted:
                        result.inserted += 1
                    else:
                        result.skipped += 1

                for query in build_evidence_queries(
                    entity_name=claim.entity_name,
                    entity_type=claim.entity_type,
                ):
                    search_response = client.search(query)
                    for item in search_response.results:
                        inserted = evidence_repo.insert_if_new(
                            candidate_item_id=claim.candidate_item_id,
                            url=item.url,
                            evidence_type=classify_evidence_type(item.url),
                            title=item.title,
                            snippet=item.content,
                            source_domain=extract_domain(item.url),
                            supports_claim="unknown",
                            confidence=item.confidence,
                            raw_payload={
                                "provider": "tavily",
                                "query": search_response.query,
                                "request_id": search_response.request_id,
                                "usage": search_response.usage,
                                "result": item.raw_payload,
                            },
                            query=query,
                        )
                        if inserted.inserted:
                            result.inserted += 1
                        else:
                            result.skipped += 1
            except Exception as exc:
                result.failed += 1
                error = f"candidate_item_id={claim.candidate_item_id}: {exc}"
                result.errors.append(error)
                LOGGER.exception("Failed to search evidence for candidate item %s", claim.candidate_item_id)
        session.commit()
    return result


def run_evidence_search_from_settings(
    *,
    settings: Settings,
    limit: int | None = 30,
) -> EvidenceSearchJobResult:
    engine = create_engine_from_url(settings.database_url)
    init_db(engine)
    session_factory = create_session_factory(engine)
    client = TavilyClient.from_settings(settings)
    return run_evidence_search_job(session_factory=session_factory, client=client, limit=limit)
