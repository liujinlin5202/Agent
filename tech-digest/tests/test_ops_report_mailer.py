# -*- coding: utf-8 -*-
"""邮件投递单测：重试与「失败必须留痕」是本模块的全部价值。"""
import unittest

from ops_report import config, mailer


class FakeSMTP:
    """记录调用；按排队脚本决定第几次成功。"""

    def __init__(self, fail_times=0, exc=None):
        self.fail_times = fail_times
        self.exc = exc
        self.calls = 0
        self.sent = []
        self.logged_in = None

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def login(self, user, code):
        self.logged_in = (user, code)

    def send_message(self, msg):
        self.calls += 1
        if self.calls <= self.fail_times:
            raise self.exc or OSError("smtp 连接被重置")
        self.sent.append(msg)


def _settings():
    return config.OpsSettings(smtp_user="a@163.com", smtp_auth_code="CODE",
                              mail_to="b@163.com")


class TestSend(unittest.TestCase):
    def test_success_first_try(self):
        box = {}
        ok, msg = mailer.send(_settings(), "主题", "<p>正文</p>",
                              smtp_factory=lambda s, box=box: box.setdefault("c", FakeSMTP()))
        self.assertTrue(ok)
        self.assertEqual(box["c"].logged_in, ("a@163.com", "CODE"))
        self.assertEqual(len(box["c"].sent), 1)

    def test_retries_then_succeeds(self):
        made = []
        # 每次重试都是新建连接（实现按次重连，不复用已重置的连接），
        # 所以失败脚本按「连接」排队：第 1 条连接首发失败，第 2 条直接成功。
        # brief 原写法每条连接都 fail_times=1，任何重试结构下都不可能第二次成功。
        plans = [1, 0]

        def factory(s, **kw):
            c = FakeSMTP(fail_times=plans.pop(0))
            made.append(c)
            return c

        slept = []
        ok, _ = mailer.send(_settings(), "主题", "<p>x</p>", retries=3, delay=7,
                            sleep=slept.append, smtp_factory=factory)
        self.assertTrue(ok)
        self.assertEqual(len(made), 2)          # 第一次失败、第二次成功
        self.assertEqual(slept, [7])            # 失败后才等

    def test_all_failures_reported(self):
        made = []

        def factory(s, **kw):
            c = FakeSMTP(fail_times=99, exc=OSError("认证失败"))
            made.append(c)
            return c

        ok, msg = mailer.send(_settings(), "主题", "<p>x</p>", retries=3, delay=0,
                              sleep=lambda s: None, smtp_factory=factory)
        self.assertFalse(ok)
        self.assertIn("认证失败", msg)
        self.assertEqual(len(made), 3)

    def test_missing_credentials_skips_network(self):
        called = []
        ok, msg = mailer.send(config.OpsSettings(), "主题", "<p>x</p>",
                              smtp_factory=lambda s, **k: called.append(1))
        self.assertFalse(ok)
        self.assertIn("未配置", msg)
        self.assertEqual(called, [])

    def test_subject_and_html_headers(self):
        box = {}

        def factory(s, **kw):
            box["c"] = FakeSMTP()
            return box["c"]

        mailer.send(_settings(), "【集市运维报告】x", "<p>正文</p>", smtp_factory=factory)
        msg = box["c"].sent[0]
        self.assertEqual(msg["To"], "b@163.com")
        # From 经 formataddr 编码为「=?utf-8?…?= <a@163.com>」（带显示名），
        # 断言收件地址在 From 里即可；精确等于裸地址会误报（brief 缺陷修正）。
        self.assertIn("a@163.com", msg["From"])
        self.assertIn("集市运维报告", str(msg["Subject"]))


if __name__ == "__main__":
    unittest.main()
