# -*- coding: utf-8 -*-
"""诊断 DeepSeek 备用通道：打印真实状态码/响应体，定位 _chat_deepseek 返回 None 的原因。"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import requests

from app.config import settings

url = settings.deepseek_base_url.rstrip("/") + "/v1/messages"
print("url =", url)
print("model =", settings.deepseek_model)

# 小 prompt 试探
payload = {
    "model": settings.deepseek_model,
    "max_tokens": 64,
    "temperature": 0.2,
    "messages": [{"role": "user", "content": "回答一个字：你好"}],
}
try:
    resp = requests.post(
        url,
        headers={"x-api-key": settings.deepseek_api_key,
                 "anthropic-version": "2023-06-01",
                 "Content-Type": "application/json"},
        json=payload, timeout=60)
    print("status =", resp.status_code)
    print("headers server =", resp.headers.get("server"))
    print("body[:500] =", resp.text[:500])
except requests.RequestException as e:
    print("EXC:", type(e).__name__, e)
