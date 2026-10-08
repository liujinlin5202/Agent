# -*- coding: utf-8 -*-
"""tech-digest spec 部署脚本（复刻上一次割接的 /tmp/deploy_td.py 做法）。

用法（服务器上）：
  KEY=$(awk '$1=="ops-ctrl"{print $2}' /root/qiudock/tenant-keys.txt) \
    python3 /tmp/deploy_td.py /tmp/tech-digest-spec.yaml
"""
import json
import pathlib
import re
import sys
import urllib.request

spec_path = sys.argv[1]
key = pathlib.Path("/root/qiudock/tenant-keys.txt").read_text()
key = next(l.split()[1] for l in key.splitlines() if l.startswith("ops-ctrl"))

env = {}
env_path = pathlib.Path("/root/SSE_Market/SSE-Agent/tech-digest/.env")
for line in env_path.read_text(encoding="utf-8").splitlines():
    line = line.strip()
    if line and not line.startswith("#") and "=" in line:
        k, _, v = line.partition("=")
        env[k.strip()] = v.strip().strip('"').strip("'")


def sub(m):
    k = m.group(1)
    if k not in env or not env[k]:
        raise SystemExit(f"占位符 {k} 在 .env 无值，中止")
    return f'{k}: "{env[k]}"'


spec = pathlib.Path(spec_path).read_text(encoding="utf-8")
spec, n = re.subn(r'(\w+): "（部署时注入）"', sub, spec)
print(f"placeholders substituted: {n}")

req = urllib.request.Request(
    "http://10.43.196.33:7700/v1/deploys",
    data=json.dumps({"spec": spec}).encode("utf-8"),
    headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
    method="POST")
with urllib.request.urlopen(req, timeout=30) as resp:
    body = json.loads(resp.read().decode())
print(json.dumps(body, ensure_ascii=False)[:500])
