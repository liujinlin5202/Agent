# -*- coding: utf-8 -*-
"""应答 AI 采集单测（样本行取自 2026-09-20 线上真实日志）。"""
import unittest
from datetime import datetime

from ops_report import analyze, config, knowledge
from ops_report.collect import assist


NGINX_502 = ('120.239.196.134 - - [20/Sep/2026:05:48:28 +0000] '
             '"POST /api/v1/assist/match HTTP/1.1" 502 559 "-" "Mozilla/5.0"')
NGINX_ERROR_502 = ('2026/09/20 05:48:28 [error] 24#24: *564 upstream prematurely closed '
                   'connection while reading response header from upstream, '
                   'client: 120.239.196.134, server: <MARKET_DOMAIN>, '
                   'request: "POST /api/v1/assist/post/6302 HTTP/1.1", '
                   'upstream: "http://172.17.0.1:13012/api/v1/assist/post/6302"')
NGINX_421 = ('120.239.196.134 - - [20/Sep/2026:05:48:28 +0000] '
             '"POST /api/v1/assist/match HTTP/2.0" 421 575 "-" "Mozilla/5.0"')
NGINX_200 = ('1.2.3.4 - - [20/Sep/2026:05:48:28 +0000] '
             '"GET /api/v1/assist/post/6325 HTTP/1.1" 200 1234 "-" "curl"')
NGINX_OTHER = ('1.2.3.4 - - [20/Sep/2026:05:48:28 +0000] '
               '"GET /api/v1/posts/hot HTTP/1.1" 200 10 "-" "curl"')


class TestParseAccessLines(unittest.TestCase):
    def test_counts_by_status_and_computes_rate(self):
        got = assist.parse_access_lines([NGINX_502, NGINX_421, NGINX_200, NGINX_OTHER])
        self.assertEqual(got["total"], 3)          # 非 assist 请求不计
        self.assertEqual(got["by_status"], {502: 1, 421: 1, 200: 1})
        self.assertEqual(got["ok"], 1)             # 只有 2xx 算成功
        self.assertEqual(got["server_errors"], 1)  # 5xx 单独计
        self.assertAlmostEqual(got["rate"], 100 / 3, places=2)

    def test_all_failed_is_zero_rate(self):
        got = assist.parse_access_lines([NGINX_502, NGINX_502])
        self.assertEqual(got["rate"], 0.0)
        self.assertEqual(got["server_errors"], 2)

    def test_empty_is_none_rate(self):
        got = assist.parse_access_lines([])
        self.assertEqual(got["total"], 0)
        self.assertIsNone(got["rate"])


class TestParseNginxErrors(unittest.TestCase):
    def test_error_line_with_assist_path_is_captured(self):
        got = assist.parse_nginx_errors([NGINX_ERROR_502])
        self.assertEqual(len(got), 1)
        self.assertIn("upstream prematurely closed connection", got[0]["text"])
        self.assertEqual(got[0]["source"], "nginx")
        self.assertEqual(got[0]["ts"], "2026-09-20T05:48:28")  # 行首时间被提取

    def test_access_lines_and_none_are_ignored(self):
        self.assertEqual(assist.parse_nginx_errors([NGINX_502, NGINX_200]), [])
        self.assertEqual(assist.parse_nginx_errors(None), [])

    def test_signature_reaches_pod_down_runbook(self):
        """error 行 → 归一化签名必须命中 assist_pod_down 规则（否则重启 runbook 永不触发）。"""
        entries = assist.parse_nginx_errors([NGINX_ERROR_502])
        advice = knowledge.match([analyze.signature(entries[0]["text"])])
        self.assertTrue(any(a.title.startswith("应答服务进程不在") for a in advice))


class TestParseProbe(unittest.TestCase):
    def test_parses_proc_and_port(self):
        self.assertEqual(assist.parse_probe("PROC=1\nPORT=1\nUPTIME=x\n"),
                         {"proc": 1, "port": 1})

    def test_missing_output_is_down(self):
        self.assertEqual(assist.parse_probe(""), {"proc": 0, "port": 0})
        self.assertEqual(assist.parse_probe(None), {"proc": 0, "port": 0})


class TestParsePodErrors(unittest.TestCase):
    def test_picks_error_lines_only(self):
        text = (
            "== service.log ==\n"
            "INFO:     127.0.0.1:55056 - \"GET /health/live HTTP/1.1\" 200 OK\n"
            "[api] 检索失败，降级为空结果: ConnectionError('qdrant refused')\n"
            "[llm] 主通道失败，已切换备用\n"
            "== sync.log ==\n"
            "[sync] 第 11098 周期失败（30.0s 后重试）: EmbeddingUnavailable(\"HTTPError 502\")\n")
        got = assist.parse_pod_errors(text)
        self.assertEqual(len(got), 3)
        self.assertTrue(all(e["source"] == "pod" for e in got))
        self.assertIn("EmbeddingUnavailable", got[2]["text"])


class TestBuildSection(unittest.TestCase):
    DOWN_ACCESS = {"total": 14577, "by_status": {502: 13223, 421: 1333, 499: 19, 404: 2},
                   "ok": 0, "server_errors": 13223, "rate": 0.0}

    def test_service_down_is_critical_with_evidence(self):
        c = assist.build_section(self.DOWN_ACCESS, {"proc": 0, "port": 0}, [], None,
                                 datetime(2026, 9, 18, 21, 30), datetime(2026, 9, 20, 21, 30))
        self.assertEqual(c.section.status, "critical")
        self.assertEqual(c.section.metrics["请求成功率"], "0%")
        self.assertEqual(c.section.metrics["请求总数"], "14577 次")
        self.assertEqual(c.section.metrics["进程/端口"], "未运行 / 未监听")
        self.assertIn("502", c.section.metrics["状态码分布"])

    def test_healthy_service_is_ok(self):
        access = {"total": 120, "by_status": {200: 118, 404: 2}, "ok": 118,
                  "server_errors": 0, "rate": 98.33}
        c = assist.build_section(access, {"proc": 1, "port": 1}, [], {"新应答数": 30, "顶": 4, "踩": 1},
                                 datetime(2026, 9, 18), datetime(2026, 9, 20))
        self.assertEqual(c.section.status, "ok")
        self.assertEqual(c.section.metrics["新应答数"], "30 条")
        self.assertEqual(c.section.metrics["用户反馈"], "顶 4 / 踩 1")

    def test_no_data_at_all_is_unknown(self):
        """连 nginx 都读不到（docker 不可用）且探活失败 → unknown，而不是 critical。"""
        c = assist.build_section(None, None, [], None,
                                 datetime(2026, 9, 18), datetime(2026, 9, 20))
        self.assertEqual(c.section.status, "unknown")
        self.assertTrue(any("nginx" in n for n in c.section.notes))

    def test_access_only_no_requests_is_warn(self):
        access = {"total": 0, "by_status": {}, "ok": 0, "server_errors": 0, "rate": None}
        c = assist.build_section(access, {"proc": 1, "port": 1}, [], None,
                                 datetime(2026, 9, 18), datetime(2026, 9, 20))
        self.assertEqual(c.section.status, "warn")
        self.assertEqual(c.section.metrics["请求总数"], "0 次")

    def test_partial_server_errors_is_warn(self):
        """服务活着但有 5xx → warn（不是 critical）。"""
        access = {"total": 100, "by_status": {200: 95, 502: 5}, "ok": 95,
                  "server_errors": 5, "rate": 95.0}
        c = assist.build_section(access, {"proc": 1, "port": 1}, [], None,
                                 datetime(2026, 9, 18), datetime(2026, 9, 20))
        self.assertEqual(c.section.status, "warn")


class TestPodSshGuard(unittest.TestCase):
    def test_no_password_returns_none_without_calling_ssh(self):
        """未配 pod 口令时必须直接返回 None，不能去连（否则报错刷屏）。"""
        s = config.OpsSettings(pod_ssh_password="")
        self.assertIsNone(assist.pod_ssh(s, "echo hi"))


if __name__ == "__main__":
    unittest.main()
