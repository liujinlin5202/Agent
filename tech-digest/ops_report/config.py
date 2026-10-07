# -*- coding: utf-8 -*-
"""ops_report 配置：独立 .env（ops_report/.env），零额外依赖（与 app/config.py 同模式）。

凭据（163 授权码、pod SSH 口令）只存在这里：权限 600、不进 git、不进日志。
这里只在 from_env() 时读取环境变量，不做 import 期单例——单测需要能构造干净实例。
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent          # tech-digest/ops_report
DIGEST_DIR = BASE_DIR.parent                        # tech-digest/
ENV_PATH = BASE_DIR / ".env"


def load_env(path: Path = ENV_PATH) -> None:
    """把 .env 读入 os.environ（已存在的环境变量优先，不覆盖）。文件不存在则跳过。"""
    p = Path(path)
    if not p.exists():
        return
    with open(p, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            if key and key not in os.environ:
                os.environ[key] = value


@dataclass
class OpsSettings:
    # ---- 邮件（163 SMTP，465 SSL；授权码即密码）----
    smtp_host: str = "smtp.163.com"
    smtp_port: int = 465
    smtp_user: str = ""
    smtp_auth_code: str = ""
    mail_to: str = ""
    # ---- 报告节奏 ----
    interval_days: int = 2
    # ---- pod（应答 AI 运行处；从服务器经 127.0.0.1:22012 进）----
    pod_ssh_host: str = "127.0.0.1"
    pod_ssh_port: int = 22012
    pod_ssh_user: str = "cloud"
    pod_ssh_password: str = ""
    pod_app_dir: str = "/home/cloud/sse_market_assist"
    # ---- 采集目标 ----
    nginx_container: str = "sse_market_server-nginx_proxy-1"
    market_db_container: str = "sse_market_db"
    market_db_name: str = "market"
    cert_glob: str = "/root/market-deploy/Nginx/live/*/fullchain.pem"
    disk_warn_pct: int = 80
    disk_crit_pct: int = 90
    # ---- 本地路径 ----
    digest_db: Path = DIGEST_DIR / "data" / "tech-digest.db"
    digest_log: Path = DIGEST_DIR / "log" / "tech-digest.log"
    data_dir: Path = BASE_DIR / "data"

    @property
    def mail_from(self) -> str:
        return self.smtp_user

    @property
    def smtp_ready(self) -> bool:
        return bool(self.smtp_user and self.smtp_auth_code and self.mail_to)

    @classmethod
    def from_env(cls) -> "OpsSettings":
        load_env()
        return cls(
            smtp_host=os.environ.get("OPS_SMTP_HOST", "smtp.163.com"),
            smtp_port=int(os.environ.get("OPS_SMTP_PORT", "465")),
            smtp_user=os.environ.get("OPS_SMTP_USER", ""),
            smtp_auth_code=os.environ.get("OPS_SMTP_AUTH_CODE", ""),
            mail_to=os.environ.get("OPS_MAIL_TO", ""),
            interval_days=int(os.environ.get("OPS_INTERVAL_DAYS", "2")),
            pod_ssh_host=os.environ.get("OPS_POD_SSH_HOST", "127.0.0.1"),
            pod_ssh_port=int(os.environ.get("OPS_POD_SSH_PORT", "22012")),
            pod_ssh_user=os.environ.get("OPS_POD_SSH_USER", "cloud"),
            pod_ssh_password=os.environ.get("OPS_POD_SSH_PASSWORD", ""),
            pod_app_dir=os.environ.get("OPS_POD_APP_DIR", "/home/cloud/sse_market_assist"),
            nginx_container=os.environ.get(
                "OPS_NGINX_CONTAINER", "sse_market_server-nginx_proxy-1"),
            market_db_container=os.environ.get("OPS_MARKET_DB_CONTAINER", "sse_market_db"),
            market_db_name=os.environ.get("OPS_MARKET_DB_NAME", "market"),
            cert_glob=os.environ.get(
                "OPS_CERT_GLOB", "/root/market-deploy/Nginx/live/*/fullchain.pem"),
            disk_warn_pct=int(os.environ.get("OPS_DISK_WARN_PCT", "80")),
            disk_crit_pct=int(os.environ.get("OPS_DISK_CRIT_PCT", "90")),
        )
