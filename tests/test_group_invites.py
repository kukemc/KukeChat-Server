"""群邀请链接按邀请人身份走（2026-11-07）：群主 / 管理员的邀请直接进群，普通成员的邀请按群的入群方式。

以前任何成员拿到的都是群的公共链接，点开直接进群，「需审核 / 回答问题 / 仅邀请」全被绕过。
"""

from __future__ import annotations

from fastapi import HTTPException
from sqlalchemy import select

from app.models.conversation import Conversation, ConversationMember, GroupJoinRequest
from app.models.enums import GroupJoinRequestStatus, InviteTargetType
from app.models.invite import InviteLink
from app.schemas.invite import InviteAcceptRequest
from app.services import conversations as conversation_service
from app.services import invites as invite_service

from test_chat_extras import ChatExtrasBase


class GroupInviteTests(ChatExtrasBase):
    async def _set_mode(self, join_mode: str, *, auto_approve: bool = False, question: str | None = None) -> None:
        async with self.sessions() as db:
            group = await db.get(Conversation, self.group_id)
            group.join_mode = join_mode
            group.auto_approve = auto_approve
            group.join_question = question
            await db.commit()

    async def _link(self, inviter: str) -> str:
        async with self.sessions() as db:
            link = await invite_service.get_or_create_invite_link(db, InviteTargetType.GROUP, self.group_id, await self._user(db, inviter))
            return link.token

    async def _resolve(self, token: str, viewer: str = "outsider"):
        async with self.sessions() as db:
            return await invite_service.resolve_invite(db, token, await self._user(db, viewer))

    async def _accept(self, token: str, payload: InviteAcceptRequest | None = None, viewer: str = "outsider"):
        async with self.sessions() as db:
            return await invite_service.accept_invite(db, token, await self._user(db, viewer), payload)

    async def _is_member(self, name: str) -> bool:
        async with self.sessions() as db:
            row = await db.execute(
                select(ConversationMember.id).where(
                    ConversationMember.conversation_id == self.group_id, ConversationMember.user_id == self.ids[name]
                )
            )
            return row.scalar_one_or_none() is not None

    async def test_owner_invite_joins_directly_even_when_group_needs_answer(self) -> None:
        await self._set_mode("question", question="暗号？")
        token = await self._link("owner")
        self.assertEqual((await self._resolve(token)).join_action, "join")
        result = await self._accept(token)
        self.assertEqual((result.status, result.joined_now), ("joined", True))
        self.assertTrue(await self._is_member("outsider"))

    async def test_member_invite_requires_answer_then_files_a_request(self) -> None:
        await self._set_mode("question", question="暗号？")
        token = await self._link("alice")
        self.assertEqual((await self._resolve(token)).join_action, "answer")
        with self.assertRaises(HTTPException) as raised:
            await self._accept(token)
        self.assertEqual(raised.exception.status_code, 422)
        self.assertFalse(await self._is_member("outsider"))

        result = await self._accept(token, InviteAcceptRequest(answer="天王盖地虎"))
        self.assertEqual(result.status, "pending")
        self.assertFalse(await self._is_member("outsider"))
        async with self.sessions() as db:
            request = await db.get(GroupJoinRequest, result.join_request_id)
            self.assertEqual((request.status, request.answer), (GroupJoinRequestStatus.PENDING, "天王盖地虎"))

    async def test_member_invite_to_approval_group_files_a_request(self) -> None:
        await self._set_mode("approval")
        token = await self._link("alice")
        self.assertEqual((await self._resolve(token)).join_action, "request")
        result = await self._accept(token)
        self.assertEqual(result.status, "pending")
        self.assertFalse(await self._is_member("outsider"))

    async def test_member_invite_to_open_or_auto_approve_group_joins(self) -> None:
        await self._set_mode("approval", auto_approve=True)
        token = await self._link("alice")
        self.assertEqual((await self._resolve(token)).join_action, "join")
        self.assertEqual((await self._accept(token)).status, "joined")
        self.assertTrue(await self._is_member("outsider"))

    async def test_invite_only_group_members_cannot_invite(self) -> None:
        await self._set_mode("invite_only")
        with self.assertRaises(HTTPException) as raised:
            await self._link("alice")
        self.assertEqual(raised.exception.status_code, 403)
        # 群主的邀请照样能进
        self.assertEqual((await self._accept(await self._link("owner"))).status, "joined")

    async def test_member_token_is_judged_by_current_role(self) -> None:
        await self._set_mode("approval")
        token = await self._link("alice")
        async with self.sessions() as db:
            alice = (
                await db.execute(
                    select(ConversationMember).where(
                        ConversationMember.conversation_id == self.group_id, ConversationMember.user_id == self.ids["alice"]
                    )
                )
            ).scalar_one()
            alice.role = conversation_service.MemberRole.ADMIN
            await db.commit()
        self.assertEqual((await self._resolve(token)).join_action, "join")

    async def test_forged_or_tampered_suffix_is_rejected(self) -> None:
        token = await self._link("alice")
        base = token.split(".")[0]
        owner_id = self.ids["owner"]
        for forged in (f"{base}.{owner_id}.000000000000", f"{base}.{owner_id}", f"{base}.x.y"):
            with self.assertRaises(HTTPException) as raised:
                await self._resolve(forged)
            self.assertEqual(raised.exception.status_code, 404)

    async def test_legacy_token_uses_link_creator(self) -> None:
        await self._set_mode("approval")
        await self._link("alice")
        async with self.sessions() as db:
            link = (await db.execute(select(InviteLink).where(InviteLink.target_id == self.group_id))).scalar_one()
            base = link.token
            link.created_by_id = self.ids["alice"]
            await db.commit()
        self.assertEqual((await self._resolve(base)).join_action, "request")
        async with self.sessions() as db:
            link = (await db.execute(select(InviteLink).where(InviteLink.target_id == self.group_id))).scalar_one()
            link.created_by_id = self.ids["owner"]
            await db.commit()
        self.assertEqual((await self._resolve(base)).join_action, "join")


class IncomingJoinRequestOrderTests(ChatExtrasBase):
    async def test_pending_requests_come_first(self) -> None:
        async with self.sessions() as db:
            db.add(GroupJoinRequest(conversation_id=self.group_id, requester_id=self.ids["outsider"], status=GroupJoinRequestStatus.PENDING))
            await db.commit()
            db.add(GroupJoinRequest(conversation_id=self.group_id, requester_id=self.ids["bob"], status=GroupJoinRequestStatus.ACCEPTED))
            await db.commit()
        async with self.sessions() as db:
            items = await conversation_service.list_incoming_group_join_requests(db, self.ids["owner"])
        self.assertEqual([item.status for item in items], [GroupJoinRequestStatus.PENDING, GroupJoinRequestStatus.ACCEPTED])
