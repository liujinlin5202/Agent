#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""MVP 实验：发帖时分区内检索 + DeepSeek 回答（只读实验版）。

安全边界：
- 集市 MySQL 只做 SELECT，不写任何数据；
- Qdrant 只查询现有 collection `market_content`，不新建、不写入；
- 不修改服务器上任何现有文件/服务；
- 全部配置来自本目录 .env。

用法：
  python3 assist_mvp.py --samples samples.json
  python3 assist_mvp.py --partition 学习交流 --title "..." --content "..."
  python3 assist_mvp.py --json ...     # 输出机器可读 JSON
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from typing import Any

import pymysql
import requests
from dotenv import load_dotenv

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
load_dotenv(os.path.join(BASE_DIR, ".env"))

# ---------------- 配置 ----------------
DEEPSEEK_URL = os.environ["DEEPSEEK_BASE_URL"].rstrip("/") + "/chat/completions"
DEEPSEEK_KEY = os.environ["DEEPSEEK_API_KEY"]
DEEPSEEK_MODEL = os.environ["DEEPSEEK_MODEL"]

EMBED_URL = os.environ["EMBEDDING_BASE_URL"].rstrip("/") + "/embeddings"
EMBED_KEY = os.environ["EMBEDDING_API_KEY"]
EMBED_MODEL = os.environ["EMBEDDING_MODEL"]

QDRANT_URL = os.environ["QDRANT_URL"].rstrip("/")
QDRANT_COLLECTION = os.environ["QDRANT_COLLECTION"]

MYSQL_CFG = dict(
    host=os.environ["MYSQL_HOST"],
    port=int(os.environ["MYSQL_PORT"]),
    user=os.environ["MYSQL_USER"],
    password=os.environ["MYSQL_PASSWORD"],
    database=os.environ["MYSQL_DATABASE"],
    charset="utf8mb4",
    cursorclass=pymysql.cursors.DictCursor,
)

PARTITIONS = set(
    "学习交流,实习就业,宿舍生活,技术分享,期末资料,生活日常,社团活动,组团捞人,课程专区,其他".split(",")
)

POST_URL = "https://<MARKET_DOMAIN>/new/postdetail/{pid}"

# ---------------- 关键词提取（简化自 rag-lab query_logic.py） ----------------
STOPWORDS = {
    "有没有", "有没有关于", "有没有一些", "什么", "怎么", "如何", "是否", "可以", "应该", "需要",
    "分别", "相关", "一下", "这个", "那个", "一些", "一个", "有没有人", "大概", "比较", "请问",
    "要不要",
}
DOMAIN_TERMS = [
    "转专业", "保研", "就业", "读研", "考研", "实习", "科研", "项目", "老师", "导师", "实验室",
    "课程", "选课", "避雷", "实训", "软工", "绩点", "竞赛", "简历", "面试", "大一", "大二",
    "大三", "大四", "主动", "帮助", "本科生", "宿舍", "食堂", "社团", "组队", "高数", "英语",
    "期末", "复习", "兼职", "志愿者", "比赛",
]


def tokenize_keywords(query: str, limit: int = 6) -> list[str]:
    cleaned = re.sub(r"[，。！？、；：,.!?;:\n\t]+", " ", query.strip())
    found: list[str] = []
    for term in DOMAIN_TERMS:
        if term in cleaned and term not in found:
            found.append(term)
    for term in re.findall(r"[A-Za-z0-9_+-]{2,}", cleaned):
        if term not in found:
            found.append(term)
    if len(found) < 3:
        for chunk in re.findall(r"[一-鿿]{2,6}", cleaned):
            if chunk in STOPWORDS:
                continue
            if any(chunk in t or t in chunk for t in found):
                continue
            found.append(chunk)
            if len(found) >= limit:
                break
    return found[:limit]


# ---------------- MySQL：分区内关键词检索（只读） ----------------
def _kw(kw: str) -> str:
    return kw.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def mysql_keyword_search(conn: pymysql.connections.Connection,
                         partition: str, keywords: list[str]) -> dict[int, float]:
    """帖子标题/正文 + 回复 + 楼中楼关键词命中，返回 {postID: 关键词分}。"""
    scores: dict[int, float] = {}
    with conn.cursor() as cur:
        for kw in keywords:
            like = f"%{_kw(kw)}%"
            # 帖子标题/正文（标题命中权重更高）
            cur.execute(
                "SELECT postID, title LIKE %s AS in_title, ptext LIKE %s AS in_text "
                "FROM posts WHERE `partition`=%s AND is_private=0",
                (like, like, partition),
            )
            for row in cur.fetchall():
                s = 2.0 if row["in_title"] else 0.0
                s += 1.0 if row["in_text"] else 0.0
                if s > 0:
                    scores[row["postID"]] = scores.get(row["postID"], 0.0) + s
            # 一级回复
            cur.execute(
                "SELECT c.ptargetID AS postID FROM pcomments c "
                "JOIN posts p ON p.postID=c.ptargetID "
                "WHERE p.`partition`=%s AND p.is_private=0 AND c.pctext LIKE %s",
                (partition, like),
            )
            for row in cur.fetchall():
                scores[row["postID"]] = min(scores.get(row["postID"], 0.0) + 0.5, 6.0)
            # 楼中楼
            cur.execute(
                "SELECT c.ptargetID AS postID FROM ccomments cc "
                "JOIN pcomments c ON c.pcommentID=cc.ctargetID "
                "JOIN posts p ON p.postID=c.ptargetID "
                "WHERE p.`partition`=%s AND p.is_private=0 AND cc.cctext LIKE %s",
                (partition, like),
            )
            for row in cur.fetchall():
                scores[row["postID"]] = min(scores.get(row["postID"], 0.0) + 0.5, 6.0)
    return scores


# ---------------- Embedding + Qdrant 向量检索（只读现有 collection） ----------------
def embed(text: str) -> list[float]:
    resp = requests.post(
        EMBED_URL,
        headers={"Authorization": f"Bearer {EMBED_KEY}"},
        json={"model": EMBED_MODEL, "input": text},
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()["data"][0]["embedding"]


def qdrant_search(vector: list[float], limit: int = 15) -> list[tuple[int, float]]:
    """返回 [(postID, score)]，同帖多 chunk 取最高分。"""
    resp = requests.post(
        f"{QDRANT_URL}/collections/{QDRANT_COLLECTION}/points/search",
        json={"vector": vector, "limit": limit, "with_payload": True},
        timeout=30,
    )
    resp.raise_for_status()
    best: dict[int, float] = {}
    for hit in resp.json().get("result", []):
        pid = (hit.get("payload") or {}).get("postID")
        if not pid:
            continue
        best[pid] = max(best.get(pid, 0.0), float(hit.get("score", 0.0)))
    return sorted(best.items(), key=lambda x: x[1], reverse=True)


def filter_by_partition(conn: pymysql.connections.Connection,
                        post_ids: list[int], partition: str) -> list[int]:
    """现有 collection 无 partition 字段，向量命中后用 MySQL 反查分区过滤。"""
    if not post_ids:
        return []
    fmt = ",".join(["%s"] * len(post_ids))
    with conn.cursor() as cur:
        cur.execute(
            f"SELECT postID FROM posts WHERE postID IN ({fmt}) "
            f"AND `partition`=%s AND is_private=0",
            (*post_ids, partition),
        )
        return [r["postID"] for r in cur.fetchall()]


# ---------------- 取帖子详情 + 高赞回复/楼中楼（组装上下文） ----------------
def post_context(conn: pymysql.connections.Connection, post_id: int) -> dict[str, Any]:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT postID, title, ptext, heat FROM posts WHERE postID=%s AND is_private=0",
            (post_id,),
        )
        post = cur.fetchone()
        if not post:
            return {}
        cur.execute(
            "SELECT pcommentID, pctext, like_num FROM pcomments WHERE ptargetID=%s "
            "ORDER BY like_num DESC, pcommentID DESC LIMIT 2",
            (post_id,),
        )
        replies = cur.fetchall()
        for r in replies:
            cur.execute(
                "SELECT cctext FROM ccomments WHERE ctargetID=%s "
                "ORDER BY like_num DESC, ccommentID DESC LIMIT 2",
                (r["pcommentID"],),
            )
            r["subs"] = [s["cctext"] for s in cur.fetchall()]
    return {
        "postID": post["postID"],
        "title": post["title"] or "",
        "text": (post["ptext"] or "")[:400],
        "heat": float(post["heat"] or 0.0),
        "replies": [
            {"text": (r["pctext"] or "")[:200], "likes": int(r["like_num"] or 0)}
            for r in replies
        ],
    }


# ---------------- DeepSeek 回答 ----------------
SYSTEM_PROMPT = """你是软工集市的站内智能助手。用户刚在【{partition}】分区发了一篇帖子，我们检索了该分区内的历史讨论（帖子、回复、楼中楼）供你参考。

严格规则：
1. 只依据【检索材料】回答；材料不足以回答时，直接说"站内暂时没有找到相关讨论"，禁止编造帖子内容、发帖人或结论。
2. 每个观点若来自某条材料，须用 [n] 形式标注来源编号。
3. 禁止泄露任何用户个人信息；匿名内容可引用观点，但不得提及发帖人身份。
4. 先给结论，再给依据，简洁分点；最后以"相关帖子"小节列出帖子标题与链接（用材料里给出的链接）。
5. 不要复述用户刚发的帖子本身，只输出站内相关讨论的结论。"""


def ask_deepseek(partition: str, title: str, content: str, context_blocks: list[dict]) -> str:
    if not context_blocks:
        return f"站内【{partition}】分区暂时没有找到与这篇帖子相关的历史讨论。"
    lines = []
    for i, b in enumerate(context_blocks, 1):
        lines.append(
            f"[{i}] 帖子《{b['title']}》｜分区：{partition}｜热度：{b['heat']:.1f}\n"
            f"正文片段：{b['text']}"
        )
        for r in b["replies"]:
            lines.append(f"其中回复（{r['likes']}赞）：{r['text']}")
            for s in r.get("subs", []):
                lines.append(f"其中楼中楼：{s}")
        lines.append(f"链接：{POST_URL.format(pid=b['postID'])}")
    material = "\n\n".join(lines)
    user_msg = (
        f"用户刚发的帖子：\n标题：{title}\n正文：{content[:500]}\n\n"
        f"【检索材料】\n{material}"
    )
    resp = requests.post(
        DEEPSEEK_URL,
        headers={"Authorization": f"Bearer {DEEPSEEK_KEY}"},
        json={
            "model": DEEPSEEK_MODEL,
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT.format(partition=partition)},
                {"role": "user", "content": user_msg},
            ],
            "temperature": 0.2,
            "max_tokens": 1024,
        },
        timeout=60,
    )
    resp.raise_for_status()
    return resp.json()["choices"][0]["message"]["content"].strip()


# ---------------- 主流程 ----------------
def run(partition: str, title: str, content: str,
        exclude_post_id: int | None = None, top_n: int = 5) -> dict[str, Any]:
    t0 = time.time()
    if partition not in PARTITIONS:
        return {"error": f"未知分区：{partition}", "ok": False}

    conn = pymysql.connect(**MYSQL_CFG)
    try:
        # 1. 关键词提取
        keywords = tokenize_keywords(f"{title} {content}")
        # 2. MySQL 关键词检索（分区内）
        kw_scores = mysql_keyword_search(conn, partition, keywords)
        # 3. 向量检索（现有 collection 只读）→ 反查分区过滤
        vec = embed(f"{title}\n{content[:300]}")
        vec_hits = qdrant_search(vec)
        kept = filter_by_partition(conn, [p for p, _ in vec_hits], partition)
        vec_scores = {p: s for p, s in vec_hits if p in kept}
        # 4. 合并打分：向量 0.45 + 关键词 0.35 + 热度 0.20
        all_ids = set(kw_scores) | set(vec_scores)
        if exclude_post_id:
            all_ids.discard(exclude_post_id)
        kw_max = max(kw_scores.values()) if kw_scores else 1.0
        with conn.cursor() as cur:
            heat_map: dict[int, float] = {}
            if all_ids:
                fmt = ",".join(["%s"] * len(all_ids))
                cur.execute(
                    f"SELECT postID, heat FROM posts WHERE postID IN ({fmt})",
                    tuple(all_ids),
                )
                heat_map = {r["postID"]: float(r["heat"] or 0.0) for r in cur.fetchall()}
        heat_max = max(heat_map.values()) if heat_map else 1.0
        ranked = []
        for pid in all_ids:
            v = vec_scores.get(pid, 0.0)
            k = kw_scores.get(pid, 0.0) / kw_max
            h = heat_map.get(pid, 0.0) / heat_max
            score = 0.45 * v + 0.35 * min(k, 1.0) + 0.20 * min(h, 1.0)
            ranked.append((pid, score, v, k, h))
        ranked.sort(key=lambda x: x[1], reverse=True)
        top = [pid for pid, *_ in ranked[:top_n]]
        # 5. 组装上下文（帖子正文 + 高赞回复 + 楼中楼）
        blocks = [post_context(conn, pid) for pid in top]
        blocks = [b for b in blocks if b]
        # 6. DeepSeek 回答
        answer = ask_deepseek(partition, title, content, blocks)
        return {
            "ok": True,
            "partition": partition,
            "keywords": keywords,
            "meta": {
                "mysql_hits": len(kw_scores),
                "qdrant_hits": len(vec_hits),
                "qdrant_partition_kept": len(kept),
                "top": len(blocks),
                "took_ms": int((time.time() - t0) * 1000),
            },
            "answer": answer,
            "references": [
                {
                    "postID": b["postID"],
                    "title": b["title"],
                    "heat": round(b["heat"], 1),
                    "url": POST_URL.format(pid=b["postID"]),
                    "snippet": b["text"][:80],
                }
                for b in blocks
            ],
        }
    finally:
        conn.close()


# ---------------- CLI ----------------
def pretty_print(r: dict[str, Any]) -> None:
    if not r.get("ok"):
        print("错误：", r.get("error"))
        return
    print("=" * 60)
    print(f"分区：{r['partition']}    关键词：{'、'.join(r['keywords'])}")
    m = r["meta"]
    print(f"检索：MySQL关键词命中 {m['mysql_hits']} 帖 | 向量命中 {m['qdrant_hits']} 帖 "
          f"(分区过滤后 {m['qdrant_partition_kept']}) | 最终取 {m['top']} 帖 | 耗时 {m['took_ms']}ms")
    print("-" * 60)
    print("【AI 回答】")
    print(r["answer"])
    print("-" * 60)
    print("【引用】")
    for ref in r["references"]:
        print(f"[{ref['postID']}] 《{ref['title']}》 heat={ref['heat']}  {ref['url']}")
    print("=" * 60)


def main() -> None:
    ap = argparse.ArgumentParser(description="MVP: 分区内检索 + DeepSeek 回答（只读实验）")
    ap.add_argument("--samples", help="样例 JSON 文件路径")
    ap.add_argument("--partition")
    ap.add_argument("--title")
    ap.add_argument("--content")
    ap.add_argument("--exclude-post-id", type=int, default=None)
    ap.add_argument("--json", action="store_true", help="输出 JSON")
    args = ap.parse_args()

    cases: list[dict] = []
    if args.samples:
        with open(args.samples, encoding="utf-8") as f:
            cases = json.load(f)
    elif args.partition and args.title and args.content:
        cases = [{"partition": args.partition, "title": args.title, "content": args.content}]
    else:
        # 交互模式：人工检测
        print("人工检测模式（分区可选：学习交流/实习就业/宿舍生活/技术分享/"
              "期末资料/生活日常/社团活动/组团捞人/课程专区/其他）")
        print("标题输入空行即退出。Ctrl+C 也可退出。\n")
        while True:
            try:
                partition = input("分区: ").strip()
                title = input("标题: ").strip()
                if not title:
                    print("已退出。")
                    break
                content = input("正文: ").strip()
                print()
                r = run(partition, title, content)
                pretty_print(r)
                print()
            except (EOFError, KeyboardInterrupt):
                print("\n已退出。")
                break
        return

    outputs = []
    for c in cases:
        r = run(c["partition"], c["title"], c["content"],
                exclude_post_id=args.exclude_post_id)
        outputs.append({"input": c, **r})
        if args.json:
            continue
        pretty_print(r)

    if args.json:
        print(json.dumps(outputs, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
