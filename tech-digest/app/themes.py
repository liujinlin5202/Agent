# -*- coding: utf-8 -*-
"""主题日：把「学生视角」从一个形容词变成每周固定的选题约束。

2026-09-13 用户决策：日报浏览量较 8 月腰斩且零互动，根因定位为内容（选题不贴学生 /
标题差 / 正文长且 AI 味重 / 形式单一）。主题日轮换是其中「选题」一项的落地：
每周固定三天给出明确选题方向，其余日交回 AI 自由判断——既保证学生相关性，
又不把五天都锁死在同一类内容上（形式单一正是要修的问题之一）。

周三原计划为「秋招/招聘」，实测无任何数据源提供真实招聘信息，AI 只能编造
（违反零编造红线）→ 降级为「技能与学习」，覆盖面试经验、学习路线、公开课等
真实存在的内容形态。
"""
from __future__ import annotations

from datetime import date

# weekday() → (主题日名, 选题引导语)。引导语直接拼进 AI prompt。
THEME_DAYS: dict[int, tuple[str, str]] = {
    0: ("工具与资源",
        "优先挑学生能立刻用起来的工具或资源：有免费额度/学生认证、课程作业或课设用得上、"
        "上手成本低。"
        "能一句话说清「拿它能干什么」的条目优先。"),
    2: ("技能与学习",
        "优先挑能写进简历、或直接帮到面试与学习的条目：学习路线、公开课、面试经验、"
        "能练手的具体技能（含开源项目的实操向更新）。"),
    4: ("开源项目精选",
        "优先从今日 GitHub Trending 里挑学生最用得上的项目，说清「拿它做什么」和"
        "「上手难不难」，而不是只念一遍项目简介。"),
}


def _parse(day: str) -> date | None:
    try:
        return date.fromisoformat(day)
    except (ValueError, TypeError):
        return None


def theme_of(day: str) -> tuple[str, str] | None:
    """返回 (主题日名, 选题引导)；自由日或日期非法 → None。"""
    d = _parse(day)
    return THEME_DAYS.get(d.weekday()) if d else None


def theme_label(day: str) -> str:
    """主题日名（用于表现追踪表）；自由日 → ""。"""
    t = theme_of(day)
    return t[0] if t else ""
