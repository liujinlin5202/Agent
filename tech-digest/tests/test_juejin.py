# -*- coding: utf-8 -*-
"""掘金源单测：payload 解析、字段映射、points 回退、API 错误上抛（fixture，不联网）。"""
import unittest

from app.sources.juejin import parse_payload

FIXTURE = {
    "err_no": 0, "err_msg": "success",
    "data": [
        {"item_info": {"article_info": {
            "article_id": "7637856870833635343",
            "title": "Cursor 转 Codex 大半个月，聊聊我的真实感受",
            "brief_content": "AI 编程的震撼……",
            "hot_index": 420, "view_count": 3100}},
         "author_user_info": {"user_name": "someone"}},
        {"item_info": {"article_info": {"article_id": "", "title": "无 id 条目"}}},
        {"item_info": {}},
    ],
}


class TestParsePayload(unittest.TestCase):
    def test_fields(self):
        items = parse_payload(FIXTURE)
        self.assertEqual(len(items), 1)          # 无 id / 无 info 的条目丢弃
        it = items[0]
        self.assertEqual(it["title"], "Cursor 转 Codex 大半个月，聊聊我的真实感受")
        self.assertEqual(it["url"], "https://juejin.cn/post/7637856870833635343")
        self.assertEqual(it["source"], "juejin")
        self.assertEqual(it["points"], 420)      # hot_index 优先
        self.assertEqual(it["summary"], "AI 编程的震撼……")

    def test_points_falls_back_to_view_count(self):
        payload = {"err_no": 0, "data": [{"item_info": {"article_info": {
            "article_id": "1", "title": "t", "view_count": 99}}}]}
        self.assertEqual(parse_payload(payload)[0]["points"], 99)

    def test_api_error_raises(self):
        with self.assertRaises(RuntimeError):
            parse_payload({"err_no": 1, "err_msg": "fail"})


if __name__ == "__main__":
    unittest.main()
