# -*- coding: utf-8 -*-
"""恢复脚本：用 publisher.refresh_access_token() 刷新并写回 .env（单次轮换的正确姿势）。"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.publisher import refresh_access_token

result = refresh_access_token()
if result:
    token, new_refresh = result
    print("refresh OK, 新 token 已写回 .env" if new_refresh else
          "refresh OK, 未返回新 refresh_token（沿用旧值）")
    print("access token 前 8 位:", (token or "")[:8], "...")
else:
    print("refresh FAILED —— .env 里的 refresh_token 已失效，需重新登录")
