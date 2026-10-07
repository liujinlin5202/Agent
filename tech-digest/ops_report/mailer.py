# -*- coding: utf-8 -*-
"""邮件投递：163 SMTP（465 SSL）+ 失败重试。

为什么重试 3 次而不是 1 次：163 偶发限流/连接重置是常态，一次失败就放弃等于把
「报告没发出去」变成静默事故。重试仍失败 → 返回 False，由编排层落告警文件。
"""
from __future__ import annotations

import logging
import smtplib
import time
from email.header import Header
from email.mime.text import MIMEText
from email.utils import formataddr, formatdate

log = logging.getLogger("tech-digest")


def _build_message(sender: str, to: str, subject: str, html: str) -> MIMEText:
    msg = MIMEText(html, "html", "utf-8")
    msg["Subject"] = Header(subject, "utf-8")
    msg["From"] = formataddr((str(Header("集市运维巡检", "utf-8")), sender))
    msg["To"] = to
    msg["Date"] = formatdate(localtime=True)
    return msg


def send(settings, subject: str, html: str, retries: int = 3, delay: int = 60,
         sleep=time.sleep, smtp_factory=None) -> tuple[bool, str]:
    """SMTP_SSL 发送；（成功?, 说明）。任何异常都被收在这里，不外抛。"""
    if not settings.smtp_ready:
        return False, "未配置 SMTP（OPS_SMTP_USER / OPS_SMTP_AUTH_CODE / OPS_MAIL_TO）"

    factory = smtp_factory or (lambda s: smtplib.SMTP_SSL(
        s.smtp_host, s.smtp_port, timeout=30))
    last_err = ""
    for attempt in range(1, retries + 1):
        try:
            with factory(settings) as client:
                client.login(settings.smtp_user, settings.smtp_auth_code)
                client.send_message(_build_message(
                    settings.mail_from, settings.mail_to, subject, html))
            return True, "已发送"
        except Exception as e:  # noqa: BLE001 — 网络/SMTP/编码异常都算投递失败
            last_err = f"{type(e).__name__}: {e}"
            log.warning("邮件发送第 %d/%d 次失败: %s", attempt, retries, last_err)
            if attempt < retries:
                sleep(delay)
    return False, last_err
