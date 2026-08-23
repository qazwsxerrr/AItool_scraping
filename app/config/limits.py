"""Operational defaults for the intelligence pipeline."""

DEFAULT_FETCH_LIMIT_PER_SOURCE: int | None = None
"""Global source-limit override. ``None`` uses each source's registry limit."""

DEFAULT_AI_REVIEW_LIMIT: int | None = None
"""Default AI item cap. ``None`` means no global cap."""

DEFAULT_AI_SCREEN_REJECT_THRESHOLD = 90
"""Minimum Stage-A confidence required for a hard rejection."""

DEFAULT_AI_ANALYSIS_MIN_SCORE = 70
"""Minimum deterministic Stage-B score admitted to the C-agent workbench."""

DEFAULT_AI_AUDIENCE_RELEVANCE_MIN = 65
"""Minimum AI subject relevance required before a B1 item can enter C."""

DEFAULT_STAGE_B_ACTIVE_TARGET = 100
DEFAULT_STAGE_B_ACTIVE_MIN = 60
DEFAULT_STAGE_B_ACTIVE_MAX = 120
DEFAULT_STAGE_B_RESERVE_LIMIT = 20
STAGE_B_ADMISSION_POLICY_VERSION = "stage_b_admission_v3"

DEFAULT_AI_REVIEW_CONCURRENCY = 4
"""Maximum concurrent provider calls for Stage A or Stage B."""

STAGE_C_AGENT_VERSION = "stage_c_agent_v8"
# These are deliberately roomy defaults for the stateful verification flow.
# Operators can configure a larger value when a run needs it; web search uses
# 0 as its explicit disable switch.
DEFAULT_STAGE_C_AGENT_MAX_TURNS = 32
DEFAULT_STAGE_C_AGENT_MAX_TOOL_CALLS = 120
DEFAULT_STAGE_C_AGENT_MAX_WEB_SEARCHES = 16
DEFAULT_STAGE_C_AGENT_HISTORY_DAYS = 3
DEFAULT_STAGE_D_MAX_WEB_SEARCHES = 6

DEFAULT_DAILY_REPORT_LIMIT = 30
"""Hard maximum number of selected events in one daily report."""

RECENT_WINDOW_HOURS = 72
"""Legacy rolling freshness window used by independent export/freshness checks."""


__all__ = [
    "DEFAULT_AI_ANALYSIS_MIN_SCORE",
    "DEFAULT_AI_AUDIENCE_RELEVANCE_MIN",
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
    "DEFAULT_STAGE_D_MAX_WEB_SEARCHES",
    "RECENT_WINDOW_HOURS",
    "STAGE_B_ADMISSION_POLICY_VERSION",
    "STAGE_C_AGENT_VERSION",
]
