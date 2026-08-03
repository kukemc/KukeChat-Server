from __future__ import annotations

from datetime import datetime

from pydantic import AliasChoices, BaseModel, EmailStr, Field

from app.schemas.user import UserRead


class UserRegister(BaseModel):
    username: str = Field(min_length=3, max_length=50, pattern=r"^[A-Za-z0-9_.-]+$")
    email: EmailStr | None = None
    password: str = Field(min_length=6, max_length=128)
    nickname: str | None = Field(default=None, max_length=80)


class UserLogin(BaseModel):
    username_or_email: str = Field(
        min_length=1,
        max_length=255,
        validation_alias=AliasChoices("username_or_email", "username"),
    )
    password: str = Field(min_length=1, max_length=128)
    remember_me: bool = Field(default=False, validation_alias=AliasChoices("remember_me", "rememberMe"))


class Token(BaseModel):
    access_token: str
    token_type: str = "bearer"
    user: UserRead


class CookieSessionRead(Token):
    expires_at: datetime


class IpLoginStatus(BaseModel):
    client_ip: str
    registration_ip: str | None = None
    ip_login_expires_at: datetime | None = None
    ip_login_available: bool


class PasswordResetRequestCreate(BaseModel):
    username: str = Field(min_length=3, max_length=50, pattern=r"^[A-Za-z0-9_.-]+$")
    ccw_profile_url: str = Field(min_length=8, max_length=500, pattern=r"^https://www\.ccw\.site/student/[^\s/]+/?$")
    requested_password: str = Field(min_length=6, max_length=128)
    reason: str = Field(min_length=10, max_length=2000)


class PasswordResetRequestRead(BaseModel):
    id: int
    username: str
    ccw_profile_url: str
    reason: str
    status: str
    created_at: datetime
    reviewed_at: datetime | None = None
    review_note: str | None = None


class AccountLookupRead(BaseModel):
    exists: bool
    ccw_bound: bool


class CcwPasswordChallengeCreate(BaseModel):
    username: str = Field(min_length=3, max_length=50, pattern=r"^[A-Za-z0-9_.-]+$")


class CcwPasswordChallengeRead(BaseModel):
    code: str
    comment_url: str
    expires_in: int


class CcwPasswordResetConfirm(BaseModel):
    username: str = Field(min_length=3, max_length=50, pattern=r"^[A-Za-z0-9_.-]+$")
    code: str = Field(min_length=9, max_length=9)
    new_password: str = Field(min_length=6, max_length=128)


class CcwPasswordChangeConfirm(BaseModel):
    code: str = Field(min_length=9, max_length=9)
    new_password: str = Field(min_length=6, max_length=128)
