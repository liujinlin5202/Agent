# -*- coding: utf-8 -*-
"""Qdrant 封装：只操作新 collection market_assist_v1（红线：绝不触碰 market_content）。"""
from __future__ import annotations

import hashlib
from typing import Iterable, Iterator

from qdrant_client import QdrantClient, models
from qdrant_client.models import Filter, FieldCondition, PointStruct

from app.config import settings

_client: QdrantClient | None = None


def get_client() -> QdrantClient:
    global _client
    if _client is None:
        # 现有 Qdrant 为 1.17.1（平台集群 1.19.0）；关闭小版本兼容告警（所用 REST 接口跨版本稳定）
        kwargs: dict = {"url": settings.qdrant_url, "timeout": 10, "check_compatibility": False}
        if settings.qdrant_api_key:  # 平台 Qdrant 需要；现有 6333 实例无需
            kwargs["api_key"] = settings.qdrant_api_key
        _client = QdrantClient(**kwargs)
    return _client


def stable_point_id(origin: str) -> int:
    """稳定 point id（md5 前 8 字节转 uint64，与现有算法同源；同 origin 幂等覆盖）。"""
    return int.from_bytes(hashlib.md5(origin.encode("utf-8")).digest()[:8], "big")


def ensure_collection() -> None:
    client = get_client()
    if not client.collection_exists(settings.qdrant_collection):
        client.create_collection(
            collection_name=settings.qdrant_collection,
            vectors_config=models.VectorParams(size=1024, distance=models.Distance.COSINE),
        )
        print(f"[qdrant] 已创建 collection {settings.qdrant_collection}", flush=True)


def recreate_collection() -> None:
    """清空并重建新 collection（仅全量重建时使用；绝不操作其他 collection）。"""
    client = get_client()
    if client.collection_exists(settings.qdrant_collection):
        client.delete_collection(settings.qdrant_collection)
    ensure_collection()


def upsert_points(points: Iterable[PointStruct]) -> None:
    client = get_client()
    batch: list[PointStruct] = []
    for p in points:
        batch.append(p)
        if len(batch) >= 200:
            client.upsert(collection_name=settings.qdrant_collection, points=batch, wait=True)
            batch.clear()
    if batch:
        client.upsert(collection_name=settings.qdrant_collection, points=batch, wait=True)


def existing_ids() -> set[int]:
    """拉取 collection 中已存在的 point id 集合（用于断点续传跳过已入库点）。"""
    client = get_client()
    if not client.collection_exists(settings.qdrant_collection):
        return set()
    ids: set[int] = set()
    records, offset = client.scroll(
        collection_name=settings.qdrant_collection,
        limit=2000, with_payload=False, with_vectors=False,
    )
    while records:
        ids.update(r.id for r in records)
        if offset is None:
            break
        records, offset = client.scroll(
            collection_name=settings.qdrant_collection,
            limit=2000, with_payload=False, with_vectors=False, offset=offset,
        )
    return ids


def delete_by_post_ids(post_ids: Iterable[int]) -> int:
    """按 postID 删除该帖所有点（私密化/删除清理）。返回实际删除点数。"""
    client = get_client()
    ids = [int(i) for i in post_ids]
    if not ids:
        return 0
    f = Filter(must=[FieldCondition(key="postID", match=models.MatchAny(any=ids))])
    before = client.count(collection_name=settings.qdrant_collection, count_filter=f, exact=True).count
    client.delete(
        collection_name=settings.qdrant_collection,
        points_selector=models.FilterSelector(filter=f),
        wait=True,
    )
    after = client.count(collection_name=settings.qdrant_collection, count_filter=f, exact=True).count
    return before - after
