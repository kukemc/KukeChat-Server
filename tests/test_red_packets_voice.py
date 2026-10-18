"""语音红包：说出口令才能领，说对了以领取人的身份把这段语音发进会话（docs/red-packets.md）。"""

from __future__ import annotations

import json
import unittest
from unittest.mock import AsyncMock, patch

from sqlalchemy import select

from app.core.config import settings
from app.models.enums import MessageType
from app.models.message import Message
from app.models.red_packet import RedPacketVoiceAttempt
from app.services import red_packets as rp
from app.services import voice_asr
from app.services.storage import StoredObject

from test_red_packets import OID_A, RedPacketTestCase

VOICE_URL = "/uploads/messages/0123456789abcdef0123456789abcdef.webm"
METADATA = {"voice": {"duration_ms": 2300, "content_type": "audio/webm", "size": 12345, "waveform": [1, 5, 9]}, "duration_ms": 2300}


class VoiceRedPacketTests(RedPacketTestCase):
    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        self.heard: list[voice_asr.VoiceMatch | Exception] = []
        self.expected: list[str] = []

        async def fake_match(audio: bytes, filename: str, content_type: str, expected: str) -> voice_asr.VoiceMatch:
            self.expected.append(expected)
            result = self.heard.pop(0)
            if isinstance(result, Exception):
                raise result
            return result

        for patcher in (
            patch.object(settings, "voice_asr_url", "http://asr.test"),
            patch.object(voice_asr, "match", fake_match),
            patch("app.services.storage.get_object", lambda key, max_bytes=None: StoredObject(data=b"audio", content_type="audio/webm")),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    def _heard(self, text: str, distance: int, length: int = 4, duration: float = 2.3) -> None:
        self.heard.append(voice_asr.VoiceMatch(text=text, distance=distance, length=length, duration=duration))

    async def _claim_voice(self, name: str, packet_id: int, url: str | None = VOICE_URL):
        return await self._as(name, rp.claim, packet_id, url, METADATA)

    async def test_config_and_sending(self) -> None:
        self.ledger.donate(OID_A, 300)
        self.assertIn("voice", (await self._as("alice", rp.config_view))["features"])
        sent = await self._send(amount=30, count=3, greeting="开工", voice_text="  恭喜发财  ")
        packet, message = sent["red_packet"], sent["message"]
        self.assertEqual((packet["voice"], packet["voice_text"], packet["chain_text"]), (True, "恭喜发财", None))
        self.assertEqual(json.loads(message.metadata_json)["red_packet"]["voice_text"], "恭喜发财")
        self.assertEqual(message.content, "[语音红包] 开工（更新到新版才能领取）")
        self.assertEqual(await self._code(self._send(voice_text="a", chain_text="b")), (422, "voice_text_rejected"))
        self.assertEqual(await self._code(self._send(voice_text="长" * 31)), (422, "voice_text_rejected"))
        # 没有识别服务：不开放
        with patch.object(settings, "voice_asr_url", None):
            self.assertNotIn("voice", (await self._as("alice", rp.config_view))["features"])
            self.assertEqual(await self._code(self._send(voice_text="恭喜发财")), (422, "voice_text_rejected"))
        await self._assert_conserved()

    async def test_saying_the_phrase_claims_and_posts_the_voice(self) -> None:
        self.ledger.donate(OID_A, 300)
        packet_id = await self._send_id(amount=30, count=3, voice_text="恭喜发财")
        # 没带录音、录音不是我们上传的
        self.assertEqual(await self._code(self._as("bob", rp.claim, packet_id)), (422, "voice_required"))
        self.assertEqual(await self._code(self._claim_voice("bob", packet_id, "https://evil.example.com/a.webm")), (422, "voice_required"))
        # 没说对：记一次，告诉他听到的是什么、还剩几次，钱没动、消息也没发
        self._heard("你好", 4)
        self._heard("恭喜发", 1, length=4, duration=2.0)
        before = await self._count(Message)
        status, code, detail = await self._fails(self._claim_voice("bob", packet_id))
        self.assertEqual((status, code, detail["recognized"], detail["attempts_left"]), (422, "voice_mismatch", "你好", 4))
        self.assertEqual(await self._count(Message), before)
        self.assertEqual((await self._packet(packet_id)).claimed_count, 0)
        # 错一个音节（4 个音节的口令允许错 1 个）：领到了，那段语音以 bob 的身份发进群里
        result, voice = await self._claim_voice("bob", packet_id)
        self.assertEqual(self.expected, ["恭喜发财", "恭喜发财"])
        self.assertGreater(result["amount"], 0)
        self.assertEqual((voice.sender_id, voice.type, voice.content), (self.ids["bob"], MessageType.VOICE, VOICE_URL))
        self.assertEqual(
            json.loads(voice.metadata_json),
            {"voice": {"duration_ms": 2300, "content_type": "audio/webm", "size": 12345, "waveform": [1, 5, 9]}, "red_packet_voice": {"red_packet_id": packet_id}},
        )
        # 领过了就不再识别
        self.assertEqual(await self._code(self._claim_voice("bob", packet_id)), (409, "already_claimed"))
        await self._assert_conserved()

    async def test_attempts_are_limited_and_service_errors_do_not_count(self) -> None:
        self.ledger.donate(OID_A, 300)
        packet_id = await self._send_id(amount=10, count=1, voice_text="恭喜发财")
        self.heard.append(voice_asr.VoiceAsrUnavailable("down"))
        self.assertEqual(await self._code(self._claim_voice("bob", packet_id)), (503, "voice_unavailable"))
        self._heard("恭喜发财", 0, duration=20)
        self.assertEqual(await self._code(self._claim_voice("bob", packet_id)), (422, "voice_too_long"))
        for _ in range(rp.VOICE_ATTEMPTS_MAX):
            self._heard("不对", 4)
            self.assertEqual(await self._code(self._claim_voice("bob", packet_id)), (422, "voice_mismatch"))
        self.assertEqual(await self._code(self._claim_voice("bob", packet_id)), (429, "voice_attempts_exhausted"))
        async with self.sessions() as db:
            row = (await db.execute(select(RedPacketVoiceAttempt))).scalar_one()
            self.assertEqual((row.user_id, row.attempts), (self.ids["bob"], rp.VOICE_ATTEMPTS_MAX))
        # 别人不受影响
        self._heard("恭喜发财", 0)
        self.assertGreater((await self._claim_voice("carol", packet_id))[0]["amount"], 0)
        await self._assert_conserved()

    def test_allowed_errors_grow_with_the_phrase(self) -> None:
        self.assertEqual([voice_asr.allowed_errors(n) for n in (1, 3, 4, 7, 8, 12, 20)], [0, 0, 1, 1, 2, 2, 4])
        self.assertFalse(voice_asr.VoiceMatch("", 0, 0, 1.0).matched)


if __name__ == "__main__":
    unittest.main()
