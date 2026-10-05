"""
DELEG8 meeting memory: live state during the meeting, executive briefing after it.

Public surface:
    - :mod:`src.memory.meeting_state`  MeetingState (topic, rolling turns, decisions, actions) and the registry.
    - :mod:`src.memory.commitments`    The commitment ledger: what the room asked the principal to do.
    - :mod:`src.memory.extraction`     Rule-based decision / action / deadline / topic extraction.
    - :mod:`src.memory.post_meeting`   ExecutiveBriefing, the LLM summariser and its template fallback, Telegram dispatch.
"""

from src.memory.commitments import (
    Commitment,
    CommitmentLedger,
    CommitmentRegistry,
    CommitmentSource,
    CommitmentStatus,
)
from src.memory.meeting_state import (
    DEFAULT_WINDOW,
    ActionItem,
    AgreedDecision,
    DecisionOrigin,
    MeetingSnapshot,
    MeetingState,
    MeetingStateRegistry,
    MeetingStats,
    OpenQuestion,
    SpeakerTurn,
    TurnUpdate,
)
from src.memory.post_meeting import (
    ActionItemSummary,
    BriefingDraft,
    ExecutiveBriefing,
    LlmSummariser,
    PostMeetingReport,
    PostMeetingReporter,
    Summariser,
    SummaryError,
    TemplateSummariser,
    build_summariser,
    render_briefing_markdown_v2,
    render_briefing_plain,
)

__all__ = [
    "DEFAULT_WINDOW",
    "ActionItem",
    "ActionItemSummary",
    "AgreedDecision",
    "BriefingDraft",
    "Commitment",
    "CommitmentLedger",
    "CommitmentRegistry",
    "CommitmentSource",
    "CommitmentStatus",
    "DecisionOrigin",
    "ExecutiveBriefing",
    "LlmSummariser",
    "MeetingSnapshot",
    "MeetingState",
    "MeetingStateRegistry",
    "MeetingStats",
    "OpenQuestion",
    "PostMeetingReport",
    "PostMeetingReporter",
    "SpeakerTurn",
    "Summariser",
    "SummaryError",
    "TemplateSummariser",
    "TurnUpdate",
    "build_summariser",
    "render_briefing_markdown_v2",
    "render_briefing_plain",
]
