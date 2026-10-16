"""乐观发送：client_nonce 在 POST 返回和 message.created 里原样回显，不落库（docs/chat-features.md 第 11 节）。"""

from __future__ import annotations

import unittest
from unittest.mock import AsyncMock, patch

import httpx
from fastapi import Depends, FastAPI
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

import app.models  # noqa: F401
from app.api.v1 import conversations as api
from app.core.database import Base, get_db
from app.core.dependencies import get_current_user
from app.models.conversation import Conversation, ConversationMember
from app.models.enums import ConversationType
from app.models.message import Message
from app.models.user import User


class ClientNonceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        self.sessions = async_sessionmaker(self.engine, expire_on_commit=False)
        async with self.engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        async with self.sessions() as db:
            user = User(username="nonce_sender", hashed_password="unused")
            db.add(user)
            await db.flush()
            self.user_id = user.id
            conversation = Conversation(type=ConversationType.DIRECT)
            db.add(conversation)
            await db.flush()
            self.conversation_id = conversation.id
            db.add(ConversationMember(conversation_id=conversation.id, user_id=user.id))
            await db.commit()

        async def isolated_db():
            async with self.sessions() as db:
                yield db

        async def isolated_user(db: AsyncSession = Depends(get_db)):
            return await db.get(User, self.user_id)

        app = FastAPI()
        app.include_router(api.router, prefix="/api/v1/conversations")
        app.dependency_overrides[get_db] = isolated_db
        app.dependency_overrides[get_current_user] = isolated_user
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver")

    async def asyncTearDown(self):
        await self.client.aclose()
        await self.engine.dispose()

    async def _post(self, body, sent):
        async def recipients(_db, _conversation_id):
            return [self.user_id]

        async def record(user_ids, event_type, data):
            sent.append((event_type, data))

        def drop_task(coro):
            coro.close()

        with patch.object(api, "online_conversation_member_ids", side_effect=recipients), patch.object(
            api, "schedule_realtime_task", side_effect=drop_task
        ), patch.object(api.moderation_service, "schedule_message_moderation"), patch.object(
            api.bot_service, "bot_conversation_ids_for_message_event", AsyncMock(return_value=[])
        ), patch.object(api.manager, "send_to_users", side_effect=record), patch.object(
            api.intimacy_service, "schedule_message"
        ):
            return await self.client.post(f"/api/v1/conversations/{self.conversation_id}/messages", json=body)

    async def test_nonce_echoed_in_response_and_event_but_not_stored(self):
        sent = []
        response = await self._post({"content": "hi", "client_nonce": "abc_DEF-123"}, sent)
        self.assertEqual(response.status_code, 201, response.text)
        self.assertEqual(response.json()["client_nonce"], "abc_DEF-123")
        created = [data for event, data in sent if event == "message.created"]
        self.assertEqual(len(created), 1)
        payload = created[0] if isinstance(created[0], dict) else created[0].model_dump()
        self.assertEqual(payload["client_nonce"], "abc_DEF-123")
        async with self.sessions() as db:
            stored = (await db.execute(select(Message).where(Message.id == response.json()["id"]))).scalar_one()
        self.assertNotIn("client_nonce", stored.metadata_json or "")

        plain = await self._post({"content": "no nonce"}, [])
        self.assertEqual(plain.status_code, 201, plain.text)
        self.assertIsNone(plain.json()["client_nonce"])

    async def test_invalid_nonce_rejected(self):
        for bad in ("", "has space", "x" * 65, "中文"):
            with self.subTest(bad=bad):
                response = await self._post({"content": "hi", "client_nonce": bad}, [])
                self.assertEqual(response.status_code, 422, response.text)


if __name__ == "__main__":
    unittest.main()
