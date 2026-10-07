# -*- coding: utf-8 -*-
"""应答 AI 采集：请求侧（nginx）+ 存活性（pod）+ 链路侧（pod 日志）+ 业务侧（集市库）。

四路里任意一路拿不到都不影响其余——服务整挂时「请求侧 + 存活性」照样出数据，
这正是本报告存在的意义（2026-09-20 实测：48h 内 14577 次请求 0 次成功）。
全部只读：docker logs / ssh 只跑 ps、ss、tail；MySQL 只 SELECT。
"""
from __future__ import annotations

import logging
import os
import re
import subprocess
from datetime import datetime

from ops_report.model import Collected, Section

log = logging.getLogger("tech-digest")

ACCESS_RE = re.compile(r'"(?:GET|POST) (?P<path>/api/v1/assist/[^ "\s]*)[^"]*" (?P<status>\d{3})')
_POD_ERR_MARKERS = ("失败", "异常", "错误", "ERROR", "Traceback", "Timeout", "refused")
_POD_ERR_SKIP = ("INFO:", "[llm] 主通道（market-deploy）失败，尝试备用通道")  # 预期内的降级提示

PROBE_CMD = ("echo PROC=$(pgrep -fc '[m]ain\\.py' 2>/dev/null || echo 0); "
             "echo PORT=$(ss -ltn 2>/dev/null | grep -c ':8080' || echo 0)")
POD_LOGS_CMD = ("for f in service.log assist.log sync.log; do "
                "p=\"$HOME/sse_market_assist/logs/$f\"; "
                "[ -f \"$p\" ] && echo \"== $f ==\" && tail -n 300 \"$p\"; done")


def parse_access_lines(lines: list[str]) -> dict:
    """nginx 访问日志行 → assist 请求的成败分布。

    口径：成功 = 2xx（用户真的拿到了回答）；5xx = 服务端故障。两者分开统计——
    421/499 既不是成功也不是服务故障，混进任何一边都会让数字失真
    （2026-09-20 实测：13223×502 + 1333×421 + 19×499 + 2×404 → 2xx 为 0，即 0 次成功）。
    """
    by_status: dict = {}
    for line in lines or []:
        m = ACCESS_RE.search(line)
        if not m:
            continue
        st = int(m.group("status"))
        by_status[st] = by_status.get(st, 0) + 1
    total = sum(by_status.values())
    ok = sum(c for s, c in by_status.items() if 200 <= s < 300)
    server_errors = sum(c for s, c in by_status.items() if s >= 500)
    rate = round(ok / total * 100, 2) if total else None
    return {"total": total, "by_status": by_status, "ok": ok,
            "server_errors": server_errors, "rate": rate}


def parse_nginx_errors(lines: list[str]) -> list[dict]:
    """nginx error 行（docker logs 的 stderr 侧）→ 原始错误条目。

    只挑「含 [error] 且含 /api/v1/assist/」的行：'upstream prematurely closed
    connection' 只出现在 error 行、访问行里没有——而它正是知识库 assist_pod_down
    （pod 重启 runbook）的唯一触发签名。漏掉这路，签名进不了信号流，
    确定性处置层整条沉默（2026-09-21 落线验证发现的缺口）。
    """
    now = datetime.now()
    out = []
    for line in lines or []:
        if "[error]" not in line or "/api/v1/assist/" not in line:
            continue
        ts = None
        for fmt in ("%Y/%m/%d %H:%M:%S", "%d/%b/%Y:%H:%M:%S"):
            try:
                ts = datetime.strptime(line.lstrip()[:19], fmt)
                break
            except ValueError:
                continue
        out.append({"ts": (ts or now).isoformat(timespec="seconds"),
                    "text": line[:400], "source": "nginx"})
    return out


def docker_logs(container: str, since: str, timeout: int = 120) -> list[str] | None:
    """docker logs --since（访问日志在 stdout、error_log 在 stderr，两路都收）。"""
    try:
        p = subprocess.run(["docker", "logs", container, "--since", since],
                           capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.SubprocessError):
        return None
    if p.returncode != 0:
        return None
    return (p.stdout + "\n" + p.stderr).splitlines()


def pod_ssh(settings, remote_cmd: str, timeout: int = 60) -> str | None:
    """经 sshpass 进 pod。口令走 SSHPASS 环境变量（不出现在 ps 命令行里）。"""
    if not settings.pod_ssh_password:
        return None
    env = dict(os.environ, SSHPASS=settings.pod_ssh_password)
    cmd = ["sshpass", "-e", "ssh",
           "-o", "StrictHostKeyChecking=no", "-o", "ConnectTimeout=8",
           "-o", "BatchMode=no", "-p", str(settings.pod_ssh_port),
           f"{settings.pod_ssh_user}@{settings.pod_ssh_host}", remote_cmd]
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, env=env)
    except (OSError, subprocess.SubprocessError):
        return None
    if p.returncode != 0:
        return None
    return p.stdout


def parse_probe(out: str | None) -> dict:
    """PROC=/PORT= → {"proc": int, "port": int}；无输出视为未运行。"""
    d = {}
    for line in (out or "").splitlines():
        if "=" in line:
            k, _, v = line.partition("=")
            d[k.strip()] = v.strip()
    def _int(key: str) -> int:
        try:
            return int(d.get(key, "0") or 0)
        except ValueError:
            return 0
    return {"proc": _int("PROC"), "port": _int("PORT")}


def parse_pod_errors(text: str | None) -> list[dict]:
    """pod 日志里的报错行 → 原始错误条目（真正的聚类在 analyze.py）。"""
    out = []
    for line in (text or "").splitlines():
        s = line.strip()
        if not s or s.startswith("=="):
            continue
        if any(skip in s for skip in _POD_ERR_SKIP):
            continue
        if any(marker in s for marker in _POD_ERR_MARKERS):
            out.append({"ts": datetime.now().isoformat(timespec="seconds"),
                        "text": s[:400], "source": "pod"})
    return out


def market_counts(since_iso: str) -> dict | None:
    """集市库 assist_responses 的窗口内业务量（只读）。docker 不可用 → None。

    复用 app.marketdb._run：那是本项目既有的「docker exec + 只读 SELECT」通道，
    口径与 content_report.py 的浏览采样完全一致；改它会影响线上追踪脚本，所以不加包装。
    """
    from app import marketdb
    sql = ("SELECT COUNT(*), COALESCE(SUM(feedback_upvotes),0), "
           "COALESCE(SUM(feedback_downvotes),0) FROM assist_responses "
           f"WHERE created_at >= '{since_iso}'")
    try:
        rows = marketdb._run(sql)
    except Exception as e:  # noqa: BLE001 — 采集失败绝不能让报告挂掉
        log.warning("集市库应答数查询失败（按不可得降级）: %s", e)
        return None
    if not rows or not rows[0] or len(rows[0]) < 3:
        return None
    try:
        return {"新应答数": int(rows[0][0]), "顶": int(rows[0][1]), "踩": int(rows[0][2])}
    except (ValueError, IndexError):
        return None


def build_section(access: dict | None, probe: dict | None,
                  pod_errors: list[dict], business: dict | None,
                  start: datetime, end: datetime) -> Collected:
    section = Section(key="assist", name="自动回复 AI（应答服务）", status="unknown")
    metrics = {}

    if access is not None:
        metrics["请求总数"] = f"{access['total']} 次"
        if access["rate"] is not None:
            metrics["请求成功率"] = f"{access['rate']:g}%"
        else:
            metrics["请求成功率"] = "窗口内无请求"
        if access["by_status"]:
            dist = "、".join(f"{s}×{c}" for s, c in sorted(access["by_status"].items()))
            metrics["状态码分布"] = dist
    else:
        section.notes.append("nginx 访问日志不可得（docker logs 读取失败）")

    if probe is not None:
        metrics["进程/端口"] = ("运行中 / 已监听" if probe["proc"] and probe["port"]
                              else f"未运行 / {'已监听' if probe['port'] else '未监听'}")
    else:
        section.notes.append("pod 探活不可得（SSH 未配置或不可达）")

    if business is not None:
        metrics["新应答数"] = f"{business['新应答数']} 条"
        metrics["用户反馈"] = f"顶 {business['顶']} / 踩 {business['踩']}"
    else:
        section.notes.append("集市库应答数不可得")

    section.metrics = metrics

    # ---- 状态判定 ----
    down = probe is not None and (probe["proc"] == 0 or probe["port"] == 0)
    if down:
        section.status = "critical"
    elif access is None and probe is None:
        section.status = "unknown"
    elif access is not None and access["total"] > 0 and access["ok"] == 0:
        section.status = "critical"
    elif access is not None and access["total"] == 0:
        section.status = "warn"
    elif access is not None and (access["server_errors"] > 0
                                 or (access["rate"] or 0) < 95):
        section.status = "warn"
    elif pod_errors:
        section.status = "warn"
    else:
        section.status = "ok"

    if down:
        section.notes.append("pod 上应答进程不在：所有 /api/v1/assist/ 请求都会 502")
    if access is not None and access["total"] == 0 and probe and probe["proc"]:
        section.notes.append("服务在跑但窗口内没有任何请求：检查 nginx 路由是否仍指向 13012")

    return Collected(section=section, raw_errors=list(pod_errors))


def collect(settings, start: datetime, end: datetime) -> Collected:
    """四路采集：任一失败返回 None 交 build_section 降级，绝不抛。"""
    since_iso = start.isoformat(timespec="seconds")
    access, nginx_errors = None, []
    lines = docker_logs(settings.nginx_container, since_iso)
    if lines is not None:
        access = parse_access_lines(lines)
        nginx_errors = parse_nginx_errors(lines)  # 访问行与 error 行同源不同用

    probe = None
    out = pod_ssh(settings, PROBE_CMD)
    if out is not None:
        probe = parse_probe(out)

    pod_errors = []
    if out is not None:
        pod_errors = parse_pod_errors(pod_ssh(settings, POD_LOGS_CMD, timeout=90))

    business = market_counts(since_iso)
    collected = build_section(access, probe, pod_errors, business, start, end)
    collected.raw_errors = nginx_errors + collected.raw_errors  # nginx 行在前
    return collected
