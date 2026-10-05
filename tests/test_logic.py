import json
import tempfile
import unittest
from pathlib import Path

from core.logic import (
    clamp_score,
    contains_any,
    parse_json_object,
    score_level,
    score_range,
)
from core.storage import JsonStore


class LogicTests(unittest.TestCase):
    def test_clamp_and_json(self):
        self.assertEqual(clamp_score(9, 2), 2)
        self.assertEqual(clamp_score(-1, 2), 0)
        self.assertEqual(parse_json_object("```json\n{\"score\": 2}\n```"), {"score": 2})
        self.assertIsNone(parse_json_object("not json"))

    def test_keywords_and_score_boundaries(self):
        self.assertEqual(contains_any("来自动漫交流群", ["动漫", "广告"]), ["动漫"])
        self.assertEqual(score_level(15, 15, 30, 1, 2), 1)
        self.assertEqual(score_level(30, 15, 30, 1, 2), 2)
        self.assertEqual(score_range(100, 1, 200, 2), 2)
        self.assertEqual(score_range(0, 1, 200, 2), 0)

    def test_atomic_store(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "state.json"
            store = JsonStore(path, {})
            store.save({"ok": True})
            self.assertEqual(store.load(), {"ok": True})
            self.assertFalse(path.with_name("state.json.tmp").exists())


if __name__ == "__main__":
    unittest.main()

