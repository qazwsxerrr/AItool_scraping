from __future__ import annotations

from app.ai.verify_client import AIVerifyResponse
from app.pipeline.verification import finalize_verification


def test_finalize_verification_recomputes_score_and_level_from_dimensions():
    response = AIVerifyResponse(
        verified=True,
        final_keep=True,
        final_score=12,
        recommendation_level="D",
        relevance_score=90,
        usefulness_score=85,
        credibility_score=80,
        novelty_score=70,
        reproducibility_score=75,
        audience_fit_score=80,
        source_quality_score=60,
        spam_risk_score=20,
        category="mcp",
        summary_cn="一个可安装的 MCP server。",
        recommendation_reason="有仓库和文档。",
        risk_reason="社区反馈较少。",
        evidence_summary=["GitHub 仓库存在", "文档包含安装方式"],
        risk_flags=[],
        raw_response={"final_score": 12},
    )

    final = finalize_verification(response, evidence_count=2)

    assert final.final_score == 80
    assert final.recommendation_level == "A"
    assert final.final_keep is True
    assert final.risk_flags == []


def test_finalize_verification_caps_unverified_items_without_evidence():
    response = AIVerifyResponse(
        verified=True,
        final_keep=True,
        final_score=95,
        recommendation_level="S",
        relevance_score=95,
        usefulness_score=95,
        credibility_score=95,
        novelty_score=95,
        reproducibility_score=95,
        audience_fit_score=95,
        source_quality_score=95,
        spam_risk_score=10,
        category="ai_tool",
        summary_cn="看起来很强的工具。",
        recommendation_reason="AI 声称值得推荐。",
        risk_reason=None,
        evidence_summary=[],
        risk_flags=[],
        raw_response={"final_score": 95},
    )

    final = finalize_verification(response, evidence_count=0)

    assert final.credibility_score == 50
    assert final.final_score == 65
    assert final.recommendation_level == "B"
    assert final.final_keep is False
    assert "weak_evidence" in final.risk_flags


def test_finalize_verification_rejects_hard_negative_flags():
    response = AIVerifyResponse(
        verified=True,
        final_keep=True,
        final_score=88,
        recommendation_level="A",
        relevance_score=90,
        usefulness_score=90,
        credibility_score=90,
        novelty_score=90,
        reproducibility_score=80,
        audience_fit_score=90,
        source_quality_score=80,
        spam_risk_score=20,
        category="model_release",
        summary_cn="声称开源的模型。",
        recommendation_reason="有发布信号。",
        risk_reason="声称开源但缺少权重。",
        evidence_summary=["只有营销页"],
        risk_flags=["fake_open_source_claim"],
        raw_response={"risk_flags": ["fake_open_source_claim"]},
    )

    final = finalize_verification(response, evidence_count=1)

    assert final.final_keep is False
    assert final.final_score == 44
    assert final.recommendation_level == "D"
