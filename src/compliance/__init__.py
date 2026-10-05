"""Compliance: the legal documents users accept, and the recording-consent gate."""

from src.compliance.consent import (
    ConsentDecision,
    ConsentGate,
    ConsentSignal,
    classify,
    describe_modes,
    disclosure_line,
    mode_from_value,
)
from src.compliance.legal import (
    REQUIRED_DOCUMENTS,
    LegalDocument,
    accept_all,
    all_documents,
    current_versions,
    load_document,
    outstanding_documents,
)

__all__ = [
    "REQUIRED_DOCUMENTS",
    "ConsentDecision",
    "ConsentGate",
    "ConsentSignal",
    "LegalDocument",
    "accept_all",
    "all_documents",
    "classify",
    "current_versions",
    "describe_modes",
    "disclosure_line",
    "load_document",
    "mode_from_value",
    "outstanding_documents",
]
