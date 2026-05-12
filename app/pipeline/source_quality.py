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


def source_quality_score(source_group: str) -> int:
    quality = source_quality_for_group(source_group)
    return int(round(float(quality["quality_weight"]) * 100))
