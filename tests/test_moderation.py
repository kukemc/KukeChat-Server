import unittest

from app.services.moderation import _ensure_moderation_response_usable, _local_text_moderation_result, _normalized_text_variant


class ModerationGuardTests(unittest.TestCase):
    def test_rejects_upstream_error_disguised_as_allow(self) -> None:
        response = {
            "decision": "allow",
            "severity": 0,
            "raw_model_output": '{"body":null,"code":"0001","msg":"message too long","status":1}',
        }

        with self.assertRaisesRegex(RuntimeError, "message too long"):
            _ensure_moderation_response_usable(response)

    def test_accepts_successful_upstream_response(self) -> None:
        _ensure_moderation_response_usable({"decision": "allow", "severity": 0, "raw_model_output": '{"code":"0000","status":0}'})

    def test_normalizes_common_obfuscation(self) -> None:
        self.assertEqual(_normalized_text_variant("我\u200b 操 他 妈 的"), "我操他妈的")
        self.assertEqual(_normalized_text_variant("Ａ Ｂ Ｃ"), "ABC")

    def test_blocks_screenshot_style_abuse_after_normalization(self) -> None:
        normalized = _normalized_text_variant("我\u200b 操 他 妈 的，这首歌怎么还不火")
        for _ in range(2):
            result = _local_text_moderation_result(normalized)
            self.assertIsNotNone(result)
            self.assertEqual(result["decision"], "block")

    def test_does_not_block_benign_text(self) -> None:
        self.assertIsNone(_local_text_moderation_result(_normalized_text_variant("今天一起做平台射击游戏吗")))


if __name__ == "__main__":
    unittest.main()
