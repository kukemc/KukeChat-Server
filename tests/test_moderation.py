import unittest

from app.models.enums import ModerationAction, ModerationSource
from app.models.report import ContentModerationRecord
from app.services.moderation import (
    _ensure_moderation_response_usable,
    _local_text_moderation_result,
    _needs_action,
    _normalized_text_variant,
    _punishment_for_record,
    _worst_record,
)


def _record(content_type: str, decision: str, severity: int, categories: str = "[]") -> ContentModerationRecord:
    return ContentModerationRecord(
        source=ModerationSource.MESSAGE_AUTO,
        content_type=content_type,
        decision=decision,
        severity=severity,
        categories=categories,
    )


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


class ImageActionPolicyTests(unittest.TestCase):
    def test_image_review_is_not_actionable(self) -> None:
        self.assertFalse(_needs_action(_record("image", "review", 1)))
        self.assertTrue(_needs_action(_record("image", "block", 2)))
        self.assertTrue(_needs_action(_record("text", "review", 1)))
        self.assertFalse(_needs_action(_record("text", "allow", 0)))

    def test_image_block_never_bans_permanently(self) -> None:
        self.assertEqual(_punishment_for_record(_record("image", "block", 2)), ModerationAction.USER_BANNED)
        self.assertEqual(_punishment_for_record(_record("image", "block", 1)), ModerationAction.RECALLED)

    def test_worst_record_prefers_actionable_over_image_review(self) -> None:
        image_review = _record("image", "review", 1)
        text_review = _record("text", "review", 1)
        self.assertIs(_worst_record([image_review, text_review]), text_review)
        self.assertIs(_worst_record([_record("text", "allow", 0), image_review]), image_review)
        self.assertFalse(_needs_action(_worst_record([_record("text", "allow", 0), image_review])))


if __name__ == "__main__":
    unittest.main()
