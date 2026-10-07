# -*- coding: utf-8 -*-
"""ops_report 入口：编排三层 + CLI。

子命令：
  run       正式执行（守卫 → 采集 → 分析 → 渲染 → 发送 → 记状态）
  dry-run   只生成不发送（落归档，供人先看样式）
  test-mail 发一封最小测试邮件（验证 SMTP 通路，不跑采集）

任何单源采集异常 → 该板块降级为 unknown 并注明，报告照发。这是刻意的：
运维报告最没用的形态就是「自己挂了所以什么都没说」。
"""
from __future__ import annotations

import logging
import sys
from datetime import datetime

from ops_report import analyze, config, knowledge, llm_advice, mailer, render, state, window
from ops_report.collect import assist as collect_assist
from ops_report.collect import digest as collect_digest
from ops_report.collect import system as collect_system
from ops_report.model import Collected, Report, Section


def _setup_logging() -> None:
    # Windows 控制台常见 GBK，编不了主题里的状态符号（🔴/⚠️/❓）——
    # 不可编码时降级为 ?，而不是让整条 CLI 崩在 print 上（UTF-8 环境不受影响）。
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            try:
                stream.reconfigure(errors="replace")
            except (ValueError, OSError):
                pass
    root = logging.getLogger()
    if not root.handlers:
        root.addHandler(logging.StreamHandler(sys.stdout))
    root.setLevel(logging.INFO)


def default_collectors() -> dict:
    return {"digest": collect_digest.collect,
            "assist": collect_assist.collect,
            "system": collect_system.collect}


SECTION_NAMES = {"digest": "自动发帖机器人", "assist": "自动回复 AI", "system": "系统层"}


def build_report(settings, now: datetime | None = None,
                 collectors: dict | None = None, chat=None) -> tuple[Report, dict]:
    """采集三层 → 聚类 → 知识库 → LLM 建议 → Report。

    窗口只算一次（来自 state 的 last_sent），采集与报告头用同一个窗口，
    避免两头各自计算导致「报告说 2 天、采集只取了 1 天」。
    """
    now = now or datetime.now()
    collectors = collectors or default_collectors()
    start, end = window.report_window(state.State(settings.data_dir).last_sent(),
                                      now, settings.interval_days)

    sections, raw = [], {}
    for key in ("digest", "assist", "system"):
        fn = collectors.get(key)
        try:
            collected = fn(settings) if key == "system" else fn(settings, start, end)
        except Exception as e:  # noqa: BLE001 — 采集异常必须降级而不是中断
            collected = Collected(Section(key=key, name=SECTION_NAMES[key], status="unknown",
                                          notes=[f"采集异常：{type(e).__name__}: {e}"]))
        sections.append(collected.section)
        raw[key] = collected.raw_errors

    for sec in sections:
        analyze.annotate(sec, raw.get(sec.key, []))

    report = Report(generated_at=now.isoformat(timespec="seconds"),
                    window_start=start.isoformat(timespec="seconds"),
                    window_end=end.isoformat(timespec="seconds"),
                    sections=sections,
                    pending_alerts=state.pending_alerts(settings.data_dir))

    for adv in knowledge.match(knowledge.signals_from(sections)):
        _guess_section(sections, adv).advice.append(adv)

    advice = llm_advice.write_advice(report, chat=chat)
    return report, advice


def _guess_section(sections: list, advice) -> Section:
    """知识库建议归属：按建议标题里的关键词找板块，找不到挂到第一个非 ok 板块。"""
    text = advice.title + advice.action
    for sec in sections:
        if sec.key == "assist" and ("应答" in text or "pod" in text or "nginx" in text):
            return sec
        if sec.key == "digest" and ("发帖" in text or "refresh_token" in text or "token" in text):
            return sec
        if sec.key == "system" and ("磁盘" in text or "证书" in text):
            return sec
    for sec in sections:
        if sec.status != "ok":
            return sec
    return sections[0]


def main(argv: list[str] | None = None, settings=None, now: datetime | None = None,
         collectors: dict | None = None, chat=None, send=None) -> int:
    _setup_logging()
    argv = list(sys.argv[1:] if argv is None else argv)
    cmd = argv[0] if argv else "run"
    settings = settings or config.OpsSettings.from_env()
    now = now or datetime.now()

    if cmd == "test-mail":
        ok, msg = (send or mailer.send)(
            settings, "【集市运维报告】SMTP 测试",
            "<p>这是一封测试邮件：若你看到它，说明巡检报告的投递链路已打通。</p>")
        print(("测试邮件已发送" if ok else f"测试邮件发送失败：{msg}"))
        return 0 if ok else 1

    if cmd not in ("run", "dry-run"):
        print(f"未知子命令：{cmd}（可用：run / dry-run / test-mail）")
        return 1

    st = state.State(settings.data_dir)
    if cmd == "run" and not window.should_send(st.last_sent(), now, settings.interval_days):
        print(f"距上次发送不足 {settings.interval_days} 天，跳过")
        return 0

    report, _advice = build_report(settings, now=now, collectors=collectors, chat=chat)
    html = render.render_html(report)
    html_path, json_path = render.write_archive(report, settings.data_dir, now)
    subj = render.subject(report)
    print(f"报告已生成：{html_path}")
    print(f"主题：{subj}")

    if cmd == "dry-run":
        print("dry-run：未发送、未更新 state.json")
        return 0

    ok, msg = (send or mailer.send)(settings, subj, html)
    if ok:
        st.mark_sent(now.isoformat(timespec="seconds"))
        state.clear_alerts(settings.data_dir)
        print("邮件已发送")
        return 0

    state.add_alert(settings.data_dir,
                    f"运维报告投递失败（{now.isoformat(timespec='seconds')}）：{msg}\n"
                    f"报告已归档：{html_path}")
    print(f"邮件发送失败：{msg}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
