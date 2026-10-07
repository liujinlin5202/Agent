# -*- coding: utf-8 -*-
"""发帖标题：确定性校验 + 兜底阶梯。

2026-09-13 用户决策背景：日报浏览量较 8 月腰斩，帖子在信息流里没有任何点击理由——
旧标题是「M/D + 头条」的报告体（如「9/12 OpenAI Agent 对 Rub…」），既无钩子又被
30 字硬截断。改为钩子式短标题后，标题质量不能只靠 prompt 约束：模型天然爱写
「今日速览」「XX 动态」这类任何一天都成立、在信息流里等于没写的词，也因此
必须由代码做最后一道校验（同 daily_ai 的防幻觉校验思路）。

四类硬约束：
  ① ≤30 字（集市后端 API 上限，见 publisher.publish_md）
  ② 不带日期前缀（日期在正文与列表页已有，标题里是纯浪费）
  ③ 不含废话词（速览/动态/一览/盘点/…——任何一天都成立 = 没有信息量）
  ④ 与近 7 期标题不像（字符二元组 Dice 相似度，防「同一句话换个数」）
"""
from __future__ import annotations

import re

from app.emoji import strip_emoji

MAX_TITLE_CHARS = 30

BANNED_WORDS = ("速览", "动态", "一览", "盘点", "汇总", "简报", "综述",
                "观察", "生态爆发", "重磅")

# 相似度 ≥ 该值视为与近期标题重复
SIMILARITY_MAX = 0.5

# 日期前缀：9/12、2026-09-12、9月12日
_DATE_PREFIX_RE = re.compile(
    r"^\s*(?:\d{4}\s*[-/年]\s*\d{1,2}\s*[-/月]\s*\d{1,2}\s*日?"
    r"|\d{1,2}\s*[/.\-月]\s*\d{1,2}\s*日?)\s*[·:：|、,，.．-]?\s*")
# 装饰性包裹与序号
_WRAP_PAIRS = [("《", "》"), ("「", "」"), ("『", "』"), ("【", "】"),
               ('"', '"'), ("'", "'")]
_NUMBERING_RE = re.compile(r"^\s*(?:第\s*)?\d{1,2}\s*[、.．)）:：]\s*")
_SPACE_RE = re.compile(r"\s+")


def clean(text: str) -> str:
    """去包裹符号/序号/日期前缀/emoji，压空格；超 30 字截断（省略号占一格）。"""
    t = strip_emoji(_SPACE_RE.sub(" ", (text or "").strip()))
    for left, right in _WRAP_PAIRS:
        if len(t) >= 2 and t.startswith(left) and t.endswith(right):
            t = t[1:-1].strip()
    t = _NUMBERING_RE.sub("", t)
    t = _DATE_PREFIX_RE.sub("", t)
    t = _SPACE_RE.sub(" ", t).strip()
    if len(t) > MAX_TITLE_CHARS:
        t = t[:MAX_TITLE_CHARS - 1].rstrip("，,。、；;：: !！?？") + "…"
    return t


def _bigrams(text: str) -> set[str]:
    s = _SPACE_RE.sub("", text or "")
    if len(s) < 2:
        return {s} if s else set()
    return {s[i:i + 2] for i in range(len(s) - 1)}


def similarity(a: str, b: str) -> float:
    """字符二元组 Dice 系数（0-1）。空格不参与比较，避免「这 3 件」vs「这 4 件」。"""
    ga, gb = _bigrams(a), _bigrams(b)
    if not ga or not gb:
        return 0.0
    return 2 * len(ga & gb) / (len(ga) + len(gb))


def is_usable(cand: str, recent: list[str]) -> tuple[bool, str]:
    """候选标题是否可直接用。返回 (可用, 不可用原因)。"""
    raw = (cand or "").strip()
    if not raw:
        return False, "空标题"
    if len(raw) > MAX_TITLE_CHARS:
        return False, f"超过 {MAX_TITLE_CHARS} 字（{len(raw)} 字）"
    if _DATE_PREFIX_RE.match(raw):
        return False, "带日期前缀"
    for w in BANNED_WORDS:
        if w in raw:
            return False, f"含禁用词「{w}」"
    for r in recent or []:
        if similarity(raw, r) >= SIMILARITY_MAX:
            return False, f"与近 7 期标题过于相似（{r}）"
    return True, ""


def pick(candidates: list[str], recent: list[str], fallback: str = "") -> str:
    """兜底阶梯，**按候选顺序逐个判**（模型给的顺序就是它的推荐序，别跳过去捡后面的）：

    ① 完全合规 → 用它；
    ② 只栽在「可确定性修复」的问题（日期前缀/包裹符号/序号）且长度本来就合规
       → clean() 后重判，过了就用；
    ③ 全部候选都栽在「与近 7 期太像」→ 复用其中一句（好过退化成模板）；
    ④ fallback（调用方给的兜底，清洗后直接用，不再校验——它是最后一道，
       校验掉就等于没标题）；⑤ 空串。

    ② 是接 main.py 时补的：prompt 已禁日期前缀，但模型仍会带；原实现一律拒收，
    3 个候选全带日期时就掉到兜底头条（实测最平庸的那类），而日期恰恰是 clean()
    能 100% 去干净的东西。超长候选**不**走 ② —— 靠截断「修」出来的是断句，
    不如换下一个候选。
    """
    cands = [c for c in (candidates or []) if c]
    for c in cands:
        if is_usable(c, recent)[0]:
            return clean(c)
        raw = c.strip()
        fixed = clean(raw)
        if fixed != raw and len(raw) <= MAX_TITLE_CHARS and is_usable(fixed, recent)[0]:
            return fixed
    for c in cands:
        if is_usable(c, [])[0]:      # 硬约束全过 → 仅是撞了近期标题，仍可用
            return clean(c)
    return clean(fallback) if fallback else ""
