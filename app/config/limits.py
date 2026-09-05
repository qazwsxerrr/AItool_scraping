"""Operational defaults for the intelligence pipeline."""

DEFAULT_FETCH_LIMIT_PER_SOURCE: int | None = None
"""Global source-limit override. ``None`` uses each source's registry limit."""

DEFAULT_AI_REVIEW_LIMIT: int | None = None
"""Default AI item cap. ``None`` means no global cap."""

DEFAULT_AI_SCREEN_REJECT_THRESHOLD = 90
"""Minimum Stage-A confidence required for a hard rejection."""

STAGE_B_ANALYSIS_MIN_SCORE = 60
"""Minimum guarded B1 score required before an item can enter Stage C."""

STAGE_B_AUDIENCE_RELEVANCE_MIN = 60
"""Minimum AI-subject relevance required before an item can enter Stage C."""

STAGE_B_ADMISSION_POLICY_VERSION = "stage_b_admission_v6"

DEFAULT_AI_REVIEW_CONCURRENCY = 4
"""Maximum concurrent provider calls for Stage A or Stage B."""

STAGE_C_AGENT_VERSION = "stage_c_agent_v9"
# These are deliberately roomy defaults for the agent verification flow.
# Operators can configure a larger value when a run needs it; web search uses
# 0 as its explicit disable switch.
DEFAULT_STAGE_C_AGENT_MAX_TURNS = 64
DEFAULT_STAGE_C_AGENT_MAX_TOOL_CALLS = 240
DEFAULT_STAGE_C_AGENT_MAX_WEB_SEARCHES = 50
DEFAULT_STAGE_C_AGENT_HISTORY_DAYS = 3
DEFAULT_STAGE_D_MAX_WEB_SEARCHES = 6

DEFAULT_DAILY_REPORT_LIMIT = 30
"""Hard maximum number of selected events in one daily report."""

__all__ = [
    "DEFAULT_AI_REVIEW_CONCURRENCY",
    "DEFAULT_AI_REVIEW_LIMIT",
    "DEFAULT_AI_SCREEN_REJECT_THRESHOLD",
    "DEFAULT_DAILY_REPORT_LIMIT",
    "DEFAULT_FETCH_LIMIT_PER_SOURCE",
    "DEFAULT_STAGE_C_AGENT_HISTORY_DAYS",
    "DEFAULT_STAGE_C_AGENT_MAX_TOOL_CALLS",
    "DEFAULT_STAGE_C_AGENT_MAX_TURNS",
    "DEFAULT_STAGE_C_AGENT_MAX_WEB_SEARCHES",
    "DEFAULT_STAGE_D_MAX_WEB_SEARCHES",
    "STAGE_B_ANALYSIS_MIN_SCORE",
    "STAGE_B_ADMISSION_POLICY_VERSION",
    "STAGE_B_AUDIENCE_RELEVANCE_MIN",
    "STAGE_C_AGENT_VERSION",
]
