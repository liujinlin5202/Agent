# -*- coding: utf-8 -*-
"""加精写通道：POST /internal/featured（集市 Go 后端内部端点）。

单一写方原则：is_high_quality 的所有写入都经 Go 业务层（写后失效主页浏览
缓存），本任务不直改库。INTERNAL_TOKEN 三端一致（ops_tokens.env 为正典），
token 不匹配/未配置一律 403（fail-closed）。
"""
from __future__ import annotations

import logging

import requests

log = logging.getLogger("post-curator.writer")


def _post(url: str, **kwargs):
    """trust_env=False 的直连 POST（requests.post 不收 trust_env，必须走 Session）。"""
    session = requests.Session()
    session.trust_env = False
    return session.post(url, **kwargs)


def mark_featured(post_ids: list[int], base_url: str, token: str,
                  timeout: int = 15) -> tuple[bool, str]:
    """批量加精。返回 (ok, 说明)。幂等：Go 侧 WHERE is_high_quality<>1。

    Go 侧单批上限 500，超出自动分批（开班自愈会拿历史全部欠账，必须能装下）。"""
    if not post_ids:
        return True, "无待加精帖"
    if not token:
        return False, "INTERNAL_TOKEN 未配置（fail-closed）"
    url = base_url.rstrip("/") + "/internal/featured"
    total = 0
    for i in range(0, len(post_ids), 500):
        batch = post_ids[i:i + 500]
        try:
            resp = _post(
                url,
                headers={"X-Internal-Token": token, "Content-Type": "application/json"},
                json={"post_ids": batch},
                timeout=timeout,
            )
        except requests.RequestException as e:
            log.warning("internal/featured 请求失败: %s", e)
            return False, f"请求异常: {e}"
        if resp.status_code != 200:
            return False, f"HTTP {resp.status_code}: {resp.text[:200]}"
        try:
            body = resp.json()
        except ValueError:
            return False, f"响应非 JSON: {resp.text[:200]}"
        if body.get("code") != 200:
            return False, f"业务失败: {body.get('msg')}"
        total += int((body.get("data") or {}).get("marked") or 0)
    return True, f"加精 {total} 篇"
