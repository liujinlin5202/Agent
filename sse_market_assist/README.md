# sse-market-assist（集市「分区智能检索 + AI 应答」独立服务）

发帖成功后，检索站内已有帖子/回复/楼中楼，由判断层按 prompt 体量选模型（小 → qwen，大 → deepseek）生成带引用的回答。

## 目录结构

```text
main.py                # 服务入口（uvicorn，127.0.0.1:19084；19082 被现有 rag-public-proxy 占用）
app/
  config.py            # 配置（.env）
  llm.py               # LLM 客户端：判断层（小 prompt→qwen / 大 prompt→deepseek）+ 双通道兜底
  embed.py             # Embedding 客户端（平台 bge-m3 {"texts":[...]} / OpenAI 兼容双协议）
  db.py                # MySQL 只读连接
  qdrant_store.py      # Qdrant 封装（仅操作 market_assist_v1；支持可选 API Key）
  sync.py              # 全量/增量/watch 索引（帖子+回复+楼中楼，≤2 QPS）
  retrieve.py          # 查询改写 + 混合检索 + 合并重排
  context.py           # 检索材料上下文拼装
  generate.py          # LCEL 回答生成
  api.py               # FastAPI 路由
scripts/run_sync.py    # 索引同步（--full/--incremental/--cleanup/--watch）
scripts/smoke_embed.py # embedding 真实 API 冒烟（本地验证供应商可用性）
tests/                 # 单元测试
deploy/sse-market-assist.service  # systemd 单元
```

## 快速开始（服务器上）

```bash
cd /root/market-deploy/sse_market_assist
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt

cp .env.example .env   # 填真实密钥（或使用已部署的 .env）

# 首次全量索引（约 30~60 分钟，断点续传：重跑 --full 自动跳过已入库点）
.venv/bin/python scripts/run_sync.py --full

# 发帖/评论自动入索引（常驻 watch，独立进程轮询 MySQL，不阻塞发帖）
.venv/bin/python scripts/run_sync.py --watch
#   每 ASSIST_SYNC_INTERVAL 秒增量同步一次；embedding 失败（欠费等）仅跳过本周期，
#   下周期自动重试；每 ASSIST_SYNC_CLEANUP_CYCLES 周期顺带清理 is_private=1 的点

# 启动服务
.venv/bin/python main.py     # 或 systemctl start sse-market-assist（阶段 2）

# 自测
curl http://127.0.0.1:19084/health
curl -X POST http://127.0.0.1:19084/api/v1/assist/match \
  -H "Content-Type: application/json" \
  -d '{"partition":"学习交流","title":"大三要不要找老师做项目","content":"对保研有帮助吗"}'

# embedding 供应商冒烟（切换供应商前先跑，验证维度=1024）
.venv/bin/python scripts/smoke_embed.py
```

## 红线（务必遵守）

- MySQL 只读（代码中仅 SELECT）；
- 只操作新 collection `market_assist_v1`，绝不触碰 `market_content`；
- 不修改 Go 主后端 / Nginx / 前端（阶段 2 评审后再动）；
- 私密帖（is_private=1）不索引、不检索；匿名帖不索引作者信息。

## 自动同步（watch）与发帖的关系

- watch 是**独立进程**，只轮询 MySQL 找新内容 → embedding → 写 Qdrant；
- 发帖链路（Go 后端）零改动，发帖请求路径上没有任何 embedding/检索调用，**天然不阻塞发帖**；
- embedding 失败只影响索引新鲜度（新帖稍晚入索引），不影响发帖与已有检索（关键词检索兜底）。

## 部署现状（2026-09-06：平台 Qdrant + market-rag pod）

服务运行在平台 **market-rag pod**（SSH `-p 22012 cloud@<MARKET_DOMAIN>`，`/home/cloud/sse_market_assist`），

- **Qdrant**：平台集群 `qdrant.infra.svc.cluster.local:6333`（v1.19.0，需 key，见 .env）；
  集合名带平台前缀：`gxxxxxx_market_assist_v1`；红线集合 `gxxxxxx_docs` 绝不触碰。
  key 首字符是**大写 I**（截图里的小写 `l` 会 401，2026-09-06 实测）。
- **Embedding**：pod 直连 `https://<MARKET_DOMAIN>:23011/v1/embeddings`（bge-m3，1024 维，无鉴权）。
- **MySQL（只读）**：生产库=市场服务器 `<SERVER_IP>:3506`（market，43 表；pod 经公网 IP 可达）。
- **进程管理**：pod 内无 systemd/supervisord，用 `nohup .venv/bin/python main.py >> logs/server.log 2>&1 &`
  启动（pod 重启后需手动拉起）；`--fresh`/`--watch` 同理写 `logs/sync.log`。
- 服务绑定 `0.0.0.0:19084`（阶段 2 做 nginx/frp 转发前，仅 pod 内部可达）。
