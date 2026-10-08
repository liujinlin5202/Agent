# tech-digest 编辑部迭代计划（content pool + 编辑部流水线）

> 2026-10-08 定稿。交接文档：写给下一个接手的会话/协作者——包含全部决策理由、
> 现状基线、架构设计、里程碑切分与上手指南。读这一篇即可开工，无需上下文考古。

---

## 1. 背景与定位

tech-digest 是集市「技术分享」区的**内容生产中心**（编辑部）：每天一篇日报帖、
周六星榜帖、周日周报帖。2026-10-08 已完成秋坞平台割接（见
[QIUDOCK_DEPLOY.md](QIUDOCK_DEPLOY.md)），生产形态 = 秋坞 job 租户，GitHub 仓库
直连部署。

割接当天的复盘结论：现有形态是「定时脚本」而非「内容运营」——

| 缺口 | 现状 | 后果 |
|---|---|---|
| 选题薄 | 抓取与发布同班次，规则闸门 60→6，1 次 LLM 终选 | 选题质量看运气 |
| 时效死 | 每天 09:15 一班，素材=当刻快照 | 突发好文永远追不上 |
| 无质量门禁 | `--dry-run` 只校验 token，内容好坏无人把关 | 翻车风险无拦截 |
| 无沉淀 | 高价值文章发过即弃，数据里无二次消费通道 | 运营纵深为零 |

**本轮目标**：把「每天跑一次的日报脚本」升级为「编辑部」——素材池化、
选题全窗口最优、生成带评审环、高价值文章沉淀复用。**发布面不变**：仍是每天
一篇日报帖（用户明确确认），星榜/周报排期不变。

## 2. 现状基线（2026-10-08 快照，接手前先核对）

### 部署与代码

- **代码仓库**：`github.com/liujinlin5202/Agent`（monorepo，本目录 `tech-digest/`
  是其中之一）。我们是仓库主协作者，可直接 push。
- **秋坞侧**：spec = qiudock 仓 `agents/tech-digest.yaml`，现役版本
  `v-2610080401`（daily/star/weekly 三 CronJob 武装）。发布 = 改代码 push GitHub
  → POST /v1/deploys（`{"spec": "<yaml文本>"}`，secrets 占位符由部署脚本从服务器
  `.env` 注入）→ 门禁试跑 → 武装。
- **systemd 全停**：tech-digest 三 timer + ops-report timer 均 disabled（unit
  保留 = 回滚锚）。
- **数据盘**：`/root/SSE_Market/SSE-Agent/tech-digest/data`（SQLite，去重/期数/
  运行日志），秋坞挂载到 `/work/state` 软链为 `tech-digest/data`。

### LLM 链路（重要，有历史坑）

- **主通道 = 秋坞网关**：`SSE_MARKET_BASE_URL=http://10.43.196.33:7700/v1/llm`
  （OpenAI 兼容），鉴权用平台注入的租户钥匙（`QDOCK_API_KEY=@platform` 哨兵 →
  `agkeys-tech-digest` secret）。模型 `qwen-3.8-flash`（GPU 实体
  qwen3.8-flash-next，**思考模型**，必须带 `reasoning_effort: "none"`——llm.py
  已带，勿删）。
- **历史坑**：旧模型 `qwen3.8-27b-awq` 2026-09-01 已从 api.ssemarket.cn 下架，
  此后主通道持续失败、线上一直静默走 DeepSeek 备用通道一个月（`llm.py` 双通道
  自动切换救了场，只有一行日志）。**改 LLM 链路后必须单独实测**——门禁
  dry-run 在 token 校验后就停，不覆盖 AI 调用。验法（服务器上）：
  ```bash
  TKEY=$(kubectl get secret -n qiudock agkeys-tech-digest -o jsonpath="{.data.QDOCK_API_KEY}" | base64 -d)
  curl -s -m 60 http://10.43.196.33:7700/v1/llm/chat/completions \
    -H "Authorization: Bearer $TKEY" -H "Content-Type: application/json" \
    -d '{"model":"qwen-3.8-flash","messages":[{"role":"user","content":"回复两个字：正常"}],"max_tokens":64,"reasoning_effort":"none"}'
  ```
- **备通道**：DeepSeek（Anthropic Messages 兼容端点，thinking disabled 已配）。
  与主通道故障域隔离，保留。

### 算力约束（本轮设计的核心边界）

- 集市云 GPU（qwen 主力）是**免费的、串行的、并发能力弱**的算力。用户原话：
  "后台任务，慢一点没事"。
- 用户预期后续可能换算力：要求**架构上并行、实际串行**（伪并行），换算力时
  改一个环境变量即可切换。
- 抓取不占 GPU（纯 HTTP）——频率不受算力约束，只受反爬约束。

### 平台约束

- qiudock ns quota：**limits.cpu 仍 3 核**（内存已 4Gi）。暖沙箱等常驻已用
  ~2800m，余量 ~450m。job pod 必须显式 `resources: {mem_mib: 512, cpu_milli: 400}`。
- 观测面已就绪：console 定时任务面板（运行历史/立即运行）、LLM 网关全量记账
  （/v1/llm/usage + qdock_llm_* metrics + audit llm.call + Langfuse span）、宿主
  crontab 失败告警（`/usr/local/bin/qdock_cron_alert.sh`，每天 10:45 查当日台账）。
- Langfuse 是 v4 `events_only` 模式：旧 observations/traces 查询 API 返回空，
  OTLP 写入正常（POST /api/public/otel/v1/traces 实测 200）。查数据用事件 API，
  别被旧 API 的 0 条骗了。

## 3. 方案选型记录（为什么是 B 骨架 + C 质量环）

对照 Anthropic《Building Effective Agents》的判定标准（固定路径用 workflow、
动态决策才用 agent）与《多 agent 研究系统》复盘（多 agent 评测 +90.2% 但
token 消耗 15×），我们评估了三个方案：

- **A 单班评审环**：班内加 generate→critique→revise。改动最小但时效/反馈两项
  缺口原地踏步。
- **B 抓取与生成解耦**：content pool + 高频 ingest + 发布窗口从全窗口选最优。
  时效性质变，抓取故障与生成故障隔离，反馈环有数据地基。
- **C 多 agent 编辑部**：orchestrator-workers 深读研究。质量上限最高，但 15×
  token + 工程量 2-4 周级，且我们算力串行——原版并行假设不成立。

**用户裁决（2026-10-08）**：B 为骨架 + 吸收 C 的质量环，理由：B 直接命中
"积累更多文章"（池子=时间杠杆）与"从日报脚本变成内容运营"（定位升级）；
C 的价值在角色分离与评审纪律，不在自主 agent——**固定四角色流水线
（评委→研究员→作者→终审）已经拿到 C 的核心收益，且可预测、可兜底、可串行**。
反馈环（浏览/点赞回流权重）**暂不做**（用户明确），但 pool schema 预留接口。

Anthropic 复盘中被直接收编的四条工程经验：

1. **effort 分级**：规则粗排→批量评分→只对 top 候选深读（串行算力下好钢用在
   刀刃上）。
2. **产物落文件不过对话**（防"传话游戏"）：深读结果写 pool 字段，后续角色从
   库里读，不靠上下文传递。
3. **LLM-as-judge 单次调用 0-1 分最稳**（他们实测与人工判断最一致的判法）：
   终审 reviewer 按此设计。
4. **错误会复合**：每步落库、失败隔离单条、断点续跑（见 §5.5 兜底清单）。

## 4. 目标架构

```
┌─ ingest 租户（每 2h 一班，cron: 7 */2 * * *，零 LLM）─────────┐
│  多源抓取（现有 fetcher 全部源）→ 归一化                       │
│  → 去重（URL 精确 + 标题模糊，复用 dedup 模块）                │
│  → 热度/新鲜度/来源分 → 入池（pool_items，status=candidate）    │
│  反爬命中（403/405）→ 该源冷却 24h，其余源不受累               │
└──────────────────┬──────────────────────────────┘
                   ▼
     content pool = 现有 tech-digest.db 新增 pool_items 表（WAL）
                   │
┌─ digest 租户（daily 09:15 主班，排期不变）───────────────────┐
│  取材：全窗口 candidate（≤7 天）按 热度+来源+新鲜度 粗排 → top15 │
│  编辑部流水线（LLMWorkerPool 伪并行，TECH_DIGEST_LLM_CONCURRENCY=1）：│
│    ① 评委：批量 LLM rubric 评分（15 条/次，2-3 次调用）→ top6-8 │
│    ② 研究员：对 top6-8 深读全文 → 要点/引文写回 pool            │
│    ③ 作者：基于深读产物成稿（现状 daily_ai 的生成逻辑升级）      │
│    ④ 终审：LLM-as-judge 单次 0-1 分（rubric 五维加权）          │
│       < 0.7 → 带评审意见打回作者改一轮 → 复审                    │
│  发帖（现状 publisher 不动）→ 消费的候选标 daily_used            │
│  高分落选者（score≥0.8 且未入选）→ 标 reserved                  │
│  池空/池故障 → 回退现状「即时抓取」老路径（永不因池空停刊）        │
└──────────────────────────────────────────────────────────┘
                   ▼
   star / weekly：排期不变，取材改从 pool（star 用热度榜；weekly 的
   「深度长文」段消费本周 reserved 沉淀——高价值二次曝光）
   生命周期：candidate 7 天未消费 → expired；reserved 留 90 天
```

**发布面确认**（用户确认过的问题）：每天仍是一篇日报帖（帖内 top15+导读，
issue 编号延续）；高价值文章不发新帖，走 weekly 段落二次曝光；「一天多篇」
的口子留在设计里（digest 支持一天多窗口），第一版刻意不开。

## 5. 设计详解（每节 = 一个设计决定 + 理由）

### 5.1 抓取高频、LLM 低频排队（算力分层）

GPU 弱串行 ≠ 全线慢。抓取是纯 HTTP（对 GPU 零占用），**频率只受反爬约束**：
2h 一班是温和起点（知乎/ARS 曾 403/405），env `TECH_DIGEST_INGEST_INTERVAL_H`
可调。所有 LLM 调用过 `LLMWorkerPool`（`asyncio.Semaphore(1)` + 指数退避），
单班总预算 12-15 次调用 × ~8s ≈ 3-5 分钟串行跑完——后台任务完全可接受。
**换算力 = 改 `TECH_DIGEST_LLM_CONCURRENCY`**，代码零改动（架构上所有调用点
已经是并行原语）。

### 5.2 pool 用 SQLite 加表，不引新组件

单机宿主、数据盘已挂载、现有 store.py 成熟——上 PG/独立服务是过度设计。
WAL 模式 + `busy_timeout=5s` 应对 ingest/digest 两 pod 罕见同碰；配合错峰
（ingest 在每 2h 的第 7 分，避开 09:15/09:25 发布班）后冲突概率趋近于零。

### 5.3 编辑部固定四角色，不做自主 lead-agent

自主编排（C 原版）在串行算力上会放大延迟与不可预测性，且失败面指数级。
固定流水线：每步产物落库、每步可独立重试、总预算恒定可算。角色 prompt 各自
独立上下文（评委看不到作者的自我辩护，终审看不到中间博弈）——这是评审独立性
的来源。将来算力升级，流水线内的深读步可平滑升格为并行研究员组（worker 池
并发度调大即可），架构不用动。

### 5.4 评分 rubric 与高价值沉淀

五维：新颖性 / 深度 / 实用性 / 可信度 / 受众匹配（权重读现有 preference.json
的偏好配置，不新增配置面）。`score` 与分项 JSON 落 pool；≥0.8 未入选 →
`reserved`。这份数据是将来反馈环的地基（给 pool_items 加行为信号列即可让
权重可学习——本期不做，接口留好）。

### 5.5 防御性兜底清单（每条对应真实故障面）

| 故障 | 兜底 | 状态 |
|---|---|---|
| LLM 网关 429/5xx | 指数退避×3（1/4/16s+抖动）→ DeepSeek 备通道 → 模板降级 | 通道切换已有，退避新增 |
| 单条评分/深读失败 | 该条标 skip 继续下一条——评委缺席≠停刊 | 新增 |
| 深读抓全文 403/超时 | 放弃深读，用抓取时简介成稿（raw_summary 兜底） | 可爬性预取已有 |
| pool 空 / ingest 连续失败 | digest 班回退「即时抓取」老路径 | 新增（永不因池空停刊） |
| 班次中断（pod 被逐/重启） | deadline_s 平台补跑 + 流水线每步落库，重跑跳过已完成步骤 | 平台已有+断点续跑新增 |
| 两 pod 同碰 SQLite | WAL + busy_timeout + 错峰 | 新增 |
| 反爬封源 | 该源冷却 24h，其余源不受累 | 新增 |

### 5.6 调度与 quota

ingest `7 */2 * * *`（deadline_s 14400），daily/star/weekly 排期不动。错峰后
单 pod 400m 的余量够用；将来若开双发布窗口（早/晚班）再议 quota 3→3.5 核。

## 6. 里程碑切分（每步独立可上线、可回滚）

| 里程碑 | 内容 | 规模 | 验收标准 |
|---|---|---|---|
| **M1 池子底座** | pool_items 表 + `main.py ingest` + digest/star/weekly 取材改 pool + WAL/错峰/空池回退 | ~2 天 | ingest 跑两轮池子有货；daily 照常发帖（内容源=pool）；双门禁（ingest/daily --dry-run）绿 |
| **M2 编辑部质量环** | LLMWorkerPool + 批量评分 + 深读 + 作者/终审修订环 + §5.5 全部兜底 | ~3 天 | 评分与评审轨迹全落库；<0.7 打回链路演练一次；网关故障演练走通备通道；伪并行并发度=1 验证 |
| **M3 高价值沉淀** | reserved 生命周期 + weekly「深度长文」段改从 reserved 深读 | ~1 天 | 下周日首个新段落上线；reserved 样本可追溯（score_detail 完整） |

每步走秋坞正常发版（push GitHub → POST /v1/deploys → 门禁 → 武装），翻车
控制台一键回滚锚点版本。**每个里程碑完成后同步更新 QIUDOCK_DEPLOY.md 的
env 契约表**。

## 7. 新增配置契约（M1 起生效，全部有默认值）

| 环境变量 | 默认 | 说明 |
|---|---|---|
| `TECH_DIGEST_INGEST_INTERVAL_H` | 2 | ingest 抓取间隔（反爬温和起点） |
| `TECH_DIGEST_LLM_CONCURRENCY` | 1 | LLM worker 池并发度（伪并行开关；换算力改这个） |
| `TECH_DIGEST_POOL_TTL_DAYS` | 7 | candidate 过期天数 |
| `TECH_DIGEST_RESERVED_TTL_DAYS` | 90 | reserved 保留天数（对齐现有 RETAIN） |
| `TECH_DIGEST_REVIEW_THRESHOLD` | 0.7 | 终审打回线 |
| `TECH_DIGEST_RESERVE_SCORE` | 0.8 | 高价值沉淀线 |

秋坞侧：spec schedules 增 `ingest` 条目 + evals 增 `ingest-dryrun` 门禁用例
（改动在 qiudock 仓 agents/tech-digest.yaml，M1 落地时提交）。

## 8. 开放问题（不阻塞 M1，到期要裁决）

1. **双发布窗口**：设计已留（digest 支持一天多班），第一版单班。观察 pool
   攒货速度与时效诉求，两周一议。
2. **反馈环**：浏览/点赞/评论回流调权重——数据地基（pool+digest_date）M1 就
   位，功能等用户发令。
3. **models.json 死条目**：`qwen3.8-27b-awq` 条目（路由到已下架模型）仍在
   平台 models.json 里，xiaoqiu-chat spec 的 llm.model 也指着死名字（靠内部
   回落链活着）——属平台清理项，与本期无关但别忘了。
4. **反爬边界**：若 2h 间隔仍触发封禁，升冷却时间或砍高频源，别硬顶。

## 9. 新会话上手指南

```bash
# 1. clone 代码（Windows 本机）
git clone https://github.com/liujinlin5202/Agent.git && cd Agent/tech-digest

# 2. 服务器（ssh SSE_Market 已配好）
export KUBECONFIG=/etc/rancher/k3s/k3s.yaml   # 服务器上执行 kubectl 需要

# 3. 秋坞 API（服务器上）
BASE=http://10.43.196.33:7700
KEY=$(awk '$1=="ops-ctrl"{print $2}' /root/qiudock/tenant-keys.txt)
curl -s -H "Authorization: Bearer $KEY" $BASE/v1/agents/tech-digest | python3 -m json.tool

# 4. 部署：POST /v1/deploys，body={"spec": "<yaml 文本>"}（占位符注入见
#    /tmp/deploy_td.py 的做法——从 tech-digest/.env 读值替换占位符）
# 5. 轮询：GET /v1/runs/{id}（stage 字段名是 "name"）
```

**高频坑清单**（本会话实踩，勿重蹈）：

1. agent-run 契约：clone 到 `/work/repo`；根 requirements.txt 存在才 pip；
   `sh -c` 执行 run。**挂载点不得落在 clone 目录内**（目录非空 clone 即败）——
   数据盘挂 `/work/state` 再软链。
2. 门禁 dry-run 15s 通过≠LLM 链路健康（token 校验后即停）。改 LLM 相关代码
   必须用 §2 的 curl 单测网关。
3. quota：job pod 必须显式 400m/512Mi（默认 500m 排不进，FailedCreate 循环）。
4. qdockd/平台发布与本题无关，但若需要：docker commit 后必须
   `docker save | k3s ctr images import -`；chmod 在宿主先做；别用 --change
   覆盖 ENTRYPOINT。
5. 服务器访问 GitHub 正常（ls-remote/git clone 实测通）。
6. 全站文案禁 emoji（团队 UI 铁律）；文档面向不懂平台的同学。

—— 本计划 2026-10-08 经用户裁决定稿。开工顺序：M1 → M2 → M3。
