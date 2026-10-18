"""Pydantic contracts for the organizations feature."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, EmailStr, Field

from src.db.models import OrganizationRole


class OrganizationSummary(BaseModel):
    """The caller's own organization, as embedded in their account."""

    id: str
    name: str
    role: OrganizationRole
    plan: str
    seats_used: int
    seats_limit: int


class MemberRow(BaseModel):
    """One organization member. Usage fields are populated for admin callers only."""

    user_id: str
    email: str
    display_name: str
    role: OrganizationRole
    joined_at: datetime
    meetings_this_month: int | None = None
    llm_tokens: float | None = None
    estimated_cost_usd: float | None = None


class InviteRow(BaseModel):
    """One invite. ``invite_url`` is present only on the response that just minted it."""

    id: str
    email: str
    role: OrganizationRole
    invite_url: str | None = None
    expires_at: datetime
    accepted_at: datetime | None = None
    revoked_at: datetime | None = None


class InvitePreview(BaseModel):
    """What an invite-landing page shows before anyone has signed in."""

    org_name: str
    email: str
    role: OrganizationRole
    valid: bool


class CreateOrganizationRequest(BaseModel):
    """``POST /api/v1/organizations``."""

    name: str = Field(..., min_length=1, max_length=200)


class InviteRequest(BaseModel):
    """``POST /api/v1/organizations/invites``."""

    email: EmailStr
    role: OrganizationRole = OrganizationRole.MEMBER


class RoleUpdateRequest(BaseModel):
    """``PATCH /api/v1/organizations/members/{user_id}``."""

    role: OrganizationRole
