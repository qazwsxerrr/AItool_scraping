"""Operational defaults for the intelligence pipeline."""

DEFAULT_FETCH_LIMIT_PER_SOURCE = 20
"""Default number of items requested from each enabled source."""

DEFAULT_AI_REVIEW_LIMIT = 1000
"""Default number of existing items considered by the AI review stage."""

DEFAULT_DAILY_REPORT_LIMIT = 30
"""Default number of items for one editorial/daily export."""


__all__ = [
    "DEFAULT_AI_REVIEW_LIMIT",
    "DEFAULT_DAILY_REPORT_LIMIT",
    "DEFAULT_FETCH_LIMIT_PER_SOURCE",
]
