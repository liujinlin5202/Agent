# -*- coding: utf-8 -*-
"""诊断 DeepSeek thinking 控制：试几种参数组合，找出能关思考/产出 text 块的写法。"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import requests

from app.config import settings

url = settings.deepseek_base_url.rstrip("/") + "/v1/messages"
HEADERS = {"x-api-key": settings.deepseek_api_key,
           "anthropic-version": "2023-06-01",
           "Content-Type": "application/json"}

variants = [
    ("baseline", {}),
    ("thinking-disabled", {"thinking": {"type": "disabled"}}),
    ("reasoning_effort-none", {"reasoning_effort": "none"}),
    ("temperature-0", {"temperature": 0.0}),
]

for name, extra in variants:
    payload = {
        "model": settings.deepseek_model,
        "max_tokens": 512,
        "temperature": 0.2,
        "messages": [{"role": "user", "content": "用一句话介绍你自己"}],
    }
    payload.update(extra)
    try:
        r = requests.post(url, headers=HEADERS, json=payload, timeout=60)
        if r.status_code != 200:
            print(f"[{name}] HTTP {r.status_code} body={r.text[:200]}")
            continue
        d = r.json()
        blocks = [(b.get("type"), len(b.get("text", "") or b.get("thinking", "") or ""))
                  for b in d.get("content", [])]
        print(f"[{name}] stop={d.get('stop_reason')} blocks={blocks} "
              f"out_tokens={d.get('usage', {}).get('output_tokens')}")
        for b in d.get("content", []):
            if b.get("type") == "text":
                print(f"    text[:80] = {b['text'][:80]}")
    except requests.RequestException as e:
        print(f"[{name}] EXC {type(e).__name__}: {e}")
