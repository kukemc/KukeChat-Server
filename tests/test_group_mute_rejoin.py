"""群禁言不能靠退群重进躲掉：退群 / 被移出时还没到期的禁言，重新进群时恢复。"""

from __future__ import annotations

import unittest
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import app.models  # noqa: F401
from app.core.database import Base
from app.models.conversation import Conversation, ConversationMember, GroupMuteCarryover
from app.models.enums import ConversationType, MemberRole
from app.models.user import User
from app.services import conversations as conversation_service


class GroupMuteRejoinTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        self.addAsyncCleanup(self.engine.dispose)
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        self.sessions = async_sessionmaker(self.engine, expire_on_commit=False)
        async with self.sessions() as db:
            owner, member = User(username="owner", hashed_password="x"), User(username="member", hashed_password="x")
            group = Conversation(type=ConversationType.GROUP, title="群", join_mode="open")
            db.add_all([owner, member, group])
            await db.flush()
            db.add_all(
                [
                    ConversationMember(conversation_id=group.id, user_id=owner.id, role=MemberRole.OWNER),
                    ConversationMember(conversation_id=group.id, user_id=member.id, role=MemberRole.MEMBER),
                ]
            )
            await db.commit()
            self.owner, self.member, self.group = owner.id, member.id, group.id

    async def _mute(self, until: datetime | None) -> None:
        async with self.sessions() as db:
            await conversation_service.update_member_mute(db, self.group, self.owner, self.member, True, until)

    async def _leave(self) -> None:
        async with self.sessions() as db:
            await conversation_service.leave_group(db, self.group, self.member)

    async def _rejoin(self) -> ConversationMember:
        async with self.sessions() as db:
            db.add(ConversationMember(conversation_id=self.group, user_id=self.member, role=MemberRole.MEMBER))
            await db.commit()
        async with self.sessions() as db:
            return (await db.execute(select(ConversationMember).where(ConversationMember.user_id == self.member))).scalar_one()

    async def _carryovers(self) -> int:
        async with self.sessions() as db:
            return len((await db.execute(select(GroupMuteCarryover))).scalars().all())

    async def test_timed_mute_survives_leaving(self) -> None:
        until = datetime.now(UTC) + timedelta(hours=1)
        await self._mute(until)
        await self._leave()
        self.assertEqual(await self._carryovers(), 1)
        member = await self._rejoin()
        self.assertTrue(member.muted)
        self.assertEqual(member.muted_until.replace(tzinfo=UTC), until)
        self.assertEqual(await self._carryovers(), 0)

    async def test_permanent_mute_survives_leave_and_leave_again(self) -> None:
        await self._mute(None)
        await self._leave()
        await self._rejoin()
        await self._leave()
        member = await self._rejoin()
        self.assertTrue(member.muted)
        self.assertIsNone(member.muted_until)

    async def test_survives_being_removed(self) -> None:
        await self._mute(None)
        async with self.sessions() as db:
            await conversation_service.remove_member(db, self.group, self.owner, self.member)
        self.assertTrue((await self._rejoin()).muted)

    async def test_mute_that_ended_while_away_is_gone(self) -> None:
        async with self.sessions() as db:
            db.add(GroupMuteCarryover(conversation_id=self.group, user_id=self.member, muted_until=datetime.now(UTC) - timedelta(minutes=1)))
            member = (await db.execute(select(ConversationMember).where(ConversationMember.user_id == self.member))).scalar_one()
            await db.delete(member)
            await db.commit()
        member = await self._rejoin()
        self.assertFalse(member.muted)
        self.assertEqual(await self._carryovers(), 0)

    async def test_not_muted_or_unmuted_leaves_nothing_behind(self) -> None:
        await self._mute(None)
        async with self.sessions() as db:
            await conversation_service.update_member_mute(db, self.group, self.owner, self.member, False)
        await self._leave()
        self.assertEqual(await self._carryovers(), 0)
        self.assertFalse((await self._rejoin()).muted)


if __name__ == "__main__":
    unittest.main()
