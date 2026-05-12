from __future__ import annotations


SOURCE_QUALITY_DEFAULTS: dict[str, dict[str, object]] = {
    "official_blog": {
        "quality_weight": 0.95,
        "source_role": "official",
        "spam_risk": "low",
        "requires_verification": False,
    },
    "producthunt": {
        "quality_weight": 0.65,
        "source_role": "launch_platform",
        "spam_risk": "medium",
        "requires_verification": True,
    },
    "linux_do": {
        "quality_weight": 0.50,
        "source_role": "forum",
        "spam_risk": "medium",
        "requires_verification": True,
    },
    "reddit_local_llama": {
        "quality_weight": 0.55,
        "source_role": "community",
        "spam_risk": "medium",
        "requires_verification": True,
    },
    "x": {
        "quality_weight": 0.45,
        "source_role": "social",
        "spam_risk": "high",
        "requires_verification": True,
    },
}


def source_quality_for_group(source_group: str) -> dict[str, object]:
    default = {
        "quality_weight": 0.50,
        "source_role": "unknown",
        "spam_risk": "medium",
        "requires_verification": True,
    }
    return dict(SOURCE_QUALITY_DEFAULTS.get(source_group, default))


def source_quality_for_source(source, *, fallback_group: str = "general") -> dict[str, object]:
    group = getattr(source, "source_group", None) or fallback_group
    quality = source_quality_for_group(group)
    if getattr(source, "quality_weight", None) is not None:
        quality["quality_weight"] = float(source.quality_weight)
    if getattr(source, "source_role", None) is not None:
        quality["source_role"] = source.source_role
    if getattr(source, "spam_risk", None) is not None:
        quality["spam_risk"] = source.spam_risk
    if getattr(source, "requires_verification", None) is not None:
        quality["requires_verification"] = bool(source.requires_verification)
    return quality


def source_quality_score(source_group: str) -> int:
    quality = source_quality_for_group(source_group)
    return int(round(float(quality["quality_weight"]) * 100))
