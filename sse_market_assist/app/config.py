# -*- coding: utf-8 -*-
"""配置加载：从服务根目录 .env 读取，全部环境变量化（与服务器现有风格一致）。"""
from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

# 服务根目录（main.py 所在目录）
BASE_DIR = Path(__file__).resolve().parent.parent
load_dotenv(BASE_DIR / ".env")

# 可选发帖分区白名单（主页为聚合流/历史默认分区，不可发帖；课程交流后端禁止发帖，仍保留以防未来开放）
PARTITIONS: tuple[str, ...] = tuple(
    p.strip()
    for p in os.environ.get(
        "ASSIST_PARTITIONS",
        "学习交流,实习就业,宿舍生活,技术分享,期末资料,生活日常,社团活动,组团捞人,课程专区,其他",
    ).split(",")
    if p.strip()
)

# 索引排除的分区。主站发帖默认分区为"主页"（聚合流，非真实子分区），
# 2026-09-08 起纳入索引 + 跨分区检索（此前排除导致主页帖检索不可达）
EXCLUDE_PARTITIONS: tuple[str, ...] = ()

# "主页"分区帖 = 默认发帖的聚合流帖子：检索时放宽为跨全部分区
ALL_PARTITIONS_MARKER: str = "主页"


class Settings:
    """运行配置（普通类，避免过度设计）。"""

    # ---- 主通道：集市自有网关（OpenAI 兼容；2026-09-10 起由 DeepSeek 官方直连改为主用）----
    # 实测（2026-09-10，真实 RAG prompt）：9.8s、引用与格式均达标，与官方通道质量持平。
    # 注意：本网关 deepseek 系模型不接受 reasoning_effort="none"（400 ModelArts.81001，
    # 仅收 low/medium/high/xhigh/max）；若换 qwen3.8-27b-awq 则必须显式设 none，
    # 否则思考烧光 token 输出空内容。
    sse_market_api_key: str = os.environ["SSE_MARKET_API_KEY"]
    sse_market_base_url: str = os.environ.get("SSE_MARKET_BASE_URL", "https://api.<MARKET_DOMAIN>/v1")
    sse_market_model: str = os.environ.get("SSE_MARKET_MODEL", "deepseek-v4-flash")

    # ---- 备用通道：DeepSeek 官方直连（主通道失败时自动切换；未配 key 则退化为单通道）----
    deepseek_api_key: str = os.environ.get("DEEPSEEK_API_KEY", "")
    deepseek_base_url: str = os.environ.get("DEEPSEEK_BASE_URL", "https://api.deepseek.com/v1")
    deepseek_model: str = os.environ.get("DEEPSEEK_MODEL", "deepseek-v4-flash")

    # ---- 判断层（2026-09-12）：按 prompt 体量分流——小的走 qwen（网关同 key），大的走 deepseek ----
    # 判定依据是实际拼装后的 prompt（系统提示 + 问题 + 检索材料）的 token 预估，而非问题本身字数：
    # 全分区检索下检索材料可达 2 万+ 字符、是 prompt 的主要构成，只看问题长度会把
    # "小问题 + 大材料"的巨型 prompt 送给小窗口模型（爆上下文）。
    # 安全不变式：route_small_max_tokens + 输出上限 + 余量 ≤ small_model_window，
    # 由 llm._check_window_safety 启动自检，越界即自动全程改走 deepseek。
    route_enabled: bool = os.environ.get("ASSIST_ROUTE_ENABLED", "1") == "1"
    route_small_max_tokens: int = int(os.environ.get("ASSIST_ROUTE_SMALL_MAX_TOKENS", "20000"))
    # 网关同 key 的另一模型；qwen 系为思考模型，llm.py 会强制 reasoning_effort=none
    small_model: str = os.environ.get("SSE_MARKET_SMALL_MODEL", "qwen3.8-27b-awq")
    # 小模型上下文窗口（2026-09-12 pod 实测标定：128K 字符被 400 拒绝，
    # 报错原文 "maximum context length is 65536 tokens"；估算器为上界估计，
    # 阈值按估算值设、真实用量更低，余量充足）
    small_model_window: int = int(os.environ.get("ASSIST_SMALL_MODEL_WINDOW", "65536"))

    # Embedding（默认平台自建 bge-m3 服务，1024 维；openai 模式兼容 DashScope/SiliconFlow）
    embedding_api_key: str = os.environ.get("EMBEDDING_API_KEY", "")
    embedding_base_url: str = os.environ.get(
        "EMBEDDING_BASE_URL", "http://127.0.0.1:23011"
    )
    embedding_model: str = os.environ.get("EMBEDDING_MODEL", "bge-m3")
    embed_mode: str = os.environ.get("EMBEDDING_MODE", "texts")  # texts=平台服务 / openai=OpenAI 兼容

    # Qdrant（复用现有实例，只操作新 collection；api_key 为空时不带鉴权头，
    # 未来切平台 Qdrant（127.0.0.1:25033）时填入即可）
    qdrant_url: str = os.environ.get("QDRANT_URL", "http://127.0.0.1:6333")
    qdrant_api_key: str = os.environ.get("QDRANT_API_KEY", "")
    qdrant_collection: str = os.environ.get("QDRANT_COLLECTION", "market_assist_v1")

    # MySQL（只读）
    mysql_host: str = os.environ.get("MYSQL_HOST", "127.0.0.1")
    mysql_port: int = int(os.environ.get("MYSQL_PORT", "3506"))
    mysql_user: str = os.environ.get("MYSQL_USER", "root")
    mysql_password: str = os.environ.get("MYSQL_PASSWORD", "")
    mysql_database: str = os.environ.get("MYSQL_DATABASE", "market")

    # 服务自身（19082 已被现有 market-rag-public-proxy 占用；阶段2 起经 23012 TLS 网关 -> frps 13012 -> 8080）
    listen: str = os.environ.get("ASSIST_LISTEN", "0.0.0.0:8080")
    admin_token: str = os.environ.get("ASSIST_ADMIN_TOKEN", "assist-admin-dev")

    @property
    def listen_host(self) -> str:
        return self.listen.split(":")[0]

    @property
    def listen_port(self) -> int:
        return int(self.listen.rsplit(":", 1)[1])

    # 检索参数（rag-lab 基线权重，评测后调优）
    top_k: int = int(os.environ.get("ASSIST_TOP_K", "5"))
    vector_limit: int = int(os.environ.get("ASSIST_VECTOR_LIMIT", "12"))
    vector_threshold: float = float(os.environ.get("ASSIST_VECTOR_THRESHOLD", "0.35"))
    evidence_limit: int = int(os.environ.get("ASSIST_EVIDENCE_LIMIT", "8"))  # 评论级证据（回复+楼中楼）条数
    w_vector: float = float(os.environ.get("ASSIST_W_VECTOR", "0.48"))
    w_keyword: float = float(os.environ.get("ASSIST_W_KEYWORD", "0.27"))
    w_heat: float = float(os.environ.get("ASSIST_W_HEAT", "0.08"))
    llm_rewrite: bool = os.environ.get("ASSIST_LLM_REWRITE", "1") == "1"

    # 防二次生成守卫（2026-09-22）：postID 有留底直接返回库内答案、同帖并发单飞；
    # 置 0 回滚旧行为（每次现场生成）
    dedup_enabled: bool = os.environ.get("ASSIST_DEDUP_ENABLED", "1") == "1"

    # 检索提供方（2026-09-22）：local=自建 MySQL LIKE+Qdrant 融合链路（默认，行为不变）；
    # rag=group-rag /kb/search（BM25+向量 RRF 融合+自适应精排，单次 HTTP 调用替代两路检索）
    retrieval_provider: str = os.environ.get("ASSIST_RETRIEVAL_PROVIDER", "local")
    rag_base_url: str = os.environ.get(
        "ASSIST_RAG_BASE_URL", "http://group-rag.clouds.svc.cluster.local:8080"
    )
    rag_timeout: float = float(os.environ.get("ASSIST_RAG_TIMEOUT", "15"))  # 可能含一次 rerank 网络调用

    # 索引参数
    chunk_size: int = int(os.environ.get("ASSIST_CHUNK_SIZE", "800"))
    chunk_overlap: int = int(os.environ.get("ASSIST_CHUNK_OVERLAP", "120"))
    embed_batch: int = int(os.environ.get("ASSIST_EMBED_BATCH", "10"))
    embed_qps: float = float(os.environ.get("ASSIST_EMBED_QPS", "2"))

    # 自动同步 watch（发帖/评论自动入索引）：独立进程周期轮询 MySQL，
    # 不在发帖请求路径上（Go 后端零改动），天然不阻塞发帖；单周期失败自动下周期重试
    sync_interval: float = float(os.environ.get("ASSIST_SYNC_INTERVAL", "30"))
    sync_cleanup_cycles: int = int(os.environ.get("ASSIST_SYNC_CLEANUP_CYCLES", "20"))

    # ---- WebSearch（站内相关内容不足时的站外搜索补充）----
    web_search_enabled: bool = os.environ.get("WEB_SEARCH_ENABLED", "1") == "1"
    # provider=bing（pod 直连 www.bing.com 实测可达；duckduckgo 域名在 pod 不可达，勿切）
    web_search_provider: str = os.environ.get("WEB_SEARCH_PROVIDER", "bing")
    web_search_timeout: int = int(os.environ.get("WEB_SEARCH_TIMEOUT", "8"))
    web_search_max_results: int = int(os.environ.get("WEB_SEARCH_MAX_RESULTS", "3"))
    # 最高候选分低于该值触发站外搜索补充。0.50：0.4~0.5 属"语义沾边但答非所问"
    # （如门诊医保问到了拔牙帖），此时站外补充价值大；强相关（≥0.5）仍纯站内。
    web_search_min_score: float = float(os.environ.get("WEB_SEARCH_MIN_SCORE", "0.50"))


settings = Settings()
