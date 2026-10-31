import json
import unittest

from core.scoring import (
    model_result,
    possible_total,
    prompt_rubric,
    scoring_evidence,
)


class ModelResultTests(unittest.TestCase):
    def response(self, **changes):
        return json.dumps(
            {
                "score": 1,
                "hit": True,
                "reason": "明确的动漫角色",
                "tags": ["动漫"],
                **changes,
            },
            ensure_ascii=False,
        )

    def test_legacy_four_fields_and_clamp_remain_supported(self):
        self.assertEqual(model_result(self.response(score=99), 2)["score"], 2)
        result = model_result(
            self.response(score=0, hit=False, reason="没有目标证据"), 2
        )
        self.assertEqual(result["state"], "scored")
        self.assertEqual(result["score"], 0)
        self.assertEqual(
            model_result("```json\n" + self.response() + "\n```", 2)["score"], 1
        )

    def test_empty_or_placeholder_reason_is_unknown(self):
        for reason in ("", " \n\t", "unknown", "未知", "\ufeff\u200b"):
            with self.subTest(reason=reason):
                self.assertEqual(
                    model_result(self.response(reason=reason), 2)["state"],
                    "unknown",
                )

    def test_hit_and_score_must_agree(self):
        for score, hit in ((1, False), (0, True), (-1, False), (-1, True)):
            with self.subTest(score=score, hit=hit):
                result = model_result(self.response(score=score, hit=hit), 2)
                self.assertEqual(result["state"], "unknown")
                self.assertEqual(result["score"], 0)

    def test_unknown_response_preserves_explanation(self):
        result = model_result(
            self.response(score=0, hit=False, unknown=True, reason="  头像无法辨认  "),
            2,
        )
        self.assertEqual(
            result,
            {"score": 0, "state": "unknown", "reason": "头像无法辨认", "tags": []},
        )
        self.assertEqual(
            model_result(self.response(unknown=True), 2)["state"], "unknown"
        )
        self.assertEqual(
            model_result(self.response(unknown="true"), 2)["state"], "unknown"
        )

    def test_tags_only_keep_unique_nonempty_strings(self):
        result = model_result(
            self.response(
                tags=[None, 1, {}, "", " ", "unknown", "动漫", " 动漫 ", "角色"]
            ),
            2,
        )
        self.assertEqual(result["tags"], ["动漫", "角色"])
        result = model_result(self.response(tags=[str(i) for i in range(1, 10)]), 2)
        self.assertEqual(len(result["tags"]), 6)

    def test_duplicate_keys_nonfinite_and_extreme_json_fail_safely(self):
        invalid = (
            '{"score":0,"score":2,"hit":true,"reason":"x","tags":[]}',
            '{"score":NaN,"hit":false,"reason":"x","tags":[]}',
            '{"score":Infinity,"hit":true,"reason":"x","tags":[]}',
            '{"score":1e999,"hit":true,"reason":"x","tags":[]}',
            self.response(score=2**53),
            self.response(score=True),
            self.response(score=1.5),
            '{"score":' + "9" * 5000 + ',"hit":true,"reason":"x","tags":[]}',
            "[" * 1100 + "]" * 1100,
            self.response(reason="x" * 70000),
        )
        for text in invalid:
            with self.subTest(text=text[:60]):
                self.assertEqual(model_result(text, 2)["state"], "unknown")

    def test_reason_and_tags_are_bounded(self):
        result = model_result(self.response(reason="证据" * 500, tags=["角色" * 50]), 2)
        self.assertEqual(len(result["reason"]), 240)
        self.assertEqual(len(result["tags"][0]), 30)


class ScoringEvidenceTests(unittest.TestCase):
    def test_weighted_coverage_preserves_uncapped_maxima(self):
        record = {
            "items": [
                {"max": 9, "state": "scored"},
                {"max": 4, "state": "unknown"},
                {"max": 7, "state": "disabled"},
                {"max": 2, "state": "skipped"},
                {"max": 0, "state": "unknown"},
            ]
        }
        self.assertEqual(
            scoring_evidence(record),
            {"enabled_max": 15, "evaluated_max": 9, "unknown_max": 4, "coverage": 0.6},
        )
        self.assertEqual(possible_total(record), 10)

    def test_disabled_and_legacy_totals_are_distinct(self):
        record = {"items": [{"max": 3, "state": "disabled"}]}
        self.assertEqual(possible_total(record), 0)
        self.assertEqual(scoring_evidence(record)["coverage"], 0.0)
        self.assertEqual(possible_total({}), 10)
        self.assertEqual(possible_total({"items": []}), 10)

    def test_one_point_rubric_is_binary_and_requires_evidence(self):
        text = prompt_rubric("friend_nickname", 1)
        self.assertIn("只有 0 或 1 分", text)
        self.assertIn("不允许部分分", text)
        self.assertIn("不根据昵称、头像等推断真实身份", text)
        self.assertIn("unknown:true", text)
        self.assertNotIn("1～", text)

    def test_multi_point_rubric_has_shared_anchors(self):
        text = prompt_rubric("group_profile", 3)
        self.assertIn("0～3 分", text)
        self.assertIn("1～2 分", text)
        self.assertIn("3 分要求有清晰、具体", text)
        self.assertIn("群名、群备注、群简介", text)


if __name__ == "__main__":
    unittest.main()
