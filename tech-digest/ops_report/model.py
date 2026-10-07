# -*- coding: utf-8 -*-
"""ops_report 的共享数据模型：采集 → 分析 → 渲染 三层之间的契约。

为什么单独一个模块：采集器（collect/*）产出 Section，分析层（analyze/knowledge/
llm_advice）就地补充 errors/advice，渲染层（render）只读不写。三层各自可单测，
换掉任何一层都不影响其他两层——前提是这些字段名不变。
"""
from __future__ import annotations

from dataclasses import dataclass, field

_STATUS_ORDER = {"ok": 0, "unknown": 1, "warn": 2, "critical": 3}


@dataclass
class ErrorCluster:
    """归一化后的同类错误：同一条报错刷 13000 次也只占一行。"""
    signature: str          # 归一化签名（数字/ID/时间戳已换成占位符）
    count: int
    first_ts: str
    last_ts: str
    sample: str             # 原始样例（截断），便于人肉核对
    source: str             # digest_log / nginx / pod / system
    expected: bool = False  # 预期行为（周末跳过、dry-run 等），不计告警


@dataclass
class Advice:
    """一条运维建议：知识库命中或 LLM 产出。"""
    title: str
    action: str             # 处置步骤（可多行）
    origin: str             # knowledge | llm | template


@dataclass
class Section:
    """报告的一个板块（发帖机器人 / 应答 AI / 系统层）。"""
    key: str                # digest | assist | system
    name: str               # 中文板块名
    status: str             # ok | warn | critical | unknown
    metrics: dict = field(default_factory=dict)      # 有序指标（渲染按插入序展示）
    errors: list = field(default_factory=list)       # list[ErrorCluster]
    advice: list = field(default_factory=list)       # list[Advice]
    notes: list = field(default_factory=list)        # 「数据不可得」等说明


@dataclass
class Collected:
    """采集层返回：板块 + 待聚类的原始错误条目。"""
    section: Section
    raw_errors: list = field(default_factory=list)   # [{ts, text, source}]


@dataclass
class Report:
    generated_at: str
    window_start: str
    window_end: str
    sections: list = field(default_factory=list)
    pending_alerts: list = field(default_factory=list)   # 上次投递失败的告警原文

    @property
    def overall(self) -> str:
        """整体状态 = 最差板块状态（unknown 比 ok 差，warn 比 unknown 差）。"""
        worst = "ok"
        for s in self.sections:
            if _STATUS_ORDER.get(s.status, 0) > _STATUS_ORDER.get(worst, 0):
                worst = s.status
        return worst

    def section(self, key: str) -> Section | None:
        for s in self.sections:
            if s.key == key:
                return s
        return None
