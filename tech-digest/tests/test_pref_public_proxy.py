# -*- coding: utf-8 -*-
"""pref_public_proxy 单测：真起桥接端口 + 桩上游（env 注入），验证白名单/转发/透传/502。

口径：
· 允许面 = 且仅 = `GET|POST × /api/v1/digest-pref`；其余一切路径/方法 → 404 JSON；
· 转发保真：method、body（Content-Length 上限 8192，与面板一致）、Authorization；
· 透传保真：上游 status/body/Content-Type 原样回传（含 4xx/5xx）；上游不可达 → 502 JSON。
"""
import json
import os
import socket
import threading
import unittest
import urllib.error
import urllib.request
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import pref_public_proxy  # noqa: E402


class _StubUpstream(BaseHTTPRequestHandler):
    """桩面板：记录收到的请求，按类级 response 配置回包（模拟面板 200/403）。"""

    requests: list[dict] = []
    response: dict = {}

    def _respond(self):
        length = int(self.headers.get("Content-Length") or 0)
        _StubUpstream.requests.append({
            "method": self.command,
            "path": self.path,
            "authorization": self.headers.get("Authorization"),
            "body": self.rfile.read(length) if length > 0 else b"",
        })
        body = _StubUpstream.response["body"]
        self.send_response(_StubUpstream.response["status"])
        self.send_header("Content-Type", _StubUpstream.response["ctype"])
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    do_GET = _respond
    do_POST = _respond

    def log_message(self, fmt, *args):  # 静音
        pass


class PrefPublicProxyTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # 桩上游先起，端口才可写进 env —— 桥接每次请求经 _upstream_base() 读 env，
        # 故 mock.patch.dict 即为测试注入桩的正式通道（镜像 rag 的 SSEMARKET_RAG_API_BASE）。
        cls.upstream = ThreadingHTTPServer(("127.0.0.1", 0), _StubUpstream)
        threading.Thread(target=cls.upstream.serve_forever, daemon=True).start()
        patcher = mock.patch.dict(os.environ, {
            "PREF_PROXY_UPSTREAM": f"http://127.0.0.1:{cls.upstream.server_address[1]}",
        })
        patcher.start()
        cls.addClassCleanup(patcher.stop)
        # 被测桥接：真起一个 ephemeral loopback 端口
        cls.proxy = ThreadingHTTPServer(("127.0.0.1", 0), pref_public_proxy.Handler)
        threading.Thread(target=cls.proxy.serve_forever, daemon=True).start()

    def setUp(self):
        _StubUpstream.requests.clear()
        _StubUpstream.response = {
            "status": 200,
            "body": json.dumps({"mode": "default", "text": ""}).encode("utf-8"),
            "ctype": "application/json; charset=utf-8",
        }

    def _request(self, method, path, body=None, headers=None):
        req = urllib.request.Request(
            f"http://127.0.0.1:{self.proxy.server_address[1]}{path}",
            data=body, headers=headers or {}, method=method)
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                return resp.status, resp.read(), resp.headers.get("Content-Type")
        except urllib.error.HTTPError as err:  # 4xx/5xx：状态码照常断言
            return err.code, err.read(), err.headers.get("Content-Type")

    # ---- 允许面：GET/POST × /api/v1/digest-pref ----

    def test_get_forwards_authorization_and_relays_response(self):
        status, body, ctype = self._request(
            "GET", "/api/v1/digest-pref", headers={"Authorization": "Bearer tok1"})
        self.assertEqual(status, 200)
        self.assertEqual(body, _StubUpstream.response["body"])
        self.assertEqual(ctype, "application/json; charset=utf-8")
        self.assertEqual(len(_StubUpstream.requests), 1)
        seen = _StubUpstream.requests[0]
        self.assertEqual(seen["method"], "GET")
        self.assertEqual(seen["path"], "/api/v1/digest-pref")
        self.assertEqual(seen["authorization"], "Bearer tok1")

    def test_get_relays_upstream_403_with_body(self):
        # 生产冒烟链路：无 token 时面板 403「无权限」，桥接必须原样透传状态与 body
        _StubUpstream.response = {"status": 403, "body": "无权限".encode("utf-8"),
                                  "ctype": "application/json; charset=utf-8"}
        status, body, _ = self._request("GET", "/api/v1/digest-pref")
        self.assertEqual(status, 403)
        self.assertEqual(body, "无权限".encode("utf-8"))

    def test_post_forwards_body_and_authorization(self):
        payload = json.dumps({"mode": "prefer", "text": "jev 相关"}).encode("utf-8")
        status, _, _ = self._request(
            "POST", "/api/v1/digest-pref", body=payload,
            headers={"Authorization": "Bearer tok2"})
        self.assertEqual(status, 200)
        self.assertEqual(len(_StubUpstream.requests), 1)
        seen = _StubUpstream.requests[0]
        self.assertEqual(seen["method"], "POST")
        self.assertEqual(seen["body"], payload)
        self.assertEqual(seen["authorization"], "Bearer tok2")

    def test_post_body_capped_at_8192(self):
        # 与面板 manual_server 的 8192 上限一致：超长 body 截断后转发
        status, _, _ = self._request(
            "POST", "/api/v1/digest-pref", body=b"x" * 10000,
            headers={"Authorization": "Bearer tok"})
        self.assertEqual(status, 200)
        self.assertEqual(len(_StubUpstream.requests[0]["body"]), 8192)

    # ---- 白名单外：一律 404 JSON，且绝不触达上游 ----

    def test_get_api_run_blocked(self):
        status, body, ctype = self._request("GET", "/api/run?task=daily")
        self.assertEqual(status, 404)
        self.assertEqual(json.loads(body), {"error": "not found"})
        self.assertIn("application/json", ctype)
        self.assertEqual(_StubUpstream.requests, [])

    def test_post_api_run_blocked(self):
        status, body, _ = self._request("POST", "/api/run?task=daily", body=b"{}")
        self.assertEqual(status, 404)
        self.assertEqual(json.loads(body), {"error": "not found"})
        self.assertEqual(_StubUpstream.requests, [])

    def test_other_panel_paths_blocked(self):
        for path in ("/api/file?name=x.md", "/api/log", "/api/status", "/"):
            with self.subTest(path=path):
                status, body, _ = self._request("GET", path)
                self.assertEqual(status, 404)
                self.assertEqual(json.loads(body), {"error": "not found"})
        self.assertEqual(_StubUpstream.requests, [])

    def test_allowed_path_prefix_not_matched(self):
        # 精确匹配：/api/v1/digest-prefX 不得放行
        status, _, _ = self._request("GET", "/api/v1/digest-prefX")
        self.assertEqual(status, 404)
        self.assertEqual(_StubUpstream.requests, [])

    def test_delete_unknown_method_blocked_even_on_allowed_path(self):
        status, body, _ = self._request("DELETE", "/api/v1/digest-pref")
        self.assertEqual(status, 404)
        self.assertEqual(json.loads(body), {"error": "not found"})
        self.assertEqual(_StubUpstream.requests, [])

    # ---- 上游不可达 ----

    def test_upstream_down_502_json(self):
        # 占一个空闲端口后立刻关闭 → 该端口无人监听，模拟面板挂掉
        probe = socket.socket()
        probe.bind(("127.0.0.1", 0))
        dead_port = probe.getsockname()[1]
        probe.close()
        with mock.patch.dict(os.environ, {
            "PREF_PROXY_UPSTREAM": f"http://127.0.0.1:{dead_port}",
        }):
            status, body, ctype = self._request("GET", "/api/v1/digest-pref")
        self.assertEqual(status, 502)
        self.assertIn("application/json", ctype)
        self.assertIn("error", json.loads(body))


if __name__ == "__main__":
    unittest.main()
