from __future__ import annotations

import csv
import json
import sqlite3
import sys
from pathlib import Path

import typer

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


STAGE_LABELS = {
    "recommended": "已推荐",
    "ai_verify_rejected": "二次核验拒绝",
    "verified_but_not_written": "已核验但未写推荐卡",
    "claim_extracted_but_not_ai_verified": "已抽取 claim，未二次核验",
    "ai_review_rejected": "AI 初筛拒绝",
    "ai_review_kept_but_not_claim_extracted": "AI 初筛保留，未抽取 claim",
    "prefilter_dropped": "本地预筛丢弃",
    "prefilter_kept_but_not_ai_reviewed": "本地预筛保留，未 AI 初筛",
    "normalized_but_no_candidate": "已标准化，未进入候选池",
}


def _text(value: object, *, missing: str = "未进入该阶段") -> str:
    if value is None:
        return missing
    text = str(value).strip()
    if not text or text == "None":
        return missing
    return text


def _json_list(value: str | None) -> list[str]:
    if not value:
        return []
    try:
        data = json.loads(value)
    except Exception:
        return [str(value)]
    if isinstance(data, list):
        return [str(item) for item in data]
    return [str(data)]


def _stage(row: dict) -> str:
    if row.get("recommendation_card_id"):
        return "recommended"
    if row.get("verification_id"):
        return "ai_verify_rejected" if not row.get("final_keep") else "verified_but_not_written"
    if row.get("claim_id"):
        return "claim_extracted_but_not_ai_verified"
    if row.get("ai_review_id"):
        return "ai_review_rejected" if not row.get("ai_keep") else "ai_review_kept_but_not_claim_extracted"
    if row.get("candidate_id"):
        return "prefilter_dropped" if row.get("candidate_status") == "dropped" else "prefilter_kept_but_not_ai_reviewed"
    return "normalized_but_no_candidate"


def _reason(row: dict) -> str:
    stage = row["audit_stage"]
    if stage == "recommended":
        return "通过 final_keep=true，并已生成 recommendation_card。"
    if stage == "ai_verify_rejected":
        parts: list[str] = []
        risk_flags = _json_list(row.get("risk_flags"))
        if risk_flags:
            parts.append("risk_flags=" + ", ".join(risk_flags))
        if row.get("risk_reason"):
            parts.append("risk_reason=" + str(row["risk_reason"]))
        parts.append("final_score=" + str(row.get("final_score")))
        parts.append("level=" + str(row.get("recommendation_level")))
        return "；".join(parts)
    if stage == "claim_extracted_but_not_ai_verified":
        return "已抽取 claim；本次 ai_verify limit=6，未进入最终二次核验。"
    if stage == "ai_review_rejected":
        return f"AI review 未保留：ai_score={row.get('ai_score')}；reason={row.get('ai_reason') or ''}"
    if stage == "ai_review_kept_but_not_claim_extracted":
        return "AI review 已保留；本次 claim_extract limit=8，未继续抽 claim。"
    if stage == "prefilter_dropped":
        return (
            f"prefilter 分数不足：candidate_score={row.get('candidate_score')}；"
            f"drop_reason={row.get('drop_reason')}；keep_reason={row.get('keep_reason')}"
        )
    if stage == "prefilter_kept_but_not_ai_reviewed":
        return "prefilter 已保留；本次 ai_review limit=12，未进入 AI 初筛。"
    return "未进入候选池。"


def _load_rows(conn: sqlite3.Connection) -> list[dict]:
    rows = [
        dict(row)
        for row in conn.execute(
            """
            SELECT
              n.id AS normalized_id,
              n.title,
              n.url,
              n.published_at,
              r.source_id,
              c.id AS candidate_id,
              c.status AS candidate_status,
              c.candidate_score,
              c.keep_reason,
              c.drop_reason,
              a.id AS ai_review_id,
              a.ai_keep,
              a.ai_score,
              a.category AS ai_category,
              a.reason AS ai_reason,
              a.summary_cn AS ai_summary_cn,
              cl.id AS claim_id,
              cl.entity_name,
              cl.entity_type,
              cl.confidence AS claim_confidence,
              cl.evidence_status,
              v.id AS verification_id,
              v.final_keep,
              v.final_score,
              v.recommendation_level,
              v.verified,
              v.credibility_score,
              v.spam_risk_score,
              v.risk_flags,
              v.recommendation_reason,
              v.risk_reason,
              rc.id AS recommendation_card_id
            FROM normalized_items n
            JOIN raw_items r ON r.id = n.raw_item_id
            LEFT JOIN candidate_items c ON c.normalized_item_id = n.id
            LEFT JOIN ai_review_items a ON a.candidate_item_id = c.id
            LEFT JOIN extracted_claims cl ON cl.candidate_item_id = c.id
            LEFT JOIN verification_items v ON v.candidate_item_id = c.id
            LEFT JOIN recommendation_cards rc ON rc.verification_item_id = v.id
            ORDER BY n.published_at, n.id
            """
        )
    ]

    evidence = {
        row["candidate_item_id"]: dict(row)
        for row in conn.execute(
            """
            SELECT
              candidate_item_id,
              COUNT(*) AS evidence_count,
              SUM(CASE WHEN supports_claim = 'support' THEN 1 ELSE 0 END) AS evidence_support,
              SUM(CASE WHEN supports_claim = 'contradict' THEN 1 ELSE 0 END) AS evidence_contradict,
              SUM(CASE WHEN supports_claim = 'unknown' THEN 1 ELSE 0 END) AS evidence_unknown,
              GROUP_CONCAT(DISTINCT source_domain) AS evidence_domains
            FROM evidence_items
            GROUP BY candidate_item_id
            """
        )
    }
    claim_verify = {
        row["candidate_item_id"]: dict(row)
        for row in conn.execute(
            """
            SELECT
              candidate_item_id,
              COUNT(*) AS claim_verify_count,
              SUM(CASE WHEN supports_claim = 'support' THEN 1 ELSE 0 END) AS claim_support,
              SUM(CASE WHEN supports_claim = 'contradict' THEN 1 ELSE 0 END) AS claim_contradict,
              SUM(CASE WHEN supports_claim = 'unknown' THEN 1 ELSE 0 END) AS claim_unknown,
              GROUP_CONCAT(DISTINCT support_strength) AS support_strengths,
              GROUP_CONCAT(DISTINCT risk_flags) AS claim_risk_flags
            FROM claim_verification_items
            GROUP BY candidate_item_id
            """
        )
    }

    for row in rows:
        candidate_id = row.get("candidate_id")
        row.update(evidence.get(candidate_id, {}))
        row.update(claim_verify.get(candidate_id, {}))
        row["audit_stage"] = _stage(row)
        row["audit_stage_cn"] = STAGE_LABELS.get(row["audit_stage"], row["audit_stage"])
        row["audit_reason"] = _reason(row)
        row["risk_flags_list"] = ", ".join(_json_list(row.get("risk_flags")))
    return rows


def _write_outputs(rows: list[dict], output_dir: Path, database_path: Path) -> tuple[Path, Path, Path, dict[str, int]]:
    output_dir.mkdir(parents=True, exist_ok=True)
    markdown_path = output_dir / "all_63_audit_cn_latest.md"
    csv_path = output_dir / "all_63_audit_cn_latest.csv"
    jsonl_path = output_dir / "all_63_audit_cn_latest.jsonl"

    stage_counts: dict[str, int] = {}
    for row in rows:
        stage_counts[row["audit_stage"]] = stage_counts.get(row["audit_stage"], 0) + 1

    fields = [
        "normalized_id",
        "candidate_id",
        "published_at",
        "source_id",
        "title",
        "url",
        "audit_stage",
        "audit_stage_cn",
        "audit_reason",
        "candidate_status",
        "candidate_score",
        "keep_reason",
        "drop_reason",
        "ai_keep",
        "ai_score",
        "ai_category",
        "ai_reason",
        "ai_summary_cn",
        "entity_name",
        "entity_type",
        "claim_confidence",
        "evidence_status",
        "evidence_count",
        "evidence_support",
        "evidence_contradict",
        "evidence_unknown",
        "evidence_domains",
        "claim_verify_count",
        "claim_support",
        "claim_contradict",
        "claim_unknown",
        "support_strengths",
        "claim_risk_flags",
        "final_keep",
        "final_score",
        "recommendation_level",
        "verified",
        "credibility_score",
        "spam_risk_score",
        "risk_flags_list",
        "recommendation_reason",
        "risk_reason",
        "recommendation_card_id",
    ]

    with jsonl_path.open("w", encoding="utf-8") as file:
        for row in rows:
            file.write(json.dumps(row, ensure_ascii=False, default=str) + "\n")

    with csv_path.open("w", encoding="utf-8-sig", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)

    lines = [
        "# GitHub 近一周 63 条全量中文审计",
        "",
        f"- 数据库：`{database_path}`",
        f"- 总条数：`{len(rows)}`",
        "",
        "## 阶段统计",
        "",
    ]
    for key, count in sorted(stage_counts.items(), key=lambda item: (-item[1], item[0])):
        lines.append(f"- {STAGE_LABELS.get(key, key)}（`{key}`）：{count}")
    lines.extend(["", "## 全量清单", ""])
    for index, row in enumerate(rows, 1):
        day = str(row.get("published_at") or "")[:10]
        lines.extend(
            [
                f"### {index}. {row.get('title')}",
                "",
                f"- URL：{_text(row.get('url'), missing='无')}",
                f"- 日期：`{day}`；来源：`{row.get('source_id')}`",
                f"- 审计阶段：{row.get('audit_stage_cn')}（`{row.get('audit_stage')}`）",
                f"- 主要原因：{row.get('audit_reason')}",
                f"- 本地预筛：状态=`{_text(row.get('candidate_status'))}` 分数=`{_text(row.get('candidate_score'))}` 保留信号=`{_text(row.get('keep_reason'), missing='无')}` 丢弃原因=`{_text(row.get('drop_reason'), missing='无')}`",
                f"- AI 初筛：是否保留=`{_text(row.get('ai_keep'))}` 分数=`{_text(row.get('ai_score'))}` 分类=`{_text(row.get('ai_category'))}` 理由={_text(row.get('ai_reason'), missing='无')}",
                f"- Claim 抽取：实体=`{_text(row.get('entity_name'))}` 置信度=`{_text(row.get('claim_confidence'))}` 证据状态=`{_text(row.get('evidence_status'))}`",
                f"- 证据抓取/分类：数量=`{_text(row.get('evidence_count'))}` 支持=`{_text(row.get('evidence_support'))}` 反证=`{_text(row.get('evidence_contradict'))}` 未知=`{_text(row.get('evidence_unknown'))}` 域名=`{_text(row.get('evidence_domains'), missing='无')}`",
                f"- Claim 核验：数量=`{_text(row.get('claim_verify_count'))}` 支持=`{_text(row.get('claim_support'))}` 反证=`{_text(row.get('claim_contradict'))}` 未知=`{_text(row.get('claim_unknown'))}` 支持强度=`{_text(row.get('support_strengths'))}`",
                f"- AI 二次核验：最终保留=`{_text(row.get('final_keep'))}` 分数=`{_text(row.get('final_score'))}` 等级=`{_text(row.get('recommendation_level'))}` 已验证=`{_text(row.get('verified'))}` 风险标签=`{_text(row.get('risk_flags_list'), missing='无')}` 风险说明={_text(row.get('risk_reason'), missing='无')}",
                "",
            ]
        )
    markdown_path.write_text("\n".join(lines), encoding="utf-8-sig")
    return markdown_path, csv_path, jsonl_path, stage_counts


def main(
    database_path: Path = typer.Option(
        Path("data/github_weekly_bucketed_20260629_123412.db"),
        help="SQLite database containing the weekly GitHub run.",
    ),
    output_dir: Path = typer.Option(
        Path("output/github_weekly_bucketed_20260629_123412"),
        help="Output directory for Markdown, CSV, and JSONL audit files.",
    ),
) -> None:
    conn = sqlite3.connect(database_path)
    conn.row_factory = sqlite3.Row
    rows = _load_rows(conn)
    markdown_path, csv_path, jsonl_path, stage_counts = _write_outputs(rows, output_dir, database_path)
    typer.echo(json.dumps({
        "rows": len(rows),
        "stage_counts": stage_counts,
        "markdown_path": str(markdown_path),
        "csv_path": str(csv_path),
        "jsonl_path": str(jsonl_path),
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    typer.run(main)
