#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""网关故障演练作业（M2 验收 3）：主通道指向死端口，验证备通道接管。

产物：/tmp/td-gw-drill.json（kubectl apply 用）。名字 td-gw-drill。
"""
import json
import pathlib

job = json.loads(pathlib.Path("/tmp/td-drycheck3.json").read_text())
job["metadata"]["name"] = "td-gw-drill"
hit = False
for e in job["spec"]["template"]["spec"]["containers"][0]["env"]:
    if e["name"] == "SSE_MARKET_BASE_URL" and "value" in e:
        e["value"] = "http://10.43.196.33:9/v1/llm"     # 死端口（立即拒绝）
        hit = True
assert hit, "SSE_MARKET_BASE_URL not found"
pathlib.Path("/tmp/td-gw-drill.json").write_text(json.dumps(job))
print("gw drill job written")
