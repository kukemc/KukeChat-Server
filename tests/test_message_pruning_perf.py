from __future__ import annotations

import unittest
from unittest.mock import patch

from sqlalchemy import event, func, insert, select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import app.models  # noqa: F401
from app.core.database import Base
from app.models.conversation import Conversation, ConversationMember
from app.models.enums import ConversationType, MemberRole, MessageType, ReportTargetType
from app.models.message import Message, MessageBookmark, MessageReaction
from app.models.report import UserReport
from app.models.user import User
from app.services import messages as message_service


class MessagePruningTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.exit_stack = patch.multiple(
            message_service, MAX_MESSAGES_PER_CONVERSATION=20, PRUNE_BATCH_SIZE=5
        )
        self.exit_stack.start()
        self.addCleanup(self.exit_stack.stop)
        self.engine = create_async_engine("sqlite+aiosqlite:///:memory:")

        @event.listens_for(self.engine.sync_engine, "connect")
        def enable_foreign_keys(connection, _record):
            connection.execute("PRAGMA foreign_keys=ON")

        self.sessions = async_sessionmaker(self.engine, expire_on_commit=False)
        async with self.engine.begin() as connection:
            await connection.run_sync(
                Base.metadata.create_all,
                tables=[User.__table__, Conversation.__table__, ConversationMember.__table__,
                        Message.__table__, MessageReaction.__table__, MessageBookmark.__table__,
                        UserReport.__table__],
            )
            await connection.execute(text("CREATE INDEX ix_messages_conversation_id_id ON messages (conversation_id, id)"))
        async with self.sessions() as db:
            user = User(username="prune-user", hashed_password="x")
            chats = [Conversation(type=ConversationType.DIRECT), Conversation(type=ConversationType.DIRECT)]
            db.add_all([user, *chats])
            await db.flush()
            self.user_id = user.id
            self.chat_ids = [chat.id for chat in chats]
            db.add_all([
                ConversationMember(conversation_id=chat.id, user_id=user.id, role=MemberRole.MEMBER)
                for chat in chats
            ])
            await db.commit()

    async def asyncTearDown(self) -> None:
        await self.engine.dispose()

    async def seed(self, chat_id: int, count: int, *, content: str = "text") -> None:
        async with self.sessions() as db:
            await db.execute(insert(Message), [
                {"conversation_id": chat_id, "sender_id": self.user_id,
                 "type": MessageType.TEXT, "content": content}
                for _ in range(count)
            ])
            await db.commit()

    async def ids(self, chat_id: int) -> list[int]:
        async with self.sessions() as db:
            rows = await db.execute(select(Message.id).where(Message.conversation_id == chat_id).order_by(Message.id))
            return list(rows.scalars())

    async def test_interleaved_global_ids_never_skip_one_chat(self) -> None:
        first, second = self.chat_ids
        async with self.sessions() as db:
            await db.execute(insert(Message), [
                {"conversation_id": chat, "sender_id": self.user_id,
                 "type": MessageType.TEXT, "content": "a" if chat == first else "b"}
                for _ in range(20) for chat in (first, second)
            ])
            await db.commit()
            # All ids in first chat are odd; its newest id cannot be divisible by 25.
            self.assertNotEqual((await self.ids(first))[-1] % 25, 0)
            db.add(Message(conversation_id=first, sender_id=self.user_id, type=MessageType.TEXT, content="new"))
            await db.flush()
            self.assertEqual(await message_service._prune_conversation_messages(db, first), 1)
            await db.commit()
        self.assertEqual(len(await self.ids(first)), 20)
        self.assertEqual(len(await self.ids(second)), 20)
        self.assertEqual((await self.ids(first))[0], 3)

    async def test_backlog_is_bounded_and_drains_even_without_modulo_tick(self) -> None:
        chat = self.chat_ids[0]
        await self.seed(chat, 41)
        for batch, remaining in [(5, 36), (5, 31), (5, 26), (5, 21), (1, 20), (0, 20)]:
            async with self.sessions() as db:
                self.assertEqual(await message_service._prune_conversation_messages(db, chat), batch)
                await db.commit()
            self.assertEqual(len(await self.ids(chat)), remaining)
        self.assertEqual((await self.ids(chat))[0], 22)

    async def test_report_and_shared_upload_survive_prune_without_file_deletion(self) -> None:
        first, second = self.chat_ids
        path = "/uploads/messages/shared.png"
        await self.seed(first, 21, content=path)
        async with self.sessions() as db:
            db.add(Message(conversation_id=second, sender_id=self.user_id, type=MessageType.IMAGE, content=path))
            db.add(UserReport(reporter_id=self.user_id, target_type=ReportTargetType.MESSAGE,
                              target_id=1, message_id=1, reason="evidence",
                              target_snapshot='{"image":"/uploads/messages/shared.png"}'))
            db.add(MessageReaction(message_id=1, user_id=self.user_id, emoji="👍"))
            db.add(MessageBookmark(message_id=1, user_id=self.user_id))
            await db.commit()
            self.assertEqual(await message_service._prune_conversation_messages(db, first), 1)
            await db.commit()
            self.assertIsNone(await db.get(Message, 1))
            self.assertEqual((await db.execute(select(func.count(MessageReaction.id)))).scalar_one(), 0)
            self.assertEqual((await db.execute(select(func.count(MessageBookmark.id)))).scalar_one(), 0)
            report = (await db.execute(select(UserReport))).scalar_one()
            self.assertEqual(report.target_snapshot, '{"image":"/uploads/messages/shared.png"}')
            self.assertEqual(report.message_id, 1)  # historical, non-FK report reference
            self.assertIsNotNone((await db.execute(select(Message.id).where(Message.conversation_id == second))).scalar_one())
            self.assertFalse(hasattr(message_service, "schedule_delete_upload_files"))

    async def test_indexed_cutoff_avoids_table_scan(self) -> None:
        async with self.sessions() as db:
            plan = await db.execute(text(
                "EXPLAIN QUERY PLAN SELECT id FROM messages WHERE conversation_id = 1 "
                "ORDER BY id DESC LIMIT 1 OFFSET 20"
            ))
            self.assertTrue(any("SEARCH messages USING COVERING INDEX" in row[3] for row in plan))
            self.assertFalse(any("SCAN messages" in row[3] for row in plan))
