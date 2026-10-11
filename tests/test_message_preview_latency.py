"""Message preview latency, ordering, authorization and writer-lock boundaries."""

from __future__ import annotations

import asyncio
import json
import threading
import time
import unittest
from unittest.mock import patch

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import app.models  # noqa: F401
from app.core.database import Base
from app.models.conversation import Conversation, ConversationMember
from app.models.enums import ConversationType, MemberRole
from app.models.message import Message
from app.models.user import User
from app.services import ccw, messages, response_cache

OIDS = ("111111111111111111111111", "222222222222222222222222", "333333333333333333333333", "444444444444444444444444")


def creation_url(oid: str, access_key: str = "") -> str:
    return f"https://www.ccw.site/detail/{oid}" + (f"?accessKey={access_key}" if access_key else "")


class PreviewConcurrencyTests(unittest.IsolatedAsyncioTestCase):
    async def test_three_fetches_concurrent_and_ordered_with_bounded_fanout(self) -> None:
        active = 0
        peak = 0
        calls: list[tuple[str, str]] = []
        lock = threading.Lock()

        def fetch(oid: str, access_key: str) -> dict:
            nonlocal active, peak
            with lock:
                active += 1
                peak = max(peak, active)
                calls.append((oid, access_key))
            try:
                # First URL is slowest, so completion order differs from URL order.
                time.sleep({OIDS[0]: 0.2, OIDS[1]: 0.08, OIDS[2]: 0.12}[oid])
                return {"body": {"oid": oid, "title": oid}}
            finally:
                with lock:
                    active -= 1

        content = " ".join([creation_url(OIDS[0], "privateA"), creation_url(OIDS[1]), creation_url(OIDS[2], "privateB"), creation_url(OIDS[3])])
        with patch.object(ccw, "_fetch_creation_detail_sync", side_effect=fetch), patch.object(response_cache, "get_ccw_creation", return_value=None), patch.object(response_cache, "set_ccw_creation"):
            start = time.monotonic()
            previews = await ccw.previews_from_text(content, limit=100)
            elapsed = time.monotonic() - start
        self.assertLess(elapsed, 0.38, "concurrent latency should approach 0.2s, not the 0.4s serial sum")
        self.assertEqual(peak, 3)
        self.assertLessEqual(peak, 3)
        self.assertEqual(len(calls), 3)
        self.assertEqual([item["oid"] for item in previews], list(OIDS[:3]))
        self.assertEqual([item["access_key"] for item in previews], ["privateA", None, "privateB"])
        self.assertIn("accessKey=privateA", previews[0]["url"])
        self.assertNotIn("accessKey=privateA", previews[1]["url"])

    async def test_one_timeout_does_not_delay_or_hide_other_previews(self) -> None:
        def fetch(oid: str, access_key: str) -> dict:
            if oid == OIDS[1]:
                time.sleep(0.2)
                raise TimeoutError("simulated timeout")
            time.sleep(0.08)
            return {"body": {"oid": oid, "title": oid}}

        with patch.object(ccw, "_fetch_creation_detail_sync", side_effect=fetch), patch.object(response_cache, "get_ccw_creation", return_value=None), patch.object(response_cache, "set_ccw_creation"):
            start = time.monotonic()
            previews = await ccw.previews_from_text(" ".join(map(creation_url, OIDS[:3])))
            elapsed = time.monotonic() - start
        self.assertLess(elapsed, 0.36)
        self.assertEqual([item["oid"] for item in previews], [OIDS[0], OIDS[2]])

    async def test_cache_separates_access_keys(self) -> None:
        cache: dict[str, dict] = {}
        with patch.object(ccw, "_fetch_creation_detail_sync", return_value={"body": {"oid": OIDS[0]}}) as fetch, patch.object(response_cache, "get_ccw_creation", side_effect=cache.get), patch.object(response_cache, "set_ccw_creation", side_effect=cache.__setitem__):
            await ccw.fetch_creation_preview(OIDS[0], "keyA")
            await ccw.fetch_creation_preview(OIDS[0], "keyB")
            await ccw.fetch_creation_preview(OIDS[0], "keyA")
        self.assertEqual(fetch.call_count, 2)
        self.assertEqual(fetch.call_args_list[0].args, (OIDS[0], "keyA"))
        self.assertEqual(fetch.call_args_list[1].args, (OIDS[0], "keyB"))


class MessagePreviewTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        self.sessions = async_sessionmaker(self.engine, expire_on_commit=False)
        async with self.engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        async with self.sessions() as db:
            sender = User(username="sender", hashed_password="x")
            outsider = User(username="outsider", hashed_password="x")
            db.add_all([sender, outsider])
            await db.flush()
            conversation = Conversation(type=ConversationType.GROUP, title="preview group")
            db.add(conversation)
            await db.flush()
            member = ConversationMember(conversation_id=conversation.id, user_id=sender.id, role=MemberRole.MEMBER)
            db.add(member)
            await db.commit()
            self.sender_id, self.outsider_id, self.conversation_id = sender.id, outsider.id, conversation.id

    async def asyncTearDown(self) -> None:
        await self.engine.dispose()

    async def test_plain_text_never_touches_preview_io_or_metadata(self) -> None:
        with patch.object(ccw, "_fetch_creation_detail_sync", side_effect=AssertionError("external IO")), patch.object(messages, "enrich_metadata_with_previews", side_effect=AssertionError("enricher entered")):
            async with self.sessions() as db:
                sent = await messages.send_message(db, self.conversation_id, await db.get(User, self.sender_id), "plain message", metadata={"client": "preserved"})
                self.assertEqual(json.loads(sent.metadata_json), {"client": "preserved"})

    async def test_preview_persisted_and_unauthorized_user_not_fetched(self) -> None:
        url = creation_url(OIDS[3], "secret")
        with patch.object(ccw, "_fetch_creation_detail_sync", return_value={"body": {"oid": OIDS[3], "title": "sample"}}) as fetch:
            async with self.sessions() as db:
                with self.assertRaises(HTTPException) as rejected:
                    await messages.send_message(db, self.conversation_id, await db.get(User, self.outsider_id), url)
                self.assertEqual(rejected.exception.status_code, 404)
                self.assertEqual(fetch.call_count, 0)
            async with self.sessions() as db:
                sent = await messages.send_message(db, self.conversation_id, await db.get(User, self.sender_id), url, metadata={"client": "preserved"})
                self.assertEqual(fetch.call_count, 1)
                self.assertEqual(json.loads(sent.metadata_json)["ccw_creations"][0]["access_key"], "secret")
                self.assertEqual(json.loads(sent.metadata_json)["client"], "preserved")
                stored = await db.scalar(select(Message).where(Message.id == sent.id))
                self.assertEqual(stored.metadata_json, sent.metadata_json)

    async def test_forbidden_mention_all_does_not_fetch(self) -> None:
        with patch.object(ccw, "_fetch_creation_detail_sync", side_effect=AssertionError("external IO")):
            async with self.sessions() as db:
                with self.assertRaises(HTTPException) as rejected:
                    await messages.send_message(db, self.conversation_id, await db.get(User, self.sender_id), creation_url(OIDS[0]), metadata={"mention_all": True})
                self.assertEqual(rejected.exception.status_code, 403)

    async def test_expired_mute_does_not_hold_write_lock_while_fetching(self) -> None:
        # A second SQLite connection can write while external preview is pending.
        # Use a file-backed DB: separate in-memory connections cannot model locking.
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as temp:
            engine = create_async_engine(f"sqlite+aiosqlite:///{Path(temp) / 'latency.db'}", connect_args={"timeout": 0.1})
            sessions = async_sessionmaker(engine, expire_on_commit=False)
            try:
                async with engine.begin() as connection:
                    await connection.run_sync(Base.metadata.create_all)
                async with sessions() as db:
                    sender = User(username="locked_sender", hashed_password="x")
                    db.add(sender)
                    await db.flush()
                    conversation = Conversation(type=ConversationType.GROUP)
                    db.add(conversation)
                    await db.flush()
                    from datetime import datetime, timedelta, timezone
                    db.add(ConversationMember(conversation_id=conversation.id, user_id=sender.id, muted=True, muted_until=datetime.now(timezone.utc) - timedelta(minutes=1)))
                    await db.commit()
                    user_id, conversation_id = sender.id, conversation.id
                fetching = threading.Event()

                def fetch(oid: str, access_key: str) -> dict:
                    fetching.set()
                    time.sleep(0.2)
                    return {"body": {"oid": oid}}

                async def send() -> None:
                    async with sessions() as db:
                        await messages.send_message(db, conversation_id, await db.get(User, user_id), creation_url(OIDS[0]))

                with patch.object(ccw, "_fetch_creation_detail_sync", side_effect=fetch), patch.object(response_cache, "get_ccw_creation", return_value=None):
                    task = asyncio.create_task(send())
                    try:
                        self.assertTrue(await asyncio.to_thread(fetching.wait, 1))
                        async with sessions() as other:
                            other.add(User(username="concurrent_write", hashed_password="x"))
                            await other.commit()
                    finally:
                        await task
            finally:
                await engine.dispose()


if __name__ == "__main__":
    unittest.main()
