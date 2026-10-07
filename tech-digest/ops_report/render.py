# -*- coding: utf-8 -*-
"""渲染层：主题行 + HTML 邮件 + JSON 归档。

HTML 全内联样式（邮件客户端不认外部 CSS）；正文只列「要看的问题」——
预期行为（周末跳过、dry-run）聚类里保留、正文里不出现，避免每周误报制造疲劳。
"""
from __future__ import annotations

import html
import json
import re
from datetime import datetime
from pathlib import Path

from ops_report import analyze

_FLAG = {"ok": "", "warn": " ⚠️", "critical": " 🔴", "unknown": " ❓"}
_COLOR = {"ok": "#1a7f37", "warn": "#9a6700", "critical": "#cf222e", "unknown": "#57606a"}

# LLM 会按习惯输出 markdown（**加粗**、# 标题、-/* 列表、`命令`），邮箱不认这些标记，
# 2026-09-28 实发邮件里就是一屏裸星号。这里做「markdown-lite → 内联样式 HTML」：
# 逐行转换，编号/圆点自绘（不依赖 <ol>/<ul> 自动编号——Outlook 会重编号）。
_MD_HEADING = re.compile(r"#{1,6}\s+(.*)")
_MD_BULLET = re.compile(r"[*+-]\s+(.*)")
_MD_NUM = re.compile(r"(\d+(?:[.、)）]|\)))\s*(.*)")
_MD_CODE = re.compile(r"`([^`]+)`")
_MD_BOLD = re.compile(r"\*\*(.+?)\*\*")


def _esc(v) -> str:
    return html.escape(str(v), quote=True)


def _inline(s: str) -> str:
    """先转义再套行内 markdown；保证转换永不引入未转义的原文。"""
    s = _esc(s)
    s = _MD_BOLD.sub(r"<b>\1</b>", s)
    return _MD_CODE.sub(r'<code style="background:#f6f8fa;padding:1px 4px;">\1</code>', s)


def _md_to_html(text: str) -> str:
    """建议类文本 → 邮件友好 HTML。纯文本原样保留（编号 1) 也是），只是去掉 md 记号。"""
    parts, pending_blank = [], False
    for raw in (text or "").splitlines():
        stripped = raw.strip()
        if not stripped:
            pending_blank = bool(parts)
            continue
        if pending_blank:
            parts.append('<div style="height:8px;"></div>')
            pending_blank = False
        indent = len(raw) - len(raw.lstrip())
        pad = 18 + min(indent // 2, 3) * 16          # 缩进层级 → 视觉层级（封顶 3 层）
        hanging = f"padding-left:{pad}px;text-indent:-14px;"
        head, bullet, num = (_MD_HEADING.match(stripped), _MD_BULLET.match(stripped),
                             _MD_NUM.match(stripped))
        if head:
            parts.append(f'<div style="font-weight:bold;margin:10px 0 4px 0;">'
                         f'{_inline(head.group(1))}</div>')
        elif bullet:
            parts.append(f'<div style="margin:3px 0;{hanging}">'
                         f'•&nbsp;{_inline(bullet.group(1))}</div>')
        elif num:
            parts.append(f'<div style="margin:3px 0;{hanging}">'
                         f'{_inline(num.group(1))} {_inline(num.group(2))}</div>')
        else:
            parts.append(f'<div style="margin:3px 0;">{_inline(stripped)}</div>')
    return "".join(parts)


def subject(report) -> str:
    def metric(key: str, name: str) -> str:
        sec = report.section(key)
        return (sec.metrics.get(name, "—") if sec else "—")

    return (f"【集市运维报告】{report.window_start[5:10]} ~ {report.window_end[5:10]} ｜ "
            f"发帖成功率 {metric('digest', '发帖成功率')} ｜ "
            f"请求成功率 {metric('assist', '请求成功率')}{_FLAG.get(report.overall, '')}")


def _section_html(sec) -> str:
    color = _COLOR.get(sec.status, "#57606a")
    rows = "".join(
        f'<tr><td style="padding:4px 12px 4px 0;color:#57606a;">{_esc(k)}</td>'
        f'<td style="padding:4px 0;"><b>{_esc(v)}</b></td></tr>'
        for k, v in (sec.metrics or {}).items())

    notes = ""
    if sec.notes:
        items = "".join(f"<li>{_esc(n)}</li>" for n in sec.notes)
        notes = (f'<div style="margin:8px 0;padding:8px 12px;background:#fff8c5;'
                 f'border-left:3px solid #d4a72c;">'
                 f'<ul style="margin:6px 0 0 0;padding-left:18px;">{items}</ul></div>')

    errs = ""
    real = analyze.actionable(sec)
    if real:
        items = "".join(
            f'<li style="margin-bottom:6px;">×{c.count} '
            f'<code style="background:#f6f8fa;padding:1px 4px;">{_esc(c.signature[:160])}</code>'
            f'<br><span style="color:#57606a;font-size:12px;">首次 {_esc(c.first_ts)} ／ '
            f'末次 {_esc(c.last_ts)} · 来源 {_esc(c.source)}</span></li>'
            for c in real[:8])
        errs = (f'<div style="margin:10px 0;"><b>报错聚类</b>'
                f'<ul style="margin:6px 0 0 0;padding-left:18px;">{items}</ul></div>')
    else:
        errs = '<div style="margin:10px 0;color:#1a7f37;">窗口内无异常报错</div>'

    advice = ""
    # LLM 叙事（origin=="llm"）全局只渲染一次（见 render_html 的综合分析块），
    # 板块下只保留知识库/模板建议——同一份文本重复 3 次只会淹没真正的信息。
    own = [a for a in (sec.advice or []) if a.origin != "llm"]
    if own:
        items = "".join(
            f'<div style="margin:8px 0;padding:8px 12px;background:#f6f8fa;'
            f'border-left:3px solid {color};">'
            f'<b>{_esc(a.title)}</b>'
            f'<div style="margin:6px 0 0 0;font-size:13px;">{_md_to_html(a.action)}</div></div>'
            for a in own)
        advice = f'<div style="margin:10px 0;"><b>运维建议</b>{items}</div>'

    return (f'<h2 style="font-size:16px;margin:22px 0 6px 0;padding-bottom:4px;'
            f'border-bottom:2px solid {color};">{_esc(sec.name)} '
            f'<span style="color:{color};font-size:13px;">[{_esc(sec.status)}]</span></h2>'
            f'<table style="border-collapse:collapse;font-size:14px;">{rows}</table>'
            f'{notes}{errs}{advice}')


def render_html(report) -> str:
    alerts = ""
    if report.pending_alerts:
        items = "".join(f"<li>{_esc(a[:300])}</li>" for a in report.pending_alerts)
        alerts = (f'<div style="margin:0 0 14px 0;padding:10px 14px;background:#ffebe9;'
                  f'border-left:4px solid #cf222e;"><b>⚠️ 上次投递未成功，以下告警未送达：</b>'
                  f'<ul style="margin:6px 0 0 0;">{items}</ul></div>')

    summary = "".join(
        f'<li>{_esc(s.name)}：<b style="color:{_COLOR.get(s.status, "#57606a")};">'
        f'{_esc(s.status)}</b></li>' for s in report.sections)

    # LLM 叙事（origin=="llm"）挂在每个板块上（T8 的单次调用设计），渲染时去重只出一次
    llm_texts = []
    for s in report.sections:
        for a in (s.advice or []):
            if a.origin == "llm" and a.action not in llm_texts:
                llm_texts.append(a.action)

    llm_block = ""
    if llm_texts:
        items = "".join(
            f'<div style="margin:8px 0;padding:8px 12px;background:#f6f8fa;'
            f'border-left:3px solid #0969da;"><b>综合分析与建议</b>'
            f'<div style="margin:6px 0 0 0;font-size:13px;">{_md_to_html(t)}</div></div>'
            for t in llm_texts)
        llm_block = f'<div style="margin:0 0 14px 0;">{items}</div>'

    body = "".join(_section_html(s) for s in report.sections)

    return (
        '<!DOCTYPE html><html><head><meta charset="utf-8"></head>'
        '<body style="margin:0;padding:0;background:#ffffff;">'
        '<div style="max-width:760px;margin:0 auto;padding:20px;'
        'font-family:-apple-system,\'PingFang SC\',\'Microsoft YaHei\',sans-serif;'
        'color:#1f2328;font-size:14px;line-height:1.6;">'
        f'<h1 style="font-size:18px;margin:0 0 4px 0;">集市运维巡检报告</h1>'
        f'<div style="color:#57606a;font-size:13px;margin-bottom:14px;">'
        f'窗口 {_esc(report.window_start)} ~ {_esc(report.window_end)} ｜ '
        f'生成于 {_esc(report.generated_at)}</div>'
        f'{alerts}{llm_block}'
        f'<div style="margin-bottom:14px;"><b>结论摘要</b>'
        f'<ul style="margin:6px 0 0 0;">{summary}</ul></div>'
        f'{body}'
        '<div style="margin-top:24px;padding-top:10px;border-top:1px solid #d0d7de;'
        'color:#57606a;font-size:12px;">由 tech-digest/ops_report 自动生成（只读采集）。'
        '归档见 ops_report/data/reports/。</div>'
        '</div></body></html>')


def archive_paths(data_dir: Path, when: datetime) -> tuple[Path, Path]:
    d = Path(data_dir) / "reports"
    day = when.strftime("%Y-%m-%d")
    return d / f"{day}.html", d / f"{day}.json"


def write_archive(report, data_dir: Path, when: datetime) -> tuple[Path, Path]:
    html_path, json_path = archive_paths(data_dir, when)
    html_path.parent.mkdir(parents=True, exist_ok=True)
    html_path.write_text(render_html(report), encoding="utf-8")
    payload = {
        "generated_at": report.generated_at,
        "window_start": report.window_start,
        "window_end": report.window_end,
        "overall": report.overall,
        "pending_alerts": report.pending_alerts,
        "sections": [
            {
                "key": s.key, "name": s.name, "status": s.status,
                "metrics": s.metrics, "notes": s.notes,
                "errors": [c.__dict__ for c in s.errors],
                "advice": [a.__dict__ for a in s.advice],
            } for s in report.sections
        ],
    }
    json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2),
                         encoding="utf-8")
    return html_path, json_path
