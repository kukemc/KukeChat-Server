"""后台赠送金币、永久群档位、群人数上限单独设置，以及服务器上的运维命令（docs/shop.md 5.2、docs/group-plus.md 9.2 / 9.3）。

复用 test_group_plus 的 PlusCase：alice 群主（绑定 OID_A）、bob 管理员、carol / dave 成员、eve 不在群里、root 平台管理员。
"""

from __future__ import annotations

import io
import json
from contextlib import redirect_stdout
from datetime import datetime, timedelta
from unittest.mock import patch

from sqlalchemy import select

from app.cli import admin_group_tools as cli
from app.core import group_plus_tiers
from app.models.conversation import ConversationMember
from app.models.report import AdminAuditLog
from app.models.shop import CoinGrant
from app.models.user import User
from app.services import group_plus as plus_service
from test_group_plus import OID_A, PlusCase


def _parse(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


# ---------------------------------------------------------------- 赠送金币


class CoinGrantTests(PlusCase):
    async def give(self, name: str, amount: int, reason: str = "活动奖励", *, expected: int = 200) -> dict:
        return await self.json("POST", "/admin/shop/grants", as_="root", expected=expected, json={"user_id": self.ids[name], "amount": amount, "reason": reason})

    async def test_grant_counts_in_balance_and_can_be_spent(self) -> None:
        result = await self.give("alice", 100)
        self.assertEqual((result["grant"]["amount"], result["grant"]["reason"], result["grant"]["admin"]["username"]), (100, "活动奖励", "root"))
        self.assertEqual((result["balance_before"], result["balance"]["balance"], result["balance"]["granted"]), (0, 100, 100))
        self.assertEqual(await self.coins("alice"), 100)
        # shop 的 balance 对象里也有
        overview = await self.json("GET", "/shop")
        self.assertEqual((overview["balance"]["granted"], overview["balance"]["balance"]), (100, 100))
        # 后台账户列表：没投过币的人赠送之后也有账户
        accounts = await self.json("GET", "/admin/shop/accounts", as_="root")
        mine = next(item for item in accounts["items"] if item["user"]["id"] == self.ids["alice"])
        self.assertEqual((mine["granted"], mine["balance"], mine["mismatch"]), (100, 100, False))
        # 赠送的币和投的币一样能花（和购买共用同一个余额表达式）
        bought = await self.buy("pro")
        self.assertEqual((bought["balance"]["balance"], bought["balance"]["granted"]), (20, 100))
        # 再扣 30 会变负数：拒绝，什么都不写
        refused = await self.give("alice", -30, expected=409)
        self.assertEqual((refused["detail"]["code"], refused["detail"]["balance"]), ("insufficient_balance", 20))
        deducted = await self.give("alice", -20, "误发收回")
        self.assertEqual((deducted["balance"]["balance"], deducted["balance"]["granted"]), (0, 80))
        self.assertEqual(await self.count(CoinGrant), 2)

    async def test_validation_permissions_list_audit_and_notification(self) -> None:
        for body, code in (
            ({"amount": 0, "reason": "x"}, "invalid_amount"),
            ({"amount": 1_000_001, "reason": "x"}, "invalid_amount"),
            ({"amount": True, "reason": "x"}, "invalid_amount"),
            ({"amount": 5, "reason": "   "}, "invalid_reason"),
            ({"amount": 5, "reason": "长" * 201}, "invalid_reason"),
        ):
            bad = await self.json("POST", "/admin/shop/grants", as_="root", expected=422, json={"user_id": self.ids["carol"], **body})
            self.assertEqual(bad["detail"]["code"], code)
        missing = await self.json("POST", "/admin/shop/grants", as_="root", expected=404, json={"user_id": 99999, "amount": 5, "reason": "x"})
        self.assertEqual(missing["detail"]["code"], "user_not_found")
        await self.json("POST", "/admin/shop/grants", as_="carol", expected=403, json={"user_id": self.ids["carol"], "amount": 5, "reason": "x"})
        await self.json("GET", "/admin/shop/grants", as_="carol", expected=403)

        await self.give("carol", 1_000_000, "  大奖  ")
        await self.give("carol", -400_000, "收回一部分")
        await self.give("dave", 7)
        page = await self.json("GET", f"/admin/shop/grants?user_id={self.ids['carol']}", as_="root")
        self.assertEqual((page["total"], [item["amount"] for item in page["items"]], page["items"][1]["reason"]), (2, [-400_000, 1_000_000], "大奖"))
        self.assertEqual(page["summary"], {"granted": 1_000_000, "deducted": 400_000, "net": 600_000})
        deducts = await self.json("GET", "/admin/shop/grants?direction=deduct", as_="root")
        self.assertEqual(deducts["total"], 1)
        self.assertEqual((await self.json("GET", "/admin/shop/grants", as_="root"))["total"], 3)

        async with self.sessions() as db:
            logs = (await db.execute(select(AdminAuditLog).where(AdminAuditLog.action == "shop.coins.grant").order_by(AdminAuditLog.id))).scalars().all()
        self.assertEqual(len(logs), 3)
        details = json.loads(logs[1].details)
        self.assertEqual((logs[1].target_id, details["amount"], details["balance_before"], details["balance_after"]), (self.ids["carol"], -400_000, 1_000_000, 600_000))

        notifications = await self.json("GET", "/notifications", as_="carol")
        grants = [item for item in notifications["items"] if item["type"] == "coin_grant"]
        self.assertEqual([item["title"] for item in grants], ["后台扣除了你 400000 金币", "后台赠送了你 1000000 金币"])
        self.assertEqual((grants[1]["category"], grants[1]["body"]), ("system", "大奖"))


# ---------------------------------------------------------------- 永久档位


class PermanentTests(PlusCase):
    async def test_permanent_grant_blocks_purchase_and_shows_everywhere(self) -> None:
        self.ledger.donate(OID_A, 10000)
        await self.buy("pro")
        granted = await self.json(
            "POST", "/admin/group-plus/grant", as_="root", json={"conversation_id": self.group_id, "tier": "max", "permanent": True}
        )
        self.assertEqual((granted["conversation"]["plus_tier"], granted["conversation"]["plus_permanent"], granted["record"]["permanent"]), ("max", True, True))
        self.assertEqual(_parse(granted["conversation"]["plus_expires_at"]).year, 2999)
        self.assertEqual(granted["record"]["days"], 0)
        broadcast = [call.args for call in self.send_to_users.await_args_list if call.args[1] == "group.plus.updated"][-1]
        self.assertEqual((broadcast[2]["plus_permanent"], broadcast[2]["reason"]), (True, "grant"))

        status = await self.json("GET", f"/conversations/{self.group_id}/plus")
        self.assertEqual((status["tier"], status["permanent"]), ("max", True))
        listing = await self.json("GET", "/conversations")
        self.assertTrue(next(item for item in listing if item["id"] == self.group_id)["plus_permanent"])
        self.assertFalse(next(item for item in listing if item["id"] == self.direct_id)["plus_permanent"])

        # 永久的群：报价和购买都 409，不扣币
        before = await self.coins("alice")
        for tier in ("max", "pro"):
            quote = await self.code("POST", f"/conversations/{self.group_id}/plus/quote", 409, json={"tier": tier, "period": "month"})
            self.assertEqual(quote["code"], "already_permanent")
            refused = await self.code("POST", f"/conversations/{self.group_id}/plus/purchase", 409, json={"tier": tier, "period": "year", "confirm_forfeit": True})
            self.assertEqual(refused["code"], "already_permanent")
        self.assertEqual(await self.coins("alice"), before)

        # 很久以后也还是 MAX
        self.tick(days=3650 * 5)
        self.assertEqual((await self.conversation()).plus_tier, "max")
        self.assertEqual(group_plus_tiers.conversation_tier(await self.conversation()), "max")
        # 同档位再送天数：保持永久
        again = await self.grant("max", 30)
        self.assertTrue(again["conversation"]["plus_permanent"])

        groups = await self.json("GET", "/admin/group-plus/groups", as_="root")
        item = next(row for row in groups["items"] if row["conversation_id"] == self.group_id)
        self.assertEqual((item["permanent"], item["tier"], item["expired"]), (True, "max", False))
        records = await self.json("GET", f"/conversations/{self.group_id}/plus/records")
        self.assertEqual([row["permanent"] for row in records["items"]], [True, True, False])
        async with self.sessions() as db:
            self.assertTrue((await plus_service.ledger_check(db))["balanced"])

        # 后台取消照样能用
        revoked = await self.grant("free")
        self.assertEqual((revoked["conversation"]["plus_tier"], revoked["conversation"]["plus_permanent"]), ("free", False))
        bought = await self.buy("pro")
        self.assertEqual(bought["record"]["action"], "new")

    async def test_permanent_validation(self) -> None:
        bad = await self.json(
            "POST", "/admin/group-plus/grant", as_="root", expected=422, json={"conversation_id": self.group_id, "tier": "max", "permanent": "yes"}
        )
        self.assertEqual(bad["detail"]["code"], "invalid_permanent")
        # 不是永久就得给天数
        bad = await self.json("POST", "/admin/group-plus/grant", as_="root", expected=422, json={"conversation_id": self.group_id, "tier": "max", "permanent": False})
        self.assertEqual(bad["detail"]["code"], "invalid_days")
        # 有期限的升级到永久，再从永久降到有期限的 Pro（后台可以降档）
        await self.json("POST", "/admin/group-plus/grant", as_="root", json={"conversation_id": self.group_id, "tier": "pro_plus", "permanent": True})
        down = await self.grant("pro", 10)
        self.assertFalse(down["conversation"]["plus_permanent"])
        self.assertEqual(_parse(down["conversation"]["plus_expires_at"]), self.now + timedelta(days=10))
        actions = [a for a in await self.audit() if a.startswith("group_plus")]
        self.assertEqual(actions, ["group_plus.grant", "group_plus.grant"])


# ---------------------------------------------------------------- 人数上限单独设置


class MemberLimitTests(PlusCase):
    async def fill(self, count: int) -> list[int]:
        """往群里塞 count 个人，再建 3 个不在群里的人，返回这 3 个人的 id。"""
        async with self.sessions() as db:
            users = [User(username=f"m{i}", hashed_password="x") for i in range(count)]
            db.add_all(users)
            await db.flush()
            db.add_all([ConversationMember(conversation_id=self.group_id, user_id=user.id) for user in users])
            extra = [User(username=f"x{i}", hashed_password="x") for i in range(3)]
            db.add_all(extra)
            await db.flush()
            await db.commit()
            return [user.id for user in extra]

    async def set_limit(self, value, *, expected: int = 200) -> dict:
        return await self.json(
            "PUT", f"/admin/group-plus/groups/{self.group_id}/member-limit", as_="root", expected=expected, json={"member_limit_override": value}
        )

    async def test_unlimited_custom_and_back_to_tier(self) -> None:
        extra = await self.fill(195)  # 群里现在 200 人，免费档满员
        full = await self.code("POST", f"/conversations/{self.group_id}/members", 409, json={"user_ids": [extra[0]]})
        self.assertEqual((full["code"], full["limit"]), ("group_full", 200))

        result = await self.set_limit(0)
        self.assertEqual((result["changed"], result["previous"], result["group"]["member_limit"], result["group"]["member_limit_unlimited"]), (True, None, None, True))
        broadcast = [call.args for call in self.send_to_users.await_args_list if call.args[1] == "group.plus.updated"][-1]
        self.assertEqual(broadcast[2]["reason"], "member_limit")
        status = await self.json("GET", f"/conversations/{self.group_id}/plus")
        self.assertEqual(
            (status["limits"]["member_limit"], status["limits"]["member_limit_unlimited"], status["limits"]["member_limit_override"]), (None, True, 0)
        )
        await self.json("POST", f"/conversations/{self.group_id}/members", json={"user_ids": [extra[0]]}, expected=200)

        # 自定义：202 人上限，现在 201 人，还能再加 1 个
        custom = await self.set_limit(202)
        self.assertEqual((custom["group"]["member_limit"], custom["group"]["member_limit_unlimited"]), (202, False))
        await self.json("POST", f"/conversations/{self.group_id}/members", json={"user_ids": [extra[1]]}, expected=200)
        full = await self.code("POST", f"/conversations/{self.group_id}/members", 409, json={"user_ids": [extra[2]]})
        self.assertEqual((full["code"], full["limit"]), ("group_full", 202))

        # 回到跟随档位：免费 200，已有成员不踢
        back = await self.set_limit(None)
        self.assertEqual((back["group"]["member_limit"], back["group"]["member_limit_override"]), (200, None))
        same = await self.set_limit(None)
        self.assertFalse(same["changed"])
        status = await self.json("GET", f"/conversations/{self.group_id}/plus")
        self.assertEqual((status["limits"]["member_limit"], status["limits"]["member_limit_unlimited"], status["usage"]["member_count"]), (200, False, 202))

        actions = [a for a in await self.audit() if a == "group_plus.member_limit"]
        self.assertEqual(len(actions), 3)

    async def test_validation_and_admin_views(self) -> None:
        for value in (-1, 100_001, True, "0", 1.5):
            bad = await self.set_limit(value, expected=422)
            self.assertEqual(bad["detail"]["code"], "invalid_member_limit")
        await self.json("PUT", f"/admin/group-plus/groups/{self.group_id}/member-limit", as_="carol", expected=403, json={"member_limit_override": 0})
        await self.json("PUT", f"/admin/group-plus/groups/{self.direct_id}/member-limit", as_="root", expected=404, json={"member_limit_override": 0})
        # 免费群也能单独查到、找到
        detail = await self.json("GET", f"/admin/group-plus/groups/{self.group_id}", as_="root")
        self.assertEqual((detail["tier"], detail["expired"], detail["member_limit"], detail["member_count"]), ("free", False, 200, 5))
        self.assertEqual((await self.json("GET", "/admin/group-plus/groups?q=测试", as_="root"))["total"], 0)
        found = await self.json("GET", "/admin/group-plus/groups?q=测试&include_free=true", as_="root")
        self.assertEqual([row["conversation_id"] for row in found["items"]], [self.group_id])
        await self.json("GET", f"/admin/group-plus/groups/{self.direct_id}", as_="root", expected=404)


# ---------------------------------------------------------------- 运维命令


class CliTests(PlusCase):
    async def run_cli(self, *argv: str) -> str:
        out = io.StringIO()
        with patch.object(cli, "AsyncSessionLocal", self.sessions), redirect_stdout(out):
            await cli.main(list(argv))
        return out.getvalue()

    async def test_dry_run_then_apply(self) -> None:
        listing = await self.run_cli("--find", "测试")
        self.assertIn(str(self.group_id), listing)
        self.assertIn("免费", listing)

        preview = await self.run_cli("--admin", "root", "--permanent-max", str(self.group_id))
        self.assertIn("[预览]", preview)
        self.assertEqual((await self.conversation()).plus_tier, "free")
        applied = await self.run_cli("--admin", "root", "--permanent-max", str(self.group_id), "--apply")
        self.assertIn("永久 MAX", applied)
        conversation = await self.conversation()
        self.assertTrue(group_plus_tiers.is_permanent(conversation.plus_expires_at))
        self.assertEqual(conversation.plus_tier, "max")

        await self.run_cli("--admin", "root", "--unlimited", str(self.group_id))
        self.assertIsNone((await self.conversation()).member_limit_override)
        await self.run_cli("--admin", "root", "--unlimited", str(self.group_id), "--apply")
        self.assertEqual((await self.conversation()).member_limit_override, 0)

        await self.run_cli("--admin", "root", "--grant-coins", "carol", "50", "--reason", "测试补偿")
        self.assertEqual(await self.coins("carol"), 0)
        done = await self.run_cli("--admin", "root", "--grant-coins", str(self.ids["carol"]), "50", "--reason", "测试补偿", "--apply")
        self.assertIn("0 → 50", done)
        self.assertEqual(await self.coins("carol"), 50)

        async with self.sessions() as db:
            logs = (await db.execute(select(AdminAuditLog).order_by(AdminAuditLog.id))).scalars().all()
        self.assertEqual([(log.action, log.actor_id) for log in logs], [
            ("group_plus.grant", self.ids["root"]), ("group_plus.member_limit", self.ids["root"]), ("shop.coins.grant", self.ids["root"]),
        ])

    async def test_admin_must_be_a_platform_admin(self) -> None:
        for argv in (("--permanent-max", str(self.group_id)), ("--admin", "alice", "--unlimited", str(self.group_id)), ("--admin", "nobody", "--unlimited", "1")):
            with self.assertRaises(SystemExit):
                await self.run_cli(*argv)
        with self.assertRaises(SystemExit):
            await self.run_cli("--admin", "root", "--grant-coins", "carol", "0", "--reason", "x")
        with self.assertRaises(SystemExit):
            await self.run_cli("--admin", "root", "--grant-coins", "carol", "-5", "--reason", "x", "--apply")
        self.assertEqual(await self.audit(), [])
