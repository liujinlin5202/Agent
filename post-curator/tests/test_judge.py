# -*- coding: utf-8 -*-
"""评审单测：LLM 输出解析（稳健 JSON 抠取）+ 确定性加精门槛。"""
import unittest

from app.judge import build_user_prompt, gate, parse_verdict


class TestParseVerdict(unittest.TestCase):
    def test_clean_json(self):
        v = parse_verdict('{"post_type": "问答", "flame_bait": false, '
                          '"content_value": 4, "comment_value": 8, "reason": "评论区有解答"}')
        self.assertEqual(v["comment_value"], 8)

    def test_codefence(self):
        v = parse_verdict('```json\n{"post_type": "经验分享", "flame_bait": false, '
                          '"content_value": 9, "comment_value": 2, "reason": "干货"}\n```')
        self.assertEqual(v["content_value"], 9)

    def test_noisy_prefix(self):
        v = parse_verdict('好的，评审如下：{"post_type": "引战", "flame_bait": true, '
                          '"content_value": 2, "comment_value": 7, "reason": "互撕"} 完毕')
        self.assertEqual(v["flame_bait"], True)

    def test_invalid(self):
        self.assertIsNone(parse_verdict("完全不是 JSON"))
        self.assertIsNone(parse_verdict(None))
        self.assertIsNone(parse_verdict('{"截断的'))

    def test_non_dict(self):
        self.assertIsNone(parse_verdict('[1,2,3]'))


class TestGate(unittest.TestCase):
    def base(self, **kw):
        d = {"post_type": "问答", "flame_bait": False,
             "content_value": 5, "comment_value": 5, "reason": "r"}
        d.update(kw)
        return d

    def test_below_threshold(self):
        featured, reason, n = gate(self.base(), 7.0, 7.0)
        self.assertFalse(featured)
        self.assertIn("未达线", reason)

    def test_comment_value_passes(self):
        featured, _, n = gate(self.base(comment_value=8), 7.0, 7.0)
        self.assertTrue(featured)
        self.assertEqual(n["post_type"], "问答")

    def test_content_value_passes(self):
        featured, _, _ = gate(self.base(content_value=9), 7.0, 7.0)
        self.assertTrue(featured)

    def test_flame_veto_beats_score(self):
        featured, reason, n = gate(
            self.base(comment_value=10, flame_bait=True), 7.0, 7.0)
        self.assertFalse(featured)
        self.assertIn("一票否决", reason)
        self.assertTrue(n["flame_bait"])

    def test_flame_string_true(self):
        featured, _, _ = gate(self.base(flame_bait="true"), 7.0, 7.0)
        self.assertFalse(featured)

    def test_unparseable_verdict(self):
        featured, reason, n = gate(None, 7.0, 7.0)
        self.assertFalse(featured)
        self.assertIsNone(n["content_value"])

    def test_score_clamped(self):
        _, _, n = gate(self.base(content_value=99, comment_value=-3), 7.0, 7.0)
        self.assertEqual(n["content_value"], 10)
        self.assertEqual(n["comment_value"], 0)

    def test_score_missing(self):
        featured, reason, _ = gate(self.base(content_value=None), 7.0, 7.0)
        self.assertFalse(featured)
        self.assertIn("评分缺失", reason)


class TestPrompt(unittest.TestCase):
    def test_contains_post_and_comments(self):
        post = {"title": "求助", "partition": "学习交流", "tag": "Python",
                "post_time": "2026-10-14 10:00:00", "browse_num": 100,
                "like_num": 3, "dislike_num": 0, "comment_num": 2, "heat": 12.5,
                "ptext": "怎么配置 venv？" * 500}
        comments = [{"text": "用 python -m venv", "like_num": 5},
                    {"text": "x" * 1000, "like_num": 1}]
        s = build_user_prompt(post, comments, post_char_cap=100, comment_char_cap=50)
        self.assertIn("求助", s)
        self.assertIn("学习交流", s)
        self.assertIn("用 python -m venv", s)
        # 截断生效：正文与评论都不超上限
        self.assertLess(len(s), 300 + 7 * 60)
        self.assertNotIn("x" * 60, s)

    def test_no_comments(self):
        post = {"title": "t", "partition": "", "tag": "", "post_time": "",
                "browse_num": 0, "like_num": 0, "dislike_num": 0,
                "comment_num": 0, "heat": 0, "ptext": ""}
        s = build_user_prompt(post, [], 100, 50)
        self.assertIn("暂无评论", s)


if __name__ == "__main__":
    unittest.main()
