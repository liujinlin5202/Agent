# -*- coding: utf-8 -*-
"""离线表现复盘：同窗口快照、当前累计值和历史近似值分别标明口径。"""
from __future__ import annotations

from datetime import date, datetime, timedelta
from statistics import median

from app.themes import theme_label

_WEEKDAY = ("周一", "周二", "周三", "周四", "周五", "周六", "周日")
_MISSING = "—"
_BASIS = {"h24_window": "21–27h 窗口", "next_day_approximate": "次日近似",
          "missing": "缺失采样"}
_TIME_BASIS = {"local_confirmation": "本地确认"}
_UNAVAILABLE = "曝光：不可用；CTR：不可用；读完率：不可用（无完成事件）。"


def report_rows(samples: list[dict], titles: dict[str | int, str] | None = None,
                post_meta: dict[int, dict] | None = None) -> list[dict]:
    """每帖一行，兼容不带发布 metadata 的历史调用。

    h24 保留旧字段名：优先从 21–27h 窗口选最接近 24h 的快照，距离相同选
    较早快照；缺时区或发布时间才回退到次日近似。明确超出窗口不回退。
    同帖同采样日保留最后一条，与 post_views 的覆盖语义一致。
    likes/comments/browse 是当前总数；比率只用 h24_* 同一快照的计数。
    """
    titles = titles or {}
    post_meta = post_meta or {}
    per: dict[int, dict[str, dict]] = {}
    for s_ in samples or []:
        per.setdefault(s_["post_id"], {})[s_["sample_day"]] = s_

    rows: list[dict] = []
    for post_id, snaps in per.items():
        values = list(snaps.values())
        day = min(snap["day"] for snap in values)
        if all(_aware_time(snap.get("sampled_at")) is not None for snap in values):
            cur = max(values, key=lambda snap: _aware_time(snap["sampled_at"]))
        else:
            cur = snaps[max(snaps)]
        d = _parse(day)
        meta = post_meta.get(post_id, {})
        published = _aware_time(meta.get("published_at"))
        candidates = [(snap, _age_hours(published, snap)) for snap in values]
        window = [(snap, age) for snap, age in candidates if age is not None and 21 <= age <= 27]
        h24, age, basis = None, None, "missing"
        if window:
            h24, age = min(window, key=lambda pair: (abs(pair[1] - 24), pair[1]))
            basis = "h24_window"
        elif d:
            next_day = snaps.get((d + timedelta(days=1)).isoformat())
            if next_day and _age_hours(published, next_day) is None:
                h24 = next_day
                basis = "next_day_approximate"
        views = h24["browse"] if h24 else None
        likes = h24["likes"] if h24 else None
        comments = h24["comments"] if h24 else None
        rows.append({
            "post_id": post_id,
            "day": day,
            "weekday": _WEEKDAY[d.weekday()] if d else "",
            "theme": theme_label(day) if d else "",
            "title": titles.get(post_id, titles.get(day, "")),
            "h24": views,
            "h24_likes": likes,
            "h24_comments": comments,
            "like_rate": likes / views if views and likes is not None else None,
            "comment_rate": comments / views if views and comments is not None else None,
            "observation_age_hours": age,
            "sample_basis": basis,
            "sampled_at": h24.get("sampled_at") if h24 else None,
            "published_at": meta.get("published_at"),
            "time_basis": meta.get("time_basis"),
            "content_version": meta.get("content_version"),
            "article_type": meta.get("article_type"),
            "learning_question": meta.get("learning_question"),
            "browse": cur["browse"],
            "likes": cur["likes"],
            "comments": cur["comments"],
            "current_sampled_at": cur.get("sampled_at"),
            "current_observation_age_hours": _age_hours(published, cur),
        })
    return sorted(rows, key=lambda row: (row["day"], row["post_id"]))


def _aware_time(value: str | None) -> datetime | None:
    try:
        timestamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return timestamp if timestamp.utcoffset() is not None else None
    except (AttributeError, TypeError, ValueError):
        return None


def _age_hours(published: datetime | None, snap: dict) -> float | None:
    sampled = _aware_time(snap.get("sampled_at"))
    if published is None or sampled is None:
        return None
    return (sampled - published).total_seconds() / 3600


def _parse(day: str) -> date | None:
    try:
        return date.fromisoformat(day)
    except (ValueError, TypeError):
        return None


def _cell(text: str) -> str:
    """表格单元格：竖线会撑破 Markdown 表格。"""
    return (text or "").replace("|", "/").replace("\n", " ").strip()


def _value(value) -> str:
    return _MISSING if value is None else str(value)


def _ratio(numerator: int | None, denominator: int | None) -> str:
    if numerator is None or denominator is None:
        return _MISSING
    rate = f"{numerator / denominator:.1%}" if denominator else "不可用"
    return f"{numerator}/{denominator}（{rate}）"


def render_table(rows: list[dict]) -> str:
    """复盘表（Markdown）。"""
    if not rows:
        return "（暂无采样数据 / 无本地数据）\n\n" + _UNAVAILABLE
    lines = ["| 日期 | post_id | 历史主题日 | 标题 | 内容版本 | 文章类型 | 学习问题 | 采样依据 | 时间依据 | 实际小时 | 约24h浏览次数 | 同窗口赞/浏览 | 同窗口评/浏览 | 当前浏览次数 | 当前赞 | 当前评 | 当前采样小时 |",
             "|:--|--:|:--|:--|:--|:--|:--|:--|:--|--:|--:|:--|:--|--:|--:|--:|--:|"]
    for r in rows:
        age = r.get("observation_age_hours")
        current_age = r.get("current_observation_age_hours")
        cells = [f"{r['day'][5:]} {r['weekday']}", _value(r.get("post_id")),
                 _cell(r["theme"]) or _MISSING, _cell(r["title"]) or _MISSING,
                 _cell(r.get("content_version")) or "未记录",
                 _cell(r.get("article_type")) or _MISSING,
                 _cell(r.get("learning_question")) or _MISSING,
                 _BASIS.get(r.get("sample_basis"), "次日近似"),
                 _cell(_TIME_BASIS.get(r.get("time_basis"), r.get("time_basis"))) or "未记录",
                 f"{age:.1f}" if age is not None else _MISSING, _value(r["h24"]),
                 _ratio(r.get("h24_likes"), r["h24"]), _ratio(r.get("h24_comments"), r["h24"]),
                 _value(r["browse"]), _value(r["likes"]), _value(r["comments"]),
                 f"{current_age:.1f}" if current_age is not None else _MISSING]
        lines.append("| " + " | ".join(cells) + " |")
    lines += ["", "h24 字段表示约24h窗口；次日近似缺乏可靠时间差，缺失采样保留为空。浏览次数未确认为去重人数。",
              _UNAVAILABLE]
    return "\n".join(lines)


def summary(rows: list[dict], split: str) -> str:
    """按前后、内容版本、采样依据和时间依据分别描述，不推断因果或完成率。"""
    lines = [f"表现描述（改版日 {split}）：整体篇数={len(rows)}。"]
    measured_counts = []
    for label, side in (("改版前", [r for r in rows if r["day"] < split]),
                        ("改版后", [r for r in rows if r["day"] >= split])):
        measured_counts.append(sum(r["h24"] is not None for r in side))
        groups: dict[tuple[str, str, str], list[dict]] = {}
        for row in side:
            key = (row.get("content_version") or "未记录", row.get("sample_basis") or "next_day_approximate",
                   row.get("time_basis") or "未记录")
            groups.setdefault(key, []).append(row)
        if not groups:
            lines.append(f"{label}：篇数=0，N=0，均值=—，中位数=—。")
        for (version, basis, time_basis), group in sorted(groups.items()):
            measured = [r for r in group if r["h24"] is not None]
            counts = [r["h24"] for r in measured]
            mean = f"{sum(counts) / len(counts):.1f}" if counts else _MISSING
            med = f"{median(counts):.1f}" if counts else _MISSING
            views = sum(counts) if counts else None
            likes = sum(r["h24_likes"] for r in measured) if measured else None
            comments = sum(r["h24_comments"] for r in measured) if measured else None
            lines.append(f"{label} / {_cell(version)} / {_BASIS.get(basis, _cell(basis))} / "
                         f"{_cell(_TIME_BASIS.get(time_basis, time_basis))}："
                         f"篇数={len(group)}，N={len(measured)}，缺失={len(group) - len(measured)}，"
                         f"均值={mean}，中位数={med}，浏览总数={_value(views)}，"
                         f"赞/浏览={_ratio(likes, views)}，评/浏览={_ratio(comments, views)}，浏览样本={counts}。")
    if min(measured_counts) < 2:
        lines.append("样本不足：任一侧不足两篇已测文章，不据此宣称增长成功。")
    lines += ["历史跨内容定位、版本或采样口径的差异仅作描述；浏览和互动不能代替理解反馈。", _UNAVAILABLE]
    return "\n".join(lines)
