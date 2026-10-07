# -*- coding: utf-8 -*-
"""digest-pref 同源桥接代理：仅放行 /api/v1/digest-pref 的 GET/POST，桥到 loopback 面板。

链路：公网 nginx（docker 容器，绑 80/443）→ 本进程（172.17.0.1:19087，容器经
host.docker.internal 可达）→ 127.0.0.1:19086 面板。面板保持绑 127.0.0.1 不动；
桥接只放行偏好这一条路径，/api/run 等其余面板端点照旧仅 loopback 可达。

仿房内先例 rag_public_proxy.py：BaseHTTPRequestHandler + ThreadingHTTPServer +
显式路径 dispatch，仅标准库。
"""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

HOST = os.getenv("PREF_PROXY_HOST", "127.0.0.1")
PORT = int(os.getenv("PREF_PROXY_PORT", "19087"))

ALLOWED_PATH = "/api/v1/digest-pref"
MAX_BODY_BYTES = 8192  # 与面板 manual_server 的 body 上限一致
UPSTREAM_TIMEOUT = 30


def _upstream_base() -> str:
    """上游面板地址：每次请求读 env，测试由此在进程内注入桩上游。"""
    return os.getenv("PREF_PROXY_UPSTREAM", "http://127.0.0.1:19086")


class Handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802
        self._proxy("GET")

    def do_POST(self) -> None:  # noqa: N802
        self._proxy("POST")

    def __getattr__(self, name: str):
        # 其余一切 HTTP 方法（DELETE/PUT/…）：白名单外 → 404 JSON，
        # 而非 BaseHTTPRequestHandler 默认的 501。
        if name.startswith("do_"):
            return self._not_found
        raise AttributeError(name)

    def _proxy(self, method: str) -> None:
        parsed = urlparse(self.path)
        if parsed.path != ALLOWED_PATH:
            return self._not_found()

        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        body = self.rfile.read(min(length, MAX_BODY_BYTES)) if length > 0 else b""

        headers = {}
        if self.headers.get("Content-Type"):
            headers["Content-Type"] = self.headers["Content-Type"]  # 防 urllib 伪造默认值
        if self.headers.get("Authorization"):
            headers["Authorization"] = self.headers["Authorization"]
        request = urllib.request.Request(
            _upstream_base() + self.path, data=body if method == "POST" else None,
            headers=headers, method=method)
        try:
            with urllib.request.urlopen(request, timeout=UPSTREAM_TIMEOUT) as resp:
                self._relay(resp.status, resp.read(), resp.headers.get("Content-Type"))
        except urllib.error.HTTPError as err:  # 上游 4xx/5xx（如无 token 403）：透传
            self._relay(err.code, err.read(), err.headers.get("Content-Type"))
        except urllib.error.URLError as err:
            self._json({"error": "upstream unavailable", "detail": str(err.reason)}, 502)
        except Exception as exc:  # noqa: BLE001 兜底：桥接自身故障不能挂连接
            self._json({"error": "proxy failure", "detail": str(exc)}, 502)

    def _relay(self, status: int, body: bytes, content_type: str | None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type or "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _json(self, payload: dict, status: int = 200) -> None:
        self._relay(status, json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                    "application/json; charset=utf-8")

    def _not_found(self) -> None:
        self._json({"error": "not found"}, 404)


if __name__ == "__main__":
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    print(f"digest-pref public proxy running at http://{HOST}:{PORT} -> {_upstream_base()}",
          flush=True)
    server.serve_forever()
