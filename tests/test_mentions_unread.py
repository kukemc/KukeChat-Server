"""GET /conversations/{cid}/mentions/unread：已读位置之后提到我的消息（docs/chat-features.md 第 7 节）。"""

from __future__ import annotations

import asyncio
import unittest
from datetime import UTC, datetime
from unittest.mock import patch

import httpx
from fastapi import Depends, FastAPI
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1 import conversations as api
from app.core.database import get_db
from app.core.dependencies import get_current_user
from app.models.conversation import ConversationMember
from app.models.enums import MessageType
from app.models.message import Message, MessageVisibility
from app.models.user import User
from app.services import messages as message_service

from test_chat_extras import ChatExtrasBase


class UnreadMentionsTests(ChatExtrasBase):
    async def asyncSetUp(self) -> None:
        asyncio.get_running_loop().set_debug(False)
        await super().asyncSetUp()
        self.current = "bob"

        async def isolated_db():
            async with self.sessions() as db:
                yield db

        async def isolated_user(db: AsyncSession = Depends(get_db)):
            return await db.get(User, self.ids[self.current])

        app = FastAPI()
        app.include_router(api.router, prefix="/api/v1/conversations")
        app.dependency_overrides[get_db] = isolated_db
        app.dependency_overrides[get_current_user] = isolated_user
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver")

    async def asyncTearDown(self) -> None:
        await self.client.aclose()
        await super().asyncTearDown()

    async def _send(self, sender: str, content: str = "hi", conversation_id: int | None = None, **metadata) -> int:
        async with self.sessions() as db:
            message = await message_service.send_message(
                db, conversation_id or self.group_id, await self._user(db, sender), content, MessageType.TEXT, metadata or None
            )
            return message.id

    def _at(self, *names: str) -> list[dict]:
        return [{"user_id": self.ids[name], "name": name} for name in names]

    async def _unread(self, name: str, conversation_id: int | None = None) -> httpx.Response:
        self.current = name
        return await self.client.get(f"/api/v1/conversations/{conversation_id or self.group_id}/mentions/unread")

    async def _items(self, name: str, conversation_id: int | None = None) -> list[tuple[int, str]]:
        response = await self._unread(name, conversation_id)
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertEqual(body["total"], len(body["items"]))
        for item in body["items"]:
            datetime.fromisoformat(item["created_at"])
        return [(item["message_id"], item["kind"]) for item in body["items"]]

    async def _read(self, name: str, message_id: int | None = None) -> None:
        async with self.sessions() as db:
            await message_service.mark_conversation_read(db, self.group_id, self.ids[name], message_id)

    async def test_lists_mentions_of_me_and_everyone_oldest_first(self) -> None:
        self.assertEqual(await self._items("bob"), [])
        await self._send("alice", "没有提到谁")
        first = await self._send("alice", "@bob 在吗", mentions=self._at("bob"))
        to_alice = await self._send("owner", "@alice", mentions=self._at("alice"))
        everyone = await self._send("owner", "@全体成员 开会", mention_all=True)
        both = await self._send("owner", "@全体成员 @bob", mention_all=True, mentions=self._at("bob"))
        await self._send("owner", "后面的普通消息")
        self.assertEqual(await self._items("bob"), [(first, "me"), (everyone, "all"), (both, "me")])
        self.assertEqual(await self._items("alice"), [(to_alice, "me"), (everyone, "all"), (both, "all")])
        # 自己发的 @全体 不算；自己发了消息等于读到了那里
        self.assertEqual(await self._items("owner"), [])
        await self._send("alice", "我来了")
        self.assertEqual(await self._items("alice"), [])
        # 读到哪儿，之前的就不再列
        await self._read("bob", everyone)
        self.assertEqual(await self._items("bob"), [(both, "me")])
        await self._read("bob")
        self.assertEqual(await self._items("bob"), [])

    async def test_recalled_hidden_and_cleared_are_skipped(self) -> None:
        first = await self._send("alice", "@bob 一", mentions=self._at("bob"))
        second = await self._send("alice", "@bob 二", mentions=self._at("bob"))
        third = await self._send("alice", "@bob 三", mentions=self._at("bob"))
        async with self.sessions() as db:
            message = await db.get(Message, first)
            message.recalled_at = datetime.now(UTC)
            db.add(MessageVisibility(message_id=second, user_id=self.ids["bob"]))
            await db.commit()
        self.assertEqual(await self._items("bob"), [(third, "me")])
        async with self.sessions() as db:
            member = (
                await db.execute(
                    select(ConversationMember).where(ConversationMember.conversation_id == self.group_id, ConversationMember.user_id == self.ids["bob"])
                )
            ).scalar_one()
            member.cleared_before_message_id = third
            await db.commit()
        self.assertEqual(await self._items("bob"), [])

    async def test_direct_chat_and_non_members(self) -> None:
        message_id = await self._send("alice", "@bob", self.direct_id, mentions=[str(self.ids["bob"])])
        self.assertEqual(await self._items("bob", self.direct_id), [(message_id, "me")])
        self.assertEqual((await self._unread("outsider")).status_code, 404)

    async def test_no_scan_when_markers_are_not_newer_than_the_read_position(self) -> None:
        for index in range(5):
            await self._send("alice", f"普通 {index}")
        executed: list[str] = []
        original = AsyncSession.execute

        async def spy(session, statement, *args, **kwargs):
            executed.append(str(statement))
            return await original(session, statement, *args, **kwargs)

        with patch.object(AsyncSession, "execute", spy):
            self.assertEqual(await self._items("bob"), [])
        self.assertFalse(any("FROM messages" in sql for sql in executed), executed)

    async def test_limit_and_total(self) -> None:
        with patch.object(message_service, "MESSAGE_RATE_LIMIT_PER_MINUTE", 10_000):
            ids = [await self._send("alice", f"@bob {index}", mentions=self._at("bob")) for index in range(message_service.UNREAD_MENTIONS_LIMIT + 3)]
        response = await self._unread("bob")
        body = response.json()
        self.assertEqual(body["total"], len(ids))
        self.assertEqual([item["message_id"] for item in body["items"]], ids[: message_service.UNREAD_MENTIONS_LIMIT])


if __name__ == "__main__":
    unittest.main()
