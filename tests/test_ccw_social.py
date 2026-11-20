"""在共创世界上（docs/ccw-social.md）：「正在玩」的心跳与推送节流、名片的可见范围与关系。"""

from __future__ import annotations

import unittest
from datetime import UTC, datetime, timedelta

from pydantic import ValidationError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import app.models  # noqa: F401
from app.core.database import Base
from app.models.conversation import Conversation, ConversationMember
from app.models.enums import ConversationType, FriendRequestStatus, MemberRole, UserStatus
from app.models.friend import FriendRequest, Friendship
from app.models.user import User
from app.schemas.ccw_social import ActivityUpdate, CcwUserBatchRequest
from app.schemas.user import UserUpdate
from app.services import ccw_activity, ccw_social
from app.services import users as user_service

OID = {name: f"{i:024x}" for i, name in enumerate(("me", "alice", "bob", "carol", "hidden", "banned"), start=1)}
WORK = "6aead64c19ff795b85998e62"
OTHER_WORK = "66d52d2366bfcb0e0b42e7c8"


def play(oid: str = WORK, title: str = "灯塔归航", cover: str | None = None) -> ActivityUpdate:
    return ActivityUpdate(kind="play", ccw_oid=oid, title=title, cover_url=cover)


async def nobody_online(_ids: list[int]) -> set[int]:
    return set()


class ActivityTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        ccw_activity.reset()

    async def test_heartbeat_keeps_start_and_throttles_pushes(self) -> None:
        t0 = datetime(2026, 11, 20, 12, 0, tzinfo=UTC)
        first, push = await ccw_activity.set_activity(1, play(), now=t0)
        self.assertTrue(push)
        self.assertEqual(first.started_at, t0)
        self.assertEqual(first.expires_at, t0 + timedelta(seconds=ccw_activity.ACTIVITY_TTL_SECONDS))

        beat, push = await ccw_activity.set_activity(1, play(), now=t0 + timedelta(seconds=60))
        self.assertFalse(push, "一分钟一次的心跳不用推")
        self.assertEqual(beat.started_at, t0, "同一个作品，开始时间不变")

        beat, push = await ccw_activity.set_activity(1, play(), now=t0 + timedelta(seconds=125))
        self.assertTrue(push, "两分钟没推过了，推一次让好友那边续期")
        self.assertEqual(beat.started_at, t0)

    async def test_switching_work_pushes_and_restarts(self) -> None:
        t0 = datetime(2026, 11, 20, 12, 0, tzinfo=UTC)
        await ccw_activity.set_activity(1, play(), now=t0)
        other, push = await ccw_activity.set_activity(1, play(OTHER_WORK, "KukeChat NEXT"), now=t0 + timedelta(seconds=30))
        self.assertTrue(push)
        self.assertEqual(other.started_at, t0 + timedelta(seconds=30))
        self.assertEqual(other.ccw_oid, OTHER_WORK)

    async def test_clear_and_batch_read(self) -> None:
        await ccw_activity.set_activity(1, play())
        await ccw_activity.set_activity(2, ActivityUpdate(kind="create", title="新作品"))
        self.assertEqual(set(await ccw_activity.get_activities([1, 2, 3])), {1, 2})
        self.assertTrue(await ccw_activity.clear_activity(1))
        self.assertFalse(await ccw_activity.clear_activity(1))
        self.assertEqual(set(await ccw_activity.get_activities([1, 2])), {2})

    def test_payload_validation(self) -> None:
        with self.assertRaises(ValidationError):
            ActivityUpdate(kind="play", ccw_oid="not-an-oid", title="x")
        with self.assertRaises(ValidationError):
            ActivityUpdate(kind="play", ccw_oid=WORK, title="   ")
        self.assertIsNone(play(cover="https://evil.example/a.png").cover_url, "只认 CCW 图床")
        self.assertIsNone(play(cover="http://m.ccw.site/a.png").cover_url, "只认 https")
        ok = "https://m.ccw.site/works-covers/a.png"
        self.assertEqual(play(cover=ok).cover_url, ok)
        self.assertEqual(play(title="  灯塔\n 归航 ").title, "灯塔 归航")
        batch = CcwUserBatchRequest(oids=[OID["alice"], OID["alice"].upper(), "bad", OID["bob"]])
        self.assertEqual(batch.oids, [OID["alice"], OID["bob"]])


class CardTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        ccw_activity.reset()
        self.engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        self.sessions = async_sessionmaker(self.engine, expire_on_commit=False)
        async with self.engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        async with self.sessions() as db:
            users = {name: User(username=name, hashed_password="x", ccw_student_oid=oid) for name, oid in OID.items()}
            users["hidden"].ccw_discoverable = False
            users["banned"].status = UserStatus.DISABLED  # 聊天封禁就是把状态改成 DISABLED
            db.add_all(users.values())
            await db.flush()
            self.ids = {name: user.id for name, user in users.items()}
            # me 和 alice、carol 是好友；alice 和 carol 也是好友（me 和 alice 的共同好友：carol）
            for a, b in (("me", "alice"), ("me", "carol"), ("alice", "carol")):
                db.add_all([Friendship(user_id=self.ids[a], friend_id=self.ids[b]), Friendship(user_id=self.ids[b], friend_id=self.ids[a])])
            # bob 给 me 发了好友申请
            db.add(FriendRequest(requester_id=self.ids["bob"], receiver_id=self.ids["me"], status=FriendRequestStatus.PENDING))
            # me 和 alice 同在两个群
            for title in ("群一", "群二"):
                group = Conversation(type=ConversationType.GROUP, title=title)
                db.add(group)
                await db.flush()
                for name in ("me", "alice"):
                    db.add(ConversationMember(conversation_id=group.id, user_id=self.ids[name], role=MemberRole.MEMBER))
            await db.commit()

    async def asyncTearDown(self) -> None:
        await self.engine.dispose()

    async def _me(self, db) -> User:
        return await db.get(User, self.ids["me"])

    async def test_card_for_friend_with_activity(self) -> None:
        await ccw_activity.set_activity(self.ids["alice"], play())

        async def alice_online(ids: list[int]) -> set[int]:
            return {self.ids["alice"]} & set(ids)

        async with self.sessions() as db:
            card = await ccw_social.get_card(db, await self._me(db), OID["alice"], alice_online)
        self.assertTrue(card.has_account)
        self.assertEqual(card.user.id, self.ids["alice"])
        self.assertTrue(card.online)
        self.assertEqual(card.friendship, "friend")
        self.assertEqual(card.mutual_friend_count, 1)
        self.assertEqual(card.mutual_group_count, 2)
        self.assertEqual(card.activity.ccw_oid, WORK)

    async def test_pending_requests_both_directions(self) -> None:
        async with self.sessions() as db:
            me = await self._me(db)
            self.assertEqual((await ccw_social.get_card(db, me, OID["bob"], nobody_online)).friendship, "incoming")
            bob = await db.get(User, self.ids["bob"])
            self.assertEqual((await ccw_social.get_card(db, bob, OID["me"], nobody_online)).friendship, "outgoing")

    async def test_hidden_banned_and_unbound_look_the_same(self) -> None:
        async with self.sessions() as db:
            me = await self._me(db)
            for oid in (OID["hidden"], OID["banned"], "f" * 24):
                card = await ccw_social.get_card(db, me, oid, nobody_online)
                self.assertFalse(card.has_account)
                self.assertIsNone(card.user)
            hidden = await db.get(User, self.ids["hidden"])
            self_card = await ccw_social.get_card(db, hidden, OID["hidden"], nobody_online)
            self.assertTrue(self_card.has_account, "关掉「能被找到」后自己仍能看到自己")
            self.assertEqual(self_card.friendship, "self")

    async def test_turning_off_activity_hides_and_clears_it(self) -> None:
        await ccw_activity.set_activity(self.ids["alice"], play())
        async with self.sessions() as db:
            alice = await db.get(User, self.ids["alice"])
            await user_service.update_profile(db, alice, UserUpdate(share_ccw_activity=False))
            self.assertFalse(alice.share_ccw_activity)
            self.assertEqual(await ccw_activity.get_activities([self.ids["alice"]]), {}, "关掉时立刻清掉")
            # 关着的时候即使存进去了（比如旧版本脚本），别人也看不到
            await ccw_activity.set_activity(self.ids["alice"], play())
            card = await ccw_social.get_card(db, await self._me(db), OID["alice"], nobody_online)
            self.assertIsNone(card.activity)
            self.assertEqual(await ccw_social.friend_activities(db, self.ids["me"]), {})
            # 其他偏好不受影响；不传就是不改
            await user_service.update_profile(db, alice, UserUpdate(nickname="Alice"))
            self.assertFalse(alice.share_ccw_activity)
            self.assertTrue(alice.ccw_discoverable)

    async def test_batch_keeps_order_and_skips_hidden(self) -> None:
        await ccw_activity.set_activity(self.ids["carol"], play())
        async with self.sessions() as db:
            briefs = await ccw_social.get_briefs(
                db, await self._me(db), [OID["carol"], OID["hidden"], OID["alice"], "e" * 24], nobody_online
            )
        self.assertEqual([b.ccw_oid for b in briefs], [OID["carol"], OID["alice"]])
        self.assertEqual(briefs[0].activity.title, "灯塔归航")
        self.assertIsNone(briefs[1].activity)

    async def test_friend_activities_only_friends(self) -> None:
        for name in ("alice", "bob", "carol"):
            await ccw_activity.set_activity(self.ids[name], play())
        async with self.sessions() as db:
            activities = await ccw_social.friend_activities(db, self.ids["me"])
        self.assertEqual(set(activities), {self.ids["alice"], self.ids["carol"]})


if __name__ == "__main__":
    unittest.main()
