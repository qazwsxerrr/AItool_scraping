"""Operational defaults for the intelligence pipeline."""

DEFAULT_FETCH_LIMIT_PER_SOURCE = 30
"""Default number of items requested from each enabled source."""

DEFAULT_AI_REVIEW_LIMIT: int | None = None
"""Default AI item cap. ``None`` means no global cap."""

DEFAULT_AI_SCREEN_REJECT_THRESHOLD = 90
"""Minimum Stage-A confidence required for a hard rejection."""

DEFAULT_AI_ANALYSIS_MIN_SCORE = 60
"""Minimum deterministic Stage-B score admitted to the C-agent workbench."""

DEFAULT_STAGE_B_ACTIVE_TARGET = 100
DEFAULT_STAGE_B_ACTIVE_MIN = 60
DEFAULT_STAGE_B_ACTIVE_MAX = 120
DEFAULT_STAGE_B_RESERVE_LIMIT = 20
STAGE_B_ADMISSION_POLICY_VERSION = "stage_b_admission_v1"

DEFAULT_AI_REVIEW_CONCURRENCY = 4
"""Maximum concurrent provider calls for Stage A or Stage B."""

STAGE_C_AGENT_VERSION = "stage_c_agent_v3"
DEFAULT_STAGE_C_AGENT_MAX_TURNS = 24
DEFAULT_STAGE_C_AGENT_MAX_TOOL_CALLS = 80
DEFAULT_STAGE_C_AGENT_MAX_WEB_SEARCHES = 8
DEFAULT_STAGE_C_AGENT_HISTORY_DAYS = 3

DEFAULT_DAILY_REPORT_LIMIT = 30
"""Hard maximum number of selected events in one daily report."""

RECENT_WINDOW_HOURS = 72
"""Hard maximum age for items admitted to a run's AI/news pipeline."""


__all__ = [
    "DEFAULT_AI_ANALYSIS_MIN_SCORE",
    "DEFAULT_AI_REVIEW_CONCURRENCY",
    "DEFAULT_AI_REVIEW_LIMIT",
    "DEFAULT_AI_SCREEN_REJECT_THRESHOLD",
    "DEFAULT_DAILY_REPORT_LIMIT",
    "DEFAULT_FETCH_LIMIT_PER_SOURCE",
    "DEFAULT_STAGE_B_ACTIVE_MAX",
    "DEFAULT_STAGE_B_ACTIVE_MIN",
    "DEFAULT_STAGE_B_ACTIVE_TARGET",
    "DEFAULT_STAGE_B_RESERVE_LIMIT",
    "DEFAULT_STAGE_C_AGENT_HISTORY_DAYS",
    "DEFAULT_STAGE_C_AGENT_MAX_TOOL_CALLS",
    "DEFAULT_STAGE_C_AGENT_MAX_TURNS",
    "DEFAULT_STAGE_C_AGENT_MAX_WEB_SEARCHES",
    "RECENT_WINDOW_HOURS",
    "STAGE_B_ADMISSION_POLICY_VERSION",
    "STAGE_C_AGENT_VERSION",
]
