# -*- coding: utf-8 -*-
"""配置加载：.env 文件 + os.environ（已存在的环境变量优先，不覆盖）。

秋坞 job 模式下没有 .env（pod 里是 clone 目录），全部配置由 spec env 注入；
宿主手动调试可在 post-curator/.env 放本地值。不引 python-dotenv，与
tech-digest 同款零依赖风格。
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent


def _load_dotenv() -> None:
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


def _get_str(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def _get_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


def _get_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


@dataclass
class Settings:
    # ---- LLM 主通道：秋坞网关（OpenAI 兼容 /v1/llm/chat/completions）----
    # key 走 QDOCK_API_KEY=@platform 哨兵注入的租户钥匙；raw key 不进 spec/git。
    # 2026-10-14 用户裁决：本任务只用秋坞 qwen，不配 glm/DeepSeek 备通道——
    # 通道故障=当日任务失败，由 10:45 qdock_cron_alert 邮件兜底。
    llm_base_url: str = "http://10.43.196.33:7700/v1/llm"
    llm_api_key: str = ""
    llm_model: str = "qwen-3.8-flash"
    llm_timeout: int = 120
    llm_max_tokens: int = 512

    # ---- 集市 MySQL 只读（xiaoqiu_ro 仅 SELECT 两个公开视图，零基表权限）----
    # job pod 是 net: host，宿主回环 3506 直达；不用 tech-digest 的 docker exec
    # 路径（pod 内无 docker，那条路永远降级）。
    db_host: str = "127.0.0.1"
    db_port: int = 3506
    db_user: str = ""
    db_password: str = ""
    db_name: str = "ssemarket"

    # ---- 写通道：集市 Go 后端内部端点（X-Internal-Token 守卫，fail-closed）----
    internal_base_url: str = "http://127.0.0.1:8080"
    internal_token: str = ""
    http_timeout: int = 20

    # ---- 策展参数 ----
    lookback_days: int = 3            # 每日扫描窗口
    content_threshold: float = 7.0    # 正文价值加精线
    comment_threshold: float = 7.0    # 评论区价值加精线（问询帖核心）
    max_judge_per_run: int = 80       # 单次评审上限（成本护栏）
    full_top_n: int = 300             # 全量模式按互动信号预排后评审 top N
    comments_per_post: int = 20       # 送审评论条数（按赞排序）
    post_char_cap: int = 4000         # 正文送审截断
    comment_char_cap: int = 500       # 单条评论送审截断
    rejudge_hours: int = 20           # 同帖评审冷却（3 天窗口内每日复审一次，
                                      # 评论区晚熟的问询帖有机会翻案；--force 可跳过）

    @property
    def data_dir(self) -> Path:
        """状态盘：秋坞挂 /work/state 后软链到 data/；宿主模式是真实目录。"""
        return BASE_DIR / "data"


def load_settings() -> Settings:
    _load_dotenv()
    s = Settings(
        llm_base_url=_get_str("LLM_BASE_URL", Settings.llm_base_url),
        llm_api_key=_get_str("LLM_API_KEY") or _get_str("QDOCK_API_KEY"),
        llm_model=_get_str("LLM_MODEL", Settings.llm_model),
        llm_timeout=_get_int("LLM_TIMEOUT", Settings.llm_timeout),
        llm_max_tokens=_get_int("LLM_MAX_TOKENS", Settings.llm_max_tokens),
        db_host=_get_str("FORUM_DB_HOST", Settings.db_host),
        db_port=_get_int("FORUM_DB_PORT", Settings.db_port),
        db_user=_get_str("FORUM_DB_USER"),
        db_password=_get_str("FORUM_DB_PASSWORD"),
        db_name=_get_str("FORUM_DB_NAME", Settings.db_name),
        internal_base_url=_get_str("MARKET_INTERNAL_BASE_URL", Settings.internal_base_url),
        internal_token=_get_str("INTERNAL_TOKEN"),
        http_timeout=_get_int("CURATOR_HTTP_TIMEOUT", Settings.http_timeout),
        lookback_days=_get_int("CURATOR_LOOKBACK_DAYS", Settings.lookback_days),
        content_threshold=_get_float("CURATOR_CONTENT_THRESHOLD", Settings.content_threshold),
        comment_threshold=_get_float("CURATOR_COMMENT_THRESHOLD", Settings.comment_threshold),
        max_judge_per_run=_get_int("CURATOR_MAX_JUDGE", Settings.max_judge_per_run),
        full_top_n=_get_int("CURATOR_FULL_TOP_N", Settings.full_top_n),
        comments_per_post=_get_int("CURATOR_COMMENTS_PER_POST", Settings.comments_per_post),
        post_char_cap=_get_int("CURATOR_POST_CHAR_CAP", Settings.post_char_cap),
        comment_char_cap=_get_int("CURATOR_COMMENT_CHAR_CAP", Settings.comment_char_cap),
        rejudge_hours=_get_int("CURATOR_REJUDGE_HOURS", Settings.rejudge_hours),
    )
    missing = [k for k, v in (
        ("LLM_API_KEY/QDOCK_API_KEY", s.llm_api_key),
        ("FORUM_DB_USER", s.db_user),
        ("FORUM_DB_PASSWORD", s.db_password),
        ("INTERNAL_TOKEN", s.internal_token),
    ) if not v]
    if missing:
        raise SystemExit(f"post-curator 配置缺失: {', '.join(missing)}")
    return s
