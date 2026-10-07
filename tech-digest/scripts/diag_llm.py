# -*- coding: utf-8 -*-
"""诊断：requests 到底连到了哪（getaddrinfo 全记录 + 重定向链 + 错误页指纹）。"""
import socket
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import requests

from app.config import settings

host = "api.<MARKET_DOMAIN>"
print("getaddrinfo:")
for fam, typ, proto, canon, sa in socket.getaddrinfo(host, 443):
    print("  ", fam, sa)

url = settings.sse_market_base_url.rstrip("/") + "/chat/completions"
print("\nurl =", url)
resp = requests.post(url, headers={
    "Authorization": f"Bearer {settings.sse_market_api_key}",
    "Content-Type": "application/json"},
    json={"model": settings.sse_market_model, "messages": [{"role": "user", "content": "hi"}],
          "max_tokens": 5, "reasoning_effort": "none"},
    timeout=30)
print("final status =", resp.status_code)
print("final url =", resp.url)
print("history:")
for h in resp.history:
    print("  ", h.status_code, "->", h.url, "| server:", h.headers.get("server"))
print("server header =", resp.headers.get("server"))
print("body[:200] =", resp.text[:200])
