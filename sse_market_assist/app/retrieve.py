# -*- coding: utf-8 -*-
"""检索层：LLM 查询改写 + MySQL 关键词检索 + Qdrant 分区语义检索 + 合并重排。

- 所有 MySQL 查询参数化、只读；Qdrant 只查新 collection；
- 排序权重采用 rag-lab 基线（0.48 向量 / 0.27 关键词 / 0.08 热度）；
- partition=主页（默认发帖聚合分区）时放宽为跨全部分区检索（config.ALL_PARTITIONS_MARKER）。
"""
from __future__ import annotations

import re
from typing import Any

import httpx
from langchain_core.output_parsers import StrOutputParser
from langchain_core.prompts import ChatPromptTemplate
from qdrant_client.models import FieldCondition, Filter, MatchAny, MatchValue

from app import db
from app.config import ALL_PARTITIONS_MARKER, settings
from app.db import fetch_all
from app.embed import embed_texts
from app.llm import get_llm
from app.qdrant_store import get_client

# ---------------------------------------------------------------------------
# 1. 查询改写（LLM；失败回退原文）
# ---------------------------------------------------------------------------

REWRITE_PROMPT = ChatPromptTemplate.from_messages(
    [
        (
            "system",
            "你是站内检索的查询改写助手。根据用户准备发布的帖子，提炼 1~3 条用于检索站内历史讨论的简洁问句。"
            "要求：去掉寒暄与废话；保留具体名词（课程名、比赛名、地点、工具名等）；每条问句不超过 25 字。"
            "只输出问句，每行一条，不要编号、不要解释、不要引号。",
        ),
        ("user", "帖子标题：{title}\n帖子正文：{content}\n\n检索问句："),
    ]
)


def rewrite_queries(title: str, content: str) -> list[str]:
    """生成 1~3 条检索问句；LLM 不可用时回退为原文拼装（语义检索仍可用）。"""
    try:
        chain = REWRITE_PROMPT | get_llm() | StrOutputParser()
        raw = chain.invoke({"title": title, "content": content[:500]})
        queries = [
            re.sub(r"^[\s\d.、\-]+", "", line).strip().strip("\"'")
            for line in raw.splitlines()
        ]
        queries = [q for q in queries if len(q) >= 2]
        if queries:
            return queries[:3]
    except Exception as e:  # noqa: BLE001
        print("[retrieve] 查询改写失败，回退原文:", repr(e), flush=True)
    return [f"{title}。{content[:300]}"]


_STOPWORDS = {
    "请问", "大家", "怎么", "什么", "如何", "有没有", "是不是", "想问", "求助", "谢谢",
    "真的", "感觉", "就是", "然后", "这个", "那个", "一个", "一下", "知道", "可以",
    "我们", "你们", "他们", "自己", "现在", "还是", "但是", "因为", "所以", "如果",
    "大概", "应该", "需要", "或者", "以及", "关于", "非常", "比较", "已经", "不会",
}


def extract_keywords(text: str, limit: int = 6) -> list[str]:
    """从（改写后的）文本提取 MySQL 检索关键词：ASCII 词 + 中文 2~3 字切块（去停用词）。"""
    words: list[str] = []
    for m in re.findall(r"[A-Za-z0-9_+#.+-]{2,}", text):
        words.append(m.lower())
    cjk = re.sub(r"[^一-鿿]", " ", text)
    for w in re.split(r"\s+", cjk):
        if not w:
            continue
        for n in (2, 3):
            for i in range(len(w) - n + 1):
                seg = w[i : i + n]
                if seg in _STOPWORDS or any(s in seg for s in _STOPWORDS):
                    continue
                words.append(seg)
    seen: set[str] = set()
    out: list[str] = []
    for w in words:
        if w not in seen:
            seen.add(w)
            out.append(w)
    return out[:limit]


# ---------------------------------------------------------------------------
# 2. MySQL 关键词检索（分区限定；私密帖排除；主页=全分区）
# ---------------------------------------------------------------------------

def _esc(kw: str) -> str:
    """LIKE 通配符转义（关键词来自用户输入/LLM，防注入防误配）。"""
    return kw.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _is_all(partition: str) -> bool:
    """是否为跨全分区检索（主页=默认发帖聚合分区）。"""
    return partition == ALL_PARTITIONS_MARKER


def keyword_search(partition: str, keywords: list[str], exclude_post_id: int) -> dict[int, dict[str, Any]]:
    """分区内关键词检索（主页=全分区）。返回 {postID: {title, heat, score}}，score 未归一化（标题 2 / 正文 1 / 回复·楼中楼 0.5）。"""
    hits: dict[int, dict[str, Any]] = {}
    all_parts = _is_all(partition)
    with db.query() as cur:
        for kw in keywords:
            like = f"%{_esc(kw)}%"
            # 帖子：标题 LIKE 计 2，正文 LIKE 计 1
            if all_parts:
                sql = (
                    "SELECT postID, title, heat, "
                    "  (CASE WHEN title LIKE %s THEN 2 ELSE 0 END "
                    "   + CASE WHEN ptext LIKE %s THEN 1 ELSE 0 END) AS s "
                    "FROM posts WHERE is_private=0 AND postID!=%s "
                    "  AND (title LIKE %s OR ptext LIKE %s) "
                    "ORDER BY s DESC, heat DESC LIMIT 30"
                )
                params = (like, like, exclude_post_id, like, like)
            else:
                sql = (
                    "SELECT postID, title, heat, "
                    "  (CASE WHEN title LIKE %s THEN 2 ELSE 0 END "
                    "   + CASE WHEN ptext LIKE %s THEN 1 ELSE 0 END) AS s "
                    "FROM posts WHERE `partition`=%s AND is_private=0 AND postID!=%s "
                    "  AND (title LIKE %s OR ptext LIKE %s) "
                    "ORDER BY s DESC, heat DESC LIMIT 30"
                )
                params = (like, like, partition, exclude_post_id, like, like)
            cur.execute(sql, params)
            for row in cur.fetchall():
                d = hits.setdefault(
                    row["postID"], {"title": row["title"], "heat": float(row["heat"] or 0), "score": 0.0}
                )
                d["score"] = min(6.0, d["score"] + row["s"])
            # 一级回复：每条命中 0.5，每关键词上限 2
            if all_parts:
                sql = (
                    "SELECT c.ptargetID AS postID, p.title, p.heat, COUNT(*) AS cnt "
                    "FROM pcomments c JOIN posts p ON p.postID=c.ptargetID "
                    "WHERE p.is_private=0 AND p.postID!=%s AND c.pctext LIKE %s "
                    "GROUP BY c.ptargetID, p.title, p.heat LIMIT 30"
                )
                params = (exclude_post_id, like)
            else:
                sql = (
                    "SELECT c.ptargetID AS postID, p.title, p.heat, COUNT(*) AS cnt "
                    "FROM pcomments c JOIN posts p ON p.postID=c.ptargetID "
                    "WHERE p.`partition`=%s AND p.is_private=0 AND p.postID!=%s AND c.pctext LIKE %s "
                    "GROUP BY c.ptargetID, p.title, p.heat LIMIT 30"
                )
                params = (partition, exclude_post_id, like)
            cur.execute(sql, params)
            for row in cur.fetchall():
                d = hits.setdefault(
                    row["postID"], {"title": row["title"], "heat": float(row["heat"] or 0), "score": 0.0}
                )
                d["score"] = min(6.0, d["score"] + min(2.0, row["cnt"] * 0.5))
            # 楼中楼：每条命中 0.5，每关键词上限 1.5
            if all_parts:
                sql = (
                    "SELECT p.postID, p.title, p.heat, COUNT(*) AS cnt "
                    "FROM ccomments cc "
                    "JOIN pcomments c ON c.pcommentID=cc.ctargetID "
                    "JOIN posts p ON p.postID=c.ptargetID "
                    "WHERE p.is_private=0 AND p.postID!=%s AND cc.cctext LIKE %s "
                    "GROUP BY p.postID, p.title, p.heat LIMIT 30"
                )
                params = (exclude_post_id, like)
            else:
                sql = (
                    "SELECT p.postID, p.title, p.heat, COUNT(*) AS cnt "
                    "FROM ccomments cc "
                    "JOIN pcomments c ON c.pcommentID=cc.ctargetID "
                    "JOIN posts p ON p.postID=c.ptargetID "
                    "WHERE p.`partition`=%s AND p.is_private=0 AND p.postID!=%s AND cc.cctext LIKE %s "
                    "GROUP BY p.postID, p.title, p.heat LIMIT 30"
                )
                params = (partition, exclude_post_id, like)
            cur.execute(sql, params)
            for row in cur.fetchall():
                d = hits.setdefault(
                    row["postID"], {"title": row["title"], "heat": float(row["heat"] or 0), "score": 0.0}
                )
                d["score"] = min(6.0, d["score"] + min(1.5, row["cnt"] * 0.5))
    return hits


# ---------------------------------------------------------------------------
# 2.5 评论级证据检索（回复 + 楼中楼；信息密度核心）
# ---------------------------------------------------------------------------

COMMENT_DOC_TYPES = ("reply", "subreply")
KEYWORD_HIT_SCORE = 0.6  # 关键词精确命中评论文本的基线分（与向量分同量纲比较）
KEYWORD_HIT_LIMIT = 3    # 每个关键词对 回复/楼中楼 各取命中数


def comment_evidence(partition: str, queries: list[str], keywords: list[str],
                     exclude_post_id: int) -> list[dict[str, Any]]:
    """评论级检索：Qdrant 语义（doc_type=reply/subreply）+ MySQL 关键词取实际命中文本。

    返回按相关度合并的评论证据列表（已截断，供 context 拼装）：
    {postID, title, partition, doc_type, commentID, text, like_num, source, score}
    语义与关键词同一评论命中时取两者较大分。
    """
    evidence: dict[str, dict[str, Any]] = {}
    all_parts = _is_all(partition)

    def add(key: str, item: dict[str, Any]) -> None:
        old = evidence.get(key)
        if old is None:
            evidence[key] = item
            return
        if item["score"] > old["score"]:
            old["score"] = item["score"]
        if not old["like_num"] and item["like_num"]:
            old["like_num"] = item["like_num"]
        old["source"] = f"{old['source']}+{item['source']}"

    # 1) 语义检索：分区 + doc_type 限定
    try:
        vecs = embed_texts(queries[:3])
        client = get_client()
        must: list[FieldCondition] = [
            FieldCondition(key="doc_type", match=MatchAny(any=list(COMMENT_DOC_TYPES)))
        ]
        if not all_parts:
            must.insert(0, FieldCondition(key="partition", match=MatchValue(value=partition)))
        # 排除自身帖子：must_not + MatchValue 替代 MatchExcept
        # （新版 qdrant-client 将 MatchExcept.except_ 收紧为 keyword 字符串，int postID 会校验失败）
        must_not = (
            [FieldCondition(key="postID", match=MatchValue(value=exclude_post_id))]
            if exclude_post_id else None
        )
        for vec in vecs:
            res = client.query_points(
                collection_name=settings.qdrant_collection,
                query=vec,
                limit=settings.vector_limit,
                score_threshold=settings.vector_threshold,
                query_filter=Filter(must=must, must_not=must_not),
                with_payload=["postID", "doc_type", "commentID", "text", "title", "like_num", "partition"],
            )
            for p in res.points:
                pl = p.payload or {}
                dt = str(pl.get("doc_type") or "")
                if dt not in COMMENT_DOC_TYPES:
                    continue
                key = f"{dt}:{pl.get('commentID')}"
                add(key, {
                    "postID": int(pl["postID"]), "title": pl.get("title") or "",
                    "partition": str(pl.get("partition") or ALL_PARTITIONS_MARKER),
                    "doc_type": dt, "commentID": int(pl["commentID"]),
                    "text": pl.get("text") or "", "like_num": float(pl.get("like_num") or 0),
                    "source": "vector", "score": float(p.score),
                })
    except Exception as e:  # noqa: BLE001
        print("[retrieve] 评论语义检索失败（仅关键词）:", repr(e), flush=True)

    # 2) MySQL 关键词：评论/楼中楼文本精确命中（每条取实际文本）
    with db.query() as cur:
        for kw in keywords:
            like = f"%{_esc(kw)}%"
            if all_parts:
                sql = (
                    "SELECT c.pcommentID AS cid, c.pctext AS txt, c.like_num, "
                    "       p.postID, p.title, p.`partition` "
                    "FROM pcomments c JOIN posts p ON p.postID=c.ptargetID "
                    "WHERE p.is_private=0 AND p.postID!=%s AND c.pctext LIKE %s "
                    "ORDER BY c.like_num DESC, c.pcommentID DESC LIMIT %s"
                )
                params = (exclude_post_id, like, KEYWORD_HIT_LIMIT)
            else:
                sql = (
                    "SELECT c.pcommentID AS cid, c.pctext AS txt, c.like_num, "
                    "       p.postID, p.title, p.`partition` "
                    "FROM pcomments c JOIN posts p ON p.postID=c.ptargetID "
                    "WHERE p.`partition`=%s AND p.is_private=0 AND p.postID!=%s AND c.pctext LIKE %s "
                    "ORDER BY c.like_num DESC, c.pcommentID DESC LIMIT %s"
                )
                params = (partition, exclude_post_id, like, KEYWORD_HIT_LIMIT)
            cur.execute(sql, params)
            for row in cur.fetchall():
                add(f"reply:{row['cid']}", {
                    "postID": int(row["postID"]), "title": row["title"] or "",
                    "partition": str(row["partition"] or ALL_PARTITIONS_MARKER),
                    "doc_type": "reply", "commentID": int(row["cid"]),
                    "text": row["txt"] or "", "like_num": float(row["like_num"] or 0),
                    "source": "keyword", "score": KEYWORD_HIT_SCORE,
                })
            if all_parts:
                sql = (
                    "SELECT cc.ccommentID AS cid, cc.cctext AS txt, cc.like_num, "
                    "       p.postID, p.title, p.`partition` "
                    "FROM ccomments cc "
                    "JOIN pcomments c ON c.pcommentID=cc.ctargetID "
                    "JOIN posts p ON p.postID=c.ptargetID "
                    "WHERE p.is_private=0 AND p.postID!=%s AND cc.cctext LIKE %s "
                    "ORDER BY cc.like_num DESC, cc.ccommentID DESC LIMIT %s"
                )
                params = (exclude_post_id, like, KEYWORD_HIT_LIMIT)
            else:
                sql = (
                    "SELECT cc.ccommentID AS cid, cc.cctext AS txt, cc.like_num, "
                    "       p.postID, p.title, p.`partition` "
                    "FROM ccomments cc "
                    "JOIN pcomments c ON c.pcommentID=cc.ctargetID "
                    "JOIN posts p ON p.postID=c.ptargetID "
                    "WHERE p.`partition`=%s AND p.is_private=0 AND p.postID!=%s AND cc.cctext LIKE %s "
                    "ORDER BY cc.like_num DESC, cc.ccommentID DESC LIMIT %s"
                )
                params = (partition, exclude_post_id, like, KEYWORD_HIT_LIMIT)
            cur.execute(sql, params)
            for row in cur.fetchall():
                add(f"subreply:{row['cid']}", {
                    "postID": int(row["postID"]), "title": row["title"] or "",
                    "partition": str(row["partition"] or ALL_PARTITIONS_MARKER),
                    "doc_type": "subreply", "commentID": int(row["cid"]),
                    "text": row["txt"] or "", "like_num": float(row["like_num"] or 0),
                    "source": "keyword", "score": KEYWORD_HIT_SCORE,
                })
    ranked = sorted(evidence.values(), key=lambda d: d["score"], reverse=True)
    return ranked[: settings.evidence_limit]


# ---------------------------------------------------------------------------
# 3. Qdrant 语义检索（分区过滤 + 排除自身帖子）
# ---------------------------------------------------------------------------

def vector_search(partition: str, queries: list[str], exclude_post_id: int = 0) -> tuple[dict[int, float], int]:
    """按 postID 聚合取最大相似分。返回 ({postID: score}, 命中点数)。

    embedding 不可用（欠费/限流）时降级返回空结果——关键词检索仍可支撑回答。
    """
    try:
        vecs = embed_texts(queries[:3])
    except Exception as e:  # noqa: BLE001
        print("[retrieve] 向量检索降级为仅关键词（embedding 不可用）:", repr(e), flush=True)
        return {}, 0
    client = get_client()
    scores: dict[int, float] = {}
    hit_count = 0
    must: list[FieldCondition] = []
    if not _is_all(partition):
        must.append(FieldCondition(key="partition", match=MatchValue(value=partition)))
    must_not = (
        [FieldCondition(key="postID", match=MatchValue(value=exclude_post_id))]
        if exclude_post_id else None
    )
    for vec in vecs:
        res = client.query_points(
            collection_name=settings.qdrant_collection,
            query=vec,
            limit=settings.vector_limit,
            score_threshold=settings.vector_threshold,
            query_filter=Filter(must=must, must_not=must_not),
            with_payload=["postID"],
        )
        for p in res.points:
            hit_count += 1
            pid = int(p.payload["postID"])
            if pid not in scores or p.score > scores[pid]:
                scores[pid] = float(p.score)
    return scores, hit_count


# ---------------------------------------------------------------------------
# 4. 合并重排（rag-lab 基线权重）
# ---------------------------------------------------------------------------

def _post_meta(post_ids: list[int]) -> dict[int, dict[str, Any]]:
    """从 MySQL 统一取标题/热度/分区（向量命中点可能来自回复，热度以帖子表为准）。"""
    if not post_ids:
        return {}
    ph = ",".join(["%s"] * len(post_ids))
    rows = db.fetch_all(
        f"SELECT postID, title, heat, `partition` FROM posts WHERE postID IN ({ph}) AND is_private=0",
        tuple(post_ids),
    )
    return {
        int(r["postID"]): {
            "title": r["title"], "heat": float(r["heat"] or 0),
            "partition": str(r["partition"] or ""),
        }
        for r in rows
    }


def merge_rank(mysql_scores: dict[int, float], vector_scores: dict[int, float],
               meta: dict[int, dict[str, Any]], top_n: int = 5,
               default_partition: str = "") -> list[dict[str, Any]]:
    """score = 0.48·向量分 + 0.27·关键词分(归一化) + 0.08·热度分(归一化)。"""
    post_ids = set(mysql_scores) | set(vector_scores)
    if not post_ids:
        return []
    max_mysql = max(mysql_scores.values()) if mysql_scores else 0.0
    heats = [meta[p]["heat"] for p in post_ids if p in meta]
    max_heat, min_heat = (max(heats), min(heats)) if heats else (0.0, 0.0)
    cands: list[dict[str, Any]] = []
    for pid in post_ids:
        vec = vector_scores.get(pid, 0.0)
        kw = mysql_scores[pid] / max_mysql if (pid in mysql_scores and max_mysql > 0) else 0.0
        heat = meta.get(pid, {}).get("heat", 0.0)
        heat_n = (heat - min_heat) / (max_heat - min_heat) if max_heat > min_heat else 0.0
        cands.append(
            {
                "postID": pid,
                "title": meta.get(pid, {}).get("title", ""),
                "heat": heat,
                "partition": meta.get(pid, {}).get("partition") or default_partition,
                "score": settings.w_vector * vec + settings.w_keyword * kw + settings.w_heat * heat_n,
            }
        )
    cands.sort(key=lambda c: c["score"], reverse=True)
    return cands[:top_n]


# ---------------------------------------------------------------------------
# 6. group-rag /kb/search 适配器（2026-09-22；ASSIST_RETRIEVAL_PROVIDER=rag 启用）
# ---------------------------------------------------------------------------

_RAG_SOURCE_RE = re.compile(r"^market_(\d+)\.txt$")
_RAG_K_MAX = 5  # RAG 服务端 rerank_top_n=5，请求侧同上限


def _rag_search(query: str, k: int) -> list[dict[str, Any]]:
    """调用 group-rag /kb/search（BM25+向量 RRF 融合+自适应精排）。

    失败向上抛（502/超时等），由 api 层 try/except 走降级路径；接口无副作用可安全重试。
    """
    resp = httpx.post(
        f"{settings.rag_base_url}/kb/search",
        json={"query": query, "k": k, "category": "market"},
        timeout=settings.rag_timeout,
    )
    resp.raise_for_status()
    return list(resp.json().get("results") or [])


def retrieve_via_rag(partition: str, title: str, content: str,
                     exclude_post_id: int = 0, limit: int | None = None) -> dict[str, Any]:
    """单次 RAG 调用替代本地 MySQL LIKE + Qdrant 两路检索。

    返回与 retrieve() 同构。要点（见接口文档）：
    - score=1/rank 只是相对排序，非绝对相关度；top1 恒 1.0 → 不会触发 WebSearch，
      仅 RAG 无命中时走站外补充，语义自洽；
    - RAG 不做权限过滤（可能含私密/已删帖）→ 回查主站 MySQL 落实可见性与分区过滤，
      分区内全滤空时放宽为全分区重查一次（集市多数帖子挂「主页」）；
    - 切片已融合一级评论 → evidence 留空，评论上下文由 build_context 从主站库补齐。
    """
    query = f"{title}。{content[:300]}"
    k = max(1, min(limit or settings.top_k, _RAG_K_MAX))
    results = _rag_search(query, k)

    parsed: list[tuple[int, int, str]] = []
    for r in results:
        m = _RAG_SOURCE_RE.match(str(r.get("source") or ""))
        if m:
            pid = int(m.group(1))
            if pid != exclude_post_id:
                rank = int(r.get("rank") or len(parsed) + 1)
                parsed.append((pid, rank, str(r.get("content") or "")))

    rows: dict[int, dict[str, Any]] = {}
    if parsed:
        ids = [pid for pid, _, _ in parsed]

        def _visible(with_partition: bool) -> dict[int, dict[str, Any]]:
            ph = ",".join(["%s"] * len(ids))
            sql = (f"SELECT postID, title, heat, `partition` FROM posts "
                   f"WHERE postID IN ({ph}) AND is_private=0")
            params: list[Any] = list(ids)
            if with_partition and not _is_all(partition):
                sql += " AND `partition`=%s"
                params.append(partition)
            return {row["postID"]: row for row in fetch_all(sql, tuple(params))}

        rows = _visible(True)
        if not rows and not _is_all(partition):
            # 集市多数帖子挂在「主页」：分区内全滤空时放宽为全分区，保住 RAG 强命中
            rows = _visible(False)

    candidates = [
        {
            "postID": pid,
            "title": rows[pid]["title"],
            "heat": float(rows[pid]["heat"] or 0),
            "partition": rows[pid]["partition"],
            "score": round(1.0 / rank, 4) if rank else 0.0,
            "rag_content": frag[:500],
        }
        for pid, rank, frag in parsed
        if pid in rows
    ]
    return {
        "queries": [query],
        "candidates": candidates,
        "evidence": [],
        "mysql_hits_count": len(rows),
        "qdrant_hits_count": len(results),
    }


# ---------------------------------------------------------------------------
# 5. 主入口
# ---------------------------------------------------------------------------

def retrieve(partition: str, title: str, content: str,
             exclude_post_id: int = 0, limit: int | None = None) -> dict[str, Any]:
    """主检索入口。provider=rag 优先 group-rag 融合链路，调用失败本次回退自建链路；
    每次请求都先试 RAG——服务恢复即自动切回，无粘性状态。
    （2026-09-24 group-rag 反复 502：原样上抛会让检索整体归零，新帖拿不到回答。）"""
    if settings.retrieval_provider == "rag":
        try:
            return retrieve_via_rag(partition, title, content, exclude_post_id, limit)
        except Exception as e:  # noqa: BLE001  RAG 宕机/超时 → 本地检索兜底
            print("[retrieve] group-rag 调用失败，本次回退本地检索:", repr(e), flush=True)
    return _retrieve_local(partition, title, content, exclude_post_id, limit)


def _retrieve_local(partition: str, title: str, content: str,
                    exclude_post_id: int = 0, limit: int | None = None) -> dict[str, Any]:
    """自建检索链路（MySQL LIKE 关键词 + Qdrant 向量 + 线性融合），2026-09-22 起为 local provider。"""
    queries = rewrite_queries(title, content) if settings.llm_rewrite else [f"{title}。{content[:300]}"]
    keywords = extract_keywords(" ".join(queries))
    mysql_hits = keyword_search(partition, keywords, exclude_post_id)
    vector_scores, qdrant_hits = vector_search(partition, queries, exclude_post_id)
    evidence = comment_evidence(partition, queries, keywords, exclude_post_id)
    meta = _post_meta(list(set(mysql_hits) | set(vector_scores)))
    candidates = merge_rank(
        {pid: d["score"] for pid, d in mysql_hits.items()},
        vector_scores, meta, top_n=limit or settings.top_k, default_partition=partition,
    )
    return {
        "queries": queries,
        "candidates": candidates,
        "evidence": evidence,
        "mysql_hits_count": len(mysql_hits),
        "qdrant_hits_count": qdrant_hits,
    }
