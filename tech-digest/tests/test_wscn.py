# -*- coding: utf-8 -*-
"""华尔街见闻源单测：resource 解包、live/付费/缺标题过滤、字段映射、抓取上限。"""
import unittest
from unittest.mock import patch

from app.sources import wscn


def _wrap(res_type="article", **kw):
    res = {"id": kw.get("id", 3781850), "title": kw.get("title", "深度长文标题"),
           "content_short": kw.get("short", "摘要内容"), "display_time": 1789979358,
           "is_paid": kw.get("paid", False), "is_priced": kw.get("priced", False),
           "author": kw.get("author", {"display_name": "刘胜与"})}
    return {"resource_type": res_type, "resource": res}


PAYLOAD = {"code": "20000", "data": {"items": [
    _wrap(),
    _wrap(res_type="live", title="快讯直播"),
    _wrap(title="付费专栏", paid=True),
    _wrap(title="标价文章", priced=True),
    _wrap(id=2, title="", author={"display_name": "x"}),      # 缺标题 → 丢
    _wrap(id=3, title="第二篇", author="字符串作者"),
]}}


class _FakeResp:
    def __init__(self, payload):
        self._payload = payload

    def json(self):
        return self._payload


class TestParseFlow(unittest.TestCase):
    def test_filter_and_fields(self):
        items = wscn.parse_flow(PAYLOAD)
        self.assertEqual(len(items), 2)                       # live/付费/标价/缺题全滤掉
        it = items[0]
        self.assertEqual(it["source"], "wallstreetcn")
        self.assertEqual(it["url"], "https://wallstreetcn.com/articles/3781850")
        self.assertEqual(it["author"], "刘胜与")
        self.assertEqual(it["summary"], "摘要内容")
        self.assertIsNotNone(it["published_at"])              # epoch → 本地 ISO

    def test_author_plain_string(self):
        items = wscn.parse_flow(PAYLOAD)
        self.assertEqual(items[1]["author"], "字符串作者")


class TestFetch(unittest.TestCase):
    def test_caps_to_max_items(self):
        big = {"code": "20000", "data": {"items": [
            _wrap(id=i, title=f"t{i}") for i in range(30)]}}
        with patch.object(wscn, "http_get", return_value=_FakeResp(big)):
            items, label, detail = wscn.fetch()
        self.assertEqual(label, "wallstreetcn")
        self.assertEqual(len(items), wscn.MAX_ITEMS)
        self.assertEqual(detail, {"keep": wscn.MAX_ITEMS})

    def test_empty_raises(self):
        with patch.object(wscn, "http_get",
                          return_value=_FakeResp({"code": "20000", "data": {"items": []}})):
            with self.assertRaises(RuntimeError):
                wscn.fetch()


if __name__ == "__main__":
    unittest.main()
