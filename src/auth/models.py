"""Request and response contracts for the authentication endpoints."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, EmailStr, Field, field_validator

from src.auth.security import MIN_PASSWORD_LENGTH, password_problems


class SignupRequest(BaseModel):
    """``POST /api/v1/auth/signup``."""

    email: EmailStr = Field(..., description="Sign-in address; also where account mail would go.")
    password: str = Field(..., min_length=MIN_PASSWORD_LENGTH, max_length=200)
    display_name: str = Field(
        ..., min_length=1, max_length=120,
        description="The principal's real name, as it appears in meetings.",
    )
    accept_terms: bool = Field(
        ..., description="Must be true: the current Terms of Service and Privacy Policy are recorded as accepted."
    )
    policy_template: str = Field(
        default="balanced", description="Which starter Representation Policy to install."
    )

    @field_validator("password")
    @classmethod
    def _strong_enough(cls, password: str) -> str:
        problems = password_problems(password)
        if problems:
            raise ValueError("password " + ", ".join(problems))
        return password

    @field_validator("accept_terms")
    @classmethod
    def _must_accept(cls, accepted: bool) -> bool:
        if not accepted:
            raise ValueError("the Terms of Service and Privacy Policy must be accepted to create an account")
        return accepted


class LoginRequest(BaseModel):
    """``POST /api/v1/auth/login``."""

    email: EmailStr
    password: str = Field(..., min_length=1, max_length=200)


class RefreshRequest(BaseModel):
    """``POST /api/v1/auth/refresh``."""

    refresh_token: str = Field(..., min_length=10)


class PasswordChangeRequest(BaseModel):
    """``POST /api/v1/auth/password``. Revokes every other session on success."""

    current_password: str = Field(..., min_length=1, max_length=200)
    new_password: str = Field(..., min_length=MIN_PASSWORD_LENGTH, max_length=200)

    @field_validator("new_password")
    @classmethod
    def _strong_enough(cls, password: str) -> str:
        problems = password_problems(password)
        if problems:
            raise ValueError("password " + ", ".join(problems))
        return password


class PrincipalSummary(BaseModel):
    """The person this account's delegate represents."""

    id: str
    slug: str = Field(..., description="What the brain calls ``user_id``.")
    display_name: str
    aliases: list[str] = Field(default_factory=list)


class AccountResponse(BaseModel):
    """The signed-in account, as the console holds it."""

    id: str
    email: str
    display_name: str
    role: str
    status: str
    created_at: datetime
    last_login_at: datetime | None = None
    principal: PrincipalSummary | None = None
    onboarding: dict[str, bool] = Field(default_factory=dict)
    onboarding_complete: bool = False
    outstanding_legal: list[str] = Field(
        default_factory=list, description="Required documents not accepted at their current version."
    )
    settings: dict = Field(default_factory=dict)


class TokenResponse(BaseModel):
    """What a successful signup, login or refresh returns."""

    access_token: str
    refresh_token: str
    token_type: str = "bearer"
    expires_at: datetime
    account: AccountResponse


class MessageResponse(BaseModel):
    """A plain acknowledgement."""

    detail: str
