"""委员看到的红包证据：接龙 / 语音口令要看得见，领取时自动发的那句话要标明出处。"""

from __future__ import annotations

import unittest

from app.services.jury import _anonymize_messages


def _message(**fields):
    return {"sender_id": 1, "created_at": "2026-10-26T11:47:00+00:00", "type": "text", "content": "", "metadata": None, **fields}


class RedPacketEvidenceTest(unittest.TestCase):
    def test_chain_text_visible(self) -> None:
        snapshot = {
            "messages": [
                _message(
                    is_target=True,
                    type="red_packet",
                    content="[接龙红包] 里面真没有骂人（更新到新版才能领取）",
                    metadata={"red_packet": {"id": 9, "kind": "lucky", "greeting": "里面真没有骂人", "count": 3, "chain_text": "骂人的话"}},
                ),
                _message(sender_id=2, content="骂人的话", metadata={"red_packet_chain": {"red_packet_id": 9}}),
            ]
        }
        target, chain = _anonymize_messages(snapshot)
        self.assertEqual(target["content"], "[接龙红包] 里面真没有骂人；接龙口令：骂人的话")
        self.assertEqual(chain["content"], "骂人的话（领取接龙红包时自动发出，口令是发红包的人定的）")

    def test_exclusive_voice_packet(self) -> None:
        packet = {"greeting": "开工", "voice_text": "芝麻开门", "target_user_id": 5}
        [item] = _anonymize_messages({"messages": [_message(is_target=True, type="red_packet", content="x", metadata={"red_packet": packet})]})
        self.assertEqual(item["content"], "[专属语音红包] 开工；语音口令：芝麻开门")

    def test_plain_text_untouched(self) -> None:
        [item] = _anonymize_messages({"messages": [_message(is_target=True, content="你好")]})
        self.assertEqual(item["content"], "你好")


if __name__ == "__main__":
    unittest.main()
