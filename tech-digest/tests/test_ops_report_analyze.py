# -*- coding: utf-8 -*-
"""错误聚类单测：13000 条同类报错必须聚成一行。"""
import unittest

from ops_report import analyze, model


class TestSignature(unittest.TestCase):
    def test_numbers_and_ids_become_placeholders(self):
        a = analyze.signature("发帖第 3 次失败: postID=6311 boom")
        b = analyze.signature("发帖第 5 次失败: postID=6322 boom")
        self.assertEqual(a, b)

    def test_timestamps_removed(self):
        a = analyze.signature("2026-09-20 09:15:30,123 ERROR x")
        b = analyze.signature("2026-09-21 10:16:31,999 ERROR x")
        self.assertEqual(a, b)

    def test_long_hex_removed(self):
        a = analyze.signature("trace 550cf88bfbb9 failed")
        b = analyze.signature("trace 6126631a9236 failed")
        self.assertEqual(a, b)

    def test_different_errors_stay_different(self):
        self.assertNotEqual(analyze.signature("embedding 502"),
                            analyze.signature("qdrant refused"))


class TestCluster(unittest.TestCase):
    def test_merges_same_signature_and_counts(self):
        entries = [
            {"ts": "2026-09-20T05:00:00", "text": "postID=1 failed", "source": "pod"},
            {"ts": "2026-09-20T06:00:00", "text": "postID=2 failed", "source": "pod"},
            {"ts": "2026-09-20T07:00:00", "text": "postID=3 failed", "source": "pod"},
        ]
        got = analyze.cluster(entries)
        self.assertEqual(len(got), 1)
        self.assertEqual(got[0].count, 3)
        self.assertEqual(got[0].first_ts, "2026-09-20T05:00:00")
        self.assertEqual(got[0].last_ts, "2026-09-20T07:00:00")
        self.assertIn("postID=1", got[0].sample)

    def test_sorted_by_count_desc(self):
        entries = (
            [{"ts": "t", "text": "b error", "source": "pod"}] * 2 +
            [{"ts": "t", "text": "a error", "source": "nginx"}] * 5)
        got = analyze.cluster(entries)
        self.assertEqual([c.count for c in got], [5, 2])

    def test_empty(self):
        self.assertEqual(analyze.cluster([]), [])


class TestAnnotate(unittest.TestCase):
    def test_writes_clusters_into_section(self):
        sec = model.Section(key="digest", name="发帖", status="warn")
        analyze.annotate(sec, [{"ts": "t", "text": "weekend skip", "source": "digest_log"}])
        self.assertEqual(len(sec.errors), 1)
        self.assertIsInstance(sec.errors[0], model.ErrorCluster)

    def test_expected_marker_from_text(self):
        sec = model.Section(key="digest", name="发帖", status="ok")
        analyze.annotate(sec, [
            {"ts": "t", "text": "周末只抓取入库、不发帖（数据仍进周报池）", "source": "digest_log"},
            {"ts": "t", "text": "发帖第 3 次失败: token 过期", "source": "digest_log"},
        ])
        flags = sorted(e.expected for e in sec.errors)
        self.assertEqual(flags, [False, True])


if __name__ == "__main__":
    unittest.main()
