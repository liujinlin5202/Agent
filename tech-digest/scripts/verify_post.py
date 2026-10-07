# -*- coding: utf-8 -*-
"""发帖确认脚本（M2 手测用）：refresh → info → userProfilePosts，打印我的帖子列表。

用法（服务器）:
  .venv/bin/python scripts/verify_post.py [标题关键字]

只读操作，不修改任何数据。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import requests  # noqa: E402

from app.config import settings  # noqa: E402
from app.publisher import refresh_access_token  # noqa: E402  # 自持久化轮换，避免 token 链断裂

BASE = settings.market_base_url or "http://172.18.0.5:8080"
keyword = sys.argv[1] if len(sys.argv) > 1 else "前沿技术周报"


def jd(obj) -> str:
    return json.dumps(obj, ensure_ascii=False)


def main() -> int:
    auth = refresh_access_token()
    if not auth:
        print("refresh 失败；可能已失效，需重新登录 fetch_refresh_token.py")
        return 1
    token = auth[0]
    h = {"Authorization": f"Bearer {token}"}
    r = requests.get(BASE + "/api/auth/info", headers=h, timeout=15)
    print("info:", r.status_code)
    user = ((r.json().get("data") or {}).get("user") or {})
    print("  user:", {k: v for k, v in user.items() if not isinstance(v, (list, dict))})
    user_id = user.get("userID") or user.get("id") or user.get("ID") or 0
    if not user_id:
        print("  (未识别 userID)")
        return 1
    rr = requests.post(BASE + "/api/auth/userProfilePosts",
                       json={"targetUserID": user_id, "limit": 10, "offset": 0},
                       headers=h, timeout=15)
    print("userProfilePosts:", rr.status_code)
    posts = rr.json()
    if isinstance(posts, dict):
        posts = posts.get("data") or posts.get("posts") or []
    items = posts if isinstance(posts, list) else []
    hit = False
    for p in items:
        if isinstance(p, dict) and keyword in jd(p):
            hit = True
            print("  MATCH:", jd(p)[:500])
    if not hit:
        print("  (无匹配，前 5 条结果:)")
        for p in items[:5]:
            print("   ", jd(p)[:400])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
