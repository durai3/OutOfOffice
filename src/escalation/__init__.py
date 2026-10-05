"""
DELEG8 escalation bridge: human-in-the-loop resolution of ESCALATE decisions.

Public surface:
    - :mod:`src.escalation.models`       Outcome enum, request/result contracts, delivery receipts.
    - :mod:`src.escalation.bridge`       The bridge and dispatcher protocols and the null fallback.
    - :mod:`src.escalation.telegram_bot` The Telegram implementation (escalations, briefings, commands).
    - :mod:`src.escalation.responses`    Proposed and final wording for the delegate.
"""

from src.escalation.bridge import (
    EscalationBridge,
    MessageDispatcher,
    NullEscalationBridge,
)
from src.escalation.models import (
    DeliveryReceipt,
    EscalationOutcome,
    EscalationRequest,
    EscalationResult,
)
from src.escalation.responses import (
    DECLINED_POLITELY_RESPONSE,
    FALLBACK_RESPONSE,
    REJECTED_RESPONSE,
    final_response,
    propose_response,
)
from src.escalation.telegram_bot import (
    SETUP_INSTRUCTIONS,
    CommandCallback,
    EscalationConfigError,
    TelegramEscalationBridge,
    TelegramSettings,
    render_escalation_message,
    render_resolution_footer,
)

__all__ = [
    "CommandCallback",
    "DECLINED_POLITELY_RESPONSE",
    "DeliveryReceipt",
    "EscalationBridge",
    "EscalationConfigError",
    "EscalationOutcome",
    "EscalationRequest",
    "EscalationResult",
    "FALLBACK_RESPONSE",
    "MessageDispatcher",
    "NullEscalationBridge",
    "REJECTED_RESPONSE",
    "SETUP_INSTRUCTIONS",
    "TelegramEscalationBridge",
    "TelegramSettings",
    "final_response",
    "propose_response",
    "render_escalation_message",
    "render_resolution_footer",
]
