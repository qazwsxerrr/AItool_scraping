from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from sqlalchemy import text

from app.ai.github_digest_client import GitHubProjectDigestClient
from app.config.settings import Settings
from app.github.digest import fallback_digest
from app.github.enrich import GitHubProjectEnricher, profile_from_row
from app.github.project_types import GitHubProjectDigest, GitHubProjectProfile, GitHubProjectRanking, GitHubProjectReportItem
from app.github.rank import rank_project
from app.storage.db import create_engine_from_url, create_session_factory, init_db

LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class GitHubProjectReportPaths:
    digest_markdown: Path
    digest_jsonl: Path
    hotlist_markdown: Path
    hotlist_jsonl: Path
    audit_markdown: Path


@dataclass
class GitHubProjectReportResult:
    processed: int = 0
    ai_digested: int = 0
    fallback_digested: int = 0
    failed: int = 0
    hotlist_count: int = 0
    output_paths: GitHubProjectReportPaths | None = None
    errors: list[str] = field(default_factory=list)


def run_github_project_report_from_settings(
    *,
    settings: Settings,
    output_dir: str | Path,
    limit: int | None = None,
    use_ai: bool = True,
    enrich: bool = True,
    hot_min_score: int = 60,
) -> GitHubProjectReportResult:
    engine = create_engine_from_url(settings.database_url)
    init_db(engine)
    session_factory = create_session_factory(engine)
    enricher = GitHubProjectEnricher.from_settings(settings)
    digest_client = GitHubProjectDigestClient.from_settings(settings)
    result = GitHubProjectReportResult()

    rows = _load_github_rows(session_factory=session_factory, limit=limit)
    items: list[GitHubProjectReportItem] = []
    for row in rows:
        try:
            base_profile = profile_from_row(row)
            profile = enricher.enrich(base_profile) if enrich else base_profile
            digest = _digest_project(profile, digest_client=digest_client, use_ai=use_ai, result=result)
            ranking = rank_project(profile, digest)
            items.append(GitHubProjectReportItem(profile=profile, digest=digest, ranking=ranking))
            result.processed += 1
        except Exception as exc:
            result.failed += 1
            result.errors.append(f"normalized_item_id={row.get('normalized_item_id')}: {exc}")
            LOGGER.exception("Failed to build GitHub project report item for normalized item %s", row.get("normalized_item_id"))

    output_paths = _write_report(items, output_dir=Path(output_dir), hot_min_score=hot_min_score)
    result.hotlist_count = sum(1 for item in items if _is_hotlist_item(item, hot_min_score=hot_min_score))
    result.output_paths = output_paths
    return result


def _load_github_rows(*, session_factory, limit: int | None) -> list[dict[str, Any]]:
    query_limit = "" if limit is None else "LIMIT :limit"
    sql = text(
        f"""
        SELECT
          n.id AS normalized_item_id,
          r.id AS raw_item_id,
          r.source_id AS source_id,
          r.raw_summary AS raw_summary,
          r.raw_payload AS raw_payload,
          n.title AS title,
          n.url AS url,
          n.published_at AS published_at
        FROM normalized_items n
        JOIN raw_items r ON r.id = n.raw_item_id
        LEFT JOIN sources s ON s.id = r.source_id
        WHERE COALESCE(s.source_group, '') = 'github'
           OR r.source_id LIKE 'github_%'
           OR n.url LIKE 'https://github.com/%'
        ORDER BY n.published_at DESC, n.id ASC
        {query_limit}
        """
    )
    params = {"limit": limit} if limit is not None else {}
    with session_factory() as session:
        rows = session.execute(sql, params).mappings().all()
        return [dict(row) for row in rows]


def _digest_project(
    profile: GitHubProjectProfile,
    *,
    digest_client: GitHubProjectDigestClient,
    use_ai: bool,
    result: GitHubProjectReportResult,
) -> GitHubProjectDigest:
    if use_ai and digest_client.is_configured:
        try:
            digest = digest_client.digest(profile)
            result.ai_digested += 1
            return digest
        except Exception as exc:
            result.errors.append(f"digest_ai_failed={profile.repo_full_name}: {exc}")
            LOGGER.exception("Failed to AI-digest GitHub project %s", profile.repo_full_name)
    result.fallback_digested += 1
    return fallback_digest(profile)


def _write_report(
    items: list[GitHubProjectReportItem],
    *,
    output_dir: Path,
    hot_min_score: int,
) -> GitHubProjectReportPaths:
    output_dir.mkdir(parents=True, exist_ok=True)
    digest_md = output_dir / "github_project_digest_cn.md"
    digest_jsonl = output_dir / "github_project_digest_cn.jsonl"
    hotlist_md = output_dir / "github_project_hotlist_cn.md"
    hotlist_jsonl = output_dir / "github_project_hotlist_cn.jsonl"
    audit_md = output_dir / "github_project_audit_cn.md"

    sorted_items = sorted(items, key=lambda item: (-item.ranking.final_score, item.profile.repo_full_name.lower()))
    hot_items = [item for item in sorted_items if _is_hotlist_item(item, hot_min_score=hot_min_score)]

    _write_jsonl(digest_jsonl, sorted_items)
    _write_jsonl(hotlist_jsonl, hot_items)
    digest_md.write_text(_digest_markdown(sorted_items), encoding="utf-8-sig")
    hotlist_md.write_text(_hotlist_markdown(hot_items, hot_min_score=hot_min_score), encoding="utf-8-sig")
    audit_md.write_text(_audit_markdown(sorted_items), encoding="utf-8-sig")
    return GitHubProjectReportPaths(
        digest_markdown=digest_md,
        digest_jsonl=digest_jsonl,
        hotlist_markdown=hotlist_md,
        hotlist_jsonl=hotlist_jsonl,
        audit_markdown=audit_md,
    )


def _is_hotlist_item(item: GitHubProjectReportItem, *, hot_min_score: int) -> bool:
    return item.ranking.final_score >= hot_min_score and item.ranking.level in {"S", "A", "B"}


def _write_jsonl(path: Path, items: list[GitHubProjectReportItem]) -> None:
    with path.open("w", encoding="utf-8") as file:
        for item in items:
            file.write(json.dumps(asdict(item), ensure_ascii=False, default=str) + "\n")


def _digest_markdown(items: list[GitHubProjectReportItem]) -> str:
    lines = [
        "# GitHub AI 项目全量中文摘要",
        "",
        f"- 项目数：{len(items)}",
        "- 说明：这是项目发现/画像报告，不等同于事实核验推荐。GitHub 项目默认按热度、活跃度、README、可用性和风险排序。",
        "",
    ]
    for index, item in enumerate(items, start=1):
        lines.extend(_project_section(index, item))
    return "\n".join(lines)


def _hotlist_markdown(items: list[GitHubProjectReportItem], *, hot_min_score: int) -> str:
    lines = [
        "# GitHub AI 项目热点列表",
        "",
        f"- 入选阈值：GitHub 专用综合分 >= {hot_min_score}，且等级为 S/A/B。",
        f"- 项目数：{len(items)}",
        "",
    ]
    if not items:
        lines.append("（无）")
        return "\n".join(lines)
    for index, item in enumerate(items, start=1):
        lines.extend(_project_section(index, item))
    return "\n".join(lines)


def _audit_markdown(items: list[GitHubProjectReportItem]) -> str:
    level_counts: dict[str, int] = {}
    for item in items:
        level_counts[item.ranking.level] = level_counts.get(item.ranking.level, 0) + 1
    lines = ["# GitHub AI 项目评分审计", "", "## 等级统计", ""]
    for level, count in sorted(level_counts.items()):
        lines.append(f"- `{level}`：{count}")
    lines.extend(["", "## 全量审计", ""])
    for index, item in enumerate(items, start=1):
        profile = item.profile
        ranking = item.ranking
        lines.extend(
            [
                f"### {index}. {profile.repo_full_name}",
                "",
                f"- GitHub 项目地址：{profile.url or '无'}",
                f"- 信息来源：`{profile.source_id}`；抓取日期：`{profile.published_at or 'unknown'}`",
                f"- 项目主页：{profile.homepage or '无'}",
                f"- Release 来源：{_release_links(profile.latest_releases)}",
                f"- GitHub 时间：创建 `{profile.created_at or 'unknown'}`；最近 pushed `{profile.pushed_at or 'unknown'}`",
                f"- 综合分：`{ranking.final_score}`；等级：`{ranking.level}`；处理建议：{ranking.decision}",
                f"- 评分原因：{ranking.rank_reason}",
                f"- 风险标签：{_join(ranking.risk_flags)}",
                f"- GitHub 补全错误：{_join(profile.profile_errors)}",
                "",
            ]
        )
    return "\n".join(lines)


def _project_section(index: int, item: GitHubProjectReportItem) -> list[str]:
    profile = item.profile
    digest = item.digest
    ranking = item.ranking
    return [
        f"## {index}. {profile.repo_full_name}",
        "",
        f"- GitHub 项目地址：{profile.url or '无'}",
        f"- 信息来源：`{profile.source_id}`；抓取日期：`{profile.published_at or 'unknown'}`",
        f"- 项目主页：{profile.homepage or '无'}",
        f"- Release 来源：{_release_links(profile.latest_releases)}",
        f"- 处理建议：{ranking.decision}（等级 `{ranking.level}`，综合分 `{ranking.final_score}`）",
        f"- 一句话介绍：{digest.summary_cn}",
        f"- 项目类型：`{digest.project_type}`；AI 相关度：`{digest.ai_relevance_score}`；摘要来源：`{digest.digest_source}`",
        f"- 关键词：{_join(digest.keywords)}",
        f"- 主要功能：{_join(digest.main_features)}",
        f"- 适合人群：{_join(digest.target_users)}",
        f"- 如何试用：{digest.how_to_try or '建议打开 README 人工确认'}",
        f"- GitHub 指标：stars `{profile.stars}`，forks `{profile.forks}`，issues `{profile.open_issues}`，语言 `{profile.primary_language or 'unknown'}`，创建 `{profile.created_at or 'unknown'}`，最近 pushed `{profile.pushed_at or 'unknown'}`",
        f"- 质量/活跃度：热度 `{ranking.popularity_score}`，活跃度 `{ranking.activity_score}`，README 质量 `{digest.readme_quality_score}`，可用性 `{digest.usability_score}`，风险 `{ranking.risk_score}`",
        f"- 风险提示：{_join([*digest.risk_notes, *ranking.risk_flags])}",
        f"- 详细介绍：{digest.description_cn}",
        "",
    ]


def _join(values: list[str]) -> str:
    return "、".join(values) if values else "无"


def _release_links(releases: list[dict[str, Any]]) -> str:
    links = []
    for release in releases[:3]:
        url = release.get("html_url")
        label = release.get("tag_name") or release.get("name") or "release"
        if url:
            links.append(f"{label}: {url}")
    return " / ".join(links) if links else "无"
