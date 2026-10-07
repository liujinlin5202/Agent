# -*- coding: utf-8 -*-
"""系统层采集：磁盘水位、证书到期。

磁盘这两项不是凑数：2026-08-30 磁盘满 97% 整机挂死过一次，阈值必须进定期报告。
证书路径实测在 /root/market-deploy/Nginx/live/*/fullchain.pem（certbot 容器挂载目录），
不是 /etc/letsencrypt —— 按 glob 扫，取最早到期的一张。
"""
from __future__ import annotations

import glob
import logging
import subprocess
from datetime import datetime, timezone

from ops_report.model import Collected, Section

log = logging.getLogger("tech-digest")

CERT_WARN_DAYS = 14
# 只盯发帖机器人的三个 timer：报告的 timer 若挂掉，症状是「收不到邮件」本身即可见，
# 把它列进来反而会在首次部署（timer 还没装）时误报 critical。
TIMER_UNITS = ["tech-digest-daily.timer", "tech-digest-star.timer",
               "tech-digest-weekly.timer"]


def parse_df(out: str) -> dict | None:
    """df -P 输出 → {"pct": 82, "used": "165G", "size": "216G"}。"""
    lines = [l for l in (out or "").splitlines() if l.strip()]
    if len(lines) < 2:
        return None
    parts = lines[1].split()
    if len(parts) < 6:
        return None
    try:
        pct = int(parts[4].rstrip("%"))
    except ValueError:
        return None
    return {"pct": pct, "used": parts[2], "size": parts[1]}


def parse_enddate(line: str) -> datetime | None:
    """openssl 的 'notAfter=Nov 22 02:29:45 2026 GMT' → 带时区 datetime。"""
    if "=" not in (line or ""):
        return None
    raw = line.split("=", 1)[1].strip()
    try:
        return datetime.strptime(raw, "%b %d %H:%M:%S %Y %Z").replace(
            tzinfo=timezone.utc)
    except ValueError:
        return None


def disk_usage(path: str = "/", runner=subprocess.run) -> dict | None:
    try:
        p = runner(["df", "-P", path], capture_output=True, text=True, timeout=15)
    except (OSError, subprocess.SubprocessError):
        return None
    if getattr(p, "returncode", 1) != 0:
        return None
    return parse_df(p.stdout)


def scan_certs(pattern: str, now: datetime, runner=subprocess.run) -> dict | None:
    """glob 出证书文件，取最早到期的一张。openssl 不可用/无证书 → None。"""
    best = None
    for path in sorted(glob.glob(pattern)):
        try:
            p = runner(["openssl", "x509", "-enddate", "-noout", "-in", path],
                       capture_output=True, text=True, timeout=15)
        except (OSError, subprocess.SubprocessError):
            continue
        dt = parse_enddate(getattr(p, "stdout", "") or "")
        if dt and (best is None or dt < best[1]):
            best = (path, dt)
    if not best:
        return None
    days = (best[1] - now).days
    domain = best[0].rstrip("/").split("/")[-2] if "/" in best[0] else best[0]
    return {"domain": domain, "expires": best[1].isoformat(), "days_left": days}


def _systemctl_show(unit: str, prop: str, runner=subprocess.run) -> str | None:
    """systemctl show -p <prop> --value；不可用/单元不存在 → None。"""
    try:
        p = runner(["systemctl", "show", unit, "-p", prop, "--value"],
                   capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return None
    if getattr(p, "returncode", 1) != 0:
        return None
    return (getattr(p, "stdout", "") or "").strip()


def timer_status(units: list[str] | None = None, runner=subprocess.run) -> dict | None:
    """各 timer 的 ActiveState 与上次触发时间。一条都拿不到 → None（降级）。"""
    out = {}
    for unit in (units or TIMER_UNITS):
        state = _systemctl_show(unit, "ActiveState", runner)
        if state is None:
            continue
        last = _systemctl_show(unit, "LastTriggerUSec", runner) or ""
        out[unit] = {"state": state,
                     "last": "未触发" if (not last or last == "n/a") else last[:24]}
    return out or None


def build_section(disk: dict | None, cert: dict | None,
                  warn_pct: int, crit_pct: int,
                  cert_warn_days: int = CERT_WARN_DAYS,
                  timers: dict | None = None) -> Collected:
    section = Section(key="system", name="系统层", status="ok")
    metrics, notes = {}, []

    if disk is not None:
        metrics["磁盘使用率"] = f"{disk['pct']}%"
        metrics["磁盘用量"] = f"{disk['used']} / {disk['size']}"
    else:
        notes.append("磁盘信息不可得")

    if cert is not None:
        metrics["最近到期证书"] = f"{cert['domain']}（剩 {cert['days_left']} 天）"
    else:
        notes.append("证书信息不可得（glob 无匹配或 openssl 不可用）")

    if timers:
        metrics["定时器"] = "；".join(
            f"{u.replace('.timer', '')}: {v['state']}" for u, v in timers.items())

    status = "ok"
    if disk is None and cert is None:
        status = "unknown"
    if disk is not None:
        if disk["pct"] >= crit_pct:
            status = "critical"
            notes.append(f"磁盘使用率 {disk['pct']}% 已达告警线 {crit_pct}%："
                         "参考 2026-08-30 磁盘满导致整机挂死，立即清理")
        elif disk["pct"] >= warn_pct and status == "ok":
            status = "warn"
            notes.append(f"磁盘使用率 {disk['pct']}% 超过提醒线 {warn_pct}%")
    if cert is not None and cert["days_left"] <= cert_warn_days and status in ("ok", "warn"):
        status = "warn"
        notes.append(f"证书 {cert['domain']} 剩 {cert['days_left']} 天到期（certbot 自动续期需确认）")
    if timers:
        dead = [u for u, v in timers.items() if v["state"] != "active"]
        if dead:
            # 无条件 critical：死 timer = 那个任务根本不会跑，比磁盘提醒/数据缺失都严重，
            # 而 status 驱动 Report.overall，条件升级会把 warn/unknown 压过低估严重度。
            status = "critical"
            notes.append("以下定时器未激活：" + "、".join(dead)
                         + "（systemctl list-timers 确认，enable --now 拉起）")

    section.status = status
    section.metrics = metrics
    section.notes = notes
    return Collected(section=section, raw_errors=[])


def collect(settings) -> Collected:
    now = datetime.now(timezone.utc)
    disk = disk_usage()
    cert = scan_certs(settings.cert_glob, now)
    timers = timer_status()
    return build_section(disk, cert, settings.disk_warn_pct, settings.disk_crit_pct,
                         timers=timers)
