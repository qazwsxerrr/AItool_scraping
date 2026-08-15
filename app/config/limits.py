"""Operational defaults for the intelligence pipeline."""

DEFAULT_FETCH_LIMIT_PER_SOURCE = 30
"""Default number of items requested from each enabled source."""

DEFAULT_AI_REVIEW_LIMIT: int | None = None
"""Default AI item cap. ``None`` means no global cap."""

DEFAULT_AI_SCREEN_REJECT_THRESHOLD = 85
"""Minimum Stage A confidence required for a hard rejection."""

DEFAULT_AI_ANALYSIS_MIN_SCORE = 60
"""Minimum Stage B score required for a candidate projection."""

DEFAULT_AI_REVIEW_CONCURRENCY = 4
"""Maximum concurrent provider calls for Stage A or Stage B."""

DEFAULT_DAILY_REPORT_LIMIT = 30
"""Default number of items for one editorial/daily export."""


__all__ = [
    "DEFAULT_AI_REVIEW_LIMIT",
    "DEFAULT_AI_SCREEN_REJECT_THRESHOLD",
    "DEFAULT_AI_ANALYSIS_MIN_SCORE",
    "DEFAULT_AI_REVIEW_CONCURRENCY",
    "DEFAULT_DAILY_REPORT_LIMIT",
    "DEFAULT_FETCH_LIMIT_PER_SOURCE",
]
