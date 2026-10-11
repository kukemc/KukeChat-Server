import unittest

from app.models.enums import ModerationSource
from app.models.report import ContentModerationRecord
from app.services.moderation import (
    _ensure_moderation_response_usable,
    _local_text_moderation_result,
    _needs_action,
    _normalized_text_variant,
    _verdict_for_record,
    _violation_source,
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


class ActionPolicyTests(unittest.TestCase):
    def test_only_block_is_actionable_by_default(self) -> None:
        self.assertFalse(_needs_action(_record("image", "review", 1)))
        self.assertFalse(_needs_action(_record("text", "review", 1)))
        self.assertFalse(_needs_action(_record("text", "allow", 0)))
        self.assertTrue(_needs_action(_record("image", "block", 2)))
        self.assertTrue(_needs_action(_record("text", "block", 1)))

    def test_worst_record_prefers_actionable_over_review(self) -> None:
        image_review = _record("image", "review", 1)
        text_block = _record("text", "block", 1)
        self.assertIs(_worst_record([image_review, text_block]), text_block)
        self.assertFalse(_needs_action(_worst_record([_record("text", "allow", 0), image_review])))


class VerdictMappingTests(unittest.TestCase):
    def test_review_and_allow_are_never_violations(self) -> None:
        self.assertIsNone(_verdict_for_record(_record("text", "review", 1, '["fraud"]')))
        self.assertIsNone(_verdict_for_record(_record("image", "review", 1)))
        self.assertIsNone(_verdict_for_record(_record("text", "allow", 0)))

    def test_image_block_is_serious_but_not_red_line(self) -> None:
        self.assertEqual(_verdict_for_record(_record("image", "block", 2)), ("sexual", 3))
        self.assertEqual(_verdict_for_record(_record("image", "block", 2, '["minor_sexual"]')), ("minor_sexual", 4))
        # 外链图片送审不了：撤回但不记违规
        self.assertEqual(_verdict_for_record(_record("image", "block", 1)), ("unverified_media", 0))

    def test_legacy_text_categories(self) -> None:
        self.assertEqual(_verdict_for_record(_record("text", "block", 2, '["fraud", "illegal"]')), ("fraud", 3))
        self.assertEqual(_verdict_for_record(_record("text", "block", 2, '["drug", "illegal"]')), ("drugs", 3))
        self.assertEqual(_verdict_for_record(_record("text", "block", 2, '["political_sensitive"]')), ("political", 2))
        self.assertEqual(_verdict_for_record(_record("text", "block", 1, '["harassment"]')), ("profanity", 1))
        self.assertEqual(_verdict_for_record(_record("text", "block", 2, '["sexual", "spam"]')), ("sexual", 3))

    def test_low_severity_block_is_downgraded(self) -> None:
        self.assertEqual(_verdict_for_record(_record("text", "block", 1, '["sexual"]')), ("vulgar", 2))
        self.assertEqual(_verdict_for_record(_record("text", "block", 1, '["privacy"]')), ("privacy", 2))
        self.assertEqual(_verdict_for_record(_record("text", "block", 1)), ("other", 1))

    def test_new_service_verdict_is_used_verbatim(self) -> None:
        record = _record("image", "block", 2)
        record.raw_response = '{"category": "suggestive", "level": 1, "model": "v2"}'
        self.assertEqual(_verdict_for_record(record), ("suggestive", 1))
        record.raw_response = '{"category": "not-a-category", "level": 4}'
        self.assertEqual(_verdict_for_record(record), ("sexual", 3))

    def test_verdict_is_read_from_unified_endpoint_envelope(self) -> None:
        record = _record("image", "block", 2)
        record.raw_response = '{"decision": "block", "results": [{"type": "image", "category": "suggestive", "level": 1}]}'
        self.assertEqual(_verdict_for_record(record), ("suggestive", 1))

    def test_source_reflects_who_judged(self) -> None:
        rule = _record("text", "block", 2)
        rule.raw_response = '{"model": "rules-first"}'
        ai = _record("text", "block", 2)
        ai.raw_response = '{"model": "Gandi-AIHub"}'
        self.assertEqual(_violation_source(rule), "auto_rule")
        self.assertEqual(_violation_source(ai), "auto_ai")
        self.assertEqual(_violation_source(_record("image", "block", 2)), "auto_image")


if __name__ == "__main__":
    unittest.main()
