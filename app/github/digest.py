from __future__ import annotations

from app.github.project_types import GitHubProjectDigest, GitHubProjectProfile


AI_KEYWORDS = {
    "agent",
    "ai",
    "chatgpt",
    "claude",
    "codex",
    "cursor",
    "embedding",
    "gpt",
    "llm",
    "mcp",
    "model-context-protocol",
    "openai",
    "rag",
    "workflow",
}


def fallback_digest(profile: GitHubProjectProfile) -> GitHubProjectDigest:
    """Build a readable deterministic digest when the AI digest API is unavailable."""
    keywords = _keywords(profile)
    project_type = _project_type(keywords, profile)
    ai_relevance_score = _ai_relevance_score(profile, keywords)
    readme_quality_score = _readme_quality_score(profile)
    usability_score = _usability_score(profile)
    risk_notes = _risk_notes(profile)

    summary = _summary(profile, project_type)
    description = _description(profile, keywords)
    return GitHubProjectDigest(
        project_name=profile.repo_full_name,
        summary_cn=summary,
        description_cn=description,
        keywords=keywords,
        project_type=project_type,
        target_users=_target_users(project_type),
        main_features=_main_features(profile),
        how_to_try=_how_to_try(profile),
        risk_notes=risk_notes,
        is_ai_related=ai_relevance_score >= 50,
        ai_relevance_score=ai_relevance_score,
        readme_quality_score=readme_quality_score,
        usability_score=usability_score,
        digest_confidence=65 if profile.readme_excerpt else 45,
        digest_source="rules_fallback",
    )


def _keywords(profile: GitHubProjectProfile) -> list[str]:
    text = " ".join(
        [
            profile.repo_full_name,
            profile.description or "",
            " ".join(profile.topics),
            profile.readme_excerpt or "",
        ]
    ).lower()
    found = sorted(keyword for keyword in AI_KEYWORDS if keyword in text)
    language = profile.primary_language
    if language:
        found.append(language)
    return _dedupe_preserve_order(found[:12])


def _project_type(keywords: list[str], profile: GitHubProjectProfile) -> str:
    text = " ".join([*keywords, profile.repo_full_name, profile.description or ""]).lower()
    if "mcp" in text or "model-context-protocol" in text:
        return "mcp"
    if "agent" in text:
        return "agent_tool"
    if "workflow" in text or "skill" in text:
        return "workflow_or_skill"
    if "rag" in text or "embedding" in text:
        return "rag_or_retrieval"
    if "openai" in text or "api" in text or "proxy" in text:
        return "api_tool"
    if "llm" in text or "gpt" in text or "claude" in text:
        return "llm_tool"
    return "developer_tool"


def _ai_relevance_score(profile: GitHubProjectProfile, keywords: list[str]) -> int:
    score = 10
    keyword_hits = sum(1 for keyword in keywords if keyword.lower() in AI_KEYWORDS)
    score += min(keyword_hits * 12, 60)
    if profile.topics:
        score += 8
    if profile.description:
        score += 6
    if profile.readme_excerpt:
        score += 10
    return _clamp(score)


def _readme_quality_score(profile: GitHubProjectProfile) -> int:
    readme = (profile.readme_excerpt or "").lower()
    if not readme:
        return 0
    score = 30
    for marker in ["install", "usage", "quickstart", "example", "docker", "npm", "pip", "api", "configuration"]:
        if marker in readme:
            score += 7
    return _clamp(score)


def _usability_score(profile: GitHubProjectProfile) -> int:
    text = " ".join([profile.readme_excerpt or "", profile.description or ""]).lower()
    score = 20
    for marker in ["install", "quickstart", "usage", "example", "demo", "docker", "npm", "pip", "cli"]:
        if marker in text:
            score += 8
    if profile.homepage:
        score += 8
    if profile.latest_releases:
        score += 8
    return _clamp(score)


def _risk_notes(profile: GitHubProjectProfile) -> list[str]:
    risks: list[str] = []
    if profile.archived:
        risks.append("仓库已归档")
    if profile.fork:
        risks.append("仓库是 fork，需确认原始项目")
    if not profile.readme_excerpt:
        risks.append("未获取到 README，项目理解置信度较低")
    if not profile.license_name:
        risks.append("未发现明确 license")
    if profile.stars < 10:
        risks.append("stars 较少，可能是早期项目")
    if profile.profile_errors:
        risks.append("部分 GitHub 补全失败：" + "；".join(profile.profile_errors[:3]))
    return risks


def _summary(profile: GitHubProjectProfile, project_type: str) -> str:
    description = profile.description or "暂无 GitHub 描述"
    return f"{profile.repo_full_name} 是一个 {project_type} 类 GitHub 项目：{description}。"


def _description(profile: GitHubProjectProfile, keywords: list[str]) -> str:
    parts = [profile.description or "暂无项目描述。"]
    if keywords:
        parts.append("识别关键词：" + "、".join(keywords[:8]) + "。")
    if profile.readme_excerpt:
        parts.append("README 已获取，可用于进一步判断安装方式、示例和适用场景。")
    return " ".join(parts)


def _target_users(project_type: str) -> list[str]:
    if project_type == "mcp":
        return ["MCP 用户", "AI Agent 开发者", "自动化工具开发者"]
    if project_type == "agent_tool":
        return ["AI Agent 开发者", "自动化工作流用户"]
    if project_type == "api_tool":
        return ["API 开发者", "LLM 应用开发者"]
    return ["AI 工具开发者", "开源项目关注者"]


def _main_features(profile: GitHubProjectProfile) -> list[str]:
    features = []
    if profile.description:
        features.append(profile.description)
    if profile.readme_excerpt:
        features.append("提供 README，可进一步查看安装、用法或示例。")
    if profile.latest_releases:
        features.append("存在 release 记录，可查看最近版本变化。")
    if profile.languages:
        features.append("主要代码语言：" + "、".join(list(profile.languages)[:3]))
    return features or ["需要人工打开仓库进一步确认主要功能。"]


def _how_to_try(profile: GitHubProjectProfile) -> str | None:
    if profile.url:
        return f"打开 GitHub README 查看安装和 quickstart：{profile.url}"
    return None


def _dedupe_preserve_order(values: list[str]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        key = value.lower()
        if key in seen:
            continue
        seen.add(key)
        result.append(value)
    return result


def _clamp(value: int) -> int:
    return max(0, min(value, 100))
