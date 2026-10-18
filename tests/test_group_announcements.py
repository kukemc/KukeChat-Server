"""群公告 v2（docs/group-announcements.md）：卡片消息、选项、弹窗、确认、权限、审核、接口。"""

from __future__ import annotations

import json
import unittest
from datetime import UTC, datetime, timedelta
from typing import Annotated
from unittest.mock import AsyncMock, patch

import httpx
from fastapi import Depends, FastAPI, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

import app.models  # noqa: F401
from app.api.v1 import group_announcements as announcements_api
from app.core.config import settings
from app.core.database import Base, get_db
from app.core.dependencies import get_current_user
from app.models.conversation import Conversation, ConversationMember, GroupAnnouncement, GroupAnnouncementReceipt
from app.models.enums import ConversationType, MemberRole, MessageType
from app.models.message import Message
from app.models.user import User
from app.schemas.group_announcement import GroupAnnouncementCreate, GroupAnnouncementUpdate
from app.services import group_announcements as service
from app.services import moderation as moderation_service

IMG = "/uploads/messages/{}.jpg"


def image(name: str) -> str:
    return IMG.format(name)


class GroupAnnouncementTestCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        self.sessions = async_sessionmaker(self.engine, expire_on_commit=False)
        async with self.engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        joined = datetime.now(UTC) - timedelta(days=3)
        async with self.sessions() as db:
            users = [User(username=name, hashed_password="x") for name in ("owner", "admin", "alice", "bob", "newbie", "outsider")]
            db.add_all(users)
            await db.flush()
            self.ids = {user.username: user.id for user in users}
            group = Conversation(type=ConversationType.GROUP, title="群")
            direct = Conversation(type=ConversationType.DIRECT)
            db.add_all([group, direct])
            await db.flush()
            self.group_id, self.direct_id = group.id, direct.id
            roles = {"owner": MemberRole.OWNER, "admin": MemberRole.ADMIN, "alice": MemberRole.MEMBER, "bob": MemberRole.MEMBER}
            for name, role in roles.items():
                db.add(ConversationMember(conversation_id=group.id, user_id=self.ids[name], role=role, joined_at=joined, nickname="群主大人" if name == "owner" else None))
            await db.commit()

    async def asyncTearDown(self) -> None:
        await self.engine.dispose()

    async def user(self, db, name: str) -> User:
        return await db.get(User, self.ids[name])

    async def publish(self, name: str = "owner", **fields):
        fields.setdefault("content", "周六晚上八点开会")
        async with self.sessions() as db:
            return await service.create_announcement(db, self.group_id, await self.user(db, name), GroupAnnouncementCreate(**fields))

    async def edit(self, announcement_id: int, name: str = "owner", **fields):
        async with self.sessions() as db:
            return await service.update_announcement(
                db, self.group_id, announcement_id, await self.user(db, name), GroupAnnouncementUpdate(**fields)
            )

    async def join(self, name: str, at: datetime) -> None:
        async with self.sessions() as db:
            db.add(ConversationMember(conversation_id=self.group_id, user_id=self.ids[name], role=MemberRole.MEMBER, joined_at=at))
            await db.commit()

    async def popups(self, name: str) -> list[int]:
        async with self.sessions() as db:
            return [item.id for item in await service.pending_popups(db, self.group_id, self.ids[name])]

    def assert_code(self, ctx, status_code: int, code: str) -> None:
        self.assertEqual(ctx.exception.status_code, status_code)
        self.assertEqual(ctx.exception.detail["code"], code)


class ServiceTests(GroupAnnouncementTestCase):
    async def test_publish_sends_a_system_card_and_reads_back_everything(self) -> None:
        read, card = await self.publish(
            content="  第一行\n第二行  ",
            images=[image("a1"), image("a2"), image("a1")],
            pinned=True,
            popup=True,
            require_confirm=True,
            guide_nickname=True,
        )
        self.assertEqual(read.content, "第一行\n第二行")
        self.assertEqual([item.url for item in read.images], [image("a1"), image("a2")])
        self.assertEqual(read.images[0].thumbnail_url, "/uploads/messages/thumbs/a1.jpg")
        self.assertTrue(read.pinned and read.popup and read.require_confirm and read.guide_nickname)
        self.assertFalse(read.send_to_new_members)
        self.assertIsNotNone(read.pinned_at)
        self.assertIsNone(read.edited_at)
        self.assertEqual(read.author_display_name, "群主大人")
        # 发布人自己算看过、确认过
        self.assertTrue(read.my_seen and read.my_confirmed)
        self.assertEqual((read.confirmed_count, read.member_count), (1, 4))

        self.assertEqual(card.type, MessageType.SYSTEM)
        self.assertEqual(card.sender_id, self.ids["owner"])
        self.assertEqual(card.content, "群主大人 发布了群公告：第一行 第二行")
        metadata = json.loads(card.metadata_json)
        self.assertEqual(
            metadata,
            {
                "system_event": "group.announcement.card",
                "announcement_id": read.id,
                "action": "created",
                "summary": "第一行\n第二行",
                "image_url": image("a1"),
                "image_thumbnail_url": "/uploads/messages/thumbs/a1.jpg",
                "image_count": 2,
                "author_id": self.ids["owner"],
                "author_name": "群主大人",
                "require_confirm": True,
            },
        )
        async with self.sessions() as db:
            conversation = await db.get(Conversation, self.group_id)
            self.assertEqual(conversation.announcement, "第一行\n第二行")

    async def test_long_preview_is_cut_in_the_fallback_text(self) -> None:
        _read, card = await self.publish(content="字" * 600)
        self.assertEqual(card.content, "群主大人 发布了群公告：" + "字" * 40 + "…")
        self.assertEqual(len(json.loads(card.metadata_json)["summary"]), 200)

    async def test_only_staff_can_manage(self) -> None:
        with self.assertRaises(HTTPException) as ctx:
            await self.publish("alice")
        self.assert_code(ctx, 403, "announcement_forbidden")
        read, _ = await self.publish("admin")
        with self.assertRaises(HTTPException) as ctx:
            await self.edit(read.id, "alice", content="改")
        self.assert_code(ctx, 403, "announcement_forbidden")
        async with self.sessions() as db:
            with self.assertRaises(HTTPException) as ctx:
                await service.delete_announcement(db, self.group_id, read.id, await self.user(db, "bob"))
            self.assert_code(ctx, 403, "announcement_forbidden")
            with self.assertRaises(HTTPException) as ctx:
                await service.confirmation_page(db, self.group_id, read.id, self.ids["bob"], "confirmed", 50, 0)
            self.assert_code(ctx, 403, "announcement_forbidden")
            # 不是成员：404；私聊：400
            with self.assertRaises(HTTPException) as ctx:
                await service.list_announcements(db, self.group_id, self.ids["outsider"])
            self.assertEqual(ctx.exception.status_code, 404)

    async def test_content_and_image_validation(self) -> None:
        with self.assertRaises(ValueError):
            GroupAnnouncementCreate(content="字" * 601)
        with self.assertRaises(ValueError):
            GroupAnnouncementCreate(content="   ")
        self.assertEqual(GroupAnnouncementCreate(content="\n" + "字" * 600 + "\n").content, "字" * 600)
        with self.assertRaises(HTTPException) as ctx:
            await self.publish(images=["https://evil.example/a.jpg"])
        self.assert_code(ctx, 422, "invalid_announcement_image")
        with self.assertRaises(HTTPException) as ctx:
            await self.publish(images=["/uploads/avatars/a.jpg"])
        self.assert_code(ctx, 422, "invalid_announcement_image")
        with self.assertRaises(HTTPException) as ctx:
            await self.publish(images=[image(f"i{index}") for index in range(10)])
        self.assert_code(ctx, 422, "too_many_images")
        # announcements/ 目录的图也认
        read, _ = await self.publish(images=["/uploads/announcements/long.png"] + [image(f"i{index}") for index in range(8)])
        self.assertEqual(len(read.images), 9)

    async def test_list_puts_pinned_first_by_pin_time(self) -> None:
        first, _ = await self.publish(content="一")
        second, _ = await self.publish(content="二", pinned=True)
        third, _ = await self.publish(content="三")
        fourth, _ = await self.publish(content="四", pinned=True)
        # 第一条后来才置顶：置顶时间最新，排最前；多条置顶共存
        await self.edit(first.id, pinned=True)
        async with self.sessions() as db:
            items = await service.list_announcements(db, self.group_id, self.ids["alice"])
        self.assertEqual([item.content for item in items], ["一", "四", "二", "三"])
        self.assertTrue(all(item.pinned for item in items[:3]))
        # 取消置顶回到按发布时间
        await self.edit(fourth.id, pinned=False)
        async with self.sessions() as db:
            items = await service.list_announcements(db, self.group_id, self.ids["alice"])
        self.assertEqual([item.content for item in items], ["一", "二", "四", "三"])
        self.assertIsNone(items[2].pinned_at)
        del third

    async def test_send_to_new_members_is_unique_per_group(self) -> None:
        first, _ = await self.publish(content="一", send_to_new_members=True)
        second, _ = await self.publish(content="二", send_to_new_members=True)
        async with self.sessions() as db:
            items = {item.id: item for item in await service.list_announcements(db, self.group_id, self.ids["alice"])}
        self.assertFalse(items[first.id].send_to_new_members)
        self.assertTrue(items[second.id].send_to_new_members)
        await self.edit(first.id, send_to_new_members=True)
        async with self.sessions() as db:
            items = {item.id: item for item in await service.list_announcements(db, self.group_id, self.ids["alice"])}
        self.assertTrue(items[first.id].send_to_new_members)
        self.assertFalse(items[second.id].send_to_new_members)

    async def test_popup_for_existing_members_and_new_member_announcement(self) -> None:
        popup, _ = await self.publish(content="弹窗", popup=True)
        welcome, _ = await self.publish(content="新人须知", send_to_new_members=True, require_confirm=True)
        plain, _ = await self.publish(content="普通")
        self.assertEqual(await self.popups("alice"), [popup.id])
        # 发布人自己不弹
        self.assertEqual(await self.popups("owner"), [])
        await self.join("newbie", datetime.now(UTC) + timedelta(minutes=5))
        self.assertEqual(await self.popups("newbie"), [welcome.id])

        # 不需确认的：看过就不弹了
        async with self.sessions() as db:
            seen = await service.mark_seen(db, self.group_id, popup.id, self.ids["alice"])
        self.assertTrue(seen.my_seen)
        self.assertFalse(seen.my_confirmed)
        self.assertEqual(await self.popups("alice"), [])
        # 需要确认的：只看过还会弹，确认之后才不弹
        async with self.sessions() as db:
            await service.mark_seen(db, self.group_id, welcome.id, self.ids["newbie"])
        self.assertEqual(await self.popups("newbie"), [welcome.id])
        async with self.sessions() as db:
            confirmed = await service.confirm(db, self.group_id, welcome.id, self.ids["newbie"])
        self.assertTrue(confirmed.my_confirmed and confirmed.my_seen)
        self.assertEqual(await self.popups("newbie"), [])
        del plain

    async def test_turning_popup_on_later_covers_members_who_were_there(self) -> None:
        read, _ = await self.publish(content="先不弹")
        self.assertEqual(await self.popups("bob"), [])
        await self.edit(read.id, popup=True)
        self.assertEqual(await self.popups("bob"), [read.id])

    async def test_confirmations(self) -> None:
        read, _ = await self.publish(require_confirm=True)
        async with self.sessions() as db:
            await service.confirm(db, self.group_id, read.id, self.ids["alice"])
            # 重复确认不出错、不重复计数
            again = await service.confirm(db, self.group_id, read.id, self.ids["alice"])
        self.assertEqual(again.confirmed_count, 2)
        async with self.sessions() as db:
            confirmed = await service.confirmation_page(db, self.group_id, read.id, self.ids["admin"], "confirmed", 50, 0)
            unconfirmed = await service.confirmation_page(db, self.group_id, read.id, self.ids["admin"], "unconfirmed", 1, 0)
            unconfirmed_rest = await service.confirmation_page(db, self.group_id, read.id, self.ids["admin"], "unconfirmed", 1, 1)
        self.assertEqual((confirmed.member_count, confirmed.confirmed_count, confirmed.unconfirmed_count), (4, 2, 2))
        self.assertEqual({item.user.id for item in confirmed.items}, {self.ids["owner"], self.ids["alice"]})
        self.assertTrue(all(item.confirmed_at is not None for item in confirmed.items))
        self.assertEqual(len(unconfirmed.items), 1)
        self.assertTrue(unconfirmed.has_more)
        self.assertFalse(unconfirmed_rest.has_more)
        self.assertEqual(
            {unconfirmed.items[0].user.id, unconfirmed_rest.items[0].user.id}, {self.ids["admin"], self.ids["bob"]}
        )
        # 退群的人不算
        async with self.sessions() as db:
            member = (
                await db.execute(
                    select(ConversationMember).where(ConversationMember.conversation_id == self.group_id, ConversationMember.user_id == self.ids["alice"])
                )
            ).scalar_one()
            await db.delete(member)
            await db.commit()
            page = await service.confirmation_page(db, self.group_id, read.id, self.ids["admin"], "confirmed", 50, 0)
        self.assertEqual((page.member_count, page.confirmed_count), (3, 1))

    async def test_confirm_requires_the_option(self) -> None:
        read, _ = await self.publish()
        async with self.sessions() as db:
            with self.assertRaises(HTTPException) as ctx:
                await service.confirm(db, self.group_id, read.id, self.ids["alice"])
        self.assert_code(ctx, 409, "confirm_not_required")

    async def test_update_is_partial_and_sends_an_updated_card(self) -> None:
        read, _ = await self.publish(images=[image("a"), image("b")], popup=True)
        # 老客户端只传 content + pinned：图片和选项都留着
        updated, card = await self.edit(read.id, content="改过了", pinned=False)
        self.assertEqual(updated.content, "改过了")
        self.assertEqual([item.url for item in updated.images], [image("a"), image("b")])
        self.assertTrue(updated.popup)
        self.assertIsNotNone(updated.edited_at)
        self.assertIsNotNone(card)
        metadata = json.loads(card.metadata_json)
        self.assertEqual((metadata["action"], metadata["announcement_id"], metadata["summary"]), ("updated", read.id, "改过了"))
        self.assertEqual(card.content, "群主大人 更新了群公告：改过了")

        # 只改置顶：不发卡片、不算编辑
        pinned, card = await self.edit(read.id, pinned=True)
        self.assertIsNone(card)
        self.assertTrue(pinned.pinned)
        self.assertEqual(pinned.edited_at, updated.edited_at)
        # 什么都没变：也不发
        _same, card = await self.edit(read.id, content="改过了")
        self.assertIsNone(card)
        # 换图
        changed, card = await self.edit(read.id, images=[image("c")])
        self.assertEqual([item.url for item in changed.images], [image("c")])
        self.assertEqual(json.loads(card.metadata_json)["image_url"], image("c"))

    async def test_delete_marks_cards_and_drops_receipts(self) -> None:
        read, created_card = await self.publish(require_confirm=True)
        _updated, updated_card = await self.edit(read.id, content="第二版")
        other, other_card = await self.publish(content="另一条")
        async with self.sessions() as db:
            await service.confirm(db, self.group_id, read.id, self.ids["alice"])
        async with self.sessions() as db:
            marked = await service.delete_announcement(db, self.group_id, read.id, await self.user(db, "admin"))
        self.assertEqual(sorted(marked), sorted([created_card.id, updated_card.id]))
        async with self.sessions() as db:
            for message_id, deleted in ((created_card.id, True), (updated_card.id, True), (other_card.id, False)):
                metadata = json.loads((await db.get(Message, message_id)).metadata_json)
                self.assertEqual(metadata.get("deleted", False), deleted)
            receipts = (await db.execute(select(GroupAnnouncementReceipt).where(GroupAnnouncementReceipt.announcement_id == read.id))).all()
            self.assertEqual(receipts, [])
            self.assertIsNone(await db.get(GroupAnnouncement, read.id))
            self.assertEqual((await db.get(Conversation, self.group_id)).announcement, "另一条")
            with self.assertRaises(HTTPException) as ctx:
                await service.get_announcement(db, self.group_id, read.id, self.ids["alice"])
            self.assert_code(ctx, 404, "announcement_not_found")
        del other

    async def test_card_lookup_does_not_match_prefix_ids(self) -> None:
        # 公告 1 的卡片不能把公告 12 的卡片也标掉
        reads = [await self.publish(content=f"第{index}条") for index in range(12)]
        target, target_card = reads[0]
        _last, last_card = reads[11]
        self.assertEqual(reads[11][0].id, 12)
        self.assertEqual(target.id, 1)
        async with self.sessions() as db:
            marked = await service.delete_announcement(db, self.group_id, target.id, await self.user(db, "owner"))
        self.assertEqual(marked, [target_card.id])
        async with self.sessions() as db:
            self.assertNotIn("deleted", json.loads((await db.get(Message, last_card.id)).metadata_json))

    async def test_moderation_blocks_text_and_new_images(self) -> None:
        block = {"type": "text", "decision": "block", "severity": 2}
        allow = {"type": "image", "decision": "allow", "severity": 0}
        with patch.object(settings, "content_moderation_enabled", True), patch.object(
            moderation_service, "_call_text_moderation_for_user", AsyncMock(return_value=block)
        ), patch.object(moderation_service, "_call_image_moderation_for_user", AsyncMock(return_value=allow)):
            with self.assertRaises(HTTPException) as ctx:
                await self.publish(content="坏内容")
            self.assert_code(ctx, 422, "announcement_rejected")
        async with self.sessions() as db:
            self.assertEqual((await db.execute(select(GroupAnnouncement))).all(), [])
            self.assertEqual((await db.execute(select(Message))).all(), [])

        read, _ = await self.publish(images=[image("ok")])
        image_check = AsyncMock(return_value={"type": "image", "decision": "block", "severity": 2})
        with patch.object(settings, "content_moderation_enabled", True), patch.object(
            moderation_service, "_call_text_moderation_for_user", AsyncMock(return_value={"decision": "allow", "severity": 0})
        ), patch.object(moderation_service, "_call_image_moderation_for_user", image_check):
            with self.assertRaises(HTTPException) as ctx:
                await self.edit(read.id, images=[image("ok"), image("bad")])
            self.assert_code(ctx, 422, "announcement_image_rejected")
            # 只送审新加的图
            self.assertEqual([call.args[0] for call in image_check.await_args_list], [image("bad")])

        # 审核服务挂了：放行
        with patch.object(settings, "content_moderation_enabled", True), patch.object(
            moderation_service, "_call_text_moderation_for_user", AsyncMock(side_effect=RuntimeError("down"))
        ):
            published, _ = await self.publish(content="审核服务挂了")
        self.assertEqual(published.content, "审核服务挂了")


class HttpTests(GroupAnnouncementTestCase):
    """走真实路由：路径不互相遮挡、事件发给谁、老客户端的请求体。"""

    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        from app.api.v1.router import api_router

        self.acting = "owner"
        self.api = FastAPI()
        self.api.include_router(api_router)

        async def isolated_db():
            async with self.sessions() as session:
                yield session

        async def acting_user(db: Annotated[AsyncSession, Depends(get_db)]) -> User:
            return await db.get(User, self.ids[self.acting])

        self.api.dependency_overrides[get_db] = isolated_db
        self.api.dependency_overrides[get_current_user] = acting_user
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=self.api), base_url="http://testserver")
        self.online = [self.ids["owner"], self.ids["alice"]]
        self.send_to_users = AsyncMock()
        self.message_updates = AsyncMock()
        for patcher in (
            patch.object(announcements_api, "online_conversation_member_ids", AsyncMock(return_value=self.online)),
            patch.object(announcements_api, "send_message_updates", self.message_updates),
            patch.object(announcements_api, "send_conversation_updates", AsyncMock()),
            patch.object(announcements_api.manager, "send_to_users", self.send_to_users),
            patch.object(announcements_api.manager, "online_user_ids_among", AsyncMock(side_effect=lambda ids: set(ids))),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    async def asyncTearDown(self) -> None:
        await self.client.aclose()
        await super().asyncTearDown()

    def url(self, suffix: str = "") -> str:
        return f"/api/v1/conversations/{self.group_id}/announcements{suffix}"

    def events(self, name: str) -> list:
        return [call.args for call in self.send_to_users.await_args_list if call.args[1] == name]

    async def test_full_flow_over_http(self) -> None:
        response = await self.client.post(
            self.url(), json={"content": "进群必读", "images": [image("x")], "popup": True, "require_confirm": True}
        )
        self.assertEqual(response.status_code, 201, response.text)
        created = response.json()
        aid = created["id"]
        self.assertEqual(created["images"], [{"url": image("x"), "thumbnail_url": "/uploads/messages/thumbs/x.jpg"}])
        (recipients, _name, payload), = self.events("group.announcement.created")
        self.assertEqual(recipients, self.online)
        # 推送里「我的」字段统一是 false
        self.assertFalse(payload.my_seen or payload.my_confirmed)
        card = self.message_updates.await_args.args[2]
        self.assertEqual(json.loads(card.metadata_json)["system_event"], "group.announcement.card")

        self.acting = "alice"
        popups = await self.client.get(self.url("/popups"))
        self.assertEqual(popups.status_code, 200, popups.text)
        self.assertEqual([item["id"] for item in popups.json()], [aid])
        detail = await self.client.get(self.url(f"/{aid}"))
        self.assertEqual((detail.status_code, detail.json()["my_confirmed"]), (200, False))
        self.assertEqual((await self.client.post(self.url(f"/{aid}/seen"))).json(), {"announcement_id": aid, "my_seen": True, "my_confirmed": False})
        forbidden = await self.client.get(self.url(f"/{aid}/confirmations"))
        self.assertEqual((forbidden.status_code, forbidden.json()["detail"]["code"]), (403, "announcement_forbidden"))
        confirmed = await self.client.post(self.url(f"/{aid}/confirm"))
        self.assertEqual((confirmed.status_code, confirmed.json()["my_confirmed"], confirmed.json()["confirmed_count"]), (200, True, 2))
        (recipients, _name, payload), = self.events("group.announcement.confirmations")
        self.assertEqual(set(recipients), {self.ids["owner"], self.ids["admin"], self.ids["alice"]})
        self.assertEqual(payload, {"conversation_id": self.group_id, "announcement_id": aid, "confirmed_count": 2, "member_count": 4})
        self.assertEqual((await self.client.get(self.url("/popups"))).json(), [])

        self.acting = "admin"
        stats = await self.client.get(self.url(f"/{aid}/confirmations"), params={"status": "unconfirmed"})
        self.assertEqual(stats.status_code, 200, stats.text)
        self.assertEqual((stats.json()["confirmed_count"], stats.json()["unconfirmed_count"]), (2, 2))
        self.assertEqual({item["user"]["id"] for item in stats.json()["items"]}, {self.ids["admin"], self.ids["bob"]})

        # 老客户端的修改：只有 content + pinned
        patched = await self.client.patch(self.url(f"/{aid}"), json={"content": "进群必读（新版）", "pinned": True})
        self.assertEqual(patched.status_code, 200, patched.text)
        self.assertEqual(len(patched.json()["images"]), 1)
        self.assertTrue(patched.json()["require_confirm"])
        self.assertEqual(len(self.events("group.announcement.updated")), 1)
        self.assertEqual(json.loads(self.message_updates.await_args.args[2].metadata_json)["action"], "updated")

        listed = await self.client.get(self.url())
        self.assertEqual([item["id"] for item in listed.json()], [aid])

        deleted = await self.client.delete(self.url(f"/{aid}"))
        self.assertEqual(deleted.status_code, 200, deleted.text)
        (_recipients, _name, payload), = self.events("group.announcement.deleted")
        self.assertEqual((payload["conversation_id"], payload["announcement_id"], len(payload["card_message_ids"])), (self.group_id, aid, 2))
        missing = await self.client.get(self.url(f"/{aid}"))
        self.assertEqual((missing.status_code, missing.json()["detail"]["code"]), (404, "announcement_not_found"))

    async def test_validation_errors_over_http(self) -> None:
        too_long = await self.client.post(self.url(), json={"content": "字" * 601})
        self.assertEqual(too_long.status_code, 422)
        self.acting = "bob"
        forbidden = await self.client.post(self.url(), json={"content": "hi"})
        self.assertEqual((forbidden.status_code, forbidden.json()["detail"]["code"]), (403, "announcement_forbidden"))
        direct = await self.client.get(f"/api/v1/conversations/{self.direct_id}/announcements")
        self.assertEqual(direct.status_code, 404)


if __name__ == "__main__":
    unittest.main()
