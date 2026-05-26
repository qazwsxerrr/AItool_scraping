from __future__ import annotations

import json

import typer

from app.config.settings import Settings
from app.jobs.ai_review_job import run_ai_review_from_settings
from app.jobs.ai_verify_job import run_ai_verify_from_settings
from app.jobs.claim_extract_job import run_claim_extract_from_settings
from app.jobs.claim_verify_job import run_claim_verify_from_settings
from app.jobs.evidence_search_job import run_evidence_search_from_settings
from app.jobs.evidence_fetch_job import run_evidence_fetch_from_settings
from app.jobs.evidence_classify_job import run_evidence_classify_from_settings
from app.jobs.entity_resolve_job import run_entity_resolve_from_settings
from app.jobs.feedback_job import add_feedback_from_settings, feedback_summary_from_settings
from app.jobs.fetch_job import run_fetch_from_registry
from app.jobs.invalidate_downstream_job import run_invalidate_downstream_from_settings
from app.jobs.normalize_job import run_normalize_from_settings
from app.jobs.pipeline_run_job import run_daily_from_settings
from app.jobs.prefilter_job import run_prefilter_from_settings
from app.jobs.recommendation_export_job import run_audit_export_from_settings, run_recommendation_export_from_settings
from app.jobs.recommendation_write_job import run_recommendation_write_from_settings
from app.jobs.review_export_job import run_review_export_from_settings
from app.logging_config import configure_logging

app = typer.Typer(help="AI tool intelligence ingestion CLI")


@app.command("fetch")
def fetch(
    limit_per_source: int | None = typer.Option(
        None,
        min=1,
        help="Maximum items to process per source. Defaults to each source's configured default_limit.",
    ),
    source: str | None = typer.Option(None, help="Only fetch one source id."),
    group: str | None = typer.Option(None, help="Only fetch one source group."),
) -> None:
    configure_logging()
    result = run_fetch_from_registry(
        settings=Settings.from_env(),
        limit_per_source=limit_per_source,
        source_filter=source,
        source_group_filter=group,
    )
    for source_id, stats in result.stats.items():
        typer.echo(
            f"{source_id}: fetched={stats.fetched} inserted={stats.inserted} "
            f"skipped={stats.skipped} failed={stats.failed}"
            + (f" error={stats.error}" if stats.error else "")
        )


@app.command("normalize")
def normalize(
    limit: int = typer.Option(100, min=1, help="Maximum raw_items to normalize in this run."),
) -> None:
    configure_logging()
    result = run_normalize_from_settings(settings=Settings.from_env(), limit=limit)
    typer.echo(
        f"processed={result.processed} inserted={result.inserted} "
        f"skipped={result.skipped} failed={result.failed}"
    )


@app.command("prefilter")
def prefilter(
    limit: int = typer.Option(100, min=1, help="Maximum normalized_items to prefilter in this run."),
) -> None:
    configure_logging()
    result = run_prefilter_from_settings(settings=Settings.from_env(), limit=limit)
    typer.echo(
        f"processed={result.processed} kept={result.kept} "
        f"dropped={result.dropped} failed={result.failed}"
    )


@app.command("review-export")
def review_export(
    limit: int = typer.Option(50, min=1, help="Maximum candidate_items to export."),
    output_dir: str = typer.Option("output", help="Directory for Markdown and JSONL review files."),
    status: str = typer.Option("kept", help="Candidate status to export, usually kept or dropped."),
) -> None:
    configure_logging()
    result = run_review_export_from_settings(
        settings=Settings.from_env(),
        output_dir=output_dir,
        limit=limit,
        status=status,
    )
    typer.echo(f"exported={result.exported}")
    typer.echo(f"markdown={result.markdown_path}")
    typer.echo(f"jsonl={result.jsonl_path}")


@app.command("ai-review")
def ai_review(
    limit: int = typer.Option(5, min=1, help="Maximum high-score candidate_items to review with AI."),
    min_score: int | None = typer.Option(
        None,
        min=0,
        max=100,
        help="Minimum candidate_score to send to AI. Defaults to AI_REVIEW_MIN_CANDIDATE_SCORE.",
    ),
) -> None:
    configure_logging()
    result = run_ai_review_from_settings(
        settings=Settings.from_env(),
        limit=limit,
        min_candidate_score=min_score,
    )
    typer.echo(
        f"processed={result.processed} inserted={result.inserted} "
        f"skipped={result.skipped} failed={result.failed}"
    )


@app.command("claim-extract")
def claim_extract(
    limit: int = typer.Option(50, min=1, help="Maximum AI-reviewed candidate_items to extract claims from."),
    min_ai_score: int | None = typer.Option(
        None,
        min=0,
        max=100,
        help="Minimum ai_score to extract claims. Defaults to CLAIM_EXTRACT_MIN_AI_SCORE.",
    ),
) -> None:
    configure_logging()
    result = run_claim_extract_from_settings(
        settings=Settings.from_env(),
        limit=limit,
        min_ai_score=min_ai_score,
    )
    typer.echo(
        f"processed={result.processed} inserted={result.inserted} "
        f"skipped={result.skipped} failed={result.failed}"
    )


@app.command("evidence-search")
def evidence_search(
    limit: int = typer.Option(30, min=1, help="Maximum extracted_claims to search evidence for."),
    max_attempts: int | None = typer.Option(
        None,
        min=1,
        help="Maximum evidence-search attempts per claim. Defaults to EVIDENCE_SEARCH_MAX_ATTEMPTS.",
    ),
) -> None:
    configure_logging()
    result = run_evidence_search_from_settings(settings=Settings.from_env(), limit=limit, max_attempts=max_attempts)
    typer.echo(
        f"processed={result.processed} inserted={result.inserted} "
        f"skipped={result.skipped} failed={result.failed}"
    )


@app.command("evidence-fetch")
def evidence_fetch(
    limit: int = typer.Option(50, min=1, help="Maximum evidence_items to fetch or verify."),
) -> None:
    configure_logging()
    result = run_evidence_fetch_from_settings(settings=Settings.from_env(), limit=limit)
    typer.echo(f"processed={result.processed} updated={result.updated} failed={result.failed}")


@app.command("evidence-classify")
def evidence_classify(
    limit: int = typer.Option(100, min=1, help="Maximum fetched evidence_items to classify."),
    force: bool = typer.Option(False, "--force", help="Reclassify completed evidence_items as well."),
    version: str = typer.Option("rules_v1", "--version", help="Evidence classification rule version."),
) -> None:
    configure_logging()
    result = run_evidence_classify_from_settings(
        settings=Settings.from_env(),
        limit=limit,
        force=force,
        classification_version=version,
    )
    typer.echo(f"processed={result.processed} updated={result.updated} failed={result.failed}")


@app.command("ai-verify")
def ai_verify(
    limit: int = typer.Option(30, min=1, help="Maximum evidence-backed candidates to verify with AI."),
    force: bool = typer.Option(False, "--force", help="Recompute existing AI verification_items."),
    version: str = typer.Option("ai_verify_v1", "--version", help="AI verification version."),
) -> None:
    configure_logging()
    result = run_ai_verify_from_settings(
        settings=Settings.from_env(),
        limit=limit,
        force=force,
        verification_version=version,
    )
    typer.echo(
        f"processed={result.processed} inserted={result.inserted} "
        f"skipped={result.skipped} failed={result.failed}"
    )


@app.command("claim-verify")
def claim_verify(
    limit: int = typer.Option(100, min=1, help="Maximum extracted_claims to verify at claim level."),
    force: bool = typer.Option(False, "--force", help="Recompute existing claim_verification_items."),
    version: str = typer.Option("claim_rules_v1", "--version", help="Claim verification rule version."),
) -> None:
    configure_logging()
    result = run_claim_verify_from_settings(
        settings=Settings.from_env(),
        limit=limit,
        force=force,
        verification_version=version,
    )
    typer.echo(
        f"processed_claims={result.processed_claims} inserted={result.inserted} "
        f"skipped={result.skipped} failed={result.failed}"
    )


@app.command("recommendation-write")
def recommendation_write(
    limit: int = typer.Option(100, min=1, help="Maximum final_keep verification_items to turn into recommendation cards."),
    force: bool = typer.Option(False, "--force", help="Rewrite existing recommendation_cards."),
    version: str = typer.Option("recommendation_writer_v1", "--version", help="Recommendation writer version."),
) -> None:
    configure_logging()
    result = run_recommendation_write_from_settings(
        settings=Settings.from_env(),
        limit=limit,
        force=force,
        writer_version=version,
    )
    typer.echo(
        f"processed={result.processed} inserted={result.inserted} "
        f"skipped={result.skipped} failed={result.failed}"
    )


@app.command("invalidate-downstream")
def invalidate_downstream(
    from_stage: str = typer.Option(
        ...,
        "--from",
        help="Invalidate downstream stages from: evidence, claim-verification, or verification.",
    ),
) -> None:
    configure_logging()
    result = run_invalidate_downstream_from_settings(
        settings=Settings.from_env(),
        from_stage=from_stage,
    )
    typer.echo(
        f"from={result.from_stage} claim_verifications={result.claim_verifications} "
        f"verification_items={result.verification_items} recommendation_cards={result.recommendation_cards}"
    )


@app.command("recommendation-export")
def recommendation_export(
    limit: int = typer.Option(20, min=1, help="Maximum verification_items to export."),
    output_dir: str = typer.Option("output", help="Directory for Markdown and JSONL recommendation files."),
) -> None:
    configure_logging()
    result = run_recommendation_export_from_settings(
        settings=Settings.from_env(),
        output_dir=output_dir,
        limit=limit,
    )
    typer.echo(f"exported={result.exported}")
    typer.echo(f"markdown={result.markdown_path}")
    typer.echo(f"jsonl={result.jsonl_path}")


@app.command("audit-export")
def audit_export(
    limit: int = typer.Option(100, min=1, help="Maximum verification_items to export for audit."),
    output_dir: str = typer.Option("output", help="Directory for Markdown and JSONL audit files."),
) -> None:
    configure_logging()
    result = run_audit_export_from_settings(
        settings=Settings.from_env(),
        output_dir=output_dir,
        limit=limit,
    )
    typer.echo(f"exported={result.exported}")
    typer.echo(f"markdown={result.markdown_path}")
    typer.echo(f"jsonl={result.jsonl_path}")


@app.command("entity-resolve")
def entity_resolve(
    limit: int = typer.Option(100, min=1, help="Maximum verification_items to resolve into canonical entities."),
) -> None:
    configure_logging()
    result = run_entity_resolve_from_settings(settings=Settings.from_env(), limit=limit)
    typer.echo(
        f"processed={result.processed} entities_created={result.entities_created} "
        f"mentions_created={result.mentions_created} failed={result.failed}"
    )


@app.command("run-daily")
def run_daily() -> None:
    configure_logging()
    result = run_daily_from_settings(settings=Settings.from_env())
    typer.echo(f"run_id={result.run_id} status={result.status}")
    if result.error:
        typer.echo(f"error={result.error}")


@app.command("feedback-add")
def feedback_add(
    action: str = typer.Argument(..., help="Feedback action: like/dislike/save/hide/click/report."),
    entity_id: int | None = typer.Option(None, help="Canonical entity id."),
    candidate_item_id: int | None = typer.Option(None, help="Candidate item id."),
    reason: str | None = typer.Option(None, help="Optional feedback reason."),
) -> None:
    configure_logging()
    result = add_feedback_from_settings(
        settings=Settings.from_env(),
        entity_id=entity_id,
        candidate_item_id=candidate_item_id,
        action=action,
        reason=reason,
    )
    typer.echo(f"inserted={result.inserted} feedback_id={result.feedback_id}")


@app.command("feedback-summary")
def feedback_summary_cmd(
    entity_id: int | None = typer.Option(None, help="Canonical entity id."),
    candidate_item_id: int | None = typer.Option(None, help="Candidate item id."),
) -> None:
    configure_logging()
    summary = feedback_summary_from_settings(
        settings=Settings.from_env(),
        entity_id=entity_id,
        candidate_item_id=candidate_item_id,
    )
    typer.echo(json.dumps(summary, ensure_ascii=False))


if __name__ == "__main__":
    app()
