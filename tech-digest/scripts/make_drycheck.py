#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""从 daily CronJob 生成 force 全路径 dry-run 的一次性 Job（真实 pod、不发帖不写库）。

产物：/tmp/td-drycheck3.json（kubectl apply 用）。
"""
import json
import pathlib
import subprocess

out = pathlib.Path("/tmp/td-drycheck2.json").read_text()
job = json.loads(out)
patched = False
for e in job["spec"]["template"]["spec"]["containers"][0]["env"]:
    if e["name"] == "QDOCK_REPO_RUN" and "value" in e:
        assert e["value"].rstrip().endswith("--dry-run"), "base must be dry-run"
        e["value"] = e["value"].rstrip() + " --force"
        patched = True
        print("cmd tail:", e["value"][-42:])
assert patched, "QDOCK_REPO_RUN not patched"
job["metadata"]["name"] = "td-daily-drycheck3"
pathlib.Path("/tmp/td-drycheck3.json").write_text(json.dumps(job))
print("written")
