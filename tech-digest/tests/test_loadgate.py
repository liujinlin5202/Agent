# -*- coding: utf-8 -*-
"""负载门控单测：阈值边界与读取容错。"""
import unittest
from unittest import mock

from app import loadgate


class TestLoadgate(unittest.TestCase):
    def test_below_threshold(self):
        with mock.patch("app.loadgate.current_load1", return_value=0.5):
            self.assertEqual(loadgate.should_skip(2.5), (False, None))

    @unittest.mock.patch("app.loadgate.current_load1", return_value=2.5)
    def test_equal_threshold_skips(self, _m):
        skip, reason = loadgate.should_skip(2.5)
        self.assertTrue(skip)
        self.assertIn("2.50", reason)

    @unittest.mock.patch("app.loadgate.current_load1", return_value=9.9)
    def test_high_load_skips(self, _m):
        self.assertTrue(loadgate.should_skip(2.5)[0])

    def test_unreadable_load_fail_open(self):
        with mock.patch("app.loadgate.current_load1", return_value=None):
            self.assertEqual(loadgate.should_skip(2.5), (False, None))

    def test_current_load1_real(self):
        # 非 Linux 环境返回 None 而不是抛异常
        try:
            v = loadgate.current_load1()
        except Exception:  # noqa: BLE001
            self.fail("current_load1 不应抛异常")
        self.assertTrue(v is None or isinstance(v, float))


if __name__ == "__main__":
    unittest.main()
