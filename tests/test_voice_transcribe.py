"""语音消息转文字：POST /conversations/{cid}/messages/{mid}/transcribe（docs/chat-features.md「语音转文字」）。识别服务一律 mock。"""

from __future__ import annotations

import json
import unittest
from datetime import UTC, datetime, timedelta
from unittest.mock import patch

import httpx
from fastapi import Depends, FastAPI
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

import app.models  # noqa: F401
from app.api.v1 import conversations as api
from app.core.config import settings
from app.core.database import Base, get_db
from app.core.dependencies import get_current_user
from app.models.conversation import Conversation, ConversationMember
from app.models.enums import ConversationType, MemberRole, MessageType
from app.models.message import Message, MessageVisibility, VoiceTranscript
from app.models.user import User
from app.services import voice_asr
from app.services import voice_transcripts
from app.services.storage import StoredObject

VOICE_URL = "/uploads/messages/0123456789abcdef0123456789abcdef.webm"


def voice_metadata(seconds: float | None = 3.2) -> str:
    return json.dumps({"voice": {"duration_ms": int(seconds * 1000)}} if seconds is not None else {"voice": {}})


class VoiceTranscribeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        self.sessions = async_sessionmaker(self.engine, expire_on_commit=False)
        async with self.engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        async with self.sessions() as db:
            users = [User(username=name, hashed_password="x") for name in ("alice", "bob", "outsider")]
            db.add_all(users)
            await db.flush()
            self.ids = {user.username: user.id for user in users}
            group = Conversation(type=ConversationType.GROUP, title="群")
            db.add(group)
            await db.flush()
            self.group_id = group.id
            for name in ("alice", "bob"):
                db.add(ConversationMember(conversation_id=group.id, user_id=self.ids[name], role=MemberRole.MEMBER))
            await db.commit()
        self.current = "bob"
        self.heard: list[voice_asr.VoiceTranscription | Exception] = []
        self.calls = 0

        async def fake_transcribe(audio: bytes, filename: str, content_type: str, language=None):
            self.calls += 1
            result = self.heard.pop(0)
            if isinstance(result, Exception):
                raise result
            return result

        for patcher in (
            patch.object(settings, "voice_asr_url", "http://asr.test"),
            patch.object(voice_asr, "transcribe", fake_transcribe),
            patch("app.services.storage.get_object", lambda key, max_bytes=None: StoredObject(data=b"audio", content_type="audio/webm")),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

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
        await self.engine.dispose()

    async def _message(self, type_: MessageType = MessageType.VOICE, content: str = VOICE_URL, metadata: str | None = None, **fields) -> int:
        async with self.sessions() as db:
            message = Message(
                conversation_id=self.group_id,
                sender_id=self.ids["alice"],
                type=type_,
                content=content,
                metadata_json=metadata if metadata is not None else (voice_metadata() if type_ == MessageType.VOICE else None),
                **fields,
            )
            db.add(message)
            await db.commit()
            return message.id

    async def _transcribe(self, message_id: int, as_user: str | None = None) -> httpx.Response:
        if as_user:
            self.current = as_user
        return await self.client.post(f"/api/v1/conversations/{self.group_id}/messages/{message_id}/transcribe")

    def _heard(self, text: str, duration: float = 3.2) -> None:
        self.heard.append(voice_asr.VoiceTranscription(text=text, duration=duration))

    async def _code(self, message_id: int, as_user: str | None = None) -> tuple[int, str | None]:
        response = await self._transcribe(message_id, as_user)
        detail = response.json().get("detail")
        return response.status_code, detail.get("code") if isinstance(detail, dict) else detail

    async def _transcript_count(self) -> int:
        async with self.sessions() as db:
            return int(await db.scalar(select(func.count(VoiceTranscript.id))))

    async def test_first_call_transcribes_then_everyone_reads_the_cache(self) -> None:
        message_id = await self._message()
        self._heard("今天下午三点开会")
        response = await self._transcribe(message_id)
        self.assertEqual((response.status_code, response.json()), (200, {"text": "今天下午三点开会", "cached": False}))
        # 再点、别人点：直接读库，不再识别
        self.assertEqual((await self._transcribe(message_id)).json(), {"text": "今天下午三点开会", "cached": True})
        self.assertEqual((await self._transcribe(message_id, "alice")).json(), {"text": "今天下午三点开会", "cached": True})
        self.assertEqual(self.calls, 1)
        async with self.sessions() as db:
            row = (await db.execute(select(VoiceTranscript))).scalar_one()
            self.assertEqual((row.message_id, row.requested_by_id), (message_id, self.ids["bob"]))
        # 消息列表上带出来：只有转过的语音有，别的消息是 null
        other_voice = await self._message()
        text_id = await self._message(MessageType.TEXT, "你好")
        listed = (await self.client.get(f"/api/v1/conversations/{self.group_id}/messages")).json()
        by_id = {item["id"]: item for item in listed}
        self.assertEqual(by_id[message_id]["voice_transcript"], "今天下午三点开会")
        self.assertIsNone(by_id[other_voice]["voice_transcript"])
        self.assertIsNone(by_id[text_id]["voice_transcript"])

    async def test_empty_result_is_cached_too(self) -> None:
        message_id = await self._message()
        self._heard("", duration=1.0)
        self.assertEqual((await self._transcribe(message_id)).json(), {"text": "", "cached": False})
        self.assertEqual((await self._transcribe(message_id)).json(), {"text": "", "cached": True})
        self.assertEqual(self.calls, 1)

    async def test_who_and_what_can_be_transcribed(self) -> None:
        voice = await self._message()
        # 不是成员、消息不存在、别的会话的消息：404
        self.assertEqual(await self._code(voice, "outsider"), (404, "message_not_found"))
        self.assertEqual(await self._code(voice + 1000, "bob"), (404, "message_not_found"))
        # 撤回的
        recalled = await self._message(recalled_at=datetime.now(UTC))
        self.assertEqual(await self._code(recalled), (404, "message_not_found"))
        # 对自己隐藏的、清空聊天记录之前的
        hidden = await self._message()
        async with self.sessions() as db:
            db.add(MessageVisibility(message_id=hidden, user_id=self.ids["bob"]))
            await db.commit()
        self.assertEqual(await self._code(hidden), (404, "message_not_found"))
        async with self.sessions() as db:
            member = (
                await db.execute(select(ConversationMember).where(ConversationMember.user_id == self.ids["bob"]))
            ).scalar_one()
            member.cleared_before_message_id = voice
            await db.commit()
        self.assertEqual(await self._code(voice, "bob"), (404, "message_not_found"))
        # 不是语音
        text_id = await self._message(MessageType.TEXT, "你好")
        packet_id = await self._message(MessageType.RED_PACKET, "[红包]", metadata="{}")
        self.assertEqual(await self._code(text_id, "alice"), (422, "not_voice"))
        self.assertEqual(await self._code(packet_id, "alice"), (422, "not_voice"))
        self.assertEqual(self.calls, 0)

    async def test_hidden_message_still_works_for_others(self) -> None:
        # 隐藏只对隐藏的那个人生效
        hidden = await self._message()
        async with self.sessions() as db:
            db.add(MessageVisibility(message_id=hidden, user_id=self.ids["bob"]))
            await db.commit()
        self._heard("别人能转")
        response = await self._transcribe(hidden, "alice")
        self.assertEqual(response.json(), {"text": "别人能转", "cached": False})

    async def test_too_long_and_service_errors(self) -> None:
        # metadata 里记着 70 秒：不去识别就挡掉
        long_one = await self._message(metadata=voice_metadata(70))
        self.assertEqual(await self._code(long_one), (422, "voice_too_long"))
        self.assertEqual(self.calls, 0)
        # 没记时长、识别服务说太长
        unknown = await self._message(metadata=voice_metadata(None))
        self.heard.append(voice_asr.VoiceTooLong("audio_too_long"))
        self.assertEqual(await self._code(unknown), (422, "voice_too_long"))
        # 识别服务挂了：503，不存、不算次数，恢复后能转
        self.heard.append(voice_asr.VoiceAsrUnavailable("down"))
        self.assertEqual(await self._code(unknown), (503, "transcribe_unavailable"))
        self.assertEqual(await self._transcript_count(), 0)
        self._heard("好了")
        self.assertEqual((await self._transcribe(unknown)).json(), {"text": "好了", "cached": False})
        # 没配识别服务
        other = await self._message()
        with patch.object(settings, "voice_asr_url", None):
            self.assertEqual(await self._code(other), (503, "transcribe_unavailable"))
            # 转过的照样能读
            self.assertEqual((await self._transcribe(unknown)).json(), {"text": "好了", "cached": True})
        # 不是我们上传的录音
        external = await self._message(content="https://evil.example.com/a.webm")
        self.assertEqual(await self._code(external), (503, "transcribe_unavailable"))

    async def test_hourly_limit_counts_only_real_transcriptions(self) -> None:
        cached = await self._message()
        self._heard("早就转过")
        await self._transcribe(cached)
        async with self.sessions() as db:
            now = datetime.now(UTC)
            for _ in range(voice_transcripts.TRANSCRIBE_HOURLY_LIMIT - 1):
                extra = Message(conversation_id=self.group_id, sender_id=self.ids["alice"], type=MessageType.VOICE, content=VOICE_URL)
                db.add(extra)
                await db.flush()
                db.add(VoiceTranscript(message_id=extra.id, text="x", requested_by_id=self.ids["bob"], created_at=now))
            # 一小时以前的不算
            old = Message(conversation_id=self.group_id, sender_id=self.ids["alice"], type=MessageType.VOICE, content=VOICE_URL)
            db.add(old)
            await db.flush()
            db.add(VoiceTranscript(message_id=old.id, text="x", requested_by_id=self.ids["alice"], created_at=now - timedelta(hours=2)))
            await db.commit()
        fresh = await self._message()
        self.assertEqual(await self._code(fresh), (429, "transcribe_rate_limited"))
        # 命中缓存的不受限制；别人不受影响
        self.assertEqual((await self._transcribe(cached)).json(), {"text": "早就转过", "cached": True})
        self._heard("alice 转")
        self.assertEqual((await self._transcribe(fresh, "alice")).json(), {"text": "alice 转", "cached": False})

    def test_duration_from_metadata(self) -> None:
        self.assertEqual(voice_transcripts.voice_duration_seconds(voice_metadata(2.5)), 2.5)
        self.assertEqual(voice_transcripts.voice_duration_seconds(json.dumps({"duration_ms": 4000})), 4.0)
        for raw in (None, "", "[]", "坏的", json.dumps({"voice": {"duration_ms": True}}), json.dumps({"voice": {"duration_ms": -1}})):
            self.assertIsNone(voice_transcripts.voice_duration_seconds(raw))


class VoiceAsrTranscribeClientTests(unittest.TestCase):
    """voice_asr.transcribe 怎么解读识别服务的返回。"""

    def _http_error(self, code: int, body: str):
        import io
        from urllib.error import HTTPError

        return HTTPError("http://asr.test/asr/transcribe", code, "err", {}, io.BytesIO(body.encode()))

    def test_errors_map_to_too_long_or_unavailable(self) -> None:
        import asyncio

        with patch.object(settings, "voice_asr_url", "http://asr.test"):
            for code, body, expected in (
                (422, '{"detail":"audio_too_long"}', voice_asr.VoiceTooLong),
                (413, '{"detail":"audio_too_large"}', voice_asr.VoiceTooLong),
                (422, '{"detail":"bad_audio"}', voice_asr.VoiceAsrUnavailable),
                (500, "boom", voice_asr.VoiceAsrUnavailable),
            ):
                with patch.object(voice_asr, "urlopen", side_effect=self._http_error(code, body)):
                    with self.assertRaises(expected):
                        asyncio.run(voice_asr.transcribe(b"a", "a.webm", "audio/webm"))
        with patch.object(settings, "voice_asr_url", None):
            with self.assertRaises(voice_asr.VoiceAsrUnavailable):
                asyncio.run(voice_asr.transcribe(b"a", "a.webm", "audio/webm"))

    def test_successful_response(self) -> None:
        import asyncio
        import io

        class Response(io.BytesIO):
            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

        captured = {}

        def fake_urlopen(request, timeout):
            captured["url"], captured["headers"] = request.full_url, dict(request.header_items())
            return Response(json.dumps({"text": " 你好 ", "duration": 2.5, "language": "zh"}).encode())

        with patch.object(settings, "voice_asr_url", "http://asr.test/"), patch.object(settings, "content_moderation_token", "t"), patch.object(
            voice_asr, "urlopen", fake_urlopen
        ):
            result = asyncio.run(voice_asr.transcribe(b"a", "a.webm", "audio/webm"))
        self.assertEqual((result.text, result.duration), ("你好", 2.5))
        self.assertEqual(captured["url"], "http://asr.test/asr/transcribe")
        self.assertEqual(captured["headers"].get("X-moderation-token"), "t")


if __name__ == "__main__":
    unittest.main()
