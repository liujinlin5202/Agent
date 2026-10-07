# -*- coding: utf-8 -*-
"""发帖表现复盘：把 post_views 采样压成「每期一行」，并给出前后对比。

2026-09-13 新增。改内容之前先得能看见效果——日报发了几十期，浏览量一直靠肉眼翻
数据库，没有「同一口径下这期比上期好还是差」的答案。

口径（与 scripts/content_report.py sample 模式配套）：
  · daily 09:15 跑 → 给「昨天发的帖」记一次采样，正好是发布后 ~24 小时的浏览数；
  · h24 取发布次日那次采样，与「当前总浏览」分开看——总浏览含长尾
    （实测 9/11 那期 h24 只有 46，后来涨到 277），拿总数比会把长尾误当内容效果。

另有一条更早的历史基线（写这份代码时从 `pbrowses` 逐条浏览日志算出来的 h24）：
9/1-9/5 约 93-135，9/6-9/13 回落到 41-58。但 pbrowses 只记到 browse_num 的
35%-92%（疑似只记登录用户），**只能当相对参照，不能当准数**——所以正式口径
用 browse_num 采样，pbrowses 的数只用于回溯改版前的量级。
"""
from __future__ import annotations

from datetime import date, timedelta

from app.themes import theme_label

_WEEKDAY = ("周一", "周二", "周三", "周四", "周五", "周六", "周日")
_MISSING = "—"


def report_rows(samples: list[dict], titles: dict[str, str] | None = None) -> list[dict]:
    """采样明细 → 每期一行。samples 为 Store.view_samples() 的输出。

    h24：发布次日的那次采样（sample_day == 发布日 +1）。缺这次采样 → None，
    **不是 0**：「没测到」和「24 小时零浏览」是两回事，混起来复盘表会骗人。
    当前浏览：该期最新一次采样（含长尾）。
    """
    titles = titles or {}
    per: dict[str, dict[str, dict]] = {}
    for s_ in samples or []:
        per.setdefault(s_["day"], {})[s_["sample_day"]] = s_

    rows: list[dict] = []
    for day in sorted(per):
        snaps = per[day]
        cur = snaps[max(snaps)]
        d = _parse(day)
        h24 = snaps.get((d + timedelta(days=1)).isoformat()) if d else None
        rows.append({
            "day": day,
            "weekday": _WEEKDAY[d.weekday()] if d else "",
            "theme": theme_label(day) if d else "",
            "title": titles.get(day, ""),
            "h24": h24["browse"] if h24 else None,
            "browse": cur["browse"],
            "likes": cur["likes"],
            "comments": cur["comments"],
        })
    return rows


def _parse(day: str) -> date | None:
    try:
        return date.fromisoformat(day)
    except (ValueError, TypeError):
        return None


def _cell(text: str) -> str:
    """表格单元格：竖线会撑破 Markdown 表格。"""
    return (text or "").replace("|", "/").replace("\n", " ").strip()


def render_table(rows: list[dict]) -> str:
    """复盘表（Markdown）。"""
    if not rows:
        return "（暂无采样数据：先跑 `content_report.py sample`）"
    lines = ["| 日期 | 主题日 | 标题 | h24 浏览 | 当前浏览 | 赞 | 评 |",
             "|:--|:--|:--|--:|--:|--:|--:|"]
    for r in rows:
        h24 = _MISSING if r["h24"] is None else str(r["h24"])
        lines.append(f"| {r['day'][5:]} {r['weekday']} | {_cell(r['theme']) or _MISSING} "
                     f"| {_cell(r['title']) or _MISSING} | {h24} | {r['browse']} "
                     f"| {r['likes']} | {r['comments']} |")
    return "\n".join(lines)


def summary(rows: list[dict], split: str) -> str:
    """改版前后 h24 均值对比。split 为改版日（YYYY-MM-DD），前后各取一侧。

    只在两侧都至少有一期「测到 h24」的样本时给结论——样本不足时说样本不足，
    不拿半截数据下判断（这正是这次要避免的：凭感觉断定「掉了」）。
    """
    before = [r["h24"] for r in rows if r["day"] < split and r["h24"] is not None]
    after = [r["h24"] for r in rows if r["day"] >= split and r["h24"] is not None]
    if not before or not after:
        return f"h24 均值对比（改版日 {split}）：样本不足（改版前 {len(before)} 期 / 改版后 {len(after)} 期）"
    a, b = sum(before) / len(before), sum(after) / len(after)
    arrow = "↑" if b > a else "↓"
    pct = abs(b - a) / a * 100 if a else 0.0
    return (f"h24 均值对比（改版日 {split}）：前 {a:.0f}（{len(before)} 期）"
            f" → 后 {b:.0f}（{len(after)} 期） {arrow}{pct:.0f}%")
