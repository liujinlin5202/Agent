# -*- coding: utf-8 -*-
"""运维知识库：现象 → 根因 → 处置。

首批规则全部来自真实事故（不是臆想的通用建议）：
  · 2026-09-15/09-20 应答 502（pod 上进程消失）；
  · 2026-08-30 磁盘 97% 整机挂死；
  · embedding 服务 502 导致同步失败；
  · 集市 refresh_token 过期；
  · nginx 改了 conf 但容器没重启（导致改回路由不生效）。
命中规则 = 给出标准处置；没命中的新错误交给 llm_advice 给候选根因。
"""
from __future__ import annotations

from dataclasses import dataclass, field

from ops_report.model import Advice


@dataclass
class Rule:
    id: str
    contains: list = field(default_factory=list)   # 全部命中才算（小写子串比较）
    title: str = ""
    cause: str = ""
    action: str = ""


RULES: list[Rule] = [
    Rule(
        id="assist_pod_down",
        contains=["upstream prematurely closed connection"],
        title="应答服务进程不在（pod 上 main.py 消失）",
        cause=("nginx 把 /api/v1/assist/ 转给 frps 13012 → pod:8080。pod 上没有进程"
               "监听 8080 时，frps 会立刻关闭连接，nginx 记 "
               "'upstream prematurely closed connection' 并返回 502。"
               "pod 被 K8s 重建后进程不会自动拉起，这是复发的根因。"),
        action=("1) 从集市服务器进 pod：sshpass -e ssh -p 22012 cloud@127.0.0.1\n"
                "2) 启动服务：cd ~/sse_market_assist && nohup .venv/bin/python main.py "
                ">> logs/service.log 2>&1 &\n"
                "3) 验证监听：ss -ltn | grep 8080\n"
                "4) 从服务器验证链路：curl -s -o /dev/null -w '%{http_code}' "
                "http://127.0.0.1:13012/docs（应 200）\n"
                "5) 用真实请求复验 /api/v1/assist/match 返回含 meta 字段\n"
                "6) 长期：给 pod 加存活探针/自启脚本，避免下次重建后又静默 502"),
    ),
    Rule(
        id="disk_critical",
        contains=["磁盘使用率", "已达告警线"],
        title="磁盘水位告急",
        cause=("2026-08-30 曾因磁盘 97% 导致整机挂死（docker 无法写日志、容器全部异常）。"
               "根分区被 build cache 与容器日志吃掉是主要来源。"),
        action=("1) 看大头：du -xh --max-depth=1 / 2>/dev/null | sort -h | tail -20\n"
                "2) 清 build cache：docker builder prune -af（保留 72h 滚动策略）\n"
                "3) 清容器日志：truncate -s 0 $(docker inspect --format='{{.LogPath}}' "
                "$(docker ps -q))\n"
                "4) 复核：df -h /（目标 < 80%）\n"
                "5) 若涉及数据库：先确认备份再动，见 INCIDENT-2026-08-30.md"),
    ),
    Rule(
        id="embedding_unavailable",
        contains=["embed"],
        title="向量服务（embedding）不可用",
        cause=("检索链路依赖 embedding HTTP 服务，它 502 时向量检索会降级为仅关键词"
               "（retrieve.py 有兜底，不会 500），但同步脚本会整周期失败。"),
        action=("1) 确认 embedding 服务地址与端口是否在跑（见 .env 的 EMBED_BASE_URL）\n"
                "2) 容器/进程重启后验证：curl -s $EMBED_BASE_URL/health\n"
                "3) 跑一次增量同步观察：.venv/bin/python scripts/run_sync.py --incremental\n"
                "4) 注意：降级期间检索质量下降但服务不中断，不必紧急回滚"),
    ),
    Rule(
        id="market_token_expired",
        contains=["refresh_token"],
        title="集市凭据（refresh_token）失效",
        cause="token 过期或站点侧重新登录导致失效，发帖/评论会被拒。",
        action=("1) 重新获取：cd tech-digest && .venv/bin/python scripts/fetch_refresh_token.py\n"
                "2) 写回 .env 的 MARKET_REFRESH_TOKEN（chmod 600）\n"
                "3) 手工验证一次：.venv/bin/python main.py daily --dry-run\n"
                "4) 若刚改过账号手机号，同步更新 MARKET_USER_TELEPHONE"),
    ),
    Rule(
        id="nginx_route_stale",
        contains=["没有任何请求"],
        title="nginx 路由疑似未生效",
        cause=("改过 nginx conf 后若没重启 nginx_proxy 容器，容器内仍是旧配置；"
               "2026-09-15 就是这么全线 404 的。"),
        action=("1) 对照容器内外配置：docker exec sse_market_server-nginx_proxy-1 "
                "cat /etc/nginx/custom.conf | grep -A3 'assist'\n"
                "2) 重启代理：cd /root/market-deploy/SSE_market_server && "
                "docker compose restart nginx_proxy\n"
                "3) 公网复验：curl -s -o /dev/null -w '%{http_code}' "
                "https://<MARKET_DOMAIN>/api/v1/assist/post/<最近一条帖ID>"),
    ),
    Rule(
        id="llm_channel_switch",
        contains=["主通道", "备用"],
        title="LLM 主通道故障（已自动切备用）",
        cause="集市网关链路异常时 app/llm.py 会自动切 DeepSeek 备用通道，服务不中断。",
        action=("1) 若长期切备用（连续多日）：查网关 https://api.<MARKET_DOMAIN>/v1 的可用性\n"
                "2) 备用通道有额度成本，长期故障要盯 DEEPSEEK 用量\n"
                "3) 不需要立即干预，但要确认不是主通道 key 被封"),
    ),
    Rule(
        id="publish_failed",
        contains=["发帖", "失败"],
        title="发帖动作重试后仍失败",
        cause="可能原因：token 失效、集市接口变更、分区/标签不存在、内容触发风控。",
        action=("1) 看 detail 里的原始错误（run_log 的 detail.error）\n"
                "2) 手工重跑一次观察：.venv/bin/python main.py daily --force\n"
                "3) token 类错误按「集市凭据失效」处理；接口变更要对比 publisher.py 的请求体\n"
                "4) 内容风控要换选题，别硬重试同一篇"),
    ),
]


def match(signals: list[str]) -> list[Advice]:
    """信号（归一化错误签名 + 指标提示）→ 命中的处置建议，去重保序。

    关键词必须在**单条信号内**全部命中：跨信号拼串会让「发帖成功率 100%」
    和另一条含「失败」的无关报错凑齐 publish_failed 的两个词，高频误触发。
    """
    sigs = [str(s).lower() for s in (signals or [])]
    out, seen = [], set()
    for rule in RULES:
        if rule.id in seen:
            continue
        if any(all(kw.lower() in sig for kw in rule.contains) for sig in sigs):
            seen.add(rule.id)
            out.append(Advice(
                title=rule.title,
                action=f"【可能原因】{rule.cause}\n【处置步骤】\n{rule.action}",
                origin="knowledge"))
    return out


def signals_from(sections: list) -> list[str]:
    """把各板块的错误签名、指标值与说明汇成信号串，供 match 匹配。"""
    sigs = []
    for sec in sections or []:
        for e in getattr(sec, "errors", []) or []:
            sigs.append(e.signature)
        for k, v in (getattr(sec, "metrics", {}) or {}).items():
            sigs.append(f"{k} {v}")
        for n in getattr(sec, "notes", []) or []:
            sigs.append(n)
    return sigs
