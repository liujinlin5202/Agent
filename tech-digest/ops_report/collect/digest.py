# -*- coding: utf-8 -*-
"""发帖机器人采集：tech-digest 自己的 run_log（结构化）+ 日志文件。

只读硬约束：SQLite 一律 mode=ro 打开，绝不写 tech-digest 的库。
口径（2026-09-20 与服务器真实数据逐条核对过）：
  · 每档期写两行 —— 采集行（带 source_stats）、结果行（带 detail）；
  · 结果行 published=成功；skipped(weekend/not_saturday/publish_dup)=预期跳过；
    degraded=降级（计失败）；
  · 应发档期 = 窗口内「工作日 daily + 周六 star + 周日 weekly」。服务整段没跑时
    该档期在 run_log 里根本没有任何行，成功率如实下降，不做「无数据即满分」的粉饰。
"""
from __future__ import annotations

import json
import logging
import re
import sqlite3
from datetime import date, datetime, timedelta
from pathlib import Path

from ops_report.model import Collected, Section

log = logging.getLogger("tech-digest")

EXPECTED_SKIP_REASONS = {"weekend", "not_saturday", "publish_dup"}
_LOG_LINE = re.compile(
    r"^(?P<ts>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}),\d+ (?P<level>\w+) (?P<msg>.*)$")


def read_run_log(db_path: Path, since: str) -> list[dict]:
    """只读取 run_log；文件不存在 → 空列表（不建库、不抛）。"""
    db = Path(db_path)
    if not db.exists():
        return []
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            "SELECT ts, task, status, detail FROM run_log WHERE ts >= ? ORDER BY ts",
            (since,)).fetchall()
    finally:
        conn.close()
    out = []
    for r in rows:
        try:
            detail = json.loads(r["detail"] or "{}")
        except ValueError:
            detail = {}
        out.append({"ts": r["ts"], "task": r["task"],
                    "status": r["status"], "detail": detail})
    return out


def expected_slots(start: date, end: date) -> list[dict]:
    """应发档期：工作日 daily、周六 star、周日 weekly（周末 daily 属预期跳过，不入账）。"""
    slots, d = [], start
    while d <= end:
        wd = d.weekday()  # 0=周一 … 5=周六 6=周日
        task = "star" if wd == 5 else "weekly" if wd == 6 else "daily"
        slots.append({"task": task, "day": d.isoformat()})
        d += timedelta(days=1)
    return slots


def outcome_kind(row: dict) -> str:
    detail = row.get("detail") or {}
    tag = detail.get("detail")
    if row["status"] == "ok" and tag == "published":
        return "published"
    if row["status"] == "ok" and tag == "dry_run_token_ok":
        return "dry_run"
    if row["status"] == "skipped" and tag in EXPECTED_SKIP_REASONS:
        return "expected_skip"
    if row["status"] == "degraded":
        return "degraded"
    return "other"


def parse_log_errors(log_path: Path, since: datetime) -> list[dict]:
    """日志文件里 since 之后的 WARNING/ERROR 行 → [{ts, level, text, source}]。"""
    p = Path(log_path)
    if not p.exists():
        return []
    out = []
    with open(p, encoding="utf-8", errors="replace") as f:
        for line in f:
            m = _LOG_LINE.match(line.strip())
            if not m or m.group("level") not in ("WARNING", "ERROR"):
                continue
            try:
                ts = datetime.strptime(m.group("ts"), "%Y-%m-%d %H:%M:%S")
            except ValueError:
                continue
            if ts >= since:
                out.append({"ts": m.group("ts"), "level": m.group("level"),
                            "text": m.group("msg"), "source": "digest_log"})
    return out


def build_section(rows: list[dict], log_errors: list[dict],
                  start: date, end: date) -> Collected:
    outcomes = [r for r in rows if "detail" in (r.get("detail") or {})]
    collections = [r for r in rows if "source_stats" in (r.get("detail") or {})]

    published = [r for r in outcomes if outcome_kind(r) == "published"]
    degraded = [r for r in outcomes if outcome_kind(r) == "degraded"]
    expected = [r for r in outcomes if outcome_kind(r) == "expected_skip"]
    skipped_reasons = sorted({(r["detail"].get("detail") or "") for r in expected})

    slots = expected_slots(start, end)
    n_slots = len(slots)
    rate = round(len(published) / n_slots * 100) if n_slots else None

    metrics: dict = {}
    metrics["应发档期"] = f"{n_slots} 次"
    metrics["实际发布"] = f"{len(published)} 次"
    metrics["发帖成功率"] = f"{rate}%" if rate is not None else "无档期"
    if expected:
        metrics["预期跳过"] = f"{len(expected)} 次（{'、'.join(skipped_reasons)}）"
    if degraded:
        metrics["降级"] = f"{len(degraded)} 次"

    # 数据源健康：窗口内最后一次采集行的 source_stats
    if collections:
        stats = collections[-1]["detail"].get("source_stats") or {}
        parts = []
        for name, st in stats.items():
            item = f"{name}: {st.get('fetched', 0)} 条"
            if st.get("error"):
                item += f"（异常：{st['error']}）"
            parts.append(item)
        if parts:
            metrics["数据源"] = "；".join(parts)

    notes = []
    if not rows:
        notes.append(f"{start} ~ {end} 窗口内没有任何运行记录（定时器或服务可能已停）")

    if not rows:
        status = "critical"
    elif degraded or (rate is not None and rate < 100):
        status = "warn"
    else:
        status = "ok"

    section = Section(key="digest", name="自动发帖机器人", status=status,
                      metrics=metrics, notes=notes)
    return Collected(section=section, raw_errors=list(log_errors))


def collect(settings, start: datetime, end: datetime) -> Collected:
    """采一次发帖机器人：run_log + 日志文件，两路各自独立、都不抛。

    SQLite 读取失败 → 板块报 unknown，备注带真实原因（库被锁/损坏 ≠ 机器人停了，
    绝不冒充「没有任何运行记录」）；日志文件读取失败 → 记一条板块 note、错误清单
    为空；任一路不可得都不影响另一路。
    """
    since_iso = start.isoformat(timespec="seconds")
    try:
        rows = read_run_log(settings.digest_db, since_iso)
    except sqlite3.Error as e:
        rows = None
        db_note = f"run_log 读取失败（库可能被锁或损坏）：{type(e).__name__}: {e}"
        log.warning("%s", db_note)
    else:
        db_note = None

    try:
        errors = parse_log_errors(settings.digest_log, start)
    except OSError as e:
        errors = []
        log_note = f"日志文件读取失败：{type(e).__name__}: {e}"
        log.warning("%s", log_note)
    else:
        log_note = None

    if rows is None:
        section = Section(key="digest", name="自动发帖机器人", status="unknown",
                          metrics={}, notes=[db_note])
        if log_note:
            section.notes.append(log_note)
        return Collected(section=section, raw_errors=errors)

    collected = build_section(rows, errors, start.date(), end.date())
    if not Path(settings.digest_db).exists():
        collected.section.notes.append(f"找不到 run_log 数据库：{settings.digest_db}")
    if log_note:
        collected.section.notes.append(log_note)
    return collected
