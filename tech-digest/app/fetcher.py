# -*- coding: utf-8 -*-
"""HTTP 抓取封装：重试/超时/常规 UA，<=0.5 req/s 低频。"""
from __future__ import annotations

import time

import requests

from app.config import settings

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")

_LAST_CALL = 0.0


def http_get(url: str, *, headers: dict | None = None, params: dict | None = None,
             timeout: int | None = None, retries: int = 3, allow_retry: bool = True) -> requests.Response:
    """GET 并重试（指数退避）。返回 response（调用方检查 status_code）。"""
    global _LAST_CALL
    timeout = timeout or settings.http_timeout
    h = {"User-Agent": UA}
    if headers:
        h.update(headers)
    last_err: Exception | None = None
    for attempt in range(retries):
        # 低频：距上次请求至少 2s（单任务 ~19 条页面 + 备用 API，整体 < 5 次请求）
        wait = 2.0 - (time.monotonic() - _LAST_CALL)
        if wait > 0:
            time.sleep(wait)
        try:
            resp = requests.get(url, headers=h, params=params, timeout=timeout)
            _LAST_CALL = time.monotonic()
            if resp.status_code == 200:
                return resp
            last_err = RuntimeError(f"HTTP {resp.status_code} for {url}")
            # 4xx 不重试（页面改版/被限流重试无意义）
            if 400 <= resp.status_code < 500 or not allow_retry:
                break
        except requests.RequestException as e:
            last_err = e
        if attempt < retries - 1:
            time.sleep(1.5 * (2 ** attempt))  # 1.5s / 3s
    raise RuntimeError(f"GET failed after {retries} attempts: {last_err}")
