from __future__ import annotations

import hashlib
import secrets
from datetime import datetime, timedelta, timezone

UTC = timezone.utc

from fastapi import HTTPException, status
from sqlalchemy import delete, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.device import DeviceContext
from app.core.security import create_access_token, hash_password, verify_password
from app.models.conversation import Conversation, ConversationMember
from app.models.enums import ConversationType, MemberRole, PasswordResetStatus
from app.models.enums import UserStatus
from app.models.report import PasswordResetRequest
from app.models.user import AuthSession, CcwPasswordChallenge, User
from app.schemas.auth import AccountLookupRead, CookieSessionRead, PasswordResetRequestCreate, Token, UserLogin, UserRegister
from app.services import device_bans, device_signals
from app.services.ccw import CHALLENGE_ALPHABET, ccw_gateway_available, fetch_comment_page


OFFICIAL_GROUP_ID = 1
COOKIE_SESSION_SEEN_INTERVAL = timedelta(seconds=60)


def hash_session_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def session_expiry(days: int) -> datetime:
    return datetime.now(UTC) + timedelta(days=days)


async def create_cookie_session(db: AsyncSession, user: User, client_ip: str, user_agent: str | None, days: int) -> tuple[str, AuthSession]:
    raw_token = secrets.token_urlsafe(48)
    session = AuthSession(
        user_id=user.id,
        session_hash=hash_session_token(raw_token),
        client_ip=client_ip,
        user_agent=user_agent[:500] if user_agent else None,
        expires_at=session_expiry(days),
    )
    db.add(session)
    await db.commit()
    await db.refresh(session)
    return raw_token, session


async def cookie_login_user(
    db: AsyncSession, payload: UserLogin, client_ip: str, user_agent: str | None, days: int, device: DeviceContext | None = None
) -> tuple[str, CookieSessionRead]:
    user = await get_user_by_username_or_email(db, payload.username_or_email)
    if user is None or not verify_password(payload.password, user.hashed_password):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid username/email or password")
    await device_bans.on_login(db, user, device)
    await ensure_user_not_disabled(db, user, issue_appeal_token=True)
    await device_bans.guard(db, device, user)
    mark_user_seen(user, client_ip, login=True)
    raw_token, session = await create_cookie_session(db, user, client_ip, user_agent, days)
    return raw_token, CookieSessionRead(access_token=create_access_token(user.id), user=user, expires_at=session.expires_at)


async def get_cookie_session_user(
    db: AsyncSession, session_token: str | None, client_ip: str, user_agent: str | None, device: DeviceContext | None = None
) -> CookieSessionRead:
    if not session_token:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="No active session")

    result = await db.execute(select(AuthSession).where(AuthSession.session_hash == hash_session_token(session_token)))
    session = result.scalar_one_or_none()
    now = datetime.now(UTC)
    if session is None or session.revoked_at is not None or ensure_aware_utc(session.expires_at) <= now:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="No active session")

    user = await db.get(User, session.user_id)
    if user is None:
        session.revoked_at = now
        await db.commit()
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="No active session")

    await ensure_user_not_disabled(db, user)
    await device_bans.guard_request(db, device, user)
    # A missing User-Agent historically leaves the stored value unchanged.
    current_user_agent = user_agent[:500] if user_agent else session.user_agent
    session_seen_recently = (
        session.last_used_at is not None
        and timedelta(0) <= now - ensure_aware_utc(session.last_used_at) < COOKIE_SESSION_SEEN_INTERVAL
    )
    user_seen_recently = (
        user.last_seen_at is not None
        and timedelta(0) <= now - ensure_aware_utc(user.last_seen_at) < COOKIE_SESSION_SEEN_INTERVAL
    )
    # Always validate both rows above, but only write when their persisted presence
    # needs advancing. No-write responses expose the freshly loaded DB user fields.
    if (
        not session_seen_recently
        or not user_seen_recently
        or session.client_ip != client_ip
        or session.user_agent != current_user_agent
        or user.last_seen_ip != client_ip
    ):
        mark_user_seen(user, client_ip)
        session.last_used_at = now
        session.client_ip = client_ip
        session.user_agent = current_user_agent
        await db.commit()
        await db.refresh(user)
    return CookieSessionRead(access_token=create_access_token(user.id), user=user, expires_at=session.expires_at)


async def revoke_cookie_session(db: AsyncSession, session_token: str | None) -> None:
    if not session_token:
        return
    await db.execute(delete(AuthSession).where(AuthSession.session_hash == hash_session_token(session_token)))
    await db.commit()


async def get_user_by_id(db: AsyncSession, user_id: int) -> User | None:
    return await db.get(User, user_id)


async def get_user_by_username_or_email(db: AsyncSession, value: str) -> User | None:
    result = await db.execute(select(User).where(or_(User.username == value, User.email == value)))
    return result.scalar_one_or_none()


APPEAL_TOKEN_SCOPE = "appeal"
APPEAL_TOKEN_MINUTES = 30


async def ensure_user_not_disabled(db: AsyncSession, user: User, *, issue_appeal_token: bool = False) -> None:
    """issue_appeal_token 只能在刚验证过密码的登录路径上打开：被封的人拿不到正常令牌，
    需要一张只能查看处罚和提交申诉的短期令牌。"""
    if user.status != UserStatus.DISABLED:
        return
    now = datetime.now(UTC)
    if user.banned_until is not None:
        banned_until = ensure_aware_utc(user.banned_until)
        if banned_until <= now:
            user.status = UserStatus.ACTIVE
            user.banned_until = None
            user.banned_at = None
            user.ban_reason = None
            await db.commit()
            await db.refresh(user)
            return
    detail: dict[str, object] = {
        "code": "account_suspended",
        "message": "账号已被封禁",
        "reason": user.ban_reason,
        "banned_until": ensure_aware_utc(user.banned_until).isoformat() if user.banned_until else None,
        "permanent": user.banned_until is None,
    }
    if issue_appeal_token:
        detail["appeal_token"] = create_access_token(
            user.id, timedelta(minutes=APPEAL_TOKEN_MINUTES), scope=APPEAL_TOKEN_SCOPE
        )
    raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=detail)


def ensure_aware_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


async def get_user_by_registration_ip(db: AsyncSession, client_ip: str) -> User | None:
    result = await db.execute(select(User).where(User.registration_ip == client_ip))
    return result.scalar_one_or_none()


async def register_user(db: AsyncSession, payload: UserRegister, client_ip: str, device: DeviceContext | None = None) -> Token:
    email = str(payload.email) if payload.email is not None else None
    checks = [User.username == payload.username]
    if email is not None:
        checks.append(User.email == email)

    existing = await db.execute(select(User).where(or_(*checks)))
    if existing.scalar_one_or_none() is not None:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Username or email already exists")

    ip_user = await get_user_by_registration_ip(db, client_ip)
    if ip_user is not None:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="This IP address has already registered an account")
    # 防小号（docs/device-bans.md 第 4 节）：设备被封、这台设备注册的号太多、同一网段今天注册太多
    await device_bans.check_registration(db, device)

    user = User(
        username=payload.username,
        email=email,
        hashed_password=hash_password(payload.password),
        nickname=payload.nickname or payload.username,
        registration_ip=client_ip,
        last_login_ip=client_ip,
        last_login_at=datetime.now(UTC),
        last_seen_ip=client_ip,
        last_seen_at=datetime.now(UTC),
    )
    db.add(user)
    if device is not None:
        await db.flush()
        await device_signals.record_now(db, user.id, device, registered=True)
    await db.commit()
    await db.refresh(user)
    await add_user_to_official_group(db, user.id)
    return Token(access_token=create_access_token(user.id), user=user)


async def add_user_to_official_group(db: AsyncSession, user_id: int) -> None:
    conversation = await db.get(Conversation, OFFICIAL_GROUP_ID)
    if conversation is None or conversation.type != ConversationType.GROUP:
        return

    existing = await db.execute(
        select(ConversationMember.id).where(
            ConversationMember.conversation_id == OFFICIAL_GROUP_ID,
            ConversationMember.user_id == user_id,
        )
    )
    if existing.scalar_one_or_none() is not None:
        return

    db.add(ConversationMember(conversation_id=OFFICIAL_GROUP_ID, user_id=user_id, role=MemberRole.MEMBER))
    await db.commit()


async def login_user(db: AsyncSession, payload: UserLogin, client_ip: str, device: DeviceContext | None = None) -> Token:
    user = await get_user_by_username_or_email(db, payload.username_or_email)
    if user is None or not verify_password(payload.password, user.hashed_password):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid username/email or password")
    # 先记下这次用的设备（被封的号来登录也记，这样才知道它换到了哪台设备上），再判断账号和设备是否被封
    await device_bans.on_login(db, user, device)
    await ensure_user_not_disabled(db, user, issue_appeal_token=True)
    await device_bans.guard(db, device, user)
    mark_user_seen(user, client_ip, login=True)
    await db.commit()
    await db.refresh(user)
    return Token(access_token=create_access_token(user.id), user=user)


def mark_user_seen(user: User, client_ip: str, login: bool = False) -> None:
    now = datetime.now(UTC)
    user.last_seen_ip = client_ip
    user.last_seen_at = now
    if login:
        user.last_login_ip = client_ip
        user.last_login_at = now


async def create_password_reset_request(db: AsyncSession, payload: PasswordResetRequestCreate, client_ip: str) -> PasswordResetRequest:
    user = await get_user_by_username_or_email(db, payload.username)
    item = PasswordResetRequest(
        username=payload.username,
        account_user_id=user.id if user else None,
        ccw_profile_url=payload.ccw_profile_url,
        reason=payload.reason,
        requested_password=payload.requested_password,
        request_ip=client_ip,
        account_registration_ip=user.registration_ip if user else None,
        status=PasswordResetStatus.PENDING,
    )
    db.add(item)
    await db.commit()
    await db.refresh(item)
    return item


# ----- CCW 评论验证码改密（忘记密码快速重置 / 设置页修改密码） -----

CCW_PASSWORD_CHALLENGE_MINUTES = 10


async def lookup_account(db: AsyncSession, username: str) -> AccountLookupRead:
    user = await get_user_by_username_or_email(db, username)
    return AccountLookupRead(exists=user is not None, ccw_bound=bool(user and user.ccw_student_oid))


def _generate_ccw_password_code() -> str:
    return f"{''.join(secrets.choice(CHALLENGE_ALPHABET) for _ in range(4))}-{''.join(secrets.choice(CHALLENGE_ALPHABET) for _ in range(4))}"


async def create_ccw_password_challenge(db: AsyncSession, user: User, purpose: str) -> CcwPasswordChallenge:
    now = datetime.now(UTC)
    stale_result = await db.execute(
        select(CcwPasswordChallenge).where(
            CcwPasswordChallenge.user_id == user.id,
            CcwPasswordChallenge.purpose == purpose,
            CcwPasswordChallenge.status == "pending",
        )
    )
    for stale in stale_result.scalars().all():
        stale.status = "expired"
    for _ in range(10):
        code = _generate_ccw_password_code()
        duplicate = await db.execute(
            select(CcwPasswordChallenge.id).where(CcwPasswordChallenge.code == code, CcwPasswordChallenge.status == "pending").limit(1)
        )
        if duplicate.scalar_one_or_none() is None:
            challenge = CcwPasswordChallenge(
                user_id=user.id,
                code=code,
                purpose=purpose,
                expires_at=now + timedelta(minutes=CCW_PASSWORD_CHALLENGE_MINUTES),
            )
            db.add(challenge)
            await db.commit()
            await db.refresh(challenge)
            return challenge
    raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="验证码生成失败，请重试")


def _find_code_comment(comments: list[dict], student_oid: str, code: str) -> dict | None:
    code_upper = code.upper()
    for comment in comments:
        content = str(comment.get("content") or "")
        if code_upper not in content.upper():
            continue
        commenter = comment.get("commenter")
        if not isinstance(commenter, dict):
            continue
        oid = commenter.get("accountOid")
        if isinstance(oid, str) and oid.lower() == student_oid.lower():
            return comment
    return None


async def confirm_ccw_password_challenge(db: AsyncSession, user: User, code: str, purpose: str, new_password: str) -> None:
    now = datetime.now(UTC)
    result = await db.execute(
        select(CcwPasswordChallenge)
        .where(
            CcwPasswordChallenge.user_id == user.id,
            CcwPasswordChallenge.purpose == purpose,
            CcwPasswordChallenge.code == code.strip().upper(),
            CcwPasswordChallenge.status == "pending",
        )
        .order_by(CcwPasswordChallenge.created_at.desc())
        .limit(1)
    )
    challenge = result.scalar_one_or_none()
    if challenge is None or ensure_aware_utc(challenge.expires_at) <= now:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="验证码不存在或已过期，请重新获取")
    if not user.ccw_student_oid:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="该账号未绑定 CCW，无法使用验证码验证")
    if not ccw_gateway_available():
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="本服务未接入 CCW，无法使用验证码验证")
    comments = await fetch_comment_page()
    matched = _find_code_comment(comments, user.ccw_student_oid, challenge.code)
    if matched is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="暂未在评论区检测到验证码，请确认已发送后重试")
    challenge.status = "consumed"
    challenge.consumed_at = now
    challenge.matched_comment_id = str(matched.get("id")) if matched.get("id") is not None else None
    user.hashed_password = hash_password(new_password)
    user.ip_login_expires_at = None
    await db.commit()
