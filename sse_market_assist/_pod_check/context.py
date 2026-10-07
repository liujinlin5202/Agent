# -*- coding: utf-8 -*-
"""检索材料上下文拼装：候选帖正文片段 + 高赞回复 + 楼中楼 + 评论级证据（不含任何用户身份信息）。

评论证据（app/retrieve.comment_evidence，语义+关键词命中）并入对应候选帖；
父帖不在候选之列的"孤儿评论"提升为补充候选（编号 [6..8]），保证 [n] 与 references 一一对应。
"""
from __future__ import annotations

from typing import Any

from app import db
from app.web_search import WebSearchResult, format_web_results

SNIPPET_LEN = 600      # 帖子正文截断
REPLY_LEN = 300        # 回复内容截断
SUBREPLY_LEN = 150     # 楼中楼截断（控制 prompt 长度）
TOP_REPLIES = 2        # 每帖取高赞回复数
TOP_SUBREPLIES = 3     # 每条回复取楼中楼数
RELATED_PER_POST = 2   # 每帖并入的相关评论/楼中楼证据数
ORPHAN_CAP = 3         # 孤儿评论父帖提升为补充候选上限（编号 [6..8]）

COMMENT_LABEL = {"reply": "相关回复", "subreply": "相关楼中楼"}


def build_context(candidates: list[dict[str, Any]],
                  evidence: list[dict[str, Any]] | None = None) -> list[dict[str, Any]]:
    """为每个候选帖补充 snippet 与 replies，并并入相关评论证据；
    孤儿评论（父帖不在候选内）提升为补充候选。返回富化后的候选列表（同时供 references 使用）。"""
    evidence = evidence or []
    by_post: dict[int, list[dict[str, Any]]] = {}
    for ev in evidence:
        by_post.setdefault(ev["postID"], []).append(ev)

    enriched: list[dict[str, Any]] = []
    known = {c["postID"] for c in candidates}
    with db.query() as cur:
        for c in candidates:
            cur.execute("SELECT ptext FROM posts WHERE postID=%s AND is_private=0", (c["postID"],))
            row = cur.fetchone()
            ptext = (row["ptext"] or "") if row else ""
            ptext = ptext[:SNIPPET_LEN] + "…" if len(ptext) > SNIPPET_LEN else ptext

            cur.execute(
                "SELECT pcommentID, pctext, like_num FROM pcomments WHERE ptargetID=%s "
                "ORDER BY like_num DESC, pcommentID DESC LIMIT %s",
                (c["postID"], TOP_REPLIES),
            )
            replies: list[dict[str, Any]] = []
            seen_ids: set[int] = set()
            for r in cur.fetchall():
                seen_ids.add(int(r["pcommentID"]))
                cur.execute(
                    "SELECT cctext FROM ccomments WHERE ctargetID=%s "
                    "ORDER BY like_num DESC LIMIT %s",
                    (r["pcommentID"], TOP_SUBREPLIES),
                )
                subs = [s["cctext"][:SUBREPLY_LEN] for s in cur.fetchall() if s["cctext"]]
                replies.append({
                    "text": (r["pctext"] or "")[:REPLY_LEN],
                    "subs": subs,
                    "comment_id": int(r["pcommentID"]),
                })
            # 相关评论证据（按分排序；与高赞回复去重后并入，标注来源标签）
            evs = sorted(by_post.get(c["postID"], []), key=lambda e: e["score"], reverse=True)
            for e in evs:
                if len(replies) >= TOP_REPLIES + RELATED_PER_POST:
                    break
                if e["commentID"] in seen_ids:
                    continue
                seen_ids.add(e["commentID"])
                replies.append({
                    "text": (e["text"] or "")[:REPLY_LEN],
                    "subs": [],
                    "comment_id": e["commentID"],
                    "related": COMMENT_LABEL.get(e["doc_type"], "相关评论"),
                })
            enriched.append({**c, "snippet": ptext, "replies": replies})

        # 孤儿评论（父帖不在候选内）→ 父帖提升为补充候选
        orphans = [
            (pid, evs) for pid, evs in by_post.items()
            if pid not in known
        ]
        # 按证据最高分排序，最多 ORPHAN_CAP 个
        orphans.sort(key=lambda item: max(e["score"] for e in item[1]), reverse=True)
        if orphans:
            pid_list = [pid for pid, _ in orphans[:ORPHAN_CAP]]
            ph = ",".join(["%s"] * len(pid_list))
            cur.execute(
                f"SELECT postID, heat FROM posts WHERE postID IN ({ph}) AND is_private=0",
                tuple(pid_list),
            )
            heat_map = {int(r["postID"]): float(r["heat"] or 0) for r in cur.fetchall()}
            for pid, evs in orphans[:ORPHAN_CAP]:
                evs = sorted(evs, key=lambda e: e["score"], reverse=True)[:RELATED_PER_POST]
                enriched.append({
                    "postID": pid,
                    "title": evs[0]["title"] or f"帖子{pid}",
                    "heat": heat_map.get(pid, 0.0),
                    "snippet": "",
                    "comment_evidence": [
                        {"text": (e["text"] or "")[:REPLY_LEN],
                         "related": COMMENT_LABEL.get(e["doc_type"], "相关评论")}
                        for e in evs
                    ],
                    "replies": [],
                    "partition": evs[0].get("partition") or "",
                })
    return enriched


def format_context(enriched: list[dict[str, Any]]) -> str:
    """拼成 [n] 编号的检索材料文本，供 LLM prompt 使用。"""
    blocks: list[str] = []
    for i, c in enumerate(enriched, start=1):
        lines = [f"[{i}] 帖子《{c['title']}》 分区:{c['partition']} 热度:{c['heat']:.1f}"]
        if c["snippet"]:
            lines.append(f"正文片段：{c['snippet']}")
        if c.get("comment_evidence"):
            for ce in c["comment_evidence"]:
                lines.append(f"{ce['related']}：{ce['text']}")
        for r in c["replies"]:
            label = r.get("related") or "高赞回复"
            lines.append(f"{label}：{r['text']}")
            for s in r["subs"]:
                lines.append(f"楼中楼：{s}")
        lines.append(f"链接：https://<MARKET_DOMAIN>/new/postdetail/{c['postID']}")
        blocks.append("\n".join(lines))
    return "\n\n".join(blocks)


def format_web_context(web_results: list[WebSearchResult]) -> str:
    """将 WebSearch 结果格式化为带 🌐 标签的文本块，供 LLM prompt 使用。"""
    if not web_results:
        return ""
    lines = ["\n【站外搜索结果】（以下内容来自站外 Web 搜索，仅供参考，请注意甄别）"]
    for i, r in enumerate(web_results, start=1):
        lines.append(f"  🌐 [{i}] {r['title']}")
        lines.append(f"      摘要：{r['snippet']}")
        lines.append(f"      链接：{r['url']}")
    lines.append("（站外信息可能与站内讨论角度不同，引用时须标注'据站外资料'）\n")
    return "\n".join(lines)
