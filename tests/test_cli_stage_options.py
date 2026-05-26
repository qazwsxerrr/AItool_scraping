from __future__ import annotations

from dataclasses import dataclass

from typer.testing import CliRunner

from app import main


runner = CliRunner()


@dataclass
class _EvidenceClassifyResult:
    processed: int = 0
    updated: int = 0
    failed: int = 0


@dataclass
class _ClaimVerifyResult:
    processed_claims: int = 0
    inserted: int = 0
    skipped: int = 0
    failed: int = 0


@dataclass
class _GenericStageResult:
    processed: int = 0
    inserted: int = 0
    skipped: int = 0
    failed: int = 0


@dataclass
class _InvalidateResult:
    from_stage: str
    claim_verifications: int = 0
    verification_items: int = 0
    recommendation_cards: int = 0


def test_evidence_classify_cli_passes_force_and_version(monkeypatch):
    captured: dict = {}
    settings = object()
    monkeypatch.setattr(main.Settings, "from_env", lambda: settings)

    def fake_run(*, settings, limit, force, classification_version):
        captured.update(
            settings=settings,
            limit=limit,
            force=force,
            classification_version=classification_version,
        )
        return _EvidenceClassifyResult()

    monkeypatch.setattr(main, "run_evidence_classify_from_settings", fake_run)

    result = runner.invoke(main.app, ["evidence-classify", "--limit", "7", "--force", "--version", "rules_v2"])

    assert result.exit_code == 0
    assert captured == {
        "settings": settings,
        "limit": 7,
        "force": True,
        "classification_version": "rules_v2",
    }


def test_claim_verify_cli_passes_force_and_version(monkeypatch):
    captured: dict = {}
    settings = object()
    monkeypatch.setattr(main.Settings, "from_env", lambda: settings)

    def fake_run(*, settings, limit, force, verification_version):
        captured.update(settings=settings, limit=limit, force=force, verification_version=verification_version)
        return _ClaimVerifyResult()

    monkeypatch.setattr(main, "run_claim_verify_from_settings", fake_run)

    result = runner.invoke(main.app, ["claim-verify", "--limit", "8", "--force", "--version", "claim_rules_v2"])

    assert result.exit_code == 0
    assert captured == {
        "settings": settings,
        "limit": 8,
        "force": True,
        "verification_version": "claim_rules_v2",
    }


def test_ai_verify_cli_passes_force_and_version(monkeypatch):
    captured: dict = {}
    settings = object()
    monkeypatch.setattr(main.Settings, "from_env", lambda: settings)

    def fake_run(*, settings, limit, force, verification_version):
        captured.update(settings=settings, limit=limit, force=force, verification_version=verification_version)
        return _GenericStageResult()

    monkeypatch.setattr(main, "run_ai_verify_from_settings", fake_run)

    result = runner.invoke(main.app, ["ai-verify", "--limit", "9", "--force", "--version", "ai_verify_v2"])

    assert result.exit_code == 0
    assert captured == {
        "settings": settings,
        "limit": 9,
        "force": True,
        "verification_version": "ai_verify_v2",
    }


def test_recommendation_write_cli_passes_force_and_version(monkeypatch):
    captured: dict = {}
    settings = object()
    monkeypatch.setattr(main.Settings, "from_env", lambda: settings)

    def fake_run(*, settings, limit, force, writer_version):
        captured.update(settings=settings, limit=limit, force=force, writer_version=writer_version)
        return _GenericStageResult()

    monkeypatch.setattr(main, "run_recommendation_write_from_settings", fake_run)

    result = runner.invoke(
        main.app,
        ["recommendation-write", "--limit", "10", "--force", "--version", "recommendation_writer_v2"],
    )

    assert result.exit_code == 0
    assert captured == {
        "settings": settings,
        "limit": 10,
        "force": True,
        "writer_version": "recommendation_writer_v2",
    }


def test_invalidate_downstream_cli_passes_from_stage(monkeypatch):
    captured: dict = {}
    settings = object()
    monkeypatch.setattr(main.Settings, "from_env", lambda: settings)

    def fake_run(*, settings, from_stage):
        captured.update(settings=settings, from_stage=from_stage)
        return _InvalidateResult(
            from_stage=from_stage,
            claim_verifications=2,
            verification_items=3,
            recommendation_cards=4,
        )

    monkeypatch.setattr(main, "run_invalidate_downstream_from_settings", fake_run)

    result = runner.invoke(main.app, ["invalidate-downstream", "--from", "claim-verification"])

    assert result.exit_code == 0
    assert captured == {"settings": settings, "from_stage": "claim-verification"}
    assert "claim_verifications=2" in result.output
    assert "verification_items=3" in result.output
    assert "recommendation_cards=4" in result.output
