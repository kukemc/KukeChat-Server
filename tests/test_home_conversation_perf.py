from __future__ import annotations

import time
import unittest
from unittest.mock import patch

from sqlalchemy import event, select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import app.models  # noqa: F401
from app.core.database import Base
from app.models.conversation import Conversation, ConversationMember
from app.models.enums import ConversationType
from app.models.message import Message, MessageVisibility
from app.models.user import User
from app.services import home, response_cache
from app.services.conversations import build_conversation_reads
from pg_redirect import requires_sqlite


class _SharedFakeRedis:
    def __init__(self):
        self.values = {}

    async def get(self, key):
        return self.values.get(key)

    async def set(self, key, value, ex):
        assert ex == home._STATS_CACHE_TTL
        self.values[key] = value


class HomeConversationPerfTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        self.sessions = async_sessionmaker(self.engine, expire_on_commit=False)
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

    async def asyncTearDown(self):
        await self.engine.dispose()

    async def test_shared_stats_only_and_live_user_specific_group_state(self):
        redis = _SharedFakeRedis()
        async with self.sessions() as db:
            first, second = User(username="perf1", hashed_password="x"), User(username="perf2", hashed_password="x")
            db.add_all([first, second])
            await db.flush()
            group = Conversation(type=ConversationType.GROUP, title="public", join_mode="open")
            db.add(group)
            await db.flush()
            db.add(ConversationMember(conversation_id=group.id, user_id=first.id))
            await db.commit()
            ids = (first.id, second.id)
            with patch.object(home.settings, "redis_url", "redis://example.invalid"), patch.object(home, "_stats_redis", redis):
                a = await home.get_home(db, ids[0], 1)
                b = await home.get_home(db, ids[1], 1)
                self.assertEqual(a.stats, b.stats)
                self.assertTrue(a.groups[0].joined)
                self.assertFalse(b.groups[0].joined)
                self.assertEqual(list(redis.values), [home._STATS_CACHE_KEY])

    async def test_latest_message_hidden_cleared_and_unread_are_independent(self):
        async with self.sessions() as db:
            viewer, peer = User(username="reader", hashed_password="x"), User(username="writer", hashed_password="x")
            db.add_all([viewer, peer])
            await db.flush()
            groups = [Conversation(type=ConversationType.GROUP, title=f"g{i}") for i in range(2)]
            db.add_all(groups)
            await db.flush()
            memberships = [ConversationMember(conversation_id=g.id, user_id=viewer.id) for g in groups]
            db.add_all(memberships)
            await db.flush()
            m1 = Message(conversation_id=groups[0].id, sender_id=peer.id, content="old")
            m2 = Message(conversation_id=groups[0].id, sender_id=viewer.id, content="self")
            m3 = Message(conversation_id=groups[0].id, sender_id=peer.id, content="hidden")
            m4 = Message(conversation_id=groups[1].id, sender_id=peer.id, content="other group")
            db.add_all([m1, m2, m3, m4])
            await db.flush()
            db.add(MessageVisibility(message_id=m3.id, user_id=viewer.id))
            memberships[0].last_read_message_id = m1.id
            await db.commit()
            reads = await build_conversation_reads(db, groups, viewer.id)
            self.assertEqual(reads[0].last_message.id, m2.id)
            self.assertEqual(reads[0].unread_count, 0)
            self.assertEqual(reads[1].last_message.id, m4.id)
            self.assertEqual(reads[1].unread_count, 1)
            memberships[0].cleared_before_message_id = m2.id
            await db.commit()
            reads = await build_conversation_reads(db, groups, viewer.id)
            self.assertIsNone(reads[0].last_message)
            self.assertEqual(reads[0].unread_count, 0)

    @requires_sqlite
    async def test_latest_query_uses_conversation_message_index(self):
        # Synthetic SQLite EXPLAIN only: never open local/production database files.
        # 两种边界：可见历史、以及清空游标后的空历史（执行计划与数据量无关）。
        async with self.sessions() as db:
            viewer = User(username="explain_reader", hashed_password="x")
            db.add(viewer)
            await db.flush()
            group = Conversation(type=ConversationType.GROUP, title="long history")
            db.add(group)
            await db.flush()
            db.add(ConversationMember(conversation_id=group.id, user_id=viewer.id))
            db.add_all([Message(conversation_id=group.id, sender_id=viewer.id, content="x") for _ in range(3)])
            await db.commit()
        async with self.engine.begin() as conn:
            for cleared_id in (0, 101):
                plan = await conn.execute(text("""
                    EXPLAIN QUERY PLAN SELECT m.id FROM messages m WHERE m.id IN (
                      SELECT (SELECT x.id FROM messages x WHERE x.conversation_id = c.id
                              AND x.id > :cleared AND x.id NOT IN
                                  (SELECT message_id FROM message_visibilities WHERE user_id = :viewer)
                              ORDER BY x.id DESC LIMIT 1)
                      FROM conversations c JOIN conversation_members cm ON cm.conversation_id = c.id
                      WHERE cm.user_id = :viewer AND c.id = :group
                    )
                """), {"cleared": cleared_id, "viewer": viewer.id, "group": group.id})
                descriptions = " ".join(str(row) for row in plan)
                self.assertIn("ix_messages_conversation_id", descriptions)
                self.assertIn("CORRELATED SCALAR SUBQUERY", descriptions)

    async def test_unread_correlated_cursor_boundaries(self):
        # All 36 read/clear combinations; hidden and sender exclusions are independent.
        async with self.sessions() as db:
            viewer, peer = User(username="cursor_reader", hashed_password="x"), User(username="cursor_writer", hashed_password="x")
            db.add_all([viewer, peer])
            await db.flush()
            group = Conversation(type=ConversationType.GROUP, title="cursor")
            db.add(group)
            await db.flush()
            member = ConversationMember(conversation_id=group.id, user_id=viewer.id)
            db.add(member)
            messages = [Message(conversation_id=group.id, sender_id=viewer.id if i == 2 else peer.id, content=str(i)) for i in range(5)]
            db.add_all(messages)
            await db.flush()
            db.add(MessageVisibility(message_id=messages[4].id, user_id=viewer.id))
            await db.commit()
            for read in [None, *(m.id for m in messages)]:
                for cleared in [None, *(m.id for m in messages)]:
                    with self.subTest(read=read, cleared=cleared):
                        member.last_read_message_id = read
                        member.cleared_before_message_id = cleared
                        await db.flush()
                        expected = sum(
                            m.sender_id != viewer.id and m.id != messages[4].id
                            and (read is None or m.id > read) and (cleared is None or m.id > cleared)
                            for m in messages
                        )
                        reads = await build_conversation_reads(db, [group], viewer.id)
                        self.assertEqual(reads[0].unread_count, expected)
                        latest = reads[0].last_message
                        self.assertEqual(latest.id if latest else None, messages[3].id if cleared is None or cleared < messages[3].id else None)

    @requires_sqlite
    async def test_unread_correlated_count_uses_range_and_many_groups(self):
        async with self.sessions() as db:
            viewer, peer = User(username="many_reader", hashed_password="x"), User(username="many_writer", hashed_password="x")
            db.add_all([viewer, peer])
            await db.flush()
            groups = [Conversation(type=ConversationType.GROUP, title=f"g{i}") for i in range(100)]
            db.add_all(groups)
            await db.flush()
            db.add_all([ConversationMember(conversation_id=g.id, user_id=viewer.id) for g in groups])
            db.add_all([Message(conversation_id=g.id, sender_id=peer.id, content="x") for g in groups])
            await db.commit()
            reads = await build_conversation_reads(db, groups, viewer.id)
            self.assertEqual(len(reads), 100)
            self.assertTrue(all(read.unread_count == 1 for read in reads))
        async with self.engine.begin() as conn:
            plan = await conn.execute(text("""
                EXPLAIN QUERY PLAN SELECT cm.conversation_id,
                  (SELECT count(m.id) FROM messages m
                    WHERE m.conversation_id=cm.conversation_id AND m.sender_id != :viewer
                    AND m.id NOT IN (SELECT message_id FROM message_visibilities WHERE user_id=:viewer)
                    AND m.id > max(coalesce(cm.last_read_message_id,0),coalesce(cm.cleared_before_message_id,0)))
                FROM conversation_members cm WHERE cm.user_id=:viewer AND cm.conversation_id IN (:group)
            """), {"viewer": viewer.id, "group": groups[0].id})
            descriptions = " ".join(str(row) for row in plan)
            self.assertIn("CORRELATED SCALAR SUBQUERY", descriptions)
            self.assertRegex(descriptions, r"ix_messages_conversation_id \(conversation_id=\? AND (?:id|rowid)>\?\)")

    async def test_batched_reads_do_not_grow_with_conversation_count(self):
        async with self.sessions() as db:
            viewer = User(username="batchviewer", hashed_password="x")
            db.add(viewer)
            await db.flush()
            groups = [Conversation(type=ConversationType.GROUP, title=f"b{i}") for i in range(10)]
            db.add_all(groups)
            await db.flush()
            db.add_all([ConversationMember(conversation_id=group.id, user_id=viewer.id) for group in groups])
            await db.commit()
            statements = []
            def count(_conn, _cursor, statement, _parameters, _context, _executemany):
                if statement.lstrip().upper().startswith("SELECT"):
                    statements.append(statement)
            event.listen(self.engine.sync_engine, "before_cursor_execute", count)
            try:
                started = time.perf_counter()
                reads = await build_conversation_reads(db, groups, viewer.id)
                elapsed = time.perf_counter() - started
            finally:
                event.remove(self.engine.sync_engine, "before_cursor_execute", count)
            self.assertEqual(len(reads), 10)
            self.assertLessEqual(len(statements), 5)
            self.assertLess(elapsed, 2.0)  # generous microbenchmark smoke bound for CI


class CacheBudgetTests(unittest.TestCase):
    def tearDown(self):
        response_cache.invalidate_user_conversations()
        response_cache.invalidate_conversation_members()

    def test_large_payload_skips_cache_and_does_not_evict_other_users(self):
        small = response_cache.build_json([{"id": 1}])
        response_cache.set_user_conversations(1, small)
        large = response_cache.build_json([{"content": "x" * (response_cache._MAX_ENTRY_BYTES + 1)}])
        response_cache.set_user_conversations(2, large)
        self.assertIsNone(response_cache.get_user_conversations(2))
        self.assertEqual(response_cache.get_user_conversations(1), small)

    def test_byte_budget_is_independent_from_entry_count(self):
        with patch.dict(response_cache._CACHE_BYTE_BUDGETS, {"conversations": 100}):
            for user_id in range(4):
                response_cache.set_user_conversations(user_id, response_cache.build_json("a" * 25))
            self.assertLessEqual(sum(entry.size_bytes for entry in response_cache._user_conversations_cache.values()), 100)
            self.assertIsNone(response_cache.get_user_conversations(0))


if __name__ == "__main__":
    unittest.main()
