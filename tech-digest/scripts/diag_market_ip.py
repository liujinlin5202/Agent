# -*- coding: utf-8 -*-
"""诊断:集市后端容器 IP 漂移后,新 IP 的 auth refresh 接口是否可用(不回显 token)。"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import requests

from app.config import settings

for ip in ("172.18.0.6", "172.18.0.5"):
    base = f"http://{ip}:8080"
    try:
        r = requests.post(base + "/api/auth/refresh",
                          json={"refresh_token": settings.market_refresh_token},
                          timeout=8)
        masked = {}
        try:
            d = r.json()
            src = d.get("data") or d
            masked = {k: ("<token>" if "token" in k.lower() else v)
                      for k, v in src.items() if not isinstance(v, dict)}
            if isinstance(d.get("data"), dict):
                masked["code"] = d.get("code")
                masked["msg"] = d.get("msg")
        except ValueError:
            masked = {"body": r.text[:120]}
        print(f"[{ip}] HTTP {r.status_code} -> {masked}")
    except requests.RequestException as e:
        print(f"[{ip}] EXC {type(e).__name__}: {e}")
