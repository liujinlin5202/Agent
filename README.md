# 自动巡检 agent

个人自动化运维 agent 集合：围绕一个内容社区站点，实现**智能检索问答、AI 辅助发帖应答、前沿技术日报/周报自动生成、运行状态巡检报告**等能力的 Python 服务与工具集。

> 仓库中的 `<SERVER_IP>`、`<MARKET_DOMAIN>`、`sk-****`、`gxxxxxx_` 等均为公开前脱敏占位符；真实值通过各服务目录下的 `.env` 注入（模板见 `.env.example`）。

## 项目组成

```
├── sse_market_assist/     # 【AI 回复】站内智能检索 + AI 应答服务（RAG）
│   ├── app/               #   FastAPI 服务：混合检索（向量+关键词+热度）、LLM 路由、生成、WebSearch 兜底
│   ├── scripts/           #   索引同步（run_sync watch）、冒烟测试、预生成
│   ├── scripts/ops/       #   服务器侧运维脚本（服务重启、同步守护）
│   ├── deploy/            #   systemd 服务单元
│   ├── tests/             #   pytest 单测
│   ├── _pod_check/        #   同一服务在 K8s pod 内运行版本的检查副本（本地诊断用）
│   ├── docs/              #   本板块技术文档与 Superpowers 计划/设计稿
│   └── archive/           #   早期原型（assist mvp）
├── tech-digest/           # 【自动发帖】前沿技术日报/周报自动生成与发布
│   ├── app/               #   抓取（HackerNews/GitHub Trending/DevTo/少数派/知乎热/华尔街见闻）、
│   │                      #   质量闸门（可爬性预取/翻译保全断言/选文标准）、LLM 摘要、集市发布
│   ├── ops_report/        #   定时运维巡检报告（采集→分析→LLM 建议→邮件）
│   ├── scripts/           #   部署、诊断、验证脚本
│   ├── deploy/            #   systemd service+timer（daily 09:15 / star 周六 / weekly 周日）
│   ├── tests/             #   pytest 单测（40+ 文件）
│   ├── docs/              #   本板块技术文档、PRD 与 Superpowers 计划/设计稿
│   └── archive/           #   早期原型（tech-digest-mvp、星榜快照）
└── _archive/              # 本地归档（一次性诊断脚本与抓取产物，不入库，见 .gitignore）
```

## 核心特性

- **RAG 检索问答**：Qdrant 向量检索 + MySQL 关键词检索 + 热度加权的混合排序；LLM 查询改写；站内不足时自动 WebSearch 兜底；RAG 不可用时自动降级本地检索、恢复后自动切回。
- **LLM 多通道路由**：主通道（OpenAI 兼容网关）+ 备用通道自动切换；按 prompt 体量分流小/大模型窗口。
- **质量闸门**（tech-digest）：发布前可爬性预取、翻译后机械断言（围栏数/段落保全/数字保留）、选文标准一票否决。
- **运维巡检**：systemd timer 定时采集服务状态与业务指标，LLM 分析并生成邮件报告；pod 存活性 SSH 探测。

## 快速开始

```bash
# 每个服务目录独立部署
cp sse_market_assist/.env.example sse_market_assist/.env   # 填入真实值
pip install -r sse_market_assist/requirements.txt

# 运行测试
cd tech-digest && python -m pytest tests/ -q
cd sse_market_assist && python -m pytest tests/ -q
```

## 说明

- `.env`、真实凭据、服务器地址一律不入库（`.gitignore` 已兜底）。
- `sse_market_assist/_pod_check` 是同一服务在 K8s pod 内运行版本的检查副本（与宿主机版本改动需双向同步）。
