# -*- coding: utf-8 -*-
"""FastAPI 路由：health / match / refs / post / followup / feedback / admin ingest。

鉴权说明（与文档 4.4 一致）：
- match/refs/post/followup/feedback 为站内服务，一期经 Nginx 暴露后由前端在登录态下调用；
  服务自身只做分区白名单校验 + 每 IP 限流，接口无任何敏感写操作、结果不含隐私；
- admin/ingest 必须携带 ASSIST_ADMIN_TOKEN（内部运维接口）。
"""
from __future__ import annotations

import json
import time
from collections import defaultdict, deque
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel, Field

from app import context as ctx_mod
from app.config import ALL_PARTITIONS_MARKER, PARTITIONS, settings
from app.db import (
    execute_write,
    ensure_assist_responses_table,
    fetch_one,
)
from app.generate import build_answer, build_followup_answer
from app.qdrant_store import get_client
from app.retrieve import retrieve
from app.sync import cleanup_private, incremental_sync
from app.web_search import web_search

router = APIRouter()

NO_HIT_ANSWER = "站内暂时没有找到相关讨论。"


class MatchRequest(BaseModel):
    partition: str
    title: str = Field(..., max_length=200)
    content: str = Field(..., max_length=20000)
    postID: Optional[int] = Field(default=0, description="刚发布的帖子 ID，用于排除自身（可为 0）")
    limit: Optional[int] = Field(default=None, ge=1, le=8)


class FollowUpRequest(BaseModel):
    postID: int
    question: str = Field(..., max_length=2000)
    history: list[dict] = Field(default_factory=list,
                                description='[{"role": "user"|"assistant", "content": "..."}]')


class FeedbackRequest(BaseModel):
    postID: int
    vote: str = Field(..., pattern=r"^(up|down)$")


# ---------------------------------------------------------------------------
# 简易限流：每 IP 5 次 / 10 秒（内网服务防误用；上线后前端侧另有登录态约束）
# ---------------------------------------------------------------------------

_rate: dict[str, deque[float]] = defaultdict(deque)


def rate_limit(request: Request) -> None:
    now = time.time()
    q = _rate[request.client.host or "unknown"]
    while q and now - q[0] > 10:
        q.popleft()
    if len(q) >= 5:
        raise HTTPException(status_code=429, detail="请求过于频繁，请稍后再试")
    q.append(now)


# ---------------------------------------------------------------------------
# 辅助：持久化至 assist_responses 表
# ---------------------------------------------------------------------------

def _save_response(partition: str, title: str, post_id: int,
                   answer: str | None, references: list[dict],
                   sources_type: str,
                   web_query: str, web_results: list[dict]) -> None:
    """将 AI 回答持久化到 MySQL（静默失败，不阻塞响应）。"""
    try:
        execute_write(
            """INSERT INTO assist_responses
               (postID, `partition`, title, answer, references_json,
                sources_type, web_search_query, web_results_json)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
               ON DUPLICATE KEY UPDATE
               answer=VALUES(answer), references_json=VALUES(references_json),
               sources_type=VALUES(sources_type),
               web_search_query=VALUES(web_search_query),
               web_results_json=VALUES(web_results_json),
               updated_at=NOW()""",
            (post_id, partition, title, answer,
             json.dumps(references, ensure_ascii=False),
             sources_type,
             web_query or None,
             json.dumps(web_results, ensure_ascii=False) if web_results else None),
        )
    except Exception as e:  # noqa: BLE001
        print(f"[api] 持久化失败（不影响响应）: {repr(e)}", flush=True)


# ---------------------------------------------------------------------------
# 核心实现
# ---------------------------------------------------------------------------

def _match_impl(partition: str, title: str, content: str,
                post_id: int, limit: Optional[int],
                generate_answer: bool = True) -> dict[str, Any]:
    # 详情页按需生成场景：帖子已入库，按 postID 回查真实分区覆盖请求值，
    # 前端无需知道分区（查不到/已删帖时沿用请求值）。
    if post_id and post_id > 0:
        try:
            row = fetch_one("SELECT `partition` FROM posts WHERE postID=%s", (post_id,))
            if row and row.get("partition"):
                partition = str(row["partition"])
        except Exception as e:  # noqa: BLE001
            print(f"[api] 按 postID 取分区失败，沿用请求分区: {repr(e)}", flush=True)
    if partition not in PARTITIONS and partition != ALL_PARTITIONS_MARKER:
        return {"ok": False, "error": f"非法分区：{partition}（可选：{'、'.join(PARTITIONS)}；主页=全分区检索）"}
    t0 = time.time()
    try:
        result = retrieve(partition, title, content, exclude_post_id=post_id or 0)
        degraded = False
    except Exception as e:  # noqa: BLE001  检索链路兜底：任何异常均不 500，返回空结果
        print("[api] 检索失败，降级为空结果:", repr(e), flush=True)
        result = {"queries": [], "candidates": [], "evidence": [],
                  "mysql_hits_count": 0, "qdrant_hits_count": 0}
        degraded = True

    cands = result["candidates"][: limit or settings.top_k]
    all_parts = partition == ALL_PARTITIONS_MARKER

    # ---- WebSearch 触发：站内候选不足（无候选或最高候选分低于阈值）时补充站外资料 ----
    sources_type = "local"
    web_results: list[dict] = []
    web_query = ""
    weak = (not cands) or (cands[0].get("score", 0.0) < settings.web_search_min_score)
    if settings.web_search_enabled and weak and not degraded:
        try:
            queries = result.get("queries") or []
            web_query = (queries[0] if queries else f"{title} {content[:100]}").strip()
            if web_query:
                raw_results = web_search(web_query)
                if raw_results:
                    web_results = [dict(r) for r in raw_results]
                    sources_type = "mixed" if cands else "web"
        except Exception as e:  # noqa: BLE001
            print(f"[api] WebSearch 失败（降级忽略）: {repr(e)}", flush=True)

    meta = {
        "mysql_hits": result["mysql_hits_count"],
        "qdrant_hits": result["qdrant_hits_count"],
        "evidence": len(result.get("evidence") or []),
        "used": len(cands),
        "scope": "all" if all_parts else "single",
        "took_ms": int((time.time() - t0) * 1000),
        "queries": result["queries"],
        "degraded": degraded,
        "sources_type": sources_type,
    }

    if not cands and not web_results:
        resp = {
            "ok": True,
            "data": {
                "answer": NO_HIT_ANSWER if generate_answer else None,
                "references": [],
                "meta": meta,
            },
        }
        _save_response(partition, title, post_id,
                       resp["data"]["answer"], [],
                       sources_type, web_query, web_results)
        return resp

    # 构建上下文（站内 + 站外）
    enriched = ctx_mod.build_context(cands, result.get("evidence") or [])
    context_parts = [ctx_mod.format_context(enriched)]
    if web_results:
        context_parts.append(ctx_mod.format_web_context(web_results))
    full_context = "\n".join(context_parts)

    answer = None
    if generate_answer:
        try:
            answer = build_answer(partition, title, content, full_context)
        except Exception as e:  # noqa: BLE001  LLM 双通道均故障 → 降级：只给引用列表
            print("[api] 回答生成失败，降级为仅引用:", repr(e), flush=True)

    references = [
        {
            "postID": c["postID"],
            "title": c["title"],
            "url": f"https://<MARKET_DOMAIN>/new/postdetail/{c['postID']}",
            "snippet": (c["snippet"]
                        or (c.get("comment_evidence") or [{}])[0].get("text", "")[:120]
                        or ""),
            "heat": round(c["heat"], 1),
        }
        for c in enriched
    ]
    meta["took_ms"] = int((time.time() - t0) * 1000)

    resp = {"ok": True, "data": {"answer": answer, "references": references, "meta": meta}}

    # 持久化到 MySQL（静默）
    _save_response(partition, title, post_id, answer, references,
                   sources_type, web_query, web_results)

    return resp


# ---------------------------------------------------------------------------
# 路由
# ---------------------------------------------------------------------------

@router.get("/health")
def health() -> dict[str, Any]:
    try:
        client = get_client()
        info = client.get_collection(settings.qdrant_collection)
        points = info.points_count
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "service": "sse-market-assist", "error": repr(e)}
    return {
        "ok": True,
        "service": "sse-market-assist",
        "version": "0.1.0",
        "collection": settings.qdrant_collection,
        "points": points,
    }


@router.get("/")
def index() -> dict[str, Any]:
    return {"service": "sse-market-assist",
            "docs": "/health, /api/v1/assist/match, /api/v1/assist/refs, "
                    "/api/v1/assist/post/{id}, /api/v1/assist/followup, /api/v1/assist/feedback"}


@router.post("/api/v1/assist/match", dependencies=[Depends(rate_limit)])
def match(req: MatchRequest) -> dict[str, Any]:
    """核心接口：分区检索 + AI 回答。"""
    return _match_impl(req.partition, req.title, req.content, req.postID or 0, req.limit)


@router.post("/api/v1/assist/refs", dependencies=[Depends(rate_limit)])
def refs(req: MatchRequest) -> dict[str, Any]:
    """降级接口：只检索不生成（LLM 故障时前端可单独调用）。"""
    return _match_impl(req.partition, req.title, req.content, req.postID or 0, req.limit,
                       generate_answer=False)


@router.get("/api/v1/assist/post/{postID}")
def get_post_assist(postID: int) -> dict[str, Any]:
    """读取已保存的 AI 回答（供帖子详情页置顶展示）。"""
    try:
        row = fetch_one("SELECT * FROM assist_responses WHERE postID=%s", (postID,))
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": repr(e)}
    if not row:
        return {"ok": True, "data": None}
    return {
        "ok": True,
        "data": {
            "answer": row["answer"],
            "references": json.loads(row["references_json"] or "[]"),
            "sources_type": row["sources_type"],
            "web_search_query": row["web_search_query"],
            "feedback_upvotes": row["feedback_upvotes"],
            "feedback_downvotes": row["feedback_downvotes"],
            "created_at": row["created_at"].isoformat() if row.get("created_at") else None,
        },
    }


@router.post("/api/v1/assist/followup", dependencies=[Depends(rate_limit)])
def followup(req: FollowUpRequest) -> dict[str, Any]:
    """追问接口：基于已有回答继续对话。"""
    # 读取原始回答上下文
    try:
        row = fetch_one(
            "SELECT answer, references_json FROM assist_responses WHERE postID=%s",
            (req.postID,),
        )
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": f"查询失败: {repr(e)}"}
    if not row:
        return {"ok": False, "error": "该帖子暂无 AI 回答记录，无法追问"}

    original_answer = row["answer"] or ""
    refs = json.loads(row["references_json"] or "[]")
    refs_text = "\n".join(
        f"  [{i+1}] {r.get('title', '')} → {r.get('url', '')}"
        for i, r in enumerate(refs[:5])
    )
    original_context = f"【原始回答】\n{original_answer}\n\n【相关引用】\n{refs_text}"

    try:
        answer = build_followup_answer(
            post_title="",
            original_context=original_context,
            references=refs,
            history=req.history,
            question=req.question,
        )
    except Exception as e:  # noqa: BLE001
        print(f"[api] 追问生成失败: {repr(e)}", flush=True)
        return {"ok": False, "error": "AI 回答生成失败，请稍后再试"}

    return {"ok": True, "data": {"answer": answer}}


@router.post("/api/v1/assist/feedback")
def feedback(req: FeedbackRequest) -> dict[str, Any]:
    """提交反馈：👍 或 👎。"""
    field = "feedback_upvotes" if req.vote == "up" else "feedback_downvotes"
    try:
        execute_write(
            f"UPDATE assist_responses SET {field} = {field} + 1, updated_at=NOW() WHERE postID=%s",
            (req.postID,),
        )
    except Exception as e:  # noqa: BLE001
        print(f"[api] 反馈记录失败: {repr(e)}", flush=True)
        return {"ok": False, "error": "记录失败"}
    return {"ok": True, "data": {"vote": req.vote}}


@router.post("/api/v1/assist/admin/ingest")
async def admin_ingest(request: Request) -> dict[str, Any]:
    """运维接口：手动触发增量同步 + 私密清理（要求内部 token）。"""
    body = await request.json()
    if body.get("token") != settings.admin_token:
        raise HTTPException(status_code=403, detail="token 错误")
    result = await run_in_threadpool(_do_ingest)
    return {"ok": True, "data": result}


def _do_ingest() -> dict[str, Any]:
    stats = incremental_sync()
    removed = cleanup_private()
    return {"incremental": stats, "private_removed": removed}
