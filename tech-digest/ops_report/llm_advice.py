# -*- coding: utf-8 -*-
"""LLM 分析与建议：把「指标 + 错误聚类 + 知识库命中」交给 LLM 写成运维叙事。

为什么需要它：知识库只能覆盖踩过的坑，新错误得靠 LLM 给候选根因。
复用 tech-digest 已验证的 app.llm.chat（集市网关主 + DeepSeek 备，双通道都挂才 None）。
LLM 不可用 → 模板兜底：报告只下降「文采」，不下降「事实」。
"""
from __future__ import annotations

import logging

from app import llm
from ops_report import analyze
from ops_report.model import Advice

log = logging.getLogger("tech-digest")

SYSTEM = (
    "你是资深 SRE，为中文运维巡检报告写「分析与运维建议」。要求："
    "只依据给定的指标与报错，不编造未出现的事实；"
    "先说结论（是否需要立刻处理），再给按优先级排序的具体动作（命令级）；"
    "对预期行为（周末跳过发帖、dry-run 降级）不要当成故障；"
    "总长控制在 300 字内，分点陈述。"
)


def build_prompt(report) -> str:
    lines = [f"报告窗口：{report.window_start} ~ {report.window_end}",
             f"整体状态：{report.overall}", ""]
    for sec in report.sections:
        lines.append(f"## {sec.name}（状态 {sec.status}）")
        for k, v in (sec.metrics or {}).items():
            lines.append(f"- {k}：{v}")
        for note in sec.notes or []:
            lines.append(f"- 说明：{note}")
        real = analyze.actionable(sec)
        if real:
            lines.append("- 报错聚类（次数降序）：")
            for c in real[:8]:
                lines.append(f"  · ×{c.count} {c.signature[:160]}")
                lines.append(f"    样例：{c.sample[:160]}")
        for a in sec.advice or []:
            if a.origin == "knowledge":
                lines.append(f"- 已知处置：{a.title}")
        lines.append("")
    lines.append("请针对以上内容写出「分析与运维建议」，逐板块给结论与动作。")
    return "\n".join(lines)


def fallback_text(section) -> str:
    """LLM 不可用时的模板兜底：事实照样完整，只是没有提炼。"""
    parts = [f"（模板兜底：LLM 不可用）当前状态 {section.status}。"]
    for k, v in (section.metrics or {}).items():
        parts.append(f"{k} {v}。")
    for note in section.notes or []:
        parts.append(f"{note}。")
    real = analyze.actionable(section)
    if real:
        parts.append("待处理报错：" + "；".join(f"×{c.count} {c.signature[:80]}" for c in real[:5]))
    for a in section.advice or []:
        if a.origin == "knowledge":
            parts.append(f"处置参考：{a.title}")
    return " ".join(parts)


def write_advice(report, chat=None) -> dict:
    """一次 LLM 调用覆盖全报告（省 token），失败则逐板块模板兜底。"""
    chat = chat or llm.chat
    text = None
    try:
        text = chat(build_prompt(report), SYSTEM, 1200, 120)
    except Exception as e:  # noqa: BLE001 — LLM 任何异常都不能让报告失败
        log.warning("LLM 建议生成异常（走模板兜底）: %s", e)

    out = {}
    for sec in report.sections:
        if text and text.strip():
            out[sec.key] = text.strip()
            sec.advice.append(Advice(title="LLM 分析与建议", action=text.strip(), origin="llm"))
        else:
            tpl = fallback_text(sec)
            out[sec.key] = tpl
            sec.advice.append(Advice(title="模板分析与建议", action=tpl, origin="template"))
    return out
