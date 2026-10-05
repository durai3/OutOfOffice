"""
Starter Representation Policies for a brand new principal.

A policy with no rules is not a safe default, it is an unusable one: the
:class:`~src.brain.models.UserProfile` contract requires at least one rule, and
more importantly a delegate with an empty policy has no notion of what is
sensitive, so the first thing it does in a real meeting is treat a salary
question like a status question.

So signup always installs a template, and onboarding asks which one. Every
template shares the same RED spine - pricing, contracts, compensation, personal
matters are never answered autonomously by anyone's delegate - and differs only
in what it is willing to speak about GREEN.
"""

from __future__ import annotations

from dataclasses import dataclass

from src.brain.models import KnowledgeEntry, PolicyRule, PolicyTier, UserProfile

# --------------------------------------------------------------------------- #
# The shared spine
# --------------------------------------------------------------------------- #

_DOCUMENTED_FACTS = PolicyRule(
    topic="documented_facts",
    tier=PolicyTier.GREEN,
    description="State facts recorded in the principal's own uploaded documents.",
    triggers=[
        "the docs", "documentation", "your cv", "your resume", "background",
        "tell me about yourself", "experience", "your work", "your projects",
    ],
)
"""The rule that authorises the delegate to answer from the dossier and the
document index. Without it, questions that match no other trigger never reach
the retriever, and the delegate is mute about the person it represents."""

_RESTRICTED: tuple[PolicyRule, ...] = (
    PolicyRule(
        topic="pricing_negotiation",
        tier=PolicyTier.RED,
        description="Negotiate pricing, discounts, or commercial terms.",
        triggers=[
            "price", "pricing", "discount", "cost", "quote", "budget", "cheaper",
            "lower the", "percent off", "% off", "rate card", "licensing fee", "commercial terms",
        ],
    ),
    PolicyRule(
        topic="contract_legal",
        tier=PolicyTier.RED,
        description="Contract, legal, or liability matters.",
        triggers=[
            "contract", "legal", "liability", "indemnity", "indemnification",
            "terms of service", "sla", "nda", "master services agreement",
        ],
    ),
    PolicyRule(
        topic="compensation",
        tier=PolicyTier.RED,
        description="Salary, compensation, equity, or what the principal expects to be paid.",
        triggers=[
            "salary", "compensation", "how much do you earn", "how much do you make",
            "expected pay", "pay expectation", "salary expectation", "equity",
            "stock options", "bonus", "day rate", "hourly rate",
        ],
    ),
    PolicyRule(
        topic="personal_sensitive",
        tier=PolicyTier.RED,
        description="Health, family, religion, politics, immigration status, or other private matters.",
        triggers=[
            "medical", "health condition", "disability", "religion", "religious",
            "politics", "political", "married", "marital", "children", "visa status",
            "immigration", "citizenship status", "date of birth", "home address",
        ],
    ),
    PolicyRule(
        topic="deadline_commitment",
        tier=PolicyTier.YELLOW,
        description="Commit to delivery dates and deadlines.",
        triggers=[
            "deadline", "deliver by", "deliver this by", "delivery date", "ship by",
            "commit to", "timeline", "when will", "when can", "how soon",
            "by monday", "by tuesday", "by wednesday", "by thursday", "by friday",
            "by tomorrow", "by end of", "by eod", "by eow", "by next week",
        ],
    ),
)


@dataclass(frozen=True)
class PolicyTemplate:
    """One named starting point, as offered during onboarding."""

    key: str
    name: str
    summary: str
    extra_rules: tuple[PolicyRule, ...] = ()
    knowledge: tuple[KnowledgeEntry, ...] = ()

    def rules(self) -> list[PolicyRule]:
        """Every rule this template installs: the shared spine plus its own."""
        return [_DOCUMENTED_FACTS, *self.extra_rules, *_RESTRICTED]


TEMPLATES: dict[str, PolicyTemplate] = {
    "balanced": PolicyTemplate(
        key="balanced",
        name="Balanced",
        summary=(
            "Answers questions about you and your documented work; escalates deadlines "
            "and scheduling; never touches money, contracts, or personal matters."
        ),
        extra_rules=(
            PolicyRule(
                topic="project_status",
                tier=PolicyTier.GREEN,
                description="Give project status and progress updates.",
                triggers=[
                    "status", "progress", "update on", "how far along",
                    "where are we", "where do we stand", "on track",
                ],
            ),
            PolicyRule(
                topic="scheduling",
                tier=PolicyTier.YELLOW,
                description="Agree to meetings, calls, or times in the principal's calendar.",
                triggers=[
                    "schedule", "set up a call", "book a meeting", "are you free",
                    "what time", "next week", "calendar", "availability",
                ],
            ),
        ),
    ),
    "candidate": PolicyTemplate(
        key="candidate",
        name="Job seeker",
        summary=(
            "Built for interviews and recruiter calls: freely discusses your background, "
            "skills and availability, escalates offers and start dates, refuses pay talk."
        ),
        extra_rules=(
            PolicyRule(
                topic="availability",
                tier=PolicyTier.GREEN,
                description="State when the principal is available to start or to meet.",
                triggers=[
                    "available", "availability", "notice period", "how soon can you",
                    "start working", "free to chat",
                ],
            ),
            PolicyRule(
                topic="offer_acceptance",
                tier=PolicyTier.YELLOW,
                description="Accepting an offer, agreeing a start date, or committing to join.",
                triggers=[
                    "accept the offer", "accept our offer", "take the job", "join us",
                    "start date", "when can you start", "sign the offer",
                ],
            ),
        ),
    ),
    "cautious": PolicyTemplate(
        key="cautious",
        name="Cautious",
        summary=(
            "Speaks only from your documents and escalates almost everything else. "
            "The safest starting point if you are not sure what your delegate should say."
        ),
        extra_rules=(
            PolicyRule(
                topic="general_questions",
                tier=PolicyTier.YELLOW,
                description="Anything asked of the principal that is not plainly documented.",
                triggers=[
                    "what do you think", "can you", "would you", "will you",
                    "do you agree", "is that ok", "shall we",
                ],
            ),
        ),
    ),
}

DEFAULT_TEMPLATE: str = "balanced"


def build_profile(
    *, slug: str, display_name: str, aliases: list[str] | None = None, template: str = DEFAULT_TEMPLATE
) -> UserProfile:
    """A complete, valid :class:`UserProfile` for a new principal."""
    chosen = TEMPLATES.get(template, TEMPLATES[DEFAULT_TEMPLATE])
    return UserProfile(
        user_id=slug,
        display_name=display_name,
        aliases=list(aliases or []),
        policy=chosen.rules(),
        knowledge_base=list(chosen.knowledge),
    )


def describe_templates() -> list[dict[str, object]]:
    """Template metadata for the onboarding picker."""
    return [
        {
            "key": t.key,
            "name": t.name,
            "summary": t.summary,
            "rule_count": len(t.rules()),
            "tiers": {
                tier.value: sum(1 for rule in t.rules() if rule.tier is tier) for tier in PolicyTier
            },
        }
        for t in TEMPLATES.values()
    ]
