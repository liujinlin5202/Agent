# -*- coding: utf-8 -*-
"""embed.py 真实 API 冒烟测试（纯 HTTP，不依赖 MySQL/Qdrant）。

用于本地验证 embedding 供应商可用性与维度兼容性，例如切 SiliconFlow 前先跑：
  $env:EMBEDDING_API_KEY="sk-xxx"
  $env:EMBEDDING_BASE_URL="https://api.siliconflow.cn/v1"
  $env:EMBEDDING_MODEL="BAAI/bge-m3"
  python scripts/smoke_embed.py
（不设环境变量则用项目 .env 的配置，默认 DashScope）
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.embed import EmbeddingUnavailable, embed_texts  # noqa: E402
from app.config import settings  # noqa: E402


def main() -> None:
    texts = ["你好，这是一条测试", "集市分区智能检索与AI应答"]
    print(f"model = {settings.embedding_model}")
    print(f"base  = {settings.embedding_base_url}")
    try:
        vecs = embed_texts(texts)
    except EmbeddingUnavailable as e:
        print(f"FAIL: {e}")
        sys.exit(1)
    dims = {len(v) for v in vecs}
    print(f"OK: {len(vecs)} 条向量，维度 {dims}")
    if dims != {1024}:
        print("错误：维度不是 1024，与 Qdrant collection (size=1024) 不兼容！")
        sys.exit(2)


if __name__ == "__main__":
    main()
