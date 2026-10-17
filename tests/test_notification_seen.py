from __future__ import annotations

import unittest
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, patch

from fastapi import HTTPException
from sqlalchemy import text, update
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import app.models  # noqa: F401
from app.api.v1 import friends as friends_api
from app.api.v1 import notifications as notifications_api
from app.core import database as database_module
from app.core.database import Base
from app.models.enums import FriendRequestStatus
from app.models.friend import FriendRequest
from app.models.user import User
from app.services import friends as friend_service
from app.services import notifications as notification_service
from pg_redirect import requires_sqlite


class NotificationSeenTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        self.sessions = async_sessionmaker(self.engine, expire_on_commit=False)
        async with self.engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        async with self.sessions() as db:
            users = [User(username=name, hashed_password="x") for name in ("alice", "bob", "carol", "dave")]
            db.add_all(users)
            await db.commit()
            self.ids = {user.username: user.id for user in users}
        self.manager = AsyncMock()
        self.patches = [
            patch.object(friends_api, "manager", self.manager),
            patch.object(notifications_api, "manager", self.manager),
        ]
        for item in self.patches:
            item.start()

    async def asyncTearDown(self) -> None:
        for item in self.patches:
            item.stop()
        await self.engine.dispose()

    async def _user(self, db, name: str) -> User:
        return await db.get(User, self.ids[name])

    async def _request(self, requester: str, receiver: str) -> int:
        async with self.sessions() as db:
            request = await friend_service.create_friend_request(db, await self._user(db, requester), self.ids[receiver])
            return request.id

    async def _notifications(self, name: str):
        async with self.sessions() as db:
            return await notification_service.list_unified_notifications(db, self.ids[name])

    async def _incoming_ids(self, name: str) -> list[int]:
        async with self.sessions() as db:
            return [request.id for request in await friend_service.list_incoming_requests(db, self.ids[name])]

    async def test_ignore_hides_from_receiver_but_requester_still_sees_pending(self) -> None:
        request_id = await self._request("alice", "bob")
        async with self.sessions() as db:
            before = (await db.get(FriendRequest, request_id)).updated_at
        async with self.sessions() as db:
            response = await friends_api.ignore_request(request_id, await self._user(db, "bob"), db)
        self.assertEqual(response.status_code, 204)

        self.assertEqual(await self._incoming_ids("bob"), [])
        bob = await self._notifications("bob")
        self.assertFalse(any(item.friend_request_id == request_id for item in bob.items))
        self.assertEqual(bob.pending_count, 0)

        # 申请方：状态照旧 pending，updated_at 没被动过，没有推给他任何事件
        async with self.sessions() as db:
            outgoing = await friend_service.list_outgoing_requests(db, self.ids["alice"])
            self.assertEqual([(r.id, r.status) for r in outgoing], [(request_id, FriendRequestStatus.PENDING)])
            self.assertEqual(outgoing[0].updated_at, before)
        alice = await self._notifications("alice")
        item = next(item for item in alice.items if item.friend_request_id == request_id)
        self.assertEqual(item.payload["status"], "pending")
        for call in self.manager.send_to_user.await_args_list:
            self.assertEqual(call.args[0], self.ids["bob"])
        self.manager.send_to_user.assert_any_await(self.ids["bob"], "friend.request.ignored", {"id": request_id, "ids": [request_id]})
        self.manager.send_to_users.assert_not_awaited()

    async def test_ignore_all_counts_only_pending_incoming(self) -> None:
        first = await self._request("alice", "bob")
        await self._request("carol", "bob")
        accepted = await self._request("dave", "bob")
        async with self.sessions() as db:
            await friend_service.accept_friend_request(db, accepted, self.ids["bob"])
            await friend_service.ignore_friend_request(db, first, self.ids["bob"])
        async with self.sessions() as db:
            result = await friends_api.ignore_all_requests(await self._user(db, "bob"), db)
        self.assertEqual(result, {"ignored": 1})
        async with self.sessions() as db:
            result = await friends_api.ignore_all_requests(await self._user(db, "bob"), db)
        self.assertEqual(result, {"ignored": 0})
        self.assertEqual(await self._incoming_ids("bob"), [accepted])
        self.assertEqual((await self._notifications("bob")).pending_count, 0)

    async def test_only_receiver_can_ignore_pending_request(self) -> None:
        request_id = await self._request("alice", "bob")
        async with self.sessions() as db:
            with self.assertRaises(HTTPException) as requester:
                await friend_service.ignore_friend_request(db, request_id, self.ids["alice"])
            self.assertEqual(requester.exception.status_code, 403)
            with self.assertRaises(HTTPException) as stranger:
                await friend_service.ignore_friend_request(db, request_id, self.ids["carol"])
            self.assertEqual(stranger.exception.status_code, 404)
            await friend_service.reject_friend_request(db, request_id, self.ids["bob"])
            with self.assertRaises(HTTPException) as processed:
                await friend_service.ignore_friend_request(db, request_id, self.ids["bob"])
            self.assertEqual(processed.exception.status_code, 409)

    async def test_ignored_request_stays_hidden_after_re_request(self) -> None:
        request_id = await self._request("alice", "bob")
        async with self.sessions() as db:
            await friend_service.ignore_friend_request(db, request_id, self.ids["bob"])
            with self.assertRaises(HTTPException) as again:
                await friend_service.create_friend_request(db, await self._user(db, "alice"), self.ids["bob"])
            self.assertEqual(again.exception.status_code, 409)
        self.assertEqual(await self._incoming_ids("bob"), [])
        self.assertFalse(any(item.friend_request_id == request_id for item in (await self._notifications("bob")).items))

        # 接收方自己反过来加对方：照常直接成为好友
        async with self.sessions() as db:
            request = await friend_service.create_friend_request(db, await self._user(db, "bob"), self.ids["alice"])
            self.assertEqual(request.id, request_id)
            self.assertEqual(request.status, FriendRequestStatus.ACCEPTED)

    async def test_seen_marker_drives_new_count_and_unread(self) -> None:
        old_request = await self._request("alice", "bob")
        never_seen = await self._notifications("bob")
        self.assertIsNone(never_seen.seen_at)
        self.assertEqual(never_seen.new_count, 1)
        self.assertEqual(never_seen.unread_count, 1)

        async with self.sessions() as db:
            seen = await notifications_api.notifications_seen(await self._user(db, "bob"), db)
        self.manager.send_to_user.assert_any_await(self.ids["bob"], "notification.seen", seen)

        after_seen = await self._notifications("bob")
        self.assertEqual(after_seen.seen_at, seen.seen_at)
        self.assertEqual(after_seen.new_count, 0)
        self.assertEqual(after_seen.unread_count, 0)
        item = next(item for item in after_seen.items if item.friend_request_id == old_request)
        self.assertFalse(item.unread)
        self.assertTrue(item.pending)  # 待处理和「新不新」无关
        self.assertEqual(after_seen.pending_count, 1)

        # 看过之后新来的才算新
        async with self.sessions() as db:
            await db.execute(update(User).where(User.id == self.ids["bob"]).values(notifications_seen_at=datetime.now(UTC) - timedelta(seconds=5)))
            await db.execute(update(FriendRequest).where(FriendRequest.id == old_request).values(updated_at=datetime.now(UTC) - timedelta(minutes=1)))
            await db.commit()
        new_request = await self._request("carol", "bob")
        later = await self._notifications("bob")
        self.assertEqual(later.new_count, 1)
        self.assertEqual(later.unread_count, 1)
        self.assertEqual({item.friend_request_id for item in later.items if item.unread}, {new_request})

        # 自己处理掉的申请不会因为 updated_at 变新就又算一条
        async with self.sessions() as db:
            await friend_service.accept_friend_request(db, new_request, self.ids["bob"])
        self.assertEqual((await self._notifications("bob")).new_count, 0)
        # 申请方那边「已通过」是新消息
        carol = await self._notifications("carol")
        self.assertEqual(carol.new_count, 1)


@requires_sqlite
class InitDbTests(unittest.IsolatedAsyncioTestCase):
    async def test_init_db_adds_the_columns_to_an_existing_database(self) -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
                await connection.execute(text("ALTER TABLE friend_requests DROP COLUMN ignored_at"))
                await connection.execute(text("ALTER TABLE users DROP COLUMN notifications_seen_at"))
            with patch.object(database_module, "engine", engine):
                await database_module.init_db()
                await database_module.init_db()
            async with engine.connect() as connection:
                for table, column in (("friend_requests", "ignored_at"), ("users", "notifications_seen_at")):
                    names = {row[1] for row in (await connection.execute(text(f"PRAGMA table_info({table})"))).all()}
                    self.assertIn(column, names, table)
        finally:
            await engine.dispose()


if __name__ == "__main__":
    unittest.main()
