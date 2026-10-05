"""
DELEG8 Policy Router Brain.

The brain is a pure-Python decision package with no web-framework
dependency. It answers one question for every transcript snippet:

    "Given who is being represented, what was just said, and by whom -
     should the delegate speak, stay silent, or escalate to the human?"

Public surface:
    - :mod:`src.brain.models`         Pydantic contracts.
    - :mod:`src.brain.profile_store`  Representation Policy lookup.
    - :mod:`src.brain.policy_engine`  The state machine.
    - :mod:`src.brain.knowledge`      The pluggable knowledge seam (profile facts, composites).
    - :mod:`src.brain.intent`         Optional LLM backstop for request detection.
"""

from src.brain.intent import (
    ClaudeIntentClassifier,
    GeminiIntentClassifier,
    RequestIntentClassifier,
    build_intent_classifier,
)
from src.brain.knowledge import (
    DOCUMENTED_FACTS_TOPIC,
    CompositeKnowledge,
    GroundedAnswer,
    KnowledgeSource,
    ProfileKnowledge,
)
from src.brain.models import (
    DecisionState,
    Grounding,
    GroundingSource,
    MeetingContext,
    PolicyDecision,
    PolicyTier,
    UserProfile,
)
from src.brain.policy_engine import PolicyEngine
from src.brain.profile_store import (
    InMemoryProfileStore,
    ProfileNotFoundError,
    ProfileStore,
    profile_store,
)

__all__ = [
    "DOCUMENTED_FACTS_TOPIC",
    "ClaudeIntentClassifier",
    "CompositeKnowledge",
    "DecisionState",
    "GeminiIntentClassifier",
    "GroundedAnswer",
    "Grounding",
    "GroundingSource",
    "InMemoryProfileStore",
    "KnowledgeSource",
    "MeetingContext",
    "PolicyDecision",
    "PolicyEngine",
    "PolicyTier",
    "ProfileKnowledge",
    "ProfileNotFoundError",
    "ProfileStore",
    "RequestIntentClassifier",
    "UserProfile",
    "build_intent_classifier",
    "profile_store",
]
