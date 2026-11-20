"""KukeAuth（统一账号）的表，契约见 docs/kukeauth.md。

令牌、授权码、验证码、client secret 都只存哈希。
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base
from app.models.user import utc_now


class OAuthClient(Base):
    __tablename__ = "oauth_clients"

    id: Mapped[int] = mapped_column(primary_key=True, index=True)
    client_id: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    # 空 = 公开 client（桌面端、纯前端），只靠 PKCE
    secret_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    name: Mapped[str] = mapped_column(String(80))
    icon_url: Mapped[str | None] = mapped_column(String(500), nullable=True)
    homepage_url: Mapped[str | None] = mapped_column(String(500), nullable=True)
    # 空 = 后台建的官方应用；有值 = 开发者在账号站自己建的第三方应用，授权页会标出开发者
    owner_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    # 下面三列都是 JSON 字符串数组
    redirect_uris: Mapped[str] = mapped_column(Text, default="[]")
    post_logout_redirect_uris: Mapped[str] = mapped_column(Text, default="[]")
    allowed_scopes: Mapped[str] = mapped_column(Text, default="[]")
    # 只有后台能停用；开发者自己不能重新启用
    disabled: Mapped[bool] = mapped_column(Boolean, default=False, server_default="0")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, onupdate=utc_now)


class OAuthRequest(Base):
    """校验通过、等用户登录或确认的授权请求。页面只拿到 id，真正的跳转地址留在服务端，所以它不会变成开放跳转。"""

    __tablename__ = "oauth_requests"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    client_id: Mapped[str] = mapped_column(String(64), index=True)
    redirect_uri: Mapped[str] = mapped_column(Text)
    scope: Mapped[str] = mapped_column(Text)
    state: Mapped[str] = mapped_column(Text)
    nonce: Mapped[str | None] = mapped_column(Text, nullable=True)
    code_challenge: Mapped[str] = mapped_column(String(128))
    force_login: Mapped[bool] = mapped_column(Boolean, default=False, server_default="0")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    consumed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class OAuthCode(Base):
    __tablename__ = "oauth_codes"

    code_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    client_id: Mapped[str] = mapped_column(String(64), index=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    redirect_uri: Mapped[str] = mapped_column(Text)
    scope: Mapped[str] = mapped_column(Text)
    nonce: Mapped[str | None] = mapped_column(Text, nullable=True)
    code_challenge: Mapped[str] = mapped_column(String(128))
    auth_time: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class OAuthRefreshToken(Base):
    """每次刷新都换新令牌。同一组（family_id）里被换掉的旧令牌再次出现，说明令牌泄露了，吊销整组。"""

    __tablename__ = "oauth_refresh_tokens"

    token_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    family_id: Mapped[str] = mapped_column(String(64), index=True)
    # 从哪个授权码来的：授权码被重放时靠它找到要吊销的令牌
    code_hash: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    client_id: Mapped[str] = mapped_column(String(64), index=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    scope: Mapped[str] = mapped_column(Text)
    auth_time: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    replaced_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class OAuthGrant(Base):
    """用户同意过某个服务拿哪些信息，也是「已授权应用」页面的数据来源。"""

    __tablename__ = "oauth_grants"
    __table_args__ = (UniqueConstraint("user_id", "client_id", name="uq_oauth_grant_user_client"),)

    id: Mapped[int] = mapped_column(primary_key=True, index=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    client_id: Mapped[str] = mapped_column(String(64), index=True)
    scope: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, onupdate=utc_now)


class AccountSession(Base):
    """账号站自己的登录会话，和聊天的 auth_sessions 分开：cookie 只属于账号站的域名。"""

    __tablename__ = "account_sessions"

    session_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    client_ip: Mapped[str | None] = mapped_column(String(45), nullable=True)
    user_agent: Mapped[str | None] = mapped_column(String(500), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)


class EmailVerification(Base):
    __tablename__ = "email_verifications"

    id: Mapped[int] = mapped_column(primary_key=True, index=True)
    # bind 时是当前登录的人；reset 时是这个邮箱的主人
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    email: Mapped[str] = mapped_column(String(255), index=True)
    purpose: Mapped[str] = mapped_column(String(16))
    code_hash: Mapped[str] = mapped_column(String(64))
    attempts: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
