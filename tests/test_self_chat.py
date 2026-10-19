"""「我的电脑 / 我的手机」（docs/self-chat.md）：自己设备之间的私密会话和 24 小时临时文件。存储用本地目录，不发外部请求。"""

from __future__ import annotations

import asyncio
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Annotated, Any
from unittest.mock import AsyncMock, Mock, patch

import httpx
from fastapi import Depends, FastAPI
from sqlalchemy import event, func, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

import app.models  # noqa: F401
from app.core.config import settings
from app.core.database import Base, configure_sqlite_connection, get_db, init_db
from app.core.dependencies import get_current_user
from app.models.conversation import Conversation, ConversationMember
from app.models.enums import ConversationType, MemberRole, MessageType, ModerationAction, ModerationSource
from app.models.message import Message, MessageBookmark
from app.models.report import ContentModerationRecord, UserReport
from app.models.self_chat import SelfTransfer
from app.models.user import User
from app.services import gifts as gift_service
from app.services import moderation as moderation_service
from app.services import moderation_idle
from app.services import red_packets as red_packet_service
from app.services import self_chat as self_service
from app.services import storage
from app.websocket.manager import manager
from pg_redirect import requires_sqlite

START = datetime(2026, 10, 20, 8, 0, tzinfo=UTC)


def make_engine(path: Path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{path.as_posix()}")
    event.listen(engine.sync_engine, "connect", configure_sqlite_connection)
    return engine


class SelfChatCase(unittest.IsolatedAsyncioTestCase):
    """alice / bob / carol；alice 和 bob 之间有一个私聊。"""

    async def asyncSetUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self.tmp.cleanup)
        self.engine = make_engine(Path(self.tmp.name) / "self.db")
        self.sessions = async_sessionmaker(self.engine, expire_on_commit=False)
        async with self.engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        async with self.sessions() as db:
            users = [User(username=name, hashed_password="x") for name in ("alice", "bob", "carol")]
            db.add_all(users)
            await db.flush()
            self.ids = {user.username: user.id for user in users}
            direct = Conversation(type=ConversationType.DIRECT, direct_key=f"{users[0].id}:{users[1].id}")
            db.add(direct)
            await db.flush()
            self.direct_id = direct.id
            for name in ("alice", "bob"):
                db.add(ConversationMember(conversation_id=direct.id, user_id=self.ids[name], role=MemberRole.MEMBER))
            await db.commit()

        self.now = START
        self.send_to_users = AsyncMock()
        self.schedule_moderation = Mock()

        async def everyone_online(user_ids: list[int]) -> set[int]:
            return set(user_ids)

        upload_dir = str(Path(self.tmp.name) / "uploads")
        for patcher in (
            patch.object(self_service, "_now", lambda: self.now),
            patch.object(manager, "send_to_user", AsyncMock()),
            patch.object(manager, "send_to_users", self.send_to_users),
            patch.object(manager, "online_user_ids_among", everyone_online),
            patch.object(settings, "upload_dir", upload_dir),
            patch.object(settings, "storage_backend", "local"),
            patch.object(settings, "s3_private_bucket", None),
            patch.object(moderation_service, "schedule_message_moderation", self.schedule_moderation),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

        from app.api.v1.router import api_router

        self.acting = "alice"
        api = FastAPI()
        api.include_router(api_router)

        async def isolated_db():
            async with self.sessions() as session:
                yield session

        async def acting_user(db: Annotated[AsyncSession, Depends(get_db)]) -> User:
            return await db.get(User, self.ids[self.acting])

        api.dependency_overrides[get_db] = isolated_db
        api.dependency_overrides[get_current_user] = acting_user
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=api), base_url="http://testserver")

    async def asyncTearDown(self) -> None:
        await self.client.aclose()
        await self.engine.dispose()

    # ------------------------------------------------------------ 工具

    async def call(self, method: str, path: str, *, as_: str = "alice", expected: int | None = None, **kwargs: Any) -> httpx.Response:
        self.acting = as_
        response = await self.client.request(method, f"/api/v1{path}", **kwargs)
        if expected is not None:
            self.assertEqual(response.status_code, expected, response.text)
        return response

    async def json(self, method: str, path: str, *, as_: str = "alice", expected: int = 200, **kwargs: Any) -> Any:
        return (await self.call(method, path, as_=as_, expected=expected, **kwargs)).json()

    async def code(self, method: str, path: str, status_code: int, *, as_: str = "alice", **kwargs: Any) -> dict[str, Any]:
        body = await self.json(method, path, as_=as_, expected=status_code, **kwargs)
        return body["detail"] if isinstance(body.get("detail"), dict) else {"code": None, "message": body.get("detail")}

    async def self_id(self, as_: str = "alice") -> int:
        return (await self.json("GET", "/conversations/self", as_=as_))["id"]

    async def send(self, cid: int, content: str = "你好", *, platform: str | None = "android", as_: str = "alice", expected: int = 201, **body: Any) -> Any:
        headers = {"X-Kuke-Platform": platform} if platform else {}
        return await self.json("POST", f"/conversations/{cid}/messages", as_=as_, expected=expected, headers=headers, json={"content": content, **body})

    def tick(self, **delta: float) -> None:
        self.now = self.now + timedelta(**delta)

    async def transfer(self, cid: int, data: bytes = b"hello world", *, name: str = "报告.pdf", platform: str = "desktop") -> dict[str, Any]:
        """开单 → 本地上传 → complete，返回 complete 的结果。"""
        created = await self.json(
            "POST", f"/conversations/{cid}/transfers", expected=201, headers={"X-Kuke-Platform": platform},
            json={"name": name, "size": len(data), "mime": "application/pdf"},
        )
        await self.json("PUT", created["upload"]["url"].removeprefix("/api/v1"), content=data, headers=created["upload"]["headers"])
        return await self.json("POST", f"/conversations/{cid}/transfers/{created['transfer_id']}/complete", expected=201, headers={"X-Kuke-Platform": platform})

    async def row(self, transfer_id: int) -> SelfTransfer | None:
        async with self.sessions() as db:
            return await db.get(SelfTransfer, transfer_id)


# ---------------------------------------------------------------- 会话本身


class ConversationTests(SelfChatCase):
    async def test_get_or_create_is_idempotent_and_unique(self) -> None:
        first = await self.json("GET", "/conversations/self")
        second = await self.json("GET", "/conversations/self")
        self.assertEqual(first["id"], second["id"])
        self.assertEqual((first["type"], first["display_title"], first["member_count"], first["unread_count"]), ("self", "我的设备", 1, 0))
        self.assertIsNone(first["direct_user"])
        bob = await self.self_id("bob")
        self.assertNotEqual(bob, first["id"])
        async with self.sessions() as db:
            self.assertEqual(int(await db.scalar(select(func.count()).select_from(Conversation).where(Conversation.type == ConversationType.SELF))), 2)
            # 唯一索引兜底：同一个人第二个「我的设备」会话建不出来
            db.add(Conversation(type=ConversationType.SELF, self_owner_id=self.ids["alice"]))
            with self.assertRaises(IntegrityError):
                await db.commit()

    async def test_concurrent_get_or_create_keeps_one(self) -> None:
        async def create() -> int:
            async with self.sessions() as db:
                return (await self_service.get_or_create(db, await db.get(User, self.ids["carol"]))).id

        ids = await asyncio.gather(*(create() for _ in range(4)))
        self.assertEqual(len(set(ids)), 1)
        async with self.sessions() as db:
            rows = (await db.execute(select(Conversation.id).where(Conversation.self_owner_id == self.ids["carol"]))).scalars().all()
            members = (await db.execute(select(ConversationMember.user_id).where(ConversationMember.conversation_id == ids[0]))).scalars().all()
        self.assertEqual((len(rows), members), (1, [self.ids["carol"]]))

    async def test_list_always_contains_it(self) -> None:
        listed = await self.json("GET", "/conversations", as_="bob")
        mine = [item for item in listed if item["type"] == "self"]
        self.assertEqual(len(mine), 1)
        self.assertIn(self.direct_id, [item["id"] for item in listed])
        # 再列一次不会多出来，也就是 GET /conversations/self 的那个
        again = await self.json("GET", "/conversations", as_="bob")
        self.assertEqual([item["id"] for item in again if item["type"] == "self"], [mine[0]["id"]])
        self.assertEqual(await self.self_id("bob"), mine[0]["id"])

    async def test_others_cannot_read_join_or_be_invited(self) -> None:
        cid = await self.self_id()
        await self.send(cid, "只有我看得到")
        # 别人：读不到、发不了、开不了单、下不了（都是 404，和别人的私聊一样）
        await self.call("GET", f"/conversations/{cid}/messages", as_="carol", expected=404)
        await self.call("POST", f"/conversations/{cid}/messages", as_="carol", expected=404, json={"content": "hi"})
        await self.call("POST", f"/conversations/{cid}/transfers", as_="carol", expected=404, json={"name": "a", "size": 1})
        await self.call("GET", f"/conversations/{cid}/members", as_="carol", expected=404)
        # 进群 / 申请进群 / 拉人 / 退出：不支持
        self.assertEqual((await self.code("POST", f"/conversations/{cid}/join", 400, as_="carol"))["code"], "self_chat_unsupported")
        self.assertEqual((await self.code("POST", f"/conversations/{cid}/join-requests", 400, as_="carol", json={}))["code"], "self_chat_unsupported")
        invite = await self.code("POST", f"/conversations/{cid}/members", 400, json={"user_ids": [self.ids["carol"]]})
        self.assertEqual(invite["code"], "self_chat_unsupported")
        self.assertIn("我的设备", invite["message"])
        self.assertEqual((await self.code("DELETE", f"/conversations/{cid}/members/me", 400))["code"], "self_chat_unsupported")
        self.assertEqual((await self.code("PATCH", f"/conversations/{cid}/settings", 400, json={"all_muted": True}))["code"], "self_chat_unsupported")
        async with self.sessions() as db:
            members = (await db.execute(select(ConversationMember.user_id).where(ConversationMember.conversation_id == cid))).scalars().all()
        self.assertEqual(members, [self.ids["alice"]])

    async def test_admin_list_hides_self_conversations_by_default(self) -> None:
        from app.services import reports as report_service

        cid = await self.self_id()
        async with self.sessions() as db:
            default = await report_service.list_conversations(db)
            only_self = await report_service.list_conversations(db, conversation_type=ConversationType.SELF)
            reads = await report_service.build_conversation_reads(db, only_self)
        self.assertNotIn(cid, [item.id for item in default])
        self.assertEqual([(item.id, item.display_title) for item in reads], [(cid, "我的设备")])


# ---------------------------------------------------------------- 消息


class MessageTests(SelfChatCase):
    async def test_device_metadata_and_realtime_to_all_my_devices(self) -> None:
        cid = await self.self_id()
        self.send_to_users.reset_mock()
        sent = await self.send(cid, "从手机发", platform="android", client_nonce="n1")
        self.assertEqual((sent["metadata"]["device"], sent["client_nonce"]), ("android", "n1"))
        events = [call.args for call in self.send_to_users.await_args_list if call.args[1] == "message.created"]
        self.assertEqual(len(events), 1)
        # 发给自己（所有在线的设备，包括发送的这台；客户端按 client_nonce 去重）
        self.assertEqual(set(events[0][0]), {self.ids["alice"]})
        self.assertEqual(events[0][2].metadata["device"], "android")
        # 客户端自己带的 device 被请求头覆盖；没带头时按 X-Kuke-Client 猜，再没有算 web
        forged = await self.send(cid, "伪造", platform="desktop", metadata={"device": "ios"})
        self.assertEqual(forged["metadata"]["device"], "desktop")
        guessed = await self.json(
            "POST", f"/conversations/{cid}/messages", expected=201, headers={"X-Kuke-Client": "kukechat-web/android-2.0.9"}, json={"content": "猜"}
        )
        self.assertEqual(guessed["metadata"]["device"], "android")
        self.assertEqual((await self.send(cid, "默认", platform=None))["metadata"]["device"], "web")
        self.assertEqual((await self.send(cid, "乱写", platform="toaster"))["metadata"]["device"], "web")
        # 别的会话不加 device
        self.assertNotIn("device", (await self.send(self.direct_id, "私聊")).get("metadata") or {})
        # 历史、搜索、会话列表的最后一条都带着
        history = await self.json("GET", f"/conversations/{cid}/messages")
        self.assertEqual([item["metadata"]["device"] for item in history], ["android", "desktop", "android", "web", "web"])
        found = await self.json("GET", f"/conversations/{cid}/messages/search", params={"q": "手机"})
        self.assertEqual(found["items"][0]["message"]["metadata"]["device"], "android")
        listed = {item["id"]: item for item in await self.json("GET", "/conversations")}
        self.assertEqual((listed[cid]["last_message"]["content"], listed[cid]["unread_count"]), ("乱写", 0))

    async def test_normal_features_work(self) -> None:
        cid = await self.self_id()
        sent = await self.send(cid, "要撤回的")
        quoted = await self.send(cid, "引用", metadata={"reply_to": {"message_id": sent["id"]}})
        self.assertEqual(quoted["metadata"]["reply_to"]["message_id"], sent["id"])
        image = await self.send(cid, "/uploads/messages/a.png", type="image")
        self.assertEqual(image["type"], "image")
        self.assertTrue((await self.json("POST", f"/conversations/{cid}/messages/{quoted['id']}/bookmark"))["bookmarked"])
        recalled = await self.json("POST", f"/conversations/{cid}/messages/{sent['id']}/recall")
        self.assertIsNotNone(recalled["recalled_at"])
        # 合并转发：从私聊转进「我的设备」，记下转发的设备
        source = await self.send(self.direct_id, "转给自己")
        bundle = await self.json(
            "POST", f"/conversations/{cid}/forward-bundle", expected=201, headers={"X-Kuke-Platform": "ios"},
            json={"source_conversation_id": self.direct_id, "message_ids": [source["id"]]},
        )
        self.assertEqual((bundle["type"], bundle["metadata"]["device"]), ("forward_bundle", "ios"))
        # 从「我的设备」转出去：来源标题是「我的设备」
        out = await self.json(
            "POST", f"/conversations/{self.direct_id}/forward-bundle", expected=201, json={"source_conversation_id": cid, "message_ids": [quoted["id"]]}
        )
        self.assertEqual(out["metadata"]["source_title"], "我的设备")

    async def test_disabled_features(self) -> None:
        cid = await self.self_id()
        mention = await self.code(
            "POST", f"/conversations/{cid}/messages", 400, json={"content": "@我", "metadata": {"mentions": [{"user_id": self.ids["alice"], "name": "alice"}]}}
        )
        self.assertEqual(mention["code"], "self_chat_unsupported")
        self.assertEqual(
            (await self.code("POST", f"/conversations/{cid}/messages", 400, json={"content": "@全体", "metadata": {"mention_all": True}}))["code"],
            "self_chat_unsupported",
        )
        poll = await self.code("POST", f"/conversations/{cid}/polls", 400, json={"title": "吃什么", "options": ["面", "饭"]})
        self.assertEqual(poll["code"], "self_chat_unsupported")
        with patch.object(red_packet_service, "get_settings", AsyncMock(return_value={**red_packet_service.DEFAULTS, "enabled": True})):
            packet = await self.code("POST", "/red-packets", 400, json={"conversation_id": cid, "kind": "normal", "amount": 1, "count": 1})
        self.assertEqual(packet["code"], "self_chat_unsupported")
        with patch.object(gift_service, "get_settings", AsyncMock(return_value={**gift_service.DEFAULTS, "enabled": True})):
            gift = await self.code("POST", "/gifts", 400, json={"to_user_id": self.ids["alice"], "conversation_id": cid, "kind": "gift", "gift_key": "rose"})
        self.assertEqual(gift["code"], "self_chat_unsupported")
        message = await self.send(cid, "设精华")
        self.assertEqual((await self.code("POST", f"/conversations/{cid}/messages/{message['id']}/feature", 400))["code"], "self_chat_unsupported")
        # 举报：消息、会话都不行
        report = await self.code("POST", "/reports", 400, json={"target_type": "message", "target_id": message["id"], "reason": "垃圾广告"})
        self.assertEqual(report["code"], "self_chat_unsupported")
        report = await self.code("POST", "/reports", 400, json={"target_type": "conversation", "target_id": cid, "reason": "垃圾广告", "details": "这个会话里一直在发广告，已经持续好几天了"})
        self.assertEqual(report["code"], "self_chat_unsupported")
        async with self.sessions() as db:
            self.assertEqual(int(await db.scalar(select(func.count()).select_from(UserReport))), 0)

    async def test_moderation_is_skipped(self) -> None:
        cid = await self.self_id()
        sent = await self.send(cid, "不审")
        self.schedule_moderation.assert_not_called()
        await self.send(self.direct_id, "要审")
        self.schedule_moderation.assert_called_once()
        # 就算有别的路径把它送进审核任务，也不会调审核服务、不落记录
        content = AsyncMock(return_value=[])
        with patch.object(moderation_service, "_moderate_message_content", content):
            async with self.sessions() as db:
                await moderation_service._moderate_message_task(sent["id"], ModerationSource.MESSAGE_AUTO, None, db)
        content.assert_not_awaited()

    async def test_idle_scan_never_picks_self_messages(self) -> None:
        cid = await self.self_id()
        now = datetime.now(UTC)
        async with self.sessions() as db:
            message = Message(conversation_id=cid, sender_id=self.ids["alice"], content="可疑", created_at=now - timedelta(hours=1))
            db.add(message)
            await db.flush()
            db.add(
                ContentModerationRecord(
                    source=ModerationSource.MESSAGE_AUTO, message_id=message.id, conversation_id=cid, user_id=self.ids["alice"],
                    content_type="text", content_text="可疑", decision="allow", action_taken=ModerationAction.NONE,
                    risk_score=9.9, created_at=now - timedelta(hours=1),
                )
            )
            await db.commit()
            rows = (await db.execute(moderation_idle._candidate_query(now))).all()
        self.assertEqual(rows, [])


# ---------------------------------------------------------------- 临时文件


class TransferTests(SelfChatCase):
    async def test_create_upload_complete_download_expire_and_sweep(self) -> None:
        cid = await self.self_id()
        data = b"%PDF-1.7 hello"
        created = await self.json(
            "POST", f"/conversations/{cid}/transfers", expected=201, headers={"X-Kuke-Platform": "desktop"},
            json={"name": "../季度/报告.pdf", "size": len(data), "content_type": "application/pdf"},
        )
        tid, upload = created["transfer_id"], created["upload"]
        self.assertEqual((upload["direct"], upload["method"]), (False, "PUT"))
        self.assertEqual(upload["url"], f"/api/v1/conversations/{cid}/transfers/{tid}/content")
        transfer = created["transfer"]
        self.assertEqual((transfer["name"], transfer["state"], transfer["device"], transfer["expires_at"]), ("报告.pdf", "pending", "desktop", None))
        # 还没传：409；大小不对：422
        self.assertEqual((await self.code("POST", f"/conversations/{cid}/transfers/{tid}/complete", 409))["code"], "upload_incomplete")
        self.assertEqual((await self.code("PUT", f"/conversations/{cid}/transfers/{tid}/content", 422, content=b"short"))["code"], "upload_size_mismatch")
        self.assertEqual((await self.code("GET", f"/conversations/{cid}/transfers/{tid}/download", 409))["code"], "upload_incomplete")
        stored = await self.json("PUT", f"/conversations/{cid}/transfers/{tid}/content", content=data)
        self.assertEqual(stored["uploaded_bytes"], len(data))
        self.send_to_users.reset_mock()
        done = await self.json("POST", f"/conversations/{cid}/transfers/{tid}/complete", expected=201, json={"client_nonce": "f1"})
        message = done["message"]
        self.assertEqual((message["type"], message["content"], message["client_nonce"]), ("text", "[文件] 报告.pdf（14 B）", "f1"))
        meta = message["metadata"]
        self.assertEqual(meta["device"], "desktop")
        self.assertEqual(
            {key: meta["transfer"][key] for key in ("id", "name", "size", "content_type", "expired")},
            {"id": tid, "name": "报告.pdf", "size": len(data), "content_type": "application/pdf", "expired": False},
        )
        expires_at = START + timedelta(hours=24)
        self.assertEqual(datetime.fromisoformat(meta["transfer"]["expires_at"]), expires_at)
        self.assertEqual((done["transfer"]["state"], done["transfer"]["message_id"]), ("ready", message["id"]))
        self.assertTrue(any(call.args[1] == "message.created" for call in self.send_to_users.await_args_list))
        self.schedule_moderation.assert_not_called()
        # 重复 complete：200，原样返回那条消息，不多落一条
        again = await self.call("POST", f"/conversations/{cid}/transfers/{tid}/complete", expected=200)
        self.assertEqual(again.json()["message"]["id"], message["id"])
        self.assertEqual(len(await self.json("GET", f"/conversations/{cid}/messages")), 1)
        self.assertEqual((await self.code("PUT", f"/conversations/{cid}/transfers/{tid}/content", 409, content=data))["code"], "already_completed")
        # 下载：attachment、octet-stream
        response = await self.call("GET", f"/conversations/{cid}/transfers/{tid}/download", expected=200)
        self.assertEqual(response.content, data)
        self.assertEqual(response.headers["content-type"], "application/octet-stream")
        self.assertTrue(response.headers["content-disposition"].startswith("attachment;"))
        self.assertEqual((await self.code("GET", f"/conversations/{cid}/transfers/{tid}/link", 409))["code"], "direct_download_unavailable")
        listed = await self.json("GET", f"/conversations/{cid}/transfers")
        self.assertEqual(([item["id"] for item in listed["items"]], listed["used_bytes"], listed["ttl_hours"], listed["direct"]), ([tid], len(data), 24, False))
        # 别人：404
        await self.call("GET", f"/conversations/{cid}/transfers/{tid}/download", as_="bob", expected=404)
        # 24 小时后：410「文件已过期」，消息上 expired = true，列表里没了
        self.now = expires_at
        expired = await self.code("GET", f"/conversations/{cid}/transfers/{tid}/download", 410)
        self.assertEqual((expired["code"], expired["message"]), ("file_expired", "文件已过期"))
        with patch.object(self_service, "_now", lambda: expires_at + timedelta(seconds=1)):
            history = await self.json("GET", f"/conversations/{cid}/messages")
        self.assertTrue(history[0]["metadata"]["transfer"]["expired"])
        self.assertEqual((await self.json("GET", f"/conversations/{cid}/transfers"))["items"], [])
        self.assertEqual((await self.json("GET", f"/conversations/{cid}/transfers/{tid}"))["state"], "expired")
        # 清理：删对象、标 expired；再跑一遍什么都不做
        key = (await self.row(tid)).storage_key
        self.assertTrue((Path(settings.upload_dir) / key).exists())
        async with self.sessions() as db:
            self.assertEqual((await self_service.sweep(db))["expired"], 1)
            self.assertEqual(await self_service.sweep(db), {"stale_uploads": 0, "expired": 0, "purged": 0})
        self.assertFalse((Path(settings.upload_dir) / key).exists())
        self.assertEqual((await self.row(tid)).state, "expired")
        self.assertEqual((await self.code("GET", f"/conversations/{cid}/transfers/{tid}/download", 410))["code"], "file_expired")
        # 过期 7 天后行也删掉
        self.tick(days=7)
        async with self.sessions() as db:
            self.assertEqual((await self_service.sweep(db))["purged"], 1)
        self.assertIsNone(await self.row(tid))

    async def test_validation_and_limits(self) -> None:
        cid = await self.self_id()
        path = f"/conversations/{cid}/transfers"
        self.assertEqual((await self.code("POST", path, 422, json={"name": "a", "size": 0}))["code"], "empty_file")
        with patch.object(settings, "self_transfer_max_bytes", 10), patch.object(settings, "self_transfer_quota_bytes", 15):
            big = await self.code("POST", path, 413, json={"name": "a", "size": 11})
            self.assertEqual((big["code"], big["max_file_bytes"]), ("file_too_large", 10))
            await self.json("POST", path, expected=201, json={"name": "a", "size": 10})
            # 没传完的上传单也占额度
            quota = await self.code("POST", path, 413, json={"name": "b", "size": 6})
            self.assertEqual((quota["code"], quota["used_bytes"], quota["quota_bytes"]), ("transfer_quota_exceeded", 10, 15))
        # 不是「我的设备」会话：400；任意文件类型都行
        self.assertEqual((await self.code("POST", f"/conversations/{self.direct_id}/transfers", 400, json={"name": "a", "size": 1}))["code"], "not_self_conversation")
        exe = await self.transfer(cid, b"MZ\x90\x00", name="setup.exe")
        self.assertEqual(exe["transfer"]["content_type"], "application/pdf")
        self.assertEqual(exe["message"]["metadata"]["transfer"]["name"], "setup.exe")
        # 不能用普通发消息接口伪造文件消息
        forged = await self.code("POST", f"/conversations/{cid}/messages", 422, json={"content": "x", "metadata": {"transfer": {"id": 1}}})
        self.assertEqual(forged["code"], "transfer_not_forwardable")

    async def test_stale_upload_is_swept(self) -> None:
        cid = await self.self_id()
        created = await self.json("POST", f"/conversations/{cid}/transfers", expected=201, json={"name": "a.bin", "size": 3})
        tid = created["transfer_id"]
        await self.json("PUT", f"/conversations/{cid}/transfers/{tid}/content", content=b"abc")
        key = (await self.row(tid)).storage_key
        self.tick(hours=1, seconds=1)
        # 过了 1 小时没 complete：上传单作废
        self.assertEqual((await self.code("POST", f"/conversations/{cid}/transfers/{tid}/complete", 404))["code"], "upload_not_found")
        async with self.sessions() as db:
            self.assertEqual((await self_service.sweep(db))["stale_uploads"], 1)
        self.assertIsNone(await self.row(tid))
        self.assertFalse((Path(settings.upload_dir) / key).exists())
        self.assertEqual((await self.code("GET", f"/conversations/{cid}/transfers/{tid}", 404))["code"], "transfer_not_found")

    async def test_cannot_be_saved_or_forwarded(self) -> None:
        cid = await self.self_id()
        done = await self.transfer(cid)
        mid = done["message"]["id"]
        bookmark = await self.code("POST", f"/conversations/{cid}/messages/{mid}/bookmark", 422)
        self.assertEqual((bookmark["code"], bookmark["message"]), ("transfer_not_savable", "临时文件不能收藏"))
        async with self.sessions() as db:
            self.assertEqual(int(await db.scalar(select(func.count()).select_from(MessageBookmark))), 0)
        forward = await self.code(
            "POST", f"/conversations/{self.direct_id}/forward-bundle", 422, json={"source_conversation_id": cid, "message_ids": [mid]}
        )
        self.assertEqual(forward["code"], "transfer_not_forwardable")
        # 逐条转发（客户端把 metadata 原样发到别的会话）同样不行
        resend = await self.code(
            "POST", f"/conversations/{self.direct_id}/messages", 422, json={"content": done["message"]["content"], "metadata": done["message"]["metadata"]}
        )
        self.assertEqual(resend["code"], "transfer_not_forwardable")

    async def test_recall_expires_the_file(self) -> None:
        cid = await self.self_id()
        done = await self.transfer(cid)
        tid, mid = done["transfer"]["id"], done["message"]["id"]
        key = (await self.row(tid)).storage_key
        await self.json("POST", f"/conversations/{cid}/messages/{mid}/recall")
        self.assertEqual((await self.row(tid)).state, "expired")
        self.assertFalse((Path(settings.upload_dir) / key).exists())
        self.assertEqual((await self.code("GET", f"/conversations/{cid}/transfers/{tid}/download", 410))["code"], "file_expired")

    async def test_rate_limited_complete_keeps_the_upload(self) -> None:
        cid = await self.self_id()
        created = await self.json("POST", f"/conversations/{cid}/transfers", expected=201, json={"name": "a.bin", "size": 3})
        tid = created["transfer_id"]
        await self.json("PUT", f"/conversations/{cid}/transfers/{tid}/content", content=b"abc")
        from app.services import messages as message_service

        with patch.object(message_service, "MESSAGE_RATE_LIMIT_PER_MINUTE", 0):
            self.assertEqual((await self.code("POST", f"/conversations/{cid}/transfers/{tid}/complete", 429))["code"], "rate_limited")
        self.assertEqual((await self.row(tid)).state, "pending")
        await self.json("POST", f"/conversations/{cid}/transfers/{tid}/complete", expected=201)


class DirectTransferTests(SelfChatCase):
    """直传：storage 的开关和预签名打桩，对象用本地存储模拟「客户端已经传到桶里」。"""

    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        self.put_calls: list[tuple[str, int]] = []

        def fake_put(key: str, size: int, expires_in: int = 900) -> tuple[str, dict[str, str]]:
            self.put_calls.append((key, size))
            return f"https://bucket.test/{key}?sig=put", {"Content-Type": "application/octet-stream"}

        def fake_get(key: str, expires_in: int = 300, content_disposition: str | None = None) -> str:
            return f"https://bucket.test/{key}?sig=get"

        for patcher in (
            patch.object(storage, "direct_transfer_enabled", lambda: True),
            patch.object(storage, "presign_put", fake_put),
            patch.object(storage, "presign_get", fake_get),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    async def test_presigned_upload_and_download(self) -> None:
        cid = await self.self_id()
        created = await self.json("POST", f"/conversations/{cid}/transfers", expected=201, json={"name": "视频.mp4", "size": 5})
        tid, upload = created["transfer_id"], created["upload"]
        key, size = self.put_calls[-1]
        self.assertTrue(key.startswith(f"self-transfers/{self.ids['alice']}/") and size == 5)
        self.assertEqual((upload["direct"], upload["url"], upload["headers"]), (True, f"https://bucket.test/{key}?sig=put", {"Content-Type": "application/octet-stream"}))
        # 开了直传就不收中转上传
        self.assertEqual((await self.code("PUT", f"/conversations/{cid}/transfers/{tid}/content", 409, content=b"12345"))["code"], "direct_upload_required")
        target = Path(settings.upload_dir) / key
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"1234")
        mismatch = await self.code("POST", f"/conversations/{cid}/transfers/{tid}/complete", 422)
        self.assertEqual((mismatch["code"], mismatch["uploaded_size"]), ("upload_size_mismatch", 4))
        self.assertFalse(target.exists())
        target.write_bytes(b"12345")
        done = await self.json("POST", f"/conversations/{cid}/transfers/{tid}/complete", expected=201)
        self.assertEqual(done["transfer"]["state"], "ready")
        link = await self.json("GET", f"/conversations/{cid}/transfers/{tid}/link")
        self.assertEqual(link["url"], f"https://bucket.test/{key}?sig=get")
        self.assertTrue((await self.json("GET", f"/conversations/{cid}/transfers"))["direct"])


class StorageTests(unittest.TestCase):
    def test_self_transfers_live_in_the_private_bucket(self) -> None:
        with patch.object(settings, "s3_private_bucket", "private"), patch.object(settings, "s3_bucket", "public"):
            self.assertEqual(storage._bucket_for("self-transfers/1/abc"), "private")
            self.assertEqual(storage._bucket_for("group-files/1/abc"), "private")
            self.assertEqual(storage._bucket_for("messages/abc.png"), "public")

    def test_uploads_mount_does_not_serve_self_transfers(self) -> None:
        from app.main import CachedUploadFiles

        self.assertIn("self-transfers", CachedUploadFiles._private_folders)


class MigrationTests(unittest.IsolatedAsyncioTestCase):
    @requires_sqlite
    async def test_old_database_gets_column_and_unique_index(self) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            engine = make_engine(Path(tmp) / "old.db")
            try:
                async with engine.begin() as connection:
                    await connection.run_sync(Base.metadata.create_all)
                    await connection.execute(text("DROP INDEX uq_conversations_self_owner"))
                    await connection.execute(text("ALTER TABLE conversations DROP COLUMN self_owner_id"))
                await init_db(engine)
                await init_db(engine)
                async with engine.connect() as connection:
                    columns = {row[1] for row in await connection.execute(text("PRAGMA table_info(conversations)"))}
                    indexes = {row[1]: row[2] for row in await connection.execute(text("PRAGMA index_list(conversations)"))}
                self.assertIn("self_owner_id", columns)
                self.assertEqual(indexes.get("uq_conversations_self_owner"), 1)
            finally:
                await engine.dispose()


if __name__ == "__main__":
    unittest.main()
