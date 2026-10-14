# -*- coding: utf-8 -*-
"""评审：rubric prompt 构造 + LLM 输出解析 + 确定性加精门槛。

设计裁决（业界实践对本项目的落法，2026-10-14）：
  · LLM 只负责语义判断（打分 + 引战识别），**加精与否在代码里判**——
    门槛可调参可审计，改阈值不用重跑 prompt
  · 引战一票否决放在门槛层：flame_bait=true 直接拒，无论分数多高
  · 0-10 小量表（业界人机一致性最高的是小量表 pointwise 打分）
"""
from __future__ import annotations

import json
import logging
from typing import Iterable

log = logging.getLogger("post-curator.judge")

SYSTEM_PROMPT = """你是校园软件工程社区「软工集市」的精华帖评审，负责判断帖子是否值得加精。

两类值得加精的帖子：
1. 高价值问询：正文提出真实、具体的问题或求助，价值集中在评论区——即使正文本身
   信息量低，只要评论区有认真解答、有价值的讨论，comment_value 就应给高分。
2. 高价值内容帖：正文本身就是干货——教程、经验总结、资源分享、踩坑复盘、
   有信息量的观点讨论。

一票否决（无论互动数据多高，flame_bait 必须为 true）：
- 引战/钓鱼/煽动对立：话题故意挑起争议、评论区互撕、无建设性输出
- 纯灌水刷帖、无意义水贴、广告引流

注意：互动数据高 ≠ 价值高。引战帖常常标题党+评论量虚高，请凭语义判断，不要被数据带偏。

评分（0-10 整数）：
- content_value：正文本身的信息价值（干货密度、原创性、对社区同学的实际帮助）
- comment_value：评论区的信息价值（认真解答、有效讨论、多元观点；灌水与互撕不计）

只输出一个 JSON 对象，不要任何多余文字：
{"post_type": "问答/经验分享/资源分享/讨论/求助/灌水/引战/广告/其他 之一",
 "flame_bait": false, "content_value": 0, "comment_value": 0, "reason": "一句话理由"}"""


def build_user_prompt(post: dict, comments: Iterable[dict],
                      post_char_cap: int, comment_char_cap: int) -> str:
    lines = [
        f"标题：{post.get('title') or '（无标题）'}",
        f"分区：{post.get('partition') or '-'}    标签：{post.get('tag') or '-'}",
        f"发布时间：{post.get('post_time') or '-'}",
        f"数据：浏览 {post.get('browse_num', 0)} / 点赞 {post.get('like_num', 0)}"
        f" / 点踩 {post.get('dislike_num', 0)} / 评论 {post.get('comment_num', 0)}"
        f" / 热度 {post.get('heat', 0)}",
        "",
        "【正文】",
        (post.get("ptext") or "（无正文）")[:post_char_cap],
    ]
    cmts = list(comments)
    if cmts:
        lines.append("")
        lines.append(f"【评论区（按点赞排序，前 {len(cmts)} 条）】")
        for i, c in enumerate(cmts, 1):
            text = (c.get("text") or "").strip()[:comment_char_cap]
            lines.append(f"{i}. [赞 {c.get('like_num', 0)}] {text or '（空）'}")
    else:
        lines.append("")
        lines.append("【评论区】（暂无评论）")
    return "\n".join(lines)


def parse_verdict(text: str | None) -> dict | None:
    """从 LLM 输出里稳健地抠出 JSON（容忍 codefence 与前后杂字）。"""
    if not text:
        return None
    s = text.strip()
    if s.startswith("```"):
        s = s.strip("`")
        if s.lower().startswith("json"):
            s = s[4:]
        s = s.strip()
    try:
        data = json.loads(s)
    except ValueError:
        # 平衡花括号扫描：从第一个 { 到与之配对的 }
        start = s.find("{")
        if start < 0:
            return None
        depth = 0
        for i in range(start, len(s)):
            if s[i] == "{":
                depth += 1
            elif s[i] == "}":
                depth -= 1
                if depth == 0:
                    try:
                        data = json.loads(s[start:i + 1])
                        break
                    except ValueError:
                        return None
        else:
            return None
    if not isinstance(data, dict):
        return None
    return data


def _score(v, lo=0, hi=10) -> int | None:
    """分数收敛：接受 int/float/数字字符串，钳到 [lo, hi]；非法 → None。"""
    try:
        n = int(float(v))
    except (TypeError, ValueError):
        return None
    return max(lo, min(hi, n))


def gate(verdict: dict | None, content_threshold: float,
         comment_threshold: float) -> tuple[bool, str, dict]:
    """确定性门槛：返回 (featured, reason, 归一化判定)。

    LLM 输出不可解析 → 不加精（宁可漏放不可错放，漏掉的还有明日复审与全量模式兜底）。
    """
    if verdict is None:
        return False, "评审输出不可解析，按不加精处理", {
            "post_type": "unknown", "flame_bait": None,
            "content_value": None, "comment_value": None}
    flame = verdict.get("flame_bait")
    flame_bait = flame is True or str(flame).lower() == "true"
    cv = _score(verdict.get("content_value"))
    mv = _score(verdict.get("comment_value"))
    post_type = str(verdict.get("post_type") or "其他")[:20]
    reason = str(verdict.get("reason") or "")[:200]
    if cv is None or mv is None:
        return False, f"评分缺失不可判，按不加精处理：{reason}", {
            "post_type": post_type, "flame_bait": flame_bait,
            "content_value": cv, "comment_value": mv}
    if flame_bait:
        return False, f"引战/灌水一票否决：{reason}", {
            "post_type": post_type, "flame_bait": True,
            "content_value": cv, "comment_value": mv}
    if cv >= content_threshold or mv >= comment_threshold:
        parts = []
        if cv >= content_threshold:
            parts.append(f"正文价值 {cv}")
        if mv >= comment_threshold:
            parts.append(f"评论区价值 {mv}")
        return True, f"加精：{'，'.join(parts)}（{reason}）", {
            "post_type": post_type, "flame_bait": False,
            "content_value": cv, "comment_value": mv}
    return False, f"未达线（正文 {cv}/{content_threshold:g}，评论区 {mv}/{comment_threshold:g}）：{reason}", {
        "post_type": post_type, "flame_bait": False,
        "content_value": cv, "comment_value": mv}
