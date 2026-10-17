"""群 / 金币的后台运维命令：在服务器的后端目录里、用生产配置跑（docs/group-plus.md 9.3）。

    python -m app.cli.admin_group_tools --find "官方"
    python -m app.cli.admin_group_tools --admin <管理员用户名> --permanent-max <群 id>
    python -m app.cli.admin_group_tools --admin <管理员用户名> --unlimited <群 id>
    python -m app.cli.admin_group_tools --admin <管理员用户名> --grant-coins <用户 id 或用户名> <金额> --reason "原因"
    ……以上都默认只预览（dry-run），确认无误后加 --apply 才写库。

scripts/admin_group_tools.py 是同一个东西的外壳（发布只带 app/ 目录，所以实现放在这里）。

写操作全部走和后台接口相同的服务函数（group_plus.admin_grant / admin_set_member_limit、shop.admin_grant_coins），
所以审计日志、group_plus_records / coin_grants 记录照常写，操作人记在 --admin 指定的管理员名下（必须是已有的平台管理员）。
实时事件（group.plus.updated、shop.balance.changed）尽力而为：配了 REDIS_URL 时经 Redis 转给在线的人；没配时这个进程
连不到在线用户，客户端下次拉取（或重连）就能看到新状态。
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from typing import Any

from sqlalchemy import func, inspect, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import group_plus_tiers as tiers
from app.core.database import AsyncSessionLocal
from app.models.conversation import Conversation, ConversationMember
from app.models.enums import ConversationType, PlatformRole
from app.models.user import User


def _fmt_limit(conversation: Conversation) -> str:
    limit = tiers.member_limit_of(conversation)
    override = conversation.member_limit_override
    source = "跟随档位" if override is None else ("后台设为不限" if override == 0 else "后台自定义")
    return f"{'不限' if limit is None else limit}（{source}）"


def _fmt_tier(conversation: Conversation) -> str:
    tier = tiers.conversation_tier(conversation)
    if tier == tiers.FREE:
        return "免费"
    name = tiers.TIERS[tier]["name"]
    if tiers.is_permanent(conversation.plus_expires_at):
        return f"永久 {name}"
    expires = tiers.aware(conversation.plus_expires_at)
    return f"{name}（到 {expires.isoformat() if expires else '?'}）"


async def _member_count(db: AsyncSession, conversation_id: int) -> int:
    return int(await db.scalar(select(func.count()).select_from(ConversationMember).where(ConversationMember.conversation_id == conversation_id)) or 0)


async def _check_schema(db: AsyncSession) -> None:
    """新列要等新版本的后端启动过一次（init_db 会补上）才有。没有就别往下走。"""
    connection = await db.connection()

    def _snapshot(sync_conn: Any) -> tuple[set[str], set[str]]:
        inspector = inspect(sync_conn)
        tables = set(inspector.get_table_names())
        columns = {column["name"] for column in inspector.get_columns("conversations")} if "conversations" in tables else set()
        return tables, columns

    tables, columns = await connection.run_sync(_snapshot)
    missing = []
    if "member_limit_override" not in columns:
        missing.append("conversations.member_limit_override")
    if "coin_grants" not in tables:
        missing.append("coin_grants 表")
    if missing:
        sys.exit(f"数据库里还没有 {'、'.join(missing)}：先部署新版本并让后端启动一次（启动时自动迁移），再跑这个命令。")


async def _require_admin(db: AsyncSession, username: str | None) -> User:
    if not username:
        sys.exit("写操作要用 --admin <用户名> 指定操作人（必须是平台管理员）")
    admin = (await db.execute(select(User).where(User.username == username))).scalar_one_or_none()
    if admin is None:
        sys.exit(f"没有用户名为 {username!r} 的用户")
    if admin.platform_role != PlatformRole.ADMIN:
        sys.exit(f"{username!r} 不是平台管理员，不能作为操作人")
    return admin


async def _require_group(db: AsyncSession, conversation_id: int) -> Conversation:
    conversation = await db.get(Conversation, conversation_id)
    if conversation is None or conversation.type != ConversationType.GROUP:
        sys.exit(f"群 {conversation_id} 不存在（或者不是群聊）")
    return conversation


async def _resolve_user(db: AsyncSession, value: str) -> User:
    user = None
    if value.isdigit():
        user = await db.get(User, int(value))
    if user is None:
        user = (await db.execute(select(User).where(User.username == value))).scalar_one_or_none()
    if user is None:
        sys.exit(f"找不到用户 {value!r}（按 id 和用户名都找过了）")
    return user


async def find_groups(db: AsyncSession, term: str) -> None:
    like = f"%{term.strip()}%"
    rows = (
        await db.execute(
            select(Conversation).where(Conversation.type == ConversationType.GROUP, Conversation.title.ilike(like)).order_by(Conversation.id.asc()).limit(50)
        )
    ).scalars().all()
    if not rows:
        print(f"没有群名包含 {term!r} 的群")
        return
    print(f"{'id':>8}  {'人数':>6}  档位 / 人数上限  群名")
    for conversation in rows:
        count = await _member_count(db, conversation.id)
        print(f"{conversation.id:>8}  {count:>6}  {_fmt_tier(conversation)} / {_fmt_limit(conversation)}  {conversation.title}")
    if len(rows) == 50:
        print("（只列前 50 个，换个更具体的关键词）")


async def _announce(db: AsyncSession, state: dict[str, Any], reason: str) -> None:
    from app.api.v1.admin_group_plus import announce_plus_updated

    await announce_plus_updated(db, state, reason)


async def permanent_max(db: AsyncSession, admin: User, conversation_id: int, apply: bool) -> None:
    from app.services import group_plus as plus_service

    conversation = await _require_group(db, conversation_id)
    print(f"群 {conversation.id}「{conversation.title}」现在：{_fmt_tier(conversation)}")
    if tiers.conversation_tier(conversation) == "max" and tiers.is_permanent(conversation.plus_expires_at):
        print("已经是永久 MAX，不用再改")
        return
    if not apply:
        print("[预览] 将改为：永久 MAX（写一条 group_plus_records、审计日志 group_plus.grant，操作人 %s）。加 --apply 执行" % admin.username)
        return
    result = await plus_service.admin_grant(db, admin, {"conversation_id": conversation_id, "tier": "max", "permanent": True, "note": "admin_group_tools: 永久 MAX"})
    await _announce(db, result["conversation"], result["reason"])
    fresh = await db.get(Conversation, conversation_id, populate_existing=True)
    print(f"[已执行] 群 {conversation_id} 现在：{_fmt_tier(fresh)}（记录 #{result['record']['id']}）")


async def unlimited(db: AsyncSession, admin: User, conversation_id: int, apply: bool) -> None:
    from app.services import group_plus as plus_service

    conversation = await _require_group(db, conversation_id)
    print(f"群 {conversation.id}「{conversation.title}」现在的人数上限：{_fmt_limit(conversation)}，现有 {await _member_count(db, conversation_id)} 人")
    if conversation.member_limit_override == tiers.MEMBER_LIMIT_UNLIMITED:
        print("已经是人数不限，不用再改")
        return
    if not apply:
        print("[预览] 将改为：人数不限（member_limit_override = 0，审计日志 group_plus.member_limit，操作人 %s）。加 --apply 执行" % admin.username)
        return
    result = await plus_service.admin_set_member_limit(db, admin, conversation_id, tiers.MEMBER_LIMIT_UNLIMITED)
    if result["changed"]:
        await _announce(db, result["conversation"], "member_limit")
    fresh = await db.get(Conversation, conversation_id, populate_existing=True)
    print(f"[已执行] 群 {conversation_id} 的人数上限：{_fmt_limit(fresh)}")


async def grant_coins(db: AsyncSession, admin: User, who: str, amount: int, reason: str | None, apply: bool) -> None:
    from fastapi import HTTPException

    from app.services import shop as shop_service

    user = await _resolve_user(db, who)
    try:
        amount, clean_reason = shop_service.validate_coin_grant(amount, reason)
    except HTTPException as exc:
        sys.exit(f"参数不对：{exc.detail.get('message') if isinstance(exc.detail, dict) else exc.detail}")
    balance = (await shop_service.balance_of(db, user.id))["balance"]
    verb = "赠送" if amount > 0 else "扣除"
    print(f"用户 {user.id}（{user.username}{' / ' + user.nickname if user.nickname else ''}）现在余额 {balance}")
    if amount < 0 and balance + amount < 0:
        sys.exit(f"扣 {-amount} 之后会变成负数（{balance + amount}），不执行")
    if not apply:
        print(f"[预览] 将{verb} {abs(amount)} 金币，余额变成 {balance + amount}，原因：{clean_reason}（操作人 {admin.username}）。加 --apply 执行")
        return
    try:
        result = await shop_service.admin_grant_coins(db, admin, user.id, amount, clean_reason)
    except HTTPException as exc:
        sys.exit(f"没有执行：{exc.detail.get('message') if isinstance(exc.detail, dict) else exc.detail}")
    print(f"[已执行] {verb} {abs(amount)} 金币（记录 #{result['grant']['id']}），余额 {result['balance_before']} → {result['balance']['balance']}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="admin_group_tools", description="群 / 金币后台运维命令。默认只预览，加 --apply 才写库。")
    actions = parser.add_mutually_exclusive_group(required=True)
    actions.add_argument("--find", metavar="群名片段", help="按群名找群：列出 id、人数、档位、人数上限")
    actions.add_argument("--permanent-max", metavar="群id", type=int, help="把这个群设为永久 MAX")
    actions.add_argument("--unlimited", metavar="群id", type=int, help="把这个群的人数上限设为不限")
    actions.add_argument("--grant-coins", nargs=2, metavar=("用户id或用户名", "金额"), help="给用户加金币（负数是扣除）")
    parser.add_argument("--reason", help="--grant-coins 的原因（必填，最多 200 字，会展示给用户）")
    parser.add_argument("--admin", help="操作人的用户名（必须是平台管理员）。写操作必填")
    parser.add_argument("--apply", action="store_true", help="真的写库；不加只预览")
    return parser


async def main(argv: list[str] | None = None) -> None:
    args = build_parser().parse_args(argv)
    from app.services import shop as shop_service
    from app.websocket.manager import manager

    async with AsyncSessionLocal() as db:
        if args.find is not None:
            await find_groups(db, args.find)
            return
        await _check_schema(db)
        admin = await _require_admin(db, args.admin)
        if args.apply:
            # 配了 REDIS_URL 时，事件经 Redis 转给在线的 worker；没配时这一步什么都不做
            await manager.start()
        try:
            if args.permanent_max is not None:
                await permanent_max(db, admin, args.permanent_max, args.apply)
            elif args.unlimited is not None:
                await unlimited(db, admin, args.unlimited, args.apply)
            else:
                who, raw_amount = args.grant_coins
                try:
                    amount = int(raw_amount)
                except ValueError:
                    sys.exit(f"金额 {raw_amount!r} 不是整数")
                await grant_coins(db, admin, who, amount, args.reason, args.apply)
            if args.apply:
                await shop_service.drain()
                if not manager.redis_enabled():
                    print("（没有配置 REDIS_URL：这个进程推不到在线用户，客户端刷新 / 重连后会看到新状态）")
        finally:
            if args.apply:
                await manager.stop()


if __name__ == "__main__":
    asyncio.run(main())
