"""Operational defaults for the intelligence pipeline."""

DEFAULT_FETCH_LIMIT_PER_SOURCE = 30
"""Default number of items requested from each enabled source."""

DEFAULT_AI_REVIEW_LIMIT: int | None = None
"""Default AI item cap. ``None`` means no global cap."""

DEFAULT_AI_SCREEN_REJECT_THRESHOLD = 90
"""Minimum Stage A confidence required for a hard rejection."""

DEFAULT_AI_ANALYSIS_MIN_SCORE = 60
"""Low-signal annotation threshold for a successful Stage-B analysis.

It only annotates a completed Stage-B analysis.  Stage C owns its own input
threshold.
"""

DEFAULT_AI_STAGE_C_INPUT_MIN_SCORE = 60
"""Minimum guarded Stage-B score admitted to the Stage-C aggregation input.

Stage C receives the successful B1 projections directly; this is only a
deterministic score floor, not a separate routing stage.
"""

STAGE_C_BATCH_ITEM_LIMIT = 32
"""Maximum number of candidates in one bounded Stage-C provider request."""

STAGE_C_BATCH_INPUT_BYTE_LIMIT = 48 * 1024
"""Conservative serialized-input budget for one Stage-C candidate batch."""

STAGE_C_AGGREGATION_MODE = "ai_partitioned_calls_v2"
"""Execution contract for bounded Stage-C aggregation requests."""

STAGE_C_INPUT_POLICY_VERSION = "stage_c_direct_b1_score_gate_v2"
"""Version of the direct-B1 Stage-C input-selection contract."""

DEFAULT_AI_REVIEW_CONCURRENCY = 4
"""Maximum concurrent provider calls for Stage A or Stage B."""

DEFAULT_DAILY_REPORT_LIMIT = 30
"""Default number of items for one editorial/daily export."""

RECENT_WINDOW_HOURS = 72
"""Hard maximum age for items admitted to a run's AI/news pipeline."""


__all__ = [
    "DEFAULT_AI_REVIEW_LIMIT",
    "DEFAULT_AI_SCREEN_REJECT_THRESHOLD",
    "DEFAULT_AI_ANALYSIS_MIN_SCORE",
    "DEFAULT_AI_STAGE_C_INPUT_MIN_SCORE",
    "STAGE_C_AGGREGATION_MODE",
    "STAGE_C_BATCH_INPUT_BYTE_LIMIT",
    "STAGE_C_BATCH_ITEM_LIMIT",
    "STAGE_C_INPUT_POLICY_VERSION",
    "DEFAULT_AI_REVIEW_CONCURRENCY",
    "DEFAULT_DAILY_REPORT_LIMIT",
    "DEFAULT_FETCH_LIMIT_PER_SOURCE",
    "RECENT_WINDOW_HOURS",
]
