# -*- coding: utf-8 -*-
"""集市 MySQL 只读查询单测（app/marketdb.py）。

2026-09-13 新增。背景：集市后端发帖成功时返回 data=nil，拿不到 postID
（2026-08-30 实测），导致「1 楼补楼」和「表现追踪」两件事都没有目标可指。
部署机与集市 MySQL 同机（容器 sse_market_db，库 market），
于是直接按「userID + 标题 + 时间窗」把自己刚发的帖子捞回来。

只读是硬约束：本模块只跑 SELECT。写操作（发帖/评论）一律走 publisher 的 API，
不绕过业务逻辑改库。
"""
import unittest
from datetime import datetime

from app import marketdb
from app.marketdb import _lit, _split_rows, stats_sql, find_post_id_sql


class TestSplitRows(unittest.TestCase):
    """mysql -N -B 输出：制表符分隔、无表头、行尾 \\n。"""

    def test_parses_tsv(self):
        self.assertEqual(_split_rows("1\ta/b\t12\n2\tc/d\t0\n"),
                         [["1", "a/b", "12"], ["2", "c/d", "0"]])

    def test_empty_output(self):
        self.assertEqual(_split_rows(""), [])
        self.assertEqual(_split_rows("\n"), [])
        self.assertEqual(_split_rows(None), [])

    def test_preserves_inner_spaces_and_empty_fields(self):
        self.assertEqual(_split_rows("6131\t9/13 Nvidia 是 AI 的中央银行\t0\n"),
                         [["6131", "9/13 Nvidia 是 AI 的中央银行", "0"]])
        self.assertEqual(_split_rows("1\t\t2\n"), [["1", "", "2"]])

    def test_blank_lines_dropped(self):
        self.assertEqual(_split_rows("1\ta\n\n2\tb\n"), [["1", "a"], ["2", "b"]])


class TestLit(unittest.TestCase):
    """MySQL 字符串字面量转义——标题里出现单引号/反斜杠不能把 SQL 拼坏。"""

    def test_plain(self):
        self.assertEqual(_lit("9/13 Nvidia"), "'9/13 Nvidia'")

    def test_escapes_quote_and_backslash(self):
        self.assertEqual(_lit("it's a\\b"), "'it\\'s a\\\\b'")


class TestFindPostIdSql(unittest.TestCase):
    def test_shape(self):
        sql = find_post_id_sql(4321, "9/13 Nvidia 是 AI 的中央银行",
                               datetime(2026, 9, 13, 9, 16, 0))
        self.assertIn("userID=4321", sql)
        self.assertIn("'9/13 Nvidia 是 AI 的中央银行'", sql)
        self.assertIn("post_time>='2026-09-13 09:16:00'", sql)
        self.assertIn("ORDER BY postID DESC", sql)
        self.assertIn("LIMIT 1", sql)

    def test_title_is_escaped(self):
        sql = find_post_id_sql(4321, "it's", datetime(2026, 9, 13, 9, 0, 0))
        self.assertIn("'it\\'s'", sql)


class TestStatsSql(unittest.TestCase):
    def test_shape(self):
        sql = stats_sql([6131, 6127])
        self.assertIn("postID IN (6131,6127)", sql)
        self.assertIn("browse_num", sql)
        self.assertIn("like_num", sql)
        self.assertIn("comment_num", sql)

    def test_empty_ids_returns_empty(self):
        self.assertEqual(stats_sql([]), "")


class _Fake:
    """记录 SQL 并按序返回预置结果，替代 subprocess。"""

    def __init__(self, *results):
        self.results = list(results)
        self.calls: list[str] = []

    def __call__(self, sql: str):
        self.calls.append(sql)
        return self.results.pop(0) if self.results else []


class TestFindPostId(unittest.TestCase):
    def test_returns_int(self):
        r = _Fake([["6131"]])
        self.assertEqual(marketdb.find_post_id(4321, "标题", datetime(2026, 9, 13, 9, 0), runner=r),
                         6131)
        self.assertEqual(len(r.calls), 1)

    def test_not_found_returns_none(self):
        self.assertIsNone(
            marketdb.find_post_id(4321, "标题", datetime(2026, 9, 13, 9, 0), runner=_Fake([])))

    def test_unavailable_returns_none(self):
        """docker/容器不可用（本地开发机）→ None，调用方据此降级，不能抛。"""
        self.assertIsNone(
            marketdb.find_post_id(4321, "标题", datetime(2026, 9, 13, 9, 0), runner=_Fake(None)))

    def test_non_numeric_returns_none(self):
        self.assertIsNone(
            marketdb.find_post_id(4321, "标题", datetime(2026, 9, 13, 9, 0),
                                  runner=_Fake([["abc"]])))


class TestPostStats(unittest.TestCase):
    def test_parses_rows(self):
        r = _Fake([["6131", "277", "1", "2", "3.5"], ["6127", "35", "0", "0", "0"]])
        got = marketdb.post_stats([6131, 6127], runner=r)
        self.assertEqual(got[6131], {"browse": 277, "likes": 1, "comments": 2, "heat": 3.5})
        self.assertEqual(got[6127]["browse"], 35)

    def test_empty_ids_skips_query(self):
        r = _Fake()
        self.assertEqual(marketdb.post_stats([], runner=r), {})
        self.assertEqual(r.calls, [])

    def test_unavailable_returns_empty(self):
        self.assertEqual(marketdb.post_stats([1], runner=_Fake(None)), {})


class TestUserIdOf(unittest.TestCase):
    def test_returns_int(self):
        self.assertEqual(marketdb.user_id_of("10000000000", runner=_Fake([["4321"]])), 4321)

    def test_missing_returns_none(self):
        self.assertIsNone(marketdb.user_id_of("10000000000", runner=_Fake([])))
        self.assertIsNone(marketdb.user_id_of("", runner=_Fake([["4321"]])))


class TestPostsSince(unittest.TestCase):
    """按 userID + 起始日取自己的帖子（采样与历史基线用）。"""

    def test_shape(self):
        sql = marketdb.posts_since_sql(4321, "2026-09-01")
        self.assertIn("userID=4321", sql)
        self.assertIn("post_time>='2026-09-01", sql)
        self.assertIn("is_private=0", sql)
        self.assertIn("ORDER BY postID", sql)

    def test_parses_rows(self):
        r = _Fake([["6131", "2026-09-13 10:01:20", "36", "0", "0", "33", "前沿技术周报"],
                   ["6127", "2026-09-13 09:16:27", "35", "1", "2", "27", "9/13 Nvidia"]])
        got = marketdb.posts_since(4321, "2026-09-01", runner=r)
        self.assertEqual(len(got), 2)
        self.assertEqual(got[0]["post_id"], 6131)
        self.assertEqual(got[0]["day"], "2026-09-13")
        self.assertEqual(got[0]["browse"], 36)
        self.assertEqual(got[0]["comments"], 0)
        self.assertEqual(got[1]["likes"], 1)
        self.assertEqual(got[1]["title"], "9/13 Nvidia")

    def test_skips_malformed_rows(self):
        r = _Fake([["abc", "2026-09-13 10:01:20", "1", "0", "0", "0", "t"]])
        self.assertEqual(marketdb.posts_since(4321, "2026-09-01", runner=r), [])

    def test_unavailable_returns_empty(self):
        self.assertEqual(marketdb.posts_since(4321, "2026-09-01", runner=_Fake(None)), [])


class TestPostVisibility(unittest.TestCase):
    """发帖后只读回读：is_private 校验（2026-09-20 星榜帖 #6311 被写私密事故）。

    返回口径：True/False = 库里查到了；None = 查不到/库不可用/字段异常（未知），
    调用方按「无法确认」降级，绝不能当成公开。
    """

    def test_visibility_sql_readonly_single_post(self):
        sql = marketdb.post_visibility_sql(6311)
        self.assertTrue(sql.upper().startswith("SELECT IS_PRIVATE"))
        self.assertIn("postID=6311", sql)

    def test_private_true(self):
        self.assertIs(marketdb.post_is_private(6311, runner=_Fake([["1"]])), True)

    def test_public_false(self):
        self.assertIs(marketdb.post_is_private(6311, runner=_Fake([["0"]])), False)

    def test_unknown_when_no_row(self):
        self.assertIsNone(marketdb.post_is_private(99999, runner=_Fake([])))

    def test_unknown_when_db_down(self):
        self.assertIsNone(marketdb.post_is_private(6311, runner=_Fake(None)))

    def test_unknown_when_garbage(self):
        self.assertIsNone(marketdb.post_is_private(6311, runner=_Fake([["x"]])))


if __name__ == "__main__":
    unittest.main()
