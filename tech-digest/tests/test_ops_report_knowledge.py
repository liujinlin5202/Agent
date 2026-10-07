# -*- coding: utf-8 -*-
"""知识库单测：每条规则的命中/不命中都要有断言——知识库错配会给出错处置。"""
import unittest

from ops_report import knowledge


POD_DOWN_SIG = ("upstream prematurely closed connection while reading response header "
                "from upstream, client: <n>.<n>.<n>.<n>")
DISK_SIG = "磁盘使用率 92% 已达告警线 90%：参考 <ts> 磁盘满导致整机挂死，立即清理"
EMBED_SIG = "[embed] HTTP <n> (<n>次): <html> <head><title><n> Bad Gateway</title>"
TOKEN_SIG = "发帖第 <n> 次失败: refresh_token 已过期 <n>"


class TestMatch(unittest.TestCase):
    def test_pod_down_matched(self):
        got = knowledge.match([POD_DOWN_SIG])
        self.assertTrue(any(a.title.startswith("应答服务进程不在") for a in got))

    def test_disk_matched(self):
        got = knowledge.match([DISK_SIG])
        self.assertTrue(any("磁盘" in a.title for a in got))

    def test_embedding_matched(self):
        got = knowledge.match([EMBED_SIG])
        self.assertTrue(any("向量" in a.title or "embedding" in a.title.lower() for a in got))

    def test_token_matched(self):
        got = knowledge.match([TOKEN_SIG])
        self.assertTrue(any("token" in a.title.lower() or "凭据" in a.title for a in got))

    def test_unrelated_signal_matches_nothing(self):
        self.assertEqual(knowledge.match(["完全无关的一行日志"]), [])

    def test_partial_match_does_not_fire(self):
        """规则要求多关键词同时命中：只命中一半不能给处置建议（错配=给错处置）。"""
        half = knowledge.match(["磁盘使用率 95%"])              # 缺「已达告警线」
        self.assertEqual([a for a in half if "磁盘" in a.title], [])
        full = knowledge.match(["磁盘使用率 95% 已达告警线 90%"])   # 两词齐了才命中
        self.assertTrue(any("磁盘" in a.title for a in full))

    def test_duplicate_signals_yield_one_advice(self):
        got = knowledge.match([POD_DOWN_SIG, POD_DOWN_SIG])
        self.assertEqual(len([a for a in got if a.title.startswith("应答服务进程不在")]), 1)

    def test_cross_signal_keywords_do_not_fire(self):
        """关键词必须同信号内凑齐：「发帖成功率 100%」+ 别处的「检索失败」
        不能拼串误触发发帖 runbook（错建议高频出现会让用户忽略建议层）。"""
        got = knowledge.match(["发帖成功率 100%", "检索失败，降级为空结果"])
        self.assertEqual([a for a in got if "发帖" in a.title], [])

    def test_every_advice_has_nonempty_action(self):
        for rule in knowledge.RULES:
            self.assertTrue(rule.action.strip(), f"规则 {rule.id} 缺处置步骤")
            self.assertTrue(rule.title.strip(), f"规则 {rule.id} 缺标题")


class TestSignalsFrom(unittest.TestCase):
    def test_collects_error_signatures_and_notes(self):
        from ops_report import model
        sec = model.Section(key="system", name="系统层", status="warn",
                            metrics={"磁盘使用率": "92%"},
                            notes=["磁盘使用率 92% 已达告警线 90%：立即清理"])
        sec.errors = [model.ErrorCluster(signature=POD_DOWN_SIG, count=1, first_ts="t",
                                         last_ts="t", sample="s", source="nginx")]
        sigs = knowledge.signals_from([sec])
        self.assertIn(POD_DOWN_SIG, sigs)
        self.assertTrue(any("磁盘使用率" in s for s in sigs))


if __name__ == "__main__":
    unittest.main()
