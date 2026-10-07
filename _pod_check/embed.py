# -*- coding: utf-8 -*-
"""Embedding 客户端（原始 HTTP，支持两种服务协议，由 EMBEDDING_MODE 切换）。

- texts（平台旧版自建格式，2026-08-28 上线时提供的 curl 格式）：
    POST {EMBEDDING_BASE_URL}/embed  {"texts": ["a", "b"]}
- openai（当前生效；平台 2026-09-06 实测为 OpenAI 兼容，注意路径是 /v1/embeddings）：
    POST {EMBEDDING_BASE_URL}/embeddings  {"model": ..., "input": [...]}，
    此时 EMBEDDING_BASE_URL 应配置为 https://<MARKET_DOMAIN>:23011/v1
    （旧路径 /embed 已下线：HTTP 会 301 到 HTTPS 且 POST 变 GET，务必配 HTTPS 直连）

说明（2026-08-26 实测）：DashScope compatible-mode 与 langchain-openai 1.x
序列化不兼容（400 InvalidParameter），故 embedding 一律绕过 langchain 用原始 HTTP；
LLM 仍走 LangChain（DeepSeek 为标准 OpenAI 兼容，见 app/llm.py）。
"""
from __future__ import annotations

import json
import time
import urllib.request
from typing import Any

from app.config import settings

EMBED_URL = (
    settings.embedding_base_url.rstrip("/")
    + ("/embed" if settings.embed_mode == "texts" else "/embeddings")
)

# 与 Qdrant collection size 一致（切模型前必须核对，防止向量维度错配入库）
EXPECTED_DIM = 1024
_dim_warned = False


class EmbeddingUnavailable(RuntimeError):
    """embedding 服务不可用（欠费/限流/网络/响应格式异常等），消息内带响应详情。"""


def _parse_vectors(data: Any) -> list[list[float]]:
    """兼容多种响应结构（平台服务格式以实测为准，暂不固定单一 schema）。"""
    if isinstance(data, list) and data and isinstance(data[0], list):
        return data  # [[...], ...]
    if isinstance(data, dict):
        if isinstance(data.get("embeddings"), list):
            return data["embeddings"]  # {"embeddings": [[...]]}
        if isinstance(data.get("vectors"), list):
            return data["vectors"]
        if isinstance(data.get("data"), list) and data["data"] \
                and isinstance(data["data"][0], dict):
            return [item["embedding"] for item in data["data"]]  # OpenAI 兼容
        if isinstance(data.get("result"), list):
            return data["result"]
    raise EmbeddingUnavailable(f"无法解析 embedding 响应结构: {str(data)[:200]!r}")


def embed_texts(texts: list[str]) -> list[list[float]]:
    """批量嵌入（数组批量输入，单次请求）。

    重试策略：网络错误/429/5xx 重试 3 次（间隔 3s）；其余 4xx（如欠费 Arrearage）
    重试无意义，立即失败并抛 EmbeddingUnavailable，由调用方（检索/同步）决定降级或中止。
    """
    global _dim_warned
    if not texts:
        return []
    if settings.embed_mode == "texts":
        payload: dict[str, Any] = {"texts": texts}
    else:
        payload = {"model": settings.embedding_model, "input": texts}
    body = json.dumps(payload).encode("utf-8")
    headers = {"Content-Type": "application/json"}
    if settings.embedding_api_key:
        headers["Authorization"] = f"Bearer {settings.embedding_api_key}"
    last_err: Exception | None = None
    for attempt in range(3):
        req = urllib.request.Request(EMBED_URL, data=body, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            vecs = _parse_vectors(data)
            if len(vecs) != len(texts):
                raise EmbeddingUnavailable(
                    f"embedding 返回条数不符：期望 {len(texts)}，实际 {len(vecs)}")
            if not _dim_warned:
                dims = {len(v) for v in vecs}
                if dims != {EXPECTED_DIM}:
                    print(f"[embed] 警告：返回维度 {dims} ≠ {EXPECTED_DIM}（与 Qdrant collection 不匹配）", flush=True)
                    _dim_warned = True
            return vecs
        except EmbeddingUnavailable:
            raise  # 响应已收到但内容不对，重试无意义
        except urllib.error.HTTPError as e:
            detail = ""
            try:
                detail = e.read().decode("utf-8", "ignore")[:300]
            except Exception:  # noqa: BLE001
                pass
            last_err = e
            print(f"[embed] HTTP {e.code} (第{attempt + 1}次): {detail}", flush=True)
            if 400 <= e.code < 500 and e.code != 429:
                break  # 4xx（欠费/无权限/参数错）重试无意义
            time.sleep(3)
        except Exception as e:  # noqa: BLE001  网络抖动/隧道后端空响应等，重试
            last_err = e
            print(f"[embed] 请求失败(第{attempt + 1}次): {e!r}", flush=True)
            time.sleep(3)
    raise EmbeddingUnavailable(f"embedding 不可用: {last_err!r}") from last_err
