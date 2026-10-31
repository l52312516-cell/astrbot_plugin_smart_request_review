import unittest
from unittest.mock import patch

from core.profiles import (
    clean_text,
    meaningful,
    merge_group,
    positive_int,
    profile_value,
)
from core import renderer


class ProfileNormalizationTests(unittest.TestCase):
    def test_placeholders_are_not_meaningful(self):
        for value in ("", "  ", "unknown", "NULL", "0", "\u200b未知\ufeff"):
            self.assertFalse(meaningful(value), value)
            self.assertEqual(clean_text(value), "", value)

    def test_alias_lookup_skips_placeholder(self):
        self.assertEqual(
            profile_value(
                {"qq_level": "unknown", "qqLevel": 18}, "qq_level", "qqLevel"
            ),
            18,
        )

    def test_clean_text_rejects_structured_values_and_protocol_noise(self):
        for value in (True, False, {}, [], {"nickname": "假的昵称"}, float("nan")):
            self.assertEqual(clean_text(value), "")
        self.assertEqual(clean_text("\x00[CQ:at,qq=123]真实内容\x08"), "真实内容")

    def test_numeric_values_are_integral_and_positive(self):
        self.assertEqual(positive_int("18"), 18)
        self.assertEqual(positive_int(18.0), 18)
        for value in (True, 0, "0", -1, 1.5, "1.5", "unknown"):
            self.assertIsNone(positive_int(value), value)

    def test_group_merge_ignores_placeholders_and_bad_numbers(self):
        info = {}
        merge_group(
            info,
            {
                "group_name": "unknown",
                "groupName": "真实群名",
                "member_count": "12.5",
                "memberNum": "128",
                "group_level": True,
                "groupLevel": 3,
                "group_description": "\u200b公开讨论\ufeff",
            },
        )
        self.assertEqual(
            info,
            {
                "name": "真实群名",
                "member_count": 128,
                "level": 3,
                "memo": "公开讨论",
                "description": "公开讨论",
            },
        )

    def test_group_description_does_not_hide_announcement(self):
        info = {}
        merge_group(info, {"group_description": "群简介"})
        self.assertEqual(info["memo"], "群简介")
        merge_group(info, {"group_memo": "群公告"})
        self.assertEqual(info["description"], "群简介")
        self.assertEqual(info["memo"], "群公告")

    def test_review_card_omits_placeholder_fields(self):
        record = {
            "kind": "friend",
            "subject_id": "123456",
            "profile": {
                "user_id": "123456",
                "nickname": "unknown",
                "level": 0,
                "sex": "male",
                "area": " 北京 ",
                "signature": "\u200bnull",
                "comment": "",
            },
            "hard_rule": {"reason": "未命中硬规则"},
            "items": [],
            "score": 0,
            "threshold": 5,
        }
        captured = []
        original = renderer.lines_for

        def observe(value, *args, **kwargs):
            captured.append(str(value))
            return original(value, *args, **kwargs)

        with patch.object(renderer, "lines_for", side_effect=observe):
            renderer.ReviewCardRenderer().render(record)
        text = "\n".join(captured)
        self.assertIn("性别 / 地区：男 / 北京", text)
        self.assertNotIn("昵称：", text)
        self.assertNotIn("QQ等级：", text)
        self.assertNotIn("签名：", text)
        self.assertNotIn("验证信息：", text)

    def test_list_card_does_not_render_placeholder_name(self):
        with patch.object(renderer, "lines_for", wraps=renderer.lines_for) as wrapped:
            renderer.ReviewCardRenderer().render_list(
                "好友列表", [{"user_id": "123456", "nickname": "unknown"}]
            )
        wrapped.assert_not_called()

    def test_wrapping_compacts_empty_lines(self):
        self.assertEqual(
            renderer.lines_for("第一行\n\n\n第二行", 900), ["第一行", "第二行"]
        )

    def test_disabled_scores_have_no_empty_addition(self):
        record = {
            "kind": "group",
            "group": {"group_id": "123456"},
            "items": [],
            "score_evidence": {"enabled_max": 0, "evaluated_max": 0, "coverage": 0},
        }
        with patch.object(renderer, "lines_for", wraps=renderer.lines_for) as wrapped:
            renderer.ReviewCardRenderer().render(record)
        texts = [str(call.args[0]) for call in wrapped.call_args_list]
        self.assertIn("无启用评分项", texts)
        self.assertFalse(any(text.startswith(" =") for text in texts))

    def test_scoring_evidence_coverage_is_not_presented_as_confidence(self):
        record = {
            "kind": "group",
            "group": {"group_id": "123456"},
            "items": [
                {"name": "群资料特征", "max": 3, "score": 2, "state": "scored"},
                {"name": "群人数", "max": 2, "score": 0, "state": "unknown"},
            ],
            "score_evidence": {"enabled_max": 5, "evaluated_max": 3, "coverage": 0.6},
        }
        with patch.object(renderer, "lines_for", wraps=renderer.lines_for) as wrapped:
            renderer.ReviewCardRenderer().render(record)
        self.assertIn(
            "有效评分依据 3 / 5（60%）",
            [str(call.args[0]) for call in wrapped.call_args_list],
        )


if __name__ == "__main__":
    unittest.main()
