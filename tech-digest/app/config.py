# -*- coding: utf-8 -*-
"""配置加载：.env 文件 + os.environ，启动即校验必需项。

不引 python-dotenv，保持零额外依赖（与 assist 的 .env 模式保持一致）。
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent


def _load_dotenv() -> None:
    """把 BASE_DIR/.env 读入 os.environ（已存在的环境变量优先，不覆盖）。"""
    env_path = BASE_DIR / ".env"
    if not env_path.exists():
        return
    with open(env_path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            if key and key not in os.environ:
                os.environ[key] = value


def _get_bool(name: str, default: bool) -> bool:
    v = os.environ.get(name)
    if v is None:
        return default
    return v.strip().lower() in ("1", "true", "yes", "on")


@dataclass
class Settings:
    # ---- AI 主通道（market-deploy 网关，与 cc-switch 配置一致）----
    sse_market_base_url: str = "https://api.<MARKET_DOMAIN>/v1"
    sse_market_api_key: str = ""
    sse_market_model: str = "qwen3.8-27b-awq"
    # ---- AI 备用通道（DeepSeek Anthropic 兼容端点；主通道失败时自动切换）----
    deepseek_base_url: str = "https://api.deepseek.com/anthropic"
    deepseek_api_key: str = ""
    deepseek_model: str = "deepseek-v4-flash-vision-exp"
    # ---- 集市发帖（M2，refresh_token 由站长登录集市一次取得）----
    market_refresh_token: str = ""
    market_user_telephone: str = ""
    market_base_url: str = ""  # 留空用 http://127.0.0.1:8080（主站内网直连）
    market_post_partition: str = "技术分享"
    market_post_tag: str = "前沿技术|周报"
    # ---- 任务参数 ----
    top_n: int = 15
    load_threshold: float = 2.5
    check_load: bool = field(default_factory=lambda: _get_bool("TECH_DIGEST_CHECK_LOAD", True))
    http_timeout: int = 20
    # ---- v2.0：多源（v4 起含 zhihu-hot：tophub 中转的知乎热榜）----
    tech_digest_sources: list[str] = field(default_factory=lambda: [
        "github-trending", "hacker-news", "sspai", "zhihu-hot", "devto",
        "wallstreetcn",
        "ars-technica", "ieee-spectrum", "freecodecamp", "ruanyifeng"])
    daily_publish: bool = field(default_factory=lambda: _get_bool("TECH_DIGEST_DAILY_PUBLISH", True))
    # 周末发帖开关（2026-09-13 用户决策）：默认关 —— 周末照常抓取入库（喂周报），但不发帖。
    publish_weekend: bool = field(default_factory=lambda: _get_bool("TECH_DIGEST_WEEKEND_PUBLISH", False))
    output_dir: Path = BASE_DIR / "output"
    data_dir: Path = BASE_DIR / "data"
    log_dir: Path = BASE_DIR / "log"
    # 保留策略
    daily_retain_days: int = 90
    run_log_retain: int = 500

    @classmethod
    def from_env(cls) -> "Settings":
        _load_dotenv()
        return cls(
            sse_market_base_url=os.environ.get("SSE_MARKET_BASE_URL", "https://api.<MARKET_DOMAIN>/v1"),
            # 秋坞部署：未显式配 SSE_MARKET_API_KEY 时回落平台租户钥匙（LLM 网关 Bearer）
            sse_market_api_key=os.environ.get("SSE_MARKET_API_KEY") or os.environ.get("QDOCK_API_KEY", ""),
            sse_market_model=os.environ.get("SSE_MARKET_MODEL", "qwen3.8-27b-awq"),
            deepseek_base_url=os.environ.get("DEEPSEEK_BASE_URL", "https://api.deepseek.com/anthropic"),
            deepseek_api_key=os.environ.get("DEEPSEEK_API_KEY", ""),
            deepseek_model=os.environ.get("DEEPSEEK_MODEL", "deepseek-v4-flash-vision-exp"),
            market_refresh_token=os.environ.get("MARKET_REFRESH_TOKEN", ""),
            market_user_telephone=os.environ.get("MARKET_USER_TELEPHONE", ""),
            market_base_url=os.environ.get("MARKET_BASE_URL", ""),
            market_post_partition=os.environ.get("MARKET_POST_PARTITION", "技术分享"),
            market_post_tag=os.environ.get("MARKET_POST_TAG", "前沿技术|周报"),
            top_n=int(os.environ.get("TECH_DIGEST_TOP_N", "15")),
            load_threshold=float(os.environ.get("TECH_DIGEST_LOAD_THRESHOLD", "2.5")),
            check_load=_get_bool("TECH_DIGEST_CHECK_LOAD", True),
            http_timeout=int(os.environ.get("TECH_DIGEST_HTTP_TIMEOUT", "20")),
            tech_digest_sources=[s.strip() for s in os.environ.get(
                "TECH_DIGEST_SOURCES",
                "github-trending,hacker-news,sspai,zhihu-hot,devto,wallstreetcn,"
                "ars-technica,ieee-spectrum,freecodecamp,ruanyifeng").split(",") if s.strip()],
            daily_publish=_get_bool("TECH_DIGEST_DAILY_PUBLISH", True),
            publish_weekend=_get_bool("TECH_DIGEST_WEEKEND_PUBLISH", False),
            daily_retain_days=int(os.environ.get("TECH_DIGEST_RETAIN_DAYS", "90")),
            run_log_retain=int(os.environ.get("TECH_DIGEST_RUNLOG_RETAIN", "500")),
        )

    def validate(self, need_ai: bool) -> None:
        """校验必需配置。need_ai=True 时（weekly）要求主/备任一 AI key；daily 允许全部缺失。"""
        missing = []
        if need_ai and not (self.sse_market_api_key or self.deepseek_api_key):
            missing.append("SSE_MARKET_API_KEY/DEEPSEEK_API_KEY（至少其一）")
        dirs = [self.output_dir, self.data_dir, self.log_dir]
        for d in dirs:
            if d.exists() and not d.is_dir():
                raise RuntimeError(f"{d} 存在但不是目录")
        if missing:
            raise RuntimeError(f"缺少必需配置: {', '.join(missing)}")


settings = Settings.from_env()
