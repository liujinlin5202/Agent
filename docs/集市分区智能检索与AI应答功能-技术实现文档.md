# 集市「分区智能检索 + AI 应答」功能技术实现文档

| 项目 | 内容 |
| --- | --- |
| 文档版本 | v1.8（评审稿 + MVP 实验结果 + 正式版代码 v0.1 + 阶段 1 验证记录 + Embedding 盘点 + 平台 bge-m3 切换与自动同步 watch + **平台 embedding 上线确认、服务迁 market-rag pod + 平台 Qdrant 写入（§13.7）**，尚未对外上线） |
| 日期 | 2026-09-07 |
| 目标 | 用户发帖时，后台将该帖信息与该分区内已有数据（帖子标题/正文/回复/楼中楼）匹配检索，并由 AI 生成回答，帮助发帖人获得相关信息 |
| 状态 | 🟢 **阶段 0/1 已全部打通（2026-09-07）**：服务运行于平台 **market-rag pod**（`/home/cloud/sse_market_assist`，绑 0.0.0.0:19084）；平台 bge-m3 服务已上线（OpenAI 兼容 `/v1/embeddings`，1024 维，2026-09-06 实测）；`--fresh` 全量重建 **22,452 点**完成，随后 `--watch` 自动同步捕获线上新帖（累计 22,463 点）；**3 分区 × 10 真实用例抽测 30/30 通过**（无降级、固定 5 引用，约 1/3 直接命中、其余诚实返回"站内暂无直接讨论"+相近参考）；Nginx/前端/Go 均未改动（阶段 2/3 待评审，评审前勿动） |

---

## 1. 背景与目标

### 1.1 功能描述

用户在集市发帖时填写**标题 + 正文**并选择**分区**（如【学习交流】【生活日常】）。新增功能要求：

1. 后台将用户表达的信息（标题 + 正文）与该**分区内已有数据**进行匹配检索；
2. 匹配范围包括该分区内已有帖子的**标题、正文、回复（一级评论）、楼中楼（回复的回复）**；
3. 基于检索结果，由 **AI 生成回答**，让发帖人在发帖后即可获得"站内是否已有相关讨论、结论是什么"的信息；
4. 回答附**相关帖子引用链接**，用户可点击跳转。

### 1.2 触发时机

推荐分两步（详见 3.3 方案对比）：

- **一期（推荐）**：发帖成功后由**前端**触发调用新服务，结果展示在发帖成功页的"AI 帮你找到了相关讨论"卡片中，仅发帖人可见。
- **二期（可选）**：由 Go 主后端在发帖成功后异步触发，结果缓存后供前端读取（需要改动主后端，风险更高）。

### 1.3 设计红线（因"担心影响现有功能"而设立）

1. **发帖主流程绝对不变**：一期方案下 Go 主后端零改动，发帖请求、数据库写入、返回内容与现在完全一致。
2. **不修改现有向量库 collection**：smart-qiu 正在使用的 `market_content` 只读不动，新功能使用**新建的独立 collection**。
3. **新服务独立部署、独立端口、可一键停**：任何异常只影响新功能本身，通过开关即可回退到无此功能的状态。
4. **数据库只读为主**：新服务对集市 MySQL 仅做 `SELECT`（索引同步使用只读查询；写入只发生在新建的 Qdrant collection 内）。

---

## 2. 现状调研（2026-08-26 服务器实测）

### 2.1 集市主站架构

| 组件 | 说明 |
| --- | --- |
| 主后端 | Go（Gin + GORM），路径 `/root/market-deploy/SSE_market_server`，容器 `sse_market_server`（:8080） |
| 数据库 | MySQL 容器 `sse_market_db`（宿主机映射 :3506，库名 `market`，账号 root/xxx） |
| 缓存/队列 | Redis 容器 `sse_market_server-redis-1`；`mq/archive_queue.go` 为 Redis 延迟队列（发帖/评论 1h 后消费，当前消费端已停用） |
| 网关 | Nginx 容器 `sse_market_server-nginx_proxy-1`（80/443），配置 `/root/market-deploy/nginx_proxy/nginx.conf` |
| 前端 | Vue3 构建产物 `/root/market-deploy/sse_market_new_client/newSSE/dist`，挂载于 Nginx `/new` |

### 2.2 数据模型（与检索功能直接相关）

表名（GORM 复数命名）：

| 表 | 角色 | 关键字段 | 数据量 |
| --- | --- | --- | --- |
| `posts` | 帖子 | `postID`, `partition varchar(10)`, `title varchar(90)`, `ptext varchar(10000)`, `heat double`, `tag`, `post_time`, `is_private`, `is_anonymous` | **4163** |
| `pcomments` | 一级回复 | `pcommentID`, `ptargetID`(→postID), `pctext varchar(3000)`, `like_num`, `deny_num`, `time` | **12126** |
| `ccomments` | 楼中楼 | `ccommentID`, `ctargetID`(→pcommentID), `cctext varchar(3000)`, `usertargetName`(被回复人), `like_num`, `time` | **8743** |

**分区枚举**（`SELECT DISTINCT partition FROM posts` 实测）：`主页`、`其他`、`学习交流`、`实习就业`、`宿舍生活`、`技术分享`、`期末资料`、`生活日常`、`社团活动`、`组团捞人`、`课程专区`。其中 `主页` 为历史默认分区（490 条早期帖子，postID 很小，与真实分区几乎无重复），非可选发帖分区；发帖可选分区以后端校验为准。**最大分区为 生活日常（1104 帖），其次 学习交流（855）**。

**隐私字段**：`is_private=1 或 is_anonymous=1` 的帖子共 **245** 条，检索与回答必须处理（见 6.2）。（2026-09-06 复核：posts 4350 / pcomments 12519 / ccomments 9119，随发帖持续增长；`--fresh` 实测入库 22,452 点，见 §13.7。）

### 2.3 现有搜索能力

- 主后端仅提供 `POST /api/auth/searchPostsByTitle`（`controller/voteController.go`），**纯标题关键词匹配**，不支持正文/回复/楼中楼检索，无分区过滤参数。
- **站内已有公开 RAG 检索**（2026-08-26 补查）：Nginx `location /api/rag-local/` → `host.docker.internal:19082` 上的 `market-rag-public-proxy`（systemd 单元 `market-rag-public-proxy.service`，脚本 `sse_market_new_client/newSSE/scripts/rag_public_proxy.py`，监听 `172.17.0.1:19082`）→ 转发 rag-lab（:19081），带搜索结果缓存与热门检索词聚类。本功能与其定位不同（**发帖语境、按分区过滤、含回复/楼中楼、AI 生成回答**），互不替代。

### 2.4 现有 AI / 检索基础设施（可直接复用）

| 基础设施 | 位置 / 现状 | 对本功能的意义 |
| --- | --- | --- |
| **LLM 主通道：集市网关（2026-09-10 起为主用）** | `https://api.<MARKET_DOMAIN>/v1/chat/completions`（OpenAI 兼容，New API 网关 `grp-new-api-poc` 23008 存活），模型 `deepseek-v4-flash`。与 tech-digest 的主通道同网关同 key。实测（2026-09-10，真实 RAG prompt）：9.8s、引用与格式达标，与 DeepSeek 官方通道质量持平 | 对话生成主用；⚠️ 该网关 deepseek 系模型**不接受** `reasoning_effort="none"`（400 `ModelArts.81001`，仅收 low/medium/high/xhigh/max）；若换 `qwen3.8-27b-awq` 则**必须**设 `none`，否则思考烧光 token 输出空内容 |
| **LLM 备用通道：DeepSeek 官方** | 服务器现有智能小秋（Go/Eino 版，生产 :18080；LangGraph 版，实验 :18081）已用 `base_url=https://api.deepseek.com/v1`、`model_name=deepseek-v4-flash`（`agent/SSE-SMART-QIU/config/config.yaml`） | 主通道失败时自动切换（LangChain `with_fallbacks`），并打印切换日志；未配 key 则退化为单通道 |
| **Embedding（现用）** | **平台 bge-m3 服务（2026-09-06 实测已上线）**：OpenAI 兼容 `POST https://<MARKET_DOMAIN>:23011/v1/embeddings` `{"model":"bge-m3","input":[...]}` → 200 / **1024 维** / 无鉴权 / 公网有效证书（strict verify OK）。⚠️ 旧格式 `POST {base}/embed {"texts":[...]}` **已下线**（HTTP 入口 301→HTTPS 且 POST 变 GET → 405；必须 HTTPS 直连），客户端以 `EMBEDDING_MODE=openai` + HTTPS base URL 使用（详见 §2.7）。2026-08-28 起本功能由 DashScope（欠费）切换到该服务 | 与 Qdrant collection 维度 1024 一致；客户端支持 texts/openai 双协议，供应商可配置切换 |
| **Embedding（备选）** | OpenAI 兼容服务（DashScope `text-embedding-v4` / SiliconFlow `BAAI/bge-m3`），改 `.env` 4 行即可切回 | 服务器无本地模型，全部远程 API（2.6 全量盘点） |
| **向量库 Qdrant（现用，2026-09-06 起）** | **平台 K8s 集群 Qdrant 1.19.0**：集群内 `http://qdrant.infra.svc.cluster.local:6333`（gRPC 6334），frp 等价入口 `127.0.0.1:25033`（REST）`/25034`（gRPC）；**需 API Key**——首字符是**大写 I**（`I****...`，截图/聊天记录里的小写 `l` 会 401）；**集合前缀实测 = `gxxxxxx_`**，集群现存 `gxxxxxx_docs`（新的红线集合，勿动；Mony 聊天给的前缀 `gb1ca3490_` 已过时） | 新功能 collection = `gxxxxxx_market_assist_v1`（1024/cosine，2026-09-06 已建，已入库 22,463 点） |
| **向量库 Qdrant（市场服务器本地，已死）** | 原宿主机 :6333 实例在 8/30 磁盘事故后的服务器重建中消失（无容器、无监听）；旧数据仅存**孤儿卷**：`sse_opsagent_qdrant_data`（含旧 `market_content` 4164 点 + `user_memory`，当前无容器挂载，**勿擅自挂载**）、smart-qiu 的 `sse-smart-qiu-langgraph_qdrant-data`（:16333 容器挂载，空集合）；另有 cwd 已删除的僵尸 qdrant 进程（PID 3607，无监听） | 旧 2180 点 DashScope 测试集合不可恢复（本就要 `--fresh` 重建，无实际损失） |
| **向量库 Milvus** | standalone v2.6.21（:19530，compose 位于 `/root/milvus`），被论坛→wiki 归档项目使用（collection `archive_wiki`） | 与本功能无关，**不触碰**；仅作背景记录 |
| **平台 LLM 网关（2026-09-10 起升为主通道）** | `https://api.<MARKET_DOMAIN>/v1/chat/completions`（OpenAI 兼容，New API 网关 `grp-new-api-poc` 23008 存活；用户提供 Bearer key） | 见上「LLM 主通道」行；切通道只需改 .env 的 base_url/key/model，代码零改动 |
| **智能小秋 LangGraph 版** | `/root/market-deploy/agent/SSE-SMART-QIU-langgraph`（Python，LangChain/LangGraph + FastAPI），内含 `app/rag/`（检索+BM25 重排+格式化）、`app/model/pool.py`（`langchain_openai.ChatOpenAI` + fallback 模型池）、`app/sync/qdrant_sync.py`（MySQL→Qdrant 同步） | 代码范式直接复用 |
| **rag-lab 混合检索实验** | `/root/market-deploy/market-rag-lab`（只读实验，HTTP :19081，systemd 管理），实现了「Fast LLM 关键词提取 → MySQL 关键词检索 → Qdrant 语义检索 → 合并去重重排」完整管线与评测脚本 | 检索管线与排序权重直接参考 |
| **rag-public-proxy（现有，勿动）** | `sse_market_new_client/newSSE/scripts/rag_public_proxy.py`，systemd `market-rag-public-proxy.service`，监听 `172.17.0.1:19082`，Nginx `/api/rag-local/` 已代理 | ⚠️ **19082 被占用**——本功能改用 19084；该服务为现有生产组件，评审红线 1 同样适用 |

### 2.5 缺口分析（新功能必须补的东西）

1. **分区过滤缺失**：`market_content` 的 payload **没有 `partition` 字段**（同步 SQL 只取 `postID,title,ptext,post_time,userID,name`），现有检索全站无分区维度。
2. **回复/楼中楼未入索引**：现有同步脚本只索引帖子标题+正文（`qdrant_sync.py` 明确输出 "only title + content"），`pcomments`/`ccomments` 共 2 万余条完全不在检索范围内。
3. **无发帖触发入口**：`controller.Post` 发帖成功后没有任何检索/AI 调用点。
4. **无"检索→AI 回答"专用 API**：smart-qiu 是通用聊天助手（会话式、全站检索），与本功能的"分区内、发帖语境、面向发帖人"场景不匹配，不应混用（避免互相影响）。

### 2.6 服务器 Embedding 全量盘点（2026-08-28 只读调查，未做任何修改）

结论先行：**服务器上没有任何本地部署的 Embedding 模型**（无 Ollama / Xinference / vLLM / TEI 进程，无本地模型目录），全部 Embedding 都是远程 API 调用，共 2 个供应商、5 个消费方。（2026-08-31 补充：市场服务器上确实没有；平台侧 K8s 容器有 bge-m3 权重但服务从未部署，见 §2.7。）

#### DashScope `text-embedding-v4`（1024 维，4 个消费方，2 把 key）

| 消费方 | 用哪把 Key | 向量库 |
| --- | --- | --- |
| rag-lab（web 本身 RAG 实验） | `sk-****...` ⚠️ **欠费中** | Qdrant `market_content` |
| **发帖助手**（本功能） | `sk-****...` ⚠️ **同一把 key** | Qdrant `market_assist_v1` |
| smart-qiu（SSE-SMART-QIU，`config/config.yaml` 的 `embed_api_key`） | `sk-****...`（另一个账号，未见欠费迹象） | Qdrant `user_memory` |
| 论坛→wiki 归档项目 `sse_market_toiwiki_server-task`（`run_worker.py` 正在跑） | `sk-****...`（与 smart-qiu 同一把） | **Milvus** `archive_wiki` |

#### SiliconFlow `BAAI/bge-m3`（1 个消费方）

| 消费方 | 说明 |
| --- | --- |
| ai_interview（Go 服务，`/root/market-deploy/tx/ai_interview/server`） | bge-m3 做 Embedding + bge-reranker-v2-m3 做重排 + TeleSpeechASR 做语音，key `sk-****...`（见其 config.yaml） |

#### 对本功能的结论与最终决策（2026-08-28）

1. **欠费只影响本功能与 rag-lab 共用的 `sk-****...`**——smart-qiu 与归档项目用的 `sk-****...` 是另一个账号，不受影响。
2. **SiliconFlow bge-m3 曾是备选**，但最终采用更优方案：**平台自建 bge-m3 服务**（用户提供接口，无鉴权、无成本），本功能 Embedding 已切至该服务（见下）。
3. **平台服务 frp 隧道拓扑**（`frps` 运行于本服务器，`/opt/frp/frps.toml`，远端小组 frpc 注册）：`23011`=`grp-embedding-web`（**pipeline embedding 服务已上线**，2026-09-06 实测：OpenAI 兼容 `POST /v1/embeddings`，1024 维，无鉴权；旧 `/embed` 格式已死）、`23012`=`grp-market-rag-web`（market-rag pod 对外入口，即本功能服务所在 pod 的 8080 映射通道规范）、`25033`=`db-qdrant-rest`（平台 Qdrant REST，**可用**，API Key 验证通过——首字符大写 I；**集合前缀已实测确认 `gxxxxxx_`**，用户提供的前缀 `gb1ca3490_` 过时）、`25034`=`db-qdrant-grpc`、`25006`=`db-mysql`、`25079`=`db-redis`；LLM 网关 `grp-new-api-poc`（23008）存活，对外域名 `api.<MARKET_DOMAIN>`。

**2026-08-31 复测修正**：`23011`=`grp-embedding-web` 隧道在线但**后端从未连接**（本机 HTTP 000、0.01s 秒拒；外网 curl 退出码 52 空响应）；`23012`=`grp-market-rag-web` 即平台 K8s 容器 `group-market-rag` 的对外入口（容器内应用绑 `0.0.0.0:8080` 即映射到 23012，见 §2.7）；`22011`=`grp-embedding-ssh` 隧道在线（curConns=3）但用市场服务器 key 登录后端被拒（`Permission denied (publickey,password)`）。

**2026-09-06 复测（终版结论）**：平台 embedding 服务**已在 23011 上线**（OpenAI 兼容 `/v1/embeddings`，2026-09-06 实测 200/1024 维/无鉴权/有效证书）——8/31 的「从未部署、待自建」判断随平台侧上线作废，§2.7 自建恢复方案不再需要；`22011` 对应 group-embedding 容器 SSH（密码与 22012 的 pod 不同）。

---

### 2.7 平台 embedding 上线确认 + 服务迁 market-rag pod（2026-09-06/07 实测）

**结论先行**：平台 embedding 服务**已于 2026-09-06 上线**，地址 `https://<MARKET_DOMAIN>:23011/v1`（OpenAI 兼容 `/v1/embeddings`，bge-m3，1024 维，无鉴权，公网有效证书）；旧 `POST {base}/embed {"texts":[...]}` 格式**已下线**（HTTP 入口 301→HTTPS 且 POST 变 GET→405，必须 HTTPS 直连）。2026-08-31 的「服务从未部署、需容器内自建」判断作废（当时平台尚未上线），8/31 版恢复方案不再需要。同日用户拍板：**本功能服务迁到平台 market-rag pod 内运行、向量写入平台 Qdrant 集群**。

#### 平台提供的 pod 连接信息（学生容器）

| 项 | 值 |
| --- | --- |
| 容器组 | `group-market-rag-*`（K8s pod，容器内 IP `10.42.x.x`，Ubuntu 24.04 / Python 3.12.3） |
| SSH | `ssh -p 22012 cloud@<MARKET_DOMAIN>`（= `<SERVER_IP>:22012`，本地 DNS 不可解析时用 IP；密码登录，密码见平台「连接信息」页，**勿写入任何公开文档/代码**；pod 重启后由 `POD_PASSWORD` 环境变量重置同一密码） |
| Web 终端 | 平台网页「在浏览器打开终端」 |
| 公网入口 | 应用绑 `0.0.0.0:8080` → `http://<MARKET_DOMAIN>:23012`（本功能不用，占 19084） |
| 共享权重（只读） | `/shared/weights/`：bge-m3、Qwen3.8-27B-AWQ-INT4、bge-reranker-v2-m3、Spark-X2.5-4B、minicpm-* 等 |
| MinIO（S3 兼容） | `http://minio.infra.svc.cluster.local:9000`（access key/secret 需向 owner 按桶申请） |
| 公共数据库 | cluster 内网：`mysql.infra.svc.cluster.local:3306` / `redis.infra.svc.cluster.local:6379` / `http://qdrant.infra.svc.cluster.local:6333`（gRPC 6334）；frp 旁路（仅 <MARKET_DOMAIN> 上的应用可达）：MySQL `127.0.0.1:25006`、Redis `25079`、Qdrant REST `25033` / gRPC `25034` |
| 生命周期 | 容器入口 `/entrypoint.sh`（2026-09-06 实测 cat）仅：按 `POD_PASSWORD` 重置 cloud 密码 → exec sshd（**无 supervisor**；8/31 记录中的「入口自动拉起 supervisord」机制经实测不存在，pod 重启后需手动重新拉起服务与 watch） |

#### 2026-09-06/07 实测结论（含平台侧变更）

| 检查项 | 结果 |
| --- | --- |
| 平台 embedding（`https://<MARKET_DOMAIN>:23011/v1/embeddings`） | ✅ 200 / 1024 维 / 批量与单串输入均可 / 无鉴权 / 证书 strict verify OK（本地 + market 服务器 + **pod 内**三处实测一致） |
| 旧 `/embed {"texts":[...]}` | ❌ 已下线（HTTP 301 会把 POST 变 GET → 405，不可用） |
| 平台 Qdrant（集群 DNS :6333 / frp :25033） | ✅ v1.19.0；**key 首字符是大写 I**（截图/聊天中的小写 `l` → 401）；集合前缀实测 **`gxxxxxx_`**，集群现存 `gxxxxxx_docs`（红线，勿动） |
| 生产 MySQL 从 pod 可达性 | ✅ `<SERVER_IP>:3506`（公网 IP，root/***，market 库 43 表）；平台 `mysql.infra:3306` 亦可达（本功能不用——集市数据在市场服务器侧，Go 后端即连该库） |
| pod 内 python/PyPI | ✅ Python 3.12.3，`python3 -m venv` 可用，pypi.org 可达（pip 安装偶发瞬时网络重试，自动恢复） |

#### 实际部署与验证记录（2026-09-06/07，均在 pod `/home/cloud/sse_market_assist` 内）

1. 项目上传 pod（app/scripts/tests/main.py/requirements.txt/.env）→ `python3 -m venv .venv` → 依赖安装（qdrant-client 1.19.0 与服务端同版）；
2. 单测 `pytest tests/ -q` → **24/24 通过**；服务启动 `/health` → 200；
3. 平台 Qdrant 建集合 `gxxxxxx_market_assist_v1`（1024/cosine，`{"result":true}`）；
4. `run_sync.py --fresh` 全量重建：**22,452 点**（posts 4459 + replies 10589 + 楼中楼 7404，约 20 分钟），源库 4350 帖/12519 回复/9119 楼中楼（私密/主页分区等过滤后入库）；
5. `run_sync.py --watch` 常驻（每 30s 增量）：首周期即捕获线上新帖 11 点（7 帖 + 4 回复）→ 累计 22,463 点；
6. **3 分区 × 10 真实用例抽测 30/30 通过**（学习交流/宿舍生活/实习就业；无 500、无 LLM 降级、固定 5 引用；约 1/3 有精准直接答案，其余诚实返回「站内暂无直接讨论」+ 相近参考——其中有真实站内无此内容的用例，属预期行为）。

**pod 运维注意事项**：无 systemd/supervisor，服务与 watch 均以 `nohup .venv/bin/python ... >> logs/*.log 2>&1 &` 拉起（pod 重启需手动重拉；可写 `deploy/restart.sh` 备查）；服务绑 `0.0.0.0:19084`，阶段 2 前仅 pod 内可达。

---

## 3. 总体设计

### 3.1 设计原则

- **不改主站**：一期前端触发，Go 后端零改动；Nginx 仅增加一条新 location（增量、可即时回滚）。
- **不动现有向量数据**：新 collection `gxxxxxx_market_assist_v1`（平台 Qdrant 集群），与红线集合 `gxxxxxx_docs`、市场服务器本地孤儿卷 `market_content` 完全隔离。
- **失败静默**：检索/生成失败时前端仅不展示卡片，**绝不影响发帖成功与否**。
- **渐进上线**：先只索引帖子（与现有数据对齐），再扩展回复/楼中楼；先灰度再全量。
- **LangChain 技术栈**：与现有 LangGraph 服务一致，使用 `langchain_openai`、`langchain_core`（LCEL）等组件。

### 3.2 架构图

```text
┌──────────────┐  1.发帖(现有逻辑不变)  ┌─────────────────────────────┐
│  浏览器/前端  │ ─────────────────────▶ │  Go 主后端 sse_market_server │
│  (Vue3 /new) │                        │  (Gin :8080，不改动)          │
└──────┬───────┘                        └──────────────┬──────────────┘
       │ 2.发帖成功后，前端调用新接口                     │ 只读
       │    POST /api/v1/assist/match                  ▼
       ▼                                      ┌────────────────────────┐
┌──────────────────────────┐  3.分区内检索     │ MySQL market        │
│  新增：sse-market-assist  │ ◀─────────────── │ <SERVER_IP>:3506   │
│  独立 Python 服务(:19084) │                  │ (posts/pcomments/     │
│  部署于平台 market-rag │                  │  ccomments)           │
│  pod(/home/cloud/...)    │  4.向量检索      └────────────────────────┘
│  FastAPI + LangChain      │ ◀─────────────── ┌────────────────────────┐
│  · 查询改写(LLM)          │                  │ 平台 Qdrant 集群         │
│  · 混合检索+重排          │                  │ qdrant.infra  │
│  · AI 回答(LLM)           │ ──── 写入 ─────▶ │ .svc.cluster.local:6333 │
│  · 增量同步(--watch)      │                  │ 新 collection:          │
└──────────────────────────┘                  │ gxxxxxx_market_   │
       5.返回 {answer, references[]} → 前端发帖成功页展示"AI 相关讨论"卡片  │
                                              │ assist_v1(已 22,463 点)  │
                                              └────────────────────────┘
       6. Embedding：平台 bge-m3（https://<MARKET_DOMAIN>:23011/v1/embeddings，
          OpenAI 兼容，1024 维，无鉴权；旧 /embed texts 格式已下线）
```

### 3.3 方案选型对比

| 方案 | 描述 | 优点 | 缺点 | 结论 |
| --- | --- | --- | --- | --- |
| **A. 独立 Python 服务（推荐）** | 新建独立服务 `sse_market_assist`，FastAPI + LangChain；**已部署于平台 market-rag pod**（`/home/cloud/sse_market_assist`，绑 `0.0.0.0:19084`，nohup 管理，pod 内即可达）；Nginx 加一条 `/api/v1/assist/` location 代理（阶段 2） | 与主站完全解耦；崩溃/停机不影响发帖；可独立评审、独立回滚；参照 rag-lab 已有先例 | 多一个进程（轻量）；前端需发版 | ✅ **采用** |
| B. 并入 smart-qiu-langgraph | 在 LangGraph 服务里加新端点 | 复用进程 | 与生产助手共享进程/限流/故障域；改动现有服务违背"不影响别的功能"红线 | ❌ 不采用 |
| C. Go 主后端原生实现 | 在 Go 里加 controller + Qdrant/LLM 客户端 | 无新进程 | 直接改主后端，风险最高 | ❌ 不采用（仅二期可选做"异步触发"小改动） |

### 3.4 核心数据流（一期）

1. 用户在发帖页填写标题/正文/分区 → 点击发布（**完全走现有链路** `POST /api/auth/post`）。
2. 发帖成功后，前端携带 `{partition, title, content, postID}` 调用 `POST /api/v1/assist/match`（异步调用，不阻塞页面；失败静默）。
3. assist 服务处理：
   - **分区白名单校验** → 非法分区直接返回空结果；
   - **查询改写**（LLM，主通道集市网关，可选开关）：把标题+正文压缩成 1~3 条检索问句；
   - **MySQL 关键词检索**：`partition=?` 限定下的 `posts.title/ptext`、`pcomments.pctext`、`ccomments.cctext` 关键词匹配（并排除 `is_private=1`）；
   - **Qdrant 语义检索**：`filter: partition=该分区`，top-k 召回（命中帖/回复/楼中楼向量）；
   - **合并重排**：按 postID 聚合，加权打分（向量 0.48 + 关键词 0.27 + 热度 0.08，参考 rag-lab 权重），取 Top 5 帖；每帖补充其下高赞回复/楼中楼片段；
   - **AI 生成回答**（LLM 双通道，主 `deepseek-v4-flash@集市网关`）：基于上下文生成回答 + 引用链接，温度调低，遵守隐私与引用规则（见 4.3）；
   - 返回 `{answer, references[]}`。
4. 前端展示"AI 帮你找到了相关讨论"卡片：AI 回答 + 相关帖子列表（标题可点击跳 `https://<MARKET_DOMAIN>/new/postdetail/{postID}`）+ "AI 生成内容仅供参考"声明。

---

## 4. 详细设计

### 4.1 索引层：新 collection `gxxxxxx_market_assist_v1`（平台 Qdrant）

**为什么新建而不是复用**：`market_content` 被生产助手 smart-qiu 使用，改它的 payload/结构会直接干扰现有功能，违反红线 2。平台集群前缀为 `gxxxxxx_`（2026-09-06 实测确认），存量红线集合 `gxxxxxx_docs` 只读不动。

**Collection 配置**（2026-09-06 已在平台集群创建，`{"result":true}`）：

```json
{
  "name": "gxxxxxx_market_assist_v1",
  "vectors": { "size": 1024, "distance": "Cosine" }
}
```

**Point payload 结构**（每 chunk 一个 point）：

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `partition` | string | **分区名，检索过滤字段**（新增，补齐缺口 1） |
| `doc_type` | string | `post` / `reply` / `subreply`（回复与楼中楼，补齐缺口 2） |
| `postID` | int | 所属帖子 ID（聚合与去重键） |
| `commentID` | int | 回复/楼中楼 ID（doc_type 为 reply/subreply 时有值） |
| `title` | string | 帖子标题（回复类 chunk 也冗余带上，便于格式化） |
| `text` | string | 被向量化的文本（帖子正文片段 / 回复内容 / 楼中楼内容） |
| `chunk_index` | int | 同文档内分块序号 |
| `post_time` | string | 帖子发布时间 ISO8601 |
| `like_num` | int | 点赞数（回复类字段；帖子用 heat 代替） |
| `heat` | float | 帖子热度（仅 post 类有值） |
| `is_anonymous` | bool | 帖子是否匿名（**不索引作者信息**，从源头避免泄露） |
| `origin_id` | string | `post:{id}:chunk:{i}` / `reply:{id}` / `subreply:{id}`，稳定 point id 生成源（沿用现有 `stable_point_id` 算法） |

**嵌入文本格式**（沿用 smart-qiu 现有风格，区分类型方便 AI 理解）：

```text
[帖子] 标题:{title}
内容:{chunk}

[回复] 帖子标题:{title}
回复内容:{pctext}

[楼中楼] 帖子标题:{title}
回复内容:{cctext}
```

**数据来源 SQL（只读）**：

```sql
-- 帖子（与现有 qdrant_sync 对齐，但补上 partition / is_anonymous 并排除私密帖）
SELECT postID, `partition`, title, ptext, post_time, heat, is_anonymous
FROM posts
WHERE is_private = 0;

-- 一级回复（JOIN 帖子拿分区与标题）
SELECT c.pcommentID, c.ptargetID, c.pctext, c.like_num, c.time,
       p.`partition`, p.title, p.is_anonymous
FROM pcomments c
JOIN posts p ON p.postID = c.ptargetID
WHERE p.is_private = 0;

-- 楼中楼（JOIN pcomments 再 JOIN posts）
SELECT cc.ccommentID, cc.ctargetID, cc.cctext, cc.like_num, cc.time,
       p.`partition`, p.title, p.is_anonymous
FROM ccomments cc
JOIN pcomments c ON c.pcommentID = cc.ctargetID
JOIN posts p ON p.postID = c.ptargetID
WHERE p.is_private = 0;
```

**增量同步策略**：

- **全量（存量 embedding）**：上线时跑一次（实测 2026-09-06：`--fresh` 全量重建 **22,452 点**（帖子 4459 + 回复 10,589 + 楼中楼 7,404），约 20 分钟，详见 §13.7）。**断点续传**：已入库 origin 自动跳过；**水位只在首次全量开始时快照并立即落盘**，断点续跑沿用原水位，保证中断期间的新内容不会被漏掉。
- **增量（发帖/评论自动 embedding，一期）**：`run_sync.py --watch` 常驻进程，默认每 30s（`ASSIST_SYNC_INTERVAL`）按时间水位增量拉取新帖/新回复/新楼中楼入索引。**与发帖链路完全无关**：不触碰 Go 后端、不在发帖请求路径上，embedding 失败只影响索引新鲜度，绝不阻塞发帖（红线 1）。水位条件用 `>=`（MySQL 时间列精确到秒，`>` 会漏掉水位同秒写入的行；重复拉取的行由稳定 point id 幂等跳过）。单周期异常（embedding 欠费/服务 down）自动下周期重试。每 `ASSIST_SYNC_CLEANUP_CYCLES`（默认 20）个周期顺带清理私密帖点。
- **实时（二期可选）**：Go 主后端在发帖/回复 hook 中推送消息到 assist 服务（Redis 队列），进一步缩短新回复入索引延迟。
- **删除/私密化处理**：watch 周期内对比 `is_private=1` 变化与已删除记录，删除对应 point（`delete postID` 过滤删除，Qdrant 原生支持按 filter 删除）。

### 4.2 检索层：分区内混合检索 + 重排

输入：`partition` + `title` + `content`（+ 可选的改写问句）。检索目标**排除刚发的帖子自身**（`filter: postID != 当前postID`）。

```
┌────────────────────────────────────────────────────────────┐
│ 输入: partition + title + content                            │
│  ├─ 1. 查询改写(LLM, 开关控制): 生成 1~3 条检索问句           │
│  ├─ 2. MySQL 关键词检索 (仅本分区):                           │
│  │     posts.title/ptext + pcomments.pctext + ccomments.cctext│
│  │     (LIKE / 全文索引, 取前 N)                              │
│  ├─ 3. Qdrant 语义检索: filter {partition=?, postID≠自身}     │
│  │     每条问句 top_k=8~12, score_threshold≈0.35              │
│  ├─ 4. 合并去重: 按 postID 聚合 (关键词命中 + 向量命中合并)    │
│  ├─ 5. 重排打分: 0.48·向量分 + 0.27·关键词分 + 0.08·热度分    │
│  │     (权重沿用 rag-lab 已验证参数, 上线后可调)               │
│  └─ 6. 取 Top 5 帖, 每帖补 1~2 条最高赞回复/楼中楼片段作为上下文│
└────────────────────────────────────────────────────────────┘
```text

**MySQL 关键词检索要点**：

- 用 `query_logic.py` 的 `tokenize_keywords`（域名词表 + ASCII 词 + 中文 2~6 字切块 + 停用词）提取 4~8 个关键词；
- 每条关键词分别对 `posts`（`partition=? AND is_private=0 AND title/ptext LIKE %kw%`）、`pcomments`/`ccomments`（JOIN 帖子限定分区）打分；命中越多关键词分越高；
- 全程参数化查询，禁止拼字符串（防注入）。

**Qdrant 检索请求示例**：

```json
POST /collections/gxxxxxx_market_assist_v1/points/search
{
  "vector": [/* 1024 维 */],
  "limit": 12,
  "with_payload": true,
  "filter": {
    "must": [
      { "key": "partition", "match": { "value": "学习交流" } },
      { "key": "postID", "match": { "except": [12345] } }
    ]
  }
}
```

### 4.3 生成层：LLM 双通道回答

**模型接入**（OpenAI 兼容协议；主通道集市网关 → 失败自动切 DeepSeek 官方，2026-09-10 起）：

```python
from langchain_openai import ChatOpenAI

# 主通道：集市自有网关
primary = ChatOpenAI(
    model="deepseek-v4-flash",
    base_url="https://api.<MARKET_DOMAIN>/v1",
    api_key=os.environ["SSE_MARKET_API_KEY"],
    temperature=0.2,                          # 检索回答场景温度调低，减少幻觉
    max_completion_tokens=1536,
    timeout=100,                              # 实测最慢 ~31s；重试只会叠时长
    max_retries=0,
)

# 备用通道：DeepSeek 官方（主通道任何异常时自动接管）
backup = ChatOpenAI(model="deepseek-v4-flash", base_url="https://api.deepseek.com/v1",
                    api_key=os.environ["DEEPSEEK_API_KEY"], temperature=0.2,
                    max_completion_tokens=1536, timeout=100, max_retries=0)

llm = primary.with_fallbacks([backup])        # 切备用时打印日志，避免静默降级
```

**Prompt 模板（核心约束）**：

```text
你是软工集市的站内智能助手。用户刚在【{partition}】分区发了一篇帖子，我们检索了该分区内的历史讨论（帖子、回复、楼中楼）供你参考。

严格规则：
1. 只依据下方【检索材料】回答；材料不足以回答时，明确说"站内暂时没有找到相关讨论"，禁止编造帖子内容、发帖人或结论。
2. 回答中提到的每个观点，若来自某条材料，须用 [n] 形式标注对应参考编号。
3. 禁止泄露任何用户个人信息（姓名、联系方式等）；匿名帖的内容可以引用观点，但不得提及发帖人身份。
4. 语言简洁，分点组织；先给结论，再给依据；最后列出相关帖子标题与链接（用材料里给出的链接）。
5. 不要复述用户刚发的帖子内容本身，只输出"站内相关讨论"的结论。

用户刚发的帖子：
标题：{title}
正文：{content 截断至 500 字}

【检索材料】
[1] 帖子《...》 分区：... 热度：... 
正文片段：...
其中高赞回复：...
其中楼中楼：...
链接：https://<MARKET_DOMAIN>/new/postdetail/{postID}
...
```

**回答输出格式**（接口内拆分为两部分返回）：

```text
answer: "根据站内已有讨论，…（正文）…"
references: [
  {"postID":111, "title":"…", "url":"…/new/postdetail/111", "snippet":"…", "heat":12.3},
  ...
]
```

**降级链**：LLM **双通道（集市网关 → DeepSeek 官方）均失败** → 返回 `answer: null` + 仅返回检索到的 references（前端降级为"相关帖子"列表）；检索零命中 → 直接返回 `{answer: "站内暂无相关讨论", references: []}`，不调用 LLM。

**判断层（2026-09-12 起，`app/llm.get_routed_llm`）**：发帖应答与追问按**实际拼装后的 prompt 体量**分流——预估 token ≤ `ASSIST_ROUTE_SMALL_MAX_TOKENS`（默认 20000）走网关 qwen3.8-27b-awq（思考模型，强制 `reasoning_effort=none`，否则思考烧光 token 输出空内容），超过则走 deepseek-v4-flash（上方主通道）。判定在 prompt 渲染后、请求发出前用字符启发式完成：**非 ASCII 按 1 token/字 + ASCII 按 3 字符/token**（保守上界，实测高估约 9%），不额外调用 LLM、不重复携带检索材料。分流只看体量、不看问题复杂度；查询改写等检索内部步骤不分流（体量恒定且影响召回质量）。

不爆上下文有**双保险**：① 启动自检 `阈值 + 1536(输出) + 2000(余量) ≤ ASSIST_SMALL_MODEL_WINDOW`，越界直接全程改走主通道；② qwen 通道同样挂 DeepSeek 官方兜底，即使误分导致 400（context 超限）也自动切换（with_fallbacks 接住）。**回滚开关**：`ASSIST_ROUTE_ENABLED=0` → 单通道，与上线前行为完全一致。

### 4.4 服务接口定义

**新服务**：`sse-market-assist`（FastAPI + LangChain，部署于平台 market-rag pod，监听 `0.0.0.0:19084`——阶段 2 前仅 pod 内可达；对公网经 Nginx 暴露（阶段 2）。

| 接口 | 方法 | 说明 |
| --- | --- | --- |
| `/health` | GET | 健康检查，供 Nginx 探活（pod 内亦可直接确认） |
| `/api/v1/assist/match` | POST | 核心接口：分区检索 + AI 回答 |
| `/api/v1/assist/refs` | POST | 降级接口：只检索不生成（LLM 故障时前端可单独调用） |
| `/api/v1/assist/admin/ingest` | POST | 运维接口：手动触发增量索引（带内部 token） |

**`/api/v1/assist/match` 请求/响应**：

```jsonc
// 请求
{
  "partition": "学习交流",
  "title": "大三要不要主动找老师做项目",
  "content": "对保研和就业有帮助吗？……",
  "postID": 12345,          // 刚发布的帖子 ID，用于排除自身
  "limit": 5                // 返回引用数上限，默认 5，最大 8
}
// 响应 200
{
  "ok": true,
  "data": {
    "answer": "根据【学习交流】分区内的历史讨论，……",
    "references": [
      {"postID": 111, "title": "…", "url": "https://<MARKET_DOMAIN>/new/postdetail/111",
       "snippet": "…", "heat": 12.3}
    ],
    "meta": {"mysql_hits": 14, "qdrant_hits": 22, "used": 5, "took_ms": 1234}
  }
}
// 降级响应（LLM 不可用）
{ "ok": true, "data": { "answer": null, "references": [...], "meta": {...} } }
```

**Nginx 增量配置**（`nginx_proxy/nginx.conf` 对应 server 块内新增，评审通过后执行）：

```nginx
# 分区智能检索助手（2026-xx-xx 新增；回滚=删除此块并 reload）
location /api/v1/assist/ {
    proxy_pass http://host.docker.internal:19084;
    proxy_http_version 1.1;
    proxy_set_header Host $host;
    proxy_read_timeout 30s;
}
```

**鉴权**：接口为站内服务，沿用现有 `/api` 的登录态体系由 Go 网关校验不可行（一期绕过 Go），因此：一期前端调用时携带现有登录 JWT，assist 服务调用 Go 主后端鉴权接口校验（或简化：仅校验分区白名单 + 全局限流 + 结果不含隐私，接口本身无敏感写操作）；二期若改为 Go 后端转发，则由 Go 统一鉴权后携带内部 token 调用 19084，公网不再暴露该接口。

### 4.5 前端交互（newSSE 前端）

- 发帖成功页（或发帖成功后弹层）新增组件 `AiPostAssistCard.vue`：
  1. 监听发帖成功回调 → 携带 `{partition, title, content, postID}` 调 `POST /api/v1/assist/match`（不阻塞用户后续操作）；
  2. 展示：**"AI 帮你查了「学习交流」分区的相关讨论"** + 流式/整段 AI 回答 + 相关帖子卡片列表（标题、摘要、热度，点击跳 `/new/postdetail/{id}`）；
  3. 底部固定声明："内容由 AI 生成，仅供参考"；
  4. 失败/超时（3s 前端超时）→ 卡片不展示，不影响发帖流程。
- 可选二期：发帖页"发布前先帮我查一下"按钮（调用 `/refs` 或 `/match` 且不带 postID），供用户发帖前参考。

### 4.6 Go 主后端集成（二期，可选，需单独评审）

若产品上希望"发帖响应里直接带 AI 结果"或"结果随帖子展示给后续浏览者"，可二期在主后端增加：

- `service/assist.go`：HTTP 客户端，POST 到 `127.0.0.1:19084`，**超时 4s**，失败仅记日志；
- 发帖成功后 `go func()` 异步调用（注意：使用独立 goroutine 时不能复用 gin.Context）；结果写入 Redis `assist:post:{postID}`，TTL 24h；
- 新增只读接口 `GET /api/auth/postAssist?postID=`，前端轮询/延迟读取；
- 全局开关：`config.EnablePostAssist`（环境变量注入，默认 `false`），上线与回滚只改配置不重新发版。

---

## 5. 关键代码设计（LangChain 组件）

> 以下为设计级示例代码；完整实现见第 13 节（已开发完成，运行于平台 pod `/home/cloud/sse_market_assist/`），不触碰任何现有文件。

### 5.1 服务骨架（FastAPI）

```python
# app/main.py（示意）
from fastapi import FastAPI
from app.routes import router

app = FastAPI(title="sse-market-assist", version="0.1.0")
app.include_router(router)
```

### 5.2 LLM 与 Embedding 客户端

```python
# app/llm.py（LLM 走 LangChain；与 smart-qiu 的 app/model/pool.py 模式一致）
# 2026-09-10 起双通道：主=集市网关，备=DeepSeek 官方
from langchain_core.runnables import Runnable, RunnableLambda
from langchain_openai import ChatOpenAI
from app.config import settings


def _make_llm(model: str, base_url: str, api_key: str) -> ChatOpenAI:
    return ChatOpenAI(model=model, base_url=base_url, api_key=api_key,
                      temperature=0.2, max_completion_tokens=1536,
                      timeout=100, max_retries=0)


def _notify_failover(runnable: Runnable, label: str) -> Runnable:
    """包在备通道外层：被调用即说明主通道已失败，先打日志再执行。"""
    def _invoke(input, **kwargs):
        print(f"[llm] 主通道（集市网关）失败，已切换到{label}", flush=True)
        return runnable.invoke(input, **kwargs)
    return RunnableLambda(_invoke)


def _build_llm(primary_key, primary_url, primary_model,
               fallback_key, fallback_url, fallback_model) -> Runnable:
    primary = _make_llm(primary_model, primary_url, primary_key)
    if not fallback_key:          # 备通道未配 key → 单通道
        return primary
    backup = _make_llm(fallback_model, fallback_url, fallback_key)
    return primary.with_fallbacks([_notify_failover(backup, "备用通道(DeepSeek)")])


@lru_cache(maxsize=1)
def get_llm() -> Runnable:
    return _build_llm(
        settings.sse_market_api_key, settings.sse_market_base_url, settings.sse_market_model,
        settings.deepseek_api_key, settings.deepseek_base_url, settings.deepseek_model,
    )
```

Embedding 不走 LangChain：2026-08-26 实测 langchain-openai 1.x 的请求序列化与 DashScope
compatible-mode 后端不兼容（返回 400 `InvalidParameter: input.contents`），
故 `app/embed.py` 用原始 HTTP（urllib），现支持**双协议**（`EMBEDDING_MODE` 切换）：

```python
# app/embed.py 核心（示意）
# openai 模式（当前默认；平台 bge-m3 2026-09-06 上线：POST {BASE}/embeddings {"model":"bge-m3","input":[...]}）
#   POST {EMBEDDING_BASE_URL}/embeddings  {"model": ..., "input": [...]}
# texts 模式（旧平台格式 /embed，已下线，仅历史兼容保留）：
#   POST {EMBEDDING_BASE_URL}/embed   {"texts": ["a", "b"]}
class EmbeddingUnavailable(RuntimeError): ...  # 消息内带响应详情（欠费/服务 down/格式异常）

def embed_texts(texts: list[str]) -> list[list[float]]:
    # 数组批量单次请求；网络错误/429/5xx 重试 3 次；
    # 其余 4xx（欠费/无权限/参数错）立即失败并抛 EmbeddingUnavailable。
    # 响应解析兼容多种 schema（embeddings/vectors/data[].embedding/裸列表）；
    # 条数不符立即失败；维度 ≠ 1024 告警一次（防与 Qdrant collection 错配）。
    # 调用方约定：检索层捕获后降级为仅关键词检索（接口不 500）；
    # 同步层（含 watch）捕获后本周期中止，下周期自动重试。
```

### 5.3 分区内混合检索

```python
# app/retrieval.py（示意）
import asyncio
from qdrant_client import QdrantClient
from qdrant_client.models import Filter, FieldCondition, MatchValue, MatchExcept

async def retrieve(partition: str, query_texts: list[str],
                   exclude_post_id: int, limit: int = 12) -> list[SearchHit]:
    """语义检索：Qdrant 分区过滤 + 排除自身帖子。"""
    vecs = await embeddings.aembed_documents(query_texts)
    hits = []
    for vec in vecs:
        res = qdrant.search(
            collection_name=COLLECTION,
            query_vector=vec,
            limit=limit,
            query_filter=Filter(must=[
                FieldCondition(key="partition", match=MatchValue(value=partition)),
                FieldCondition(key="postID", match=MatchExcept(**{"except": [exclude_post_id]})),
            ]),
        )
        hits.extend(res)
    return hits

def keyword_search(partition: str, keywords: list[str]) -> list[Candidate]:
    """MySQL 关键词检索：posts + pcomments + ccomments，参数化查询，仅本分区。"""
    # SELECT ... FROM posts WHERE `partition`=%s AND is_private=0
    #   AND (title LIKE %s OR ptext LIKE %s)  -- 逐关键词
    # 同理 JOIN pcomments / ccomments
    ...

def merge_rank(mysql_hits, qdrant_hits, top_n=5) -> list[PostCandidate]:
    """按 postID 聚合；score = 0.48*向量分 + 0.27*关键词分 + 0.08*热度分
    （权重沿用 rag-lab；上线后用评测调优）"""
    ...
```

### 5.4 AI 回答生成（LCEL）

```python
# app/generate.py（示意）
from langchain_core.prompts import ChatPromptTemplate
from langchain_core.output_parsers import StrOutputParser

PROMPT = ChatPromptTemplate.from_messages([
    ("system", SYSTEM_TEMPLATE),          # 见 4.3 规则
    ("user", "{context}\n\n请回答："),
])

def build_answer(partition, title, content, candidates) -> str:
    context = format_context(candidates)  # 检索材料拼装（含高赞回复/楼中楼）
    chain = PROMPT | create_llm() | StrOutputParser()
    return chain.invoke({
        "partition": partition,
        "title": title,
        "content": content[:500],
        "context": context,
    })
```

### 5.5 索引同步（复用 smart-qiu `qdrant_sync.py` 模式扩展）

```python
# app/sync.py（示意；全量 + 增量定时）
# 全量：跑 4.1 的三条 SELECT → 分块(800 字/120 重叠) → embed → upsert
# 增量：每 300s 按时间戳拉取新增 posts/pcomments/ccomments 入索引
# 清理：定时对比 is_private=1 与已删除，按 postID filter 删除 points
# 限速：embedding 请求 ≤ 2 QPS（供应商限速保护）
```

### 5.6 Go 侧调用（二期可选，示意）

```go
// service/assist.go（示意）
func (s *assistService) MatchPost(ctx context.Context, in AssistInput) (*AssistResult, error) {
    ctx, cancel := context.WithTimeout(ctx, 4*time.Second)
    defer cancel()
    // POST http://127.0.0.1:19084/api/v1/assist/match
    // 失败: 仅 log.Printf，返回 (nil, err)；上层忽略错误，不影响发帖
}
```

---

## 6. 安全与合规

### 6.1 密钥管理

- 全部密钥放新服务目录 `.env`（chmod 600），**不进 git、不硬编码**；
- 服务器上已存在的密钥继续复用（见 7.1），新 key（网关 + DeepSeek 备用）仅注入新服务；
- 运维接口 `/admin/*` 一律要求内部 token（环境变量 `ASSIST_ADMIN_TOKEN`）。

### 6.2 隐私规则（硬约束）

| 数据 | 处理方式 |
| --- | --- |
| `is_private=1` 帖子 | **不索引、不检索、不出现在任何结果中** |
| `is_anonymous=1` 帖子 | 内容可参与检索；但向量 payload 不含作者字段；回答中引用观点时**不提发帖人**；前端引用卡片同样不显示作者 |
| 回复/楼中楼 | 文本可检索；回答引用时只说"有同学提到"；索引不含任何用户 ID |
| 检索结果可见范围 | 仅发起调用的发帖人本人可见（一期前端展示；接口结果不落库、不缓存到共享存储） |

### 6.3 内容安全

- 回答 prompt 强制约束："禁止编造、禁止泄露个人信息、材料不足直说"；
- 对 AI 回答做敏感词兜底过滤（复用主后端 `util` 或独立词表，出现违禁词则整条降级为仅引用列表）；
- 用户新帖正文若命中现有发帖敏感词拦截逻辑，则根本不触发 AI 检索（前端判断发帖失败则不调用）。

### 6.4 限流与资源保护

- 接口全局限流：每用户 1 次/5s，全局 5 QPS（参考 smart-qiu `rate_limit_rps` 做法）；
- 单次检索 embedding 请求数 ≤ 3（改写问句数上限）；
- LLM 调用设 100s 超时 + 0 重试（实测最慢 ~31s；重试只会叠时长），防止拖垮发帖体验；
- 服务内存占用目标 < 200MB（纯检索+HTTP，无本地模型）。

---

## 7. 配置清单

### 7.1 新服务 `.env`（已创建于平台 pod `/home/cloud/sse_market_assist/.env`；仓库内 `.env.example` 同步且不填真实密钥）

```bash
# ---- 主通道：集市自有网关（OpenAI 兼容；2026-09-10 起为主用）----
# ⚠️ 该网关 deepseek 系模型不接受 reasoning_effort="none"（400 ModelArts.81001）；
#    若换 qwen3.8-27b-awq 则必须设 none，否则思考烧光 token 输出空内容。
SSE_MARKET_API_KEY=sk-xxx（部署时填真实值，仅 pod 内）
SSE_MARKET_BASE_URL=https://api.<MARKET_DOMAIN>/v1
SSE_MARKET_MODEL=deepseek-v4-flash

# ---- 备用通道：DeepSeek 官方（主通道失败时自动切换；留空则退化为单通道）----
DEEPSEEK_API_KEY=sk-xxx（部署时填真实值，仅 pod 内）
DEEPSEEK_BASE_URL=https://api.deepseek.com/v1
DEEPSEEK_MODEL=deepseek-v4-flash

# ---- Embedding（平台 bge-m3，2026-09-06 上线实测；OpenAI 兼容协议，1024 维，无鉴权）----
# ⚠️ 必须 HTTPS https://<MARKET_DOMAIN>:23011/v1（HTTP 会 301→HTTPS 且 POST 变 GET→405）
# ⚠️ 旧格式 POST {base}/embed {"texts":[...]} 已下线，勿用（EMBEDDING_MODE=texts 时才会走它）
EMBEDDING_API_KEY=
EMBEDDING_BASE_URL=https://<MARKET_DOMAIN>:23011/v1
EMBEDDING_MODEL=bge-m3
EMBEDDING_MODE=openai

# ---- Embedding 备选：其他 OpenAI 兼容服务（DashScope/SiliconFlow，切换改下面 4 行）----
# EMBEDDING_MODE=openai
# EMBEDDING_API_KEY=sk-****...（ai_interview config.yaml 的 AI_EMBEDDING_API_KEY）
# EMBEDDING_BASE_URL=https://api.siliconflow.cn/v1
# EMBEDDING_MODEL=BAAI/bge-m3
# 说明：切换供应商后 .venv/bin/python scripts/run_sync.py --fresh 重建索引（向量空间不同不能混用）

# ---- Qdrant（平台 K8s 集群；API Key 首字符是大写 I，小写 l 会 401）----
QDRANT_URL=http://qdrant.infra.svc.cluster.local:6333
QDRANT_API_KEY=<填入真实 key，勿提交>
QDRANT_COLLECTION=gxxxxxx_market_assist_v1

# ---- 自动同步 watch（发帖/评论自动入索引；独立进程轮询，不阻塞发帖）----
ASSIST_SYNC_INTERVAL=30
ASSIST_SYNC_CLEANUP_CYCLES=20

# ---- MySQL（生产集市库，pod 经公网 IP 直连，只读 SQL；本地/服务器跑时改回 127.0.0.1）----
MYSQL_HOST=<SERVER_IP>
MYSQL_PORT=3506
MYSQL_USER=root
MYSQL_PASSWORD=xxx
MYSQL_DATABASE=market

# ---- 服务自身 ----
# 19082 已被现有 market-rag-public-proxy 占用；pod 内绑 0.0.0.0（阶段 2 前仅 pod 内可达）
ASSIST_LISTEN=0.0.0.0:19084
ASSIST_ADMIN_TOKEN=<部署时生成随机串>
ASSIST_PARTITIONS=学习交流,实习就业,宿舍生活,技术分享,期末资料,生活日常,社团活动,组团捞人,课程专区,其他

# ---- 检索参数（rag-lab 基线，评测后调优）----
ASSIST_TOP_K=5
ASSIST_VECTOR_LIMIT=12
ASSIST_VECTOR_THRESHOLD=0.35
ASSIST_W_VECTOR=0.48
ASSIST_W_KEYWORD=0.27
ASSIST_W_HEAT=0.08
ASSIST_LLM_REWRITE=1
```

### 7.2 端口与目录规划

| 项 | 值 |
| --- | --- |
| 服务目录 | **平台 market-rag pod** `/home/cloud/sse_market_assist/`（全新；本地仓库 `sse_market_assist/` 为其源） |
| 进程管理 | pod 无 systemd/supervisor：`nohup .venv/bin/python main.py >> logs/service.log 2>&1 &`（pod 重启需手动重拉，见 §2.7） |
| 监听地址 | `0.0.0.0:19084`（阶段 2 前仅 pod 内可达；pod 的 Web 入口 23012 是 8080 的映射，本功能不用） |
| 公网入口 | 阶段 2：Nginx `location /api/v1/assist/` → 19084（唯一需要改动的现有文件，增量 7 行） |
| 新 Qdrant collection | `gxxxxxx_market_assist_v1`（平台集群；不动红线 `gxxxxxx_docs` 与市场服务器孤儿卷 `market_content`） |

---

## 8. 部署方案（阶段 0/1 已完成；阶段 2/3 ⚠️ 评审通过后再执行）

### 8.0 上线前置条件

- [ ] 本文档经产品/后端/AI 三方评审通过；
- [ ] 代码完成 code review（重点：SQL 参数化、隐私过滤、超时与降级）；
- [ ] 单元测试 + 评测报告通过（见 8.2；单元测试已随代码提供 `tests/`，评测集待人工打分）。

### 8.1 分阶段实施

#### 阶段 0 — 代码开发（✅ 已完成，见 13；新增独立目录 + 独立 venv，不影响任何现有文件；2026-09-06 代码迁移至平台 pod `/home/cloud/sse_market_assist/`）

1. 编写服务代码 + `.env` + systemd unit；
2. 本地语法检查与单测。

#### 阶段 1 — 只读验证（✅ 全部完成，2026-09-07；平台 Qdrant + market-rag pod 部署，不配 Nginx、不接前端）

1. 启动服务 → `/health` 通过（✅ 2026-08-26 初版 2180 点 → 2026-09-06 pod 版 `/health` 200）；
2. 全量索引到 `gxxxxxx_market_assist_v1`（**只写新 collection**）（✅ 2026-09-06 `--fresh`：**22,452 点**，约 20 分钟；原 2180 点旧 DashScope 向量空间已作废重建）；
3. 3 个分区各 10 条真实用例人工检查：分区过滤正确、无隐私泄露、引用链接可跳转、回答质量（✅ 2026-09-07 **30/30 通过**，见 §13.7）。

#### 阶段 2 — 灰度（Nginx 增量配置 + 前端开关）

1. 添加 7 行 location → `nginx -t` → reload；
2. 前端加 feature flag（默认仅测试账号可见）；
3. 观察 1~2 周：接口错误率、发帖成功率波动（应无变化）、DeepSeek 消耗。

#### 阶段 3 — 全量 + 二期评估

1. 全量放开前端开关；
2. 视数据决定是否启用增量同步加速（5min→实时）与 Go 后端异步集成（4.6）。

### 8.2 测试与评测

- **单元测试**：分区过滤、隐私排除（private/anonymous）、postID 排除自身、候选合并去重、关键词提取（沿用 rag-lab `tests/test_query_logic.py` 风格）；
- **接口测试**：`pytest + httpx` 覆盖 match/refs/health 及异常降级路径；
- **评测**：从各分区抽 20 条真实发帖请求，人工按"相关性 1-3 / 回答有用性 1-3 / 引用准确性 1-3"打分，达标线（相关性≥2.3）后放量；评测脚本可参考 rag-lab `scripts/eval_api.py`；**（阶段 1 已先行 3 分区 × 10 用例实测 30/30 无异常，正式打分表待评测集完成后补）**；
- **回归确认**：灰度期间每日对比 `POST /api/auth/post` 的 p99 时延与错误率（一期零改动，理论上无波动，作为对照基线）。

### 8.3 回滚方案

| 层级 | 回滚动作 | 影响 |
| --- | --- | --- |
| 前端 | 关闭 feature flag / 不调用接口 | 立即恢复原样 |
| Nginx | 删除 `location /api/v1/assist/` 块并 reload | 公网不可达 |
| 服务 | pod 内结束 nohup 进程（`pkill -f '[m]ain.py'`；无 systemd，无需 stop） | 前端调用失败→静默不展示 |
| 数据 | 删除 `gxxxxxx_market_assist_v1` collection（**勿动** `gxxxxxx_docs`） | 不影响红线集合与主站 |

---

## 9. 风险与对策

| 风险 | 影响 | 对策 |
| --- | --- | --- |
| LLM 主通道（集市网关）故障/限流 | AI 回答质量或不可用 | **已实现双通道**（2026-09-10）：主通道异常自动切 DeepSeek 官方并打日志；双通道均失败才降级返回仅检索列表 |
| DashScope embedding 计费超预期 | 成本 | 增量同步限速（≤2 QPS）；仅新内容需嵌入 |
| embedding 供应商不可用（欠费/服务 down） | 语义召回失效 | **已切换平台 bge-m3**（2026-09-06 上线实测：无成本、无鉴权、HTTPS）；检索层仍保留自动降级为仅关键词检索（接口不 500，已实测）；watch 单周期失败自动重试，不影响存量索引与检索。**兜底**：切回 SiliconFlow/DashScope 仅改 .env 4 行 + `--fresh` 重建 |
| 检索结果误纳隐私内容 | 合规事故 | 索引侧排除 private + 匿名脱敏 + 回答 prompt 硬约束 + 评测抽查 |
| AI 幻觉/编造帖子 | 用户误导 | 低温 + "材料不足直说"规则 + 每条观点强制 [n] 标注 + 前端免责声明 |
| 服务故障拖慢发帖 | 影响核心功能 | 一期前端异步调用，与发帖完全解耦；二期 Go 集成必须 4s 超时+开关 |
| 分区名变更（DB 出现新分区） | 检索空白 | `ASSIST_PARTITIONS` 配置 + 增量同步自动覆盖新分区 + 未知分区返回空结果 |
| 回复量增长导致索引膨胀 | 检索变慢 | 回复/楼中楼最长 3000 字，索引同步已按 800 字分块；上下文按帖聚合截断（正文 600 / 回复 300 / 楼中楼 150 字）；后续可只索引高赞回复 |

---

## 10. 实施计划（评审后）

> 2026-09-07 更新：任务 1~5 已实际完成并部署（见 §13.7），下表仅剩任务 6~8 待评审后执行。

| 序号 | 任务 | 产出 | 预估 |
| --- | --- | --- | --- |
| 1 | 新服务骨架 + 配置 + 进程管理 | `sse_market_assist/` 可启动（✅ 已运行于 pod） | 0.5d |
| 2 | 全量/增量索引同步（帖子+回复+楼中楼） | `gxxxxxx_market_assist_v1` 数据就绪（✅ 22,463 点 + watch 常驻） | 1d |
| 3 | 混合检索 + 重排（分区过滤） | `/refs` 接口可用 | 1d |
| 4 | DeepSeek 回答 + Prompt 打磨 | `/match` 接口可用 | 1d |
| 5 | 单元/接口测试 + 评测集 | 测试报告 | 1d |
| 6 | Nginx 增量 + 前端卡片组件 | 灰度可用 | 1d |
| 7 | 灰度观察与调优（权重/阈值） | 评测报告 | 1~2w |
| 8 | （可选二期）Go 异步集成 + 实时增量 | 评审后再排期 | — |

---

## 11. 附录：调研关键事实（2026-08-26 实测）

- 生产智能小秋 = Go/Eino 版（:18080，经 Nginx `/api/v1/agent/` 对外）；LangGraph 版（:18081）为实验版，其 `app/rag/`、`app/sync/qdrant_sync.py`、`app/model/pool.py` 是本方案代码范式来源。
- 现有 `market_content` collection：1024 维 cosine，4164 点，仅含帖子标题+正文，**无 partition 字段**。
- rag-lab（:19081）为只读混合检索实验，其排序权重（向量 0.48/关键词 0.27/热度 0.08）与评测脚本可复用。
- 发帖接口 `POST /api/auth/post`（`controller/postController.go`）校验：标题≤30 字、正文≤`config.PostContentMaxRunes`、分区非空、禁言拦截、`课程交流`分区禁用；成功后走 `service.Posts.Create`；归档 MQ（`mq/archive_queue.go`）当前入队代码已注释停用。
- 帖子详情页链接格式：`https://<MARKET_DOMAIN>/new/postdetail/{postID}`（出自 smart-qiu `format_rag.py`）。
- 回复关系：`pcomments.ptargetID → posts.postID`；`ccomments.ctargetID → pcomments.pcommentID`（并含 `usertargetName` 被回复人昵称，**索引时忽略该字段**，避免泄露昵称）。
- 数据规模：posts 4170（其中 `主页` 490 条历史帖，索引时排除）/ pcomments 12126 / ccomments 8743；私密或匿名帖 245 条。`pctext`/`cctext` 实际为 varchar(3000)，索引同步需按 800 字分块（文档 2.2 已勘误）。
- 站内已有公开检索链路：Nginx `/api/rag-local/` → `market-rag-public-proxy`（172.17.0.1:19082，systemd，2026-08-17 起运行）→ rag-lab（:19081）。本功能端口因此定为 **19084**。
- 服务器 Embedding 全量盘点（2026-08-28，只读）：无本地模型，全部为远程 API。DashScope `text-embedding-v4` 共 4 个消费方、2 把 key（`sk-****...` = rag-lab + 发帖助手【欠费】；`sk-****...` = smart-qiu + 论坛→wiki 归档项目）；SiliconFlow `bge-m3` 1 个消费方（ai_interview，兼用 bge-reranker-v2-m3）。Milvus（:19530）归属论坛→wiki 归档项目（`archive_wiki`），与本功能无关；Qdrant :16333 为 smart-qiu 自起实例。

---

## 12. MVP 实验结果（2026-08-26 已跑通）

> 为尽快验证效果，先做了一个**只读最小实验**：不改任何现有代码/服务，单个脚本跑通「分区内混合检索 + DeepSeek 回答」。

### 12.1 MVP 是什么

- 位置：服务器 `/root/market-deploy/agent/assist-mvp/`（全新独立目录：`assist_mvp.py`、`samples.json`、`web_test.py`、`.env`）。
- 管线（单文件 Python 脚本，无框架依赖）：
  1. 规则关键词提取（简化自 rag-lab `query_logic.py`）；
  2. MySQL 关键词检索：分区内 posts 标题/正文 + pcomments + ccomments（只读，排除 `is_private=1`）；
  3. **复用现有** `market_content` collection 做向量检索（只读），命中后用 MySQL 反查完成**分区过滤**（现有 collection 无 partition 字段的过渡方案）；
  4. 合并打分：向量 0.45 + 关键词 0.35 + 热度 0.20，取 Top 5；
  5. 每帖补 2 条高赞回复及其楼中楼作上下文 → DeepSeek `deepseek-v4-flash` 生成回答。
- 运行方式（服务器上）：

  ```bash
  cd /root/market-deploy/agent/assist-mvp
  python3 assist_mvp.py --samples samples.json            # 批量样例
  python3 assist_mvp.py --partition 学习交流 --title "..." --content "..."   # 单条
  python3 assist_mvp.py --samples samples.json --json     # 机器可读输出
  ```

- **MVP 未做**（正式版才有）：新向量索引（回复/楼中楼暂未入向量库，仅靠关键词覆盖）、前端展示、Go 集成、增量同步。这些不影响效果验证。

### 12.2 实验结果（8 个真实样例，覆盖 8 个分区）

| 样例 | 结果评价 |
| --- | --- |
| 学习交流「大三要不要主动找老师做项目」 | ✅ 检索到《要不要主动找老师要项目？》《进组后续》等高相关帖；回答有据可依、带 [n] 标注；并诚实指出"对保研/就业的具体帮助无直接讨论" |
| 学习交流「保研需要科研经历吗」 | ✅ 找到《保研》《大一有必要进组吗》，引用真实回复原话，结论合理 |
| 实习就业「大二暑假实习怎么找」 | ✅ 效果最好：5 篇引用全部高相关，回答覆盖投递渠道、技能准备、实习价值 |
| 技术分享「如何入门 Python」 | ✅ 向量命中 15 帖经分区过滤后为 0（该分区无相关内容），回答诚实声明"站内暂无直接讨论"并解释原因，**未编造** |
| 期末资料「高数期末怎么复习」 | ✅ 直接给出站内真题网盘链接与提取码，复习策略引用真实回复 |
| 生活日常「宿舍晚上断电怎么办」 | ✅ 无对应讨论时诚实说明，仅列 1 条弱相关帖，不硬凑 |
| 社团活动「有什么好玩的社团推荐」 | ✅ 找到《中珠有什么好玩的社团？》《软工网球社招新》等，回答提炼了招新渠道与注意事项 |
| 组团捞人「有没有人组队打比赛」 | ✅ 找到多条组队帖；诚实说明"未检索到完全对应的比赛"，建议补充比赛名 |

**关键结论**：分区过滤有效（向量命中 9~15 帖 → 过滤后 0~14 帖）、诚实回答机制有效（无结果不乱编）、引用链接可用。单条耗时 4~8s（含 embedding + LLM 调用），对"发帖后异步展示"场景可接受。

### 12.3 暴露的问题与正式版改进点

1. **规则关键词质量不稳**：中文 2~6 字切块会产出"零基础想学编"等无意义词，污染 MySQL 检索。正式版改用 **DeepSeek 提取关键词/改写问句**（文档 4.2 已设计）或引入 jieba 分词。
2. **回复/楼中楼仅关键词覆盖**：向量检索不含回复（现有 collection 无此数据），语义相近但用词不同的回复会漏检。正式版建新 collection 后解决（文档 4.1）。
3. **向量命中分区过滤损耗**：如"技术分享"样例 15 个向量命中全部被过滤，说明现有全站 collection 对该小分区覆盖率低。正式版新 collection 自带 partition 字段并索引回复，召回率会显著提升。
4. 排序权重（0.45/0.35/0.20）为初值，正式版已改为 rag-lab 基线（0.48/0.27/0.08），仍需用标注集调优。

> 以上 4 点均已在正式版（第 13 节）落实：① DeepSeek 提取关键词/改写问句（规则提取仅作回退）；② 新建 collection 索引帖子+回复+楼中楼；③ 分区原生过滤，不再"全站命中后反查"；④ 权重对齐 rag-lab。

### 12.4 手动测试方式（已搭建）

- **网页测试页**：服务器 `/root/market-deploy/agent/assist-mvp/web_test.py`（监听 0.0.0.0:19083，nohup 常驻），浏览器访问 `http://<SERVER_IP>:19083/`，输入测试口令（`.env` 中 `TEST_TOKEN`）+ 分区/标题/正文即可测试；
- **公网直连被安全组拦截**：腾讯云安全组未放行 19083 → 用 SSH 本地隧道访问：`ssh -N -L 19083:127.0.0.1:19083 market-server`，然后浏览器打开 `http://127.0.0.1:19083/`；
- **免密登录已配置**：本机生成 ed25519 密钥并安装到服务器 `~/.ssh/authorized_keys`（`~/.ssh/config` 中 Host 别名 `market-server`），VSCode Remote-SSH 可直接连接；
- 正式版完成后，测试页已改为调用新服务 `/api/v1/assist/match`（见 13.3），测试的即为正式版管线。

---

## 13. 正式版实现状态（2026-08-26）

### 13.1 代码位置与结构

代码已开发完成，2026-09-06 起运行于**平台 market-rag pod** `/home/cloud/sse_market_assist/`（全新独立目录 + 独立 venv，不触碰任何现有文件；pod 无 systemd，nohup 管理）：

```text
sse_market_assist/
├── main.py                  # 服务入口（uvicorn，pod 内 0.0.0.0:19084）
├── requirements.txt         # 依赖清单（独立 venv 安装）
├── .env / .env.example      # 密钥与配置（chmod 600，不进 git）
├── app/
│   ├── config.py            # 配置加载 + 分区白名单 + 检索/索引参数
│   ├── llm.py               # LLM 双通道（集市网关主 + DeepSeek 备，with_fallbacks）
│   ├── embed.py             # Embedding 客户端（openai 模式默认 / texts 模式兼容，双协议）
│   ├── db.py                # MySQL 只读连接（仅 SELECT）
│   ├── qdrant_store.py      # Qdrant 封装（平台集群，只操作 gxxxxxx_market_assist_v1）
│   ├── sync.py              # 全量/增量索引（帖子+回复+楼中楼，≤2 QPS，断点续传）
│   ├── retrieve.py          # LLM 查询改写 + MySQL 关键词 + Qdrant 向量 + 合并重排
│   ├── context.py           # 检索材料上下文拼装（高赞回复/楼中楼，脱敏截断）
│   ├── generate.py          # LCEL 回答生成（4.3 约束 prompt）
│   └── api.py               # FastAPI 路由（health/match/refs/admin + 每 IP 限流）
├── scripts/run_sync.py      # 索引入口（--full/--incremental/--cleanup/--fresh/--watch）
├── scripts/smoke_embed.py   # embedding 真实 API 冒烟（本地验证供应商/维度）
├── tests/test_core.py       # 单元测试（分区白名单/分块/点ID/关键词/合并排序）
└── deploy/sse-market-assist.service  # systemd 单元（市场服务器版参考；pod 用 nohup）
```

### 13.2 与 MVP 的差异（12.3 的问题均已落实）

| MVP 问题 | 正式版实现 |
| --- | --- |
| 规则关键词产生垃圾词 | LLM 查询改写（1~3 条检索问句），关键词从改写文本提取，规则仅作回退 |
| 回复/楼中楼不在向量索引 | 新 collection `gxxxxxx_market_assist_v1` 索引帖子+回复+楼中楼（payload 含 partition/doc_type/postID/commentID/title/text/chunk_index/post_time/like_num/heat/is_anonymous/origin_id） |
| 全站命中后反查分区 | 向量检索原生 `filter: partition=该分区`，无召回损耗 |
| 权重为初值 | 对齐 rag-lab 基线 0.48/0.27/0.08（仍待评测调优） |

另：全量同步支持**断点续传**（已入库 origin 自动跳过，重跑不重复消耗额度）；新增 `/admin/ingest` 手动增量入口；服务内置每 IP 限流（5 次/10 秒）。

### 13.3 运行方式（平台 pod `/home/cloud/sse_market_assist`，2026-09-06 实测）

```bash
cd /home/cloud/sse_market_assist
.venv/bin/python scripts/run_sync.py --fresh                          # 全量重建索引（实测约 20 分钟 / 22,452 点）
nohup .venv/bin/python main.py >> logs/service.log 2>&1 &             # 启动服务（0.0.0.0:19084）
nohup .venv/bin/python scripts/run_sync.py --watch >> logs/sync.log 2>&1 &  # 常驻增量同步（每 30s）
curl http://127.0.0.1:19084/health                                    # 健康检查（pod 内）
curl -s -m 10 http://qdrant.infra.svc.cluster.local:6333/collections/gxxxxxx_market_assist_v1 \
  -H 'api-key: <实际key>'                                             # 集合点数核对（pod 内）
```

### 13.4 验证记录（阶段 1 只读验证，2026-08-26 实测）

#### 单元测试

- `tests/test_core.py` 7 项全部通过（分区白名单、排除"主页"、稳定点 ID、切块、关键词提取、合并重排 top-N 与空集）。

#### 服务与接口（127.0.0.1:19084）

| 项目 | 结果 |
| --- | --- |
| `/health` | ✅ `{"ok":true,"collection":"market_assist_v1","points":2180}` |
| `/api/v1/assist/match`（学习交流，"大三要不要主动找老师做项目…"） | ✅ 返回 AI 回答 + 3 条引用（命中 4867《要不要主动找老师要项目？》、4315《"一人一事"创新人才培养课题》、3884 保研排名），引用链接可跳转、带摘要与热度，回答遵守 `[n]` 标注 |
| `/api/v1/assist/refs`（生活日常，"宿舍网速太慢怎么办"） | ✅ answer=null，5 条引用全部为校园网相关帖（3963/2424/3190/2245/3198），mysql_hits=111，改写问句质量正常，took 1283ms |
| 网页测试页（19083 `/test` 代理 → 19084） | ✅ 全链路通过，页面可展示改写问句/命中数/引用 |
| 分区过滤 / 隐私 | ✅ 仅命中指定分区；检索 SQL 均带 `is_private=0`；楼中楼上下文只用 `cctext`，不触碰 `usertargetName` |

#### 索引进度与中断事件

- 全量同步至 **2180 点**（帖子约 59%）时 DashScope 返回 400 `Arrearage`（阿里云账号欠费，`Access denied ... overdue-payment`），embedding 全部被拒，同步进程中止；
- **已修复**：`app/embed.py` 区分 4xx（不重试、报出响应体）与网络/429/5xx（重试 3 次）；`retrieve.py` 捕获 `EmbeddingUnavailable` 后**降级为仅关键词检索**，`api.py` 检索链路整体兜底——实测 /match 在 embedding 不可用时**不再 500**，仍能返回关键词检索 + AI 回答（上表 match/refs 两条用例即降级模式下测得）；
- **续跑**：充值后执行 `.venv/bin/python scripts/run_sync.py --full`（按 `data/sync_state.json` 与 `existing_ids()` 断点续传，已入库的点跳过），预计总量约 2.6 万点（帖子 3.7k×1~2 块、一级回复 1.2 万、楼中楼 0.9 万）；**或**（2026-08-28 盘点确认的备选路径）不改代码、仅把 `.env` 的 Embedding 3 行切到 SiliconFlow bge-m3（1024 维兼容，ai_interview 已在用），再执行同一命令断点续跑；
- **注意**：向量索引未建成期间，回答质量以关键词检索为下限（上表用例仍可正常回答），充值续跑后语义召回自动生效，无需改代码。

#### 待办（索引补齐后）

- 对 3 个分区各 10 条真实用例做人工质检（8.1 阶段 1 第 3 项）；
- 若回答质量达标，进入阶段 2 评审。

### 13.5 当前状态与下一步（2026-09-07）

- ✅ 阶段 0：代码开发完成（本地语法检查 + 单元测试 24/24 通过）；
- ✅ 阶段 1：**全部完成并部署于平台**——平台 bge-m3 上线（2026-09-06 实测）、平台 Qdrant `gxxxxxx_market_assist_v1` 建集合 + `--fresh` 入库 **22,452 点**、服务运行于 market-rag pod（:19084）、`--watch` 常驻增量（累计 22,463 点）、**30/30 真实用例质量抽测通过**（详见 §13.7）；
- ⏸ 阶段 2/3（Nginx/前端/Go）：**未执行**，待评审后按第 8 节进行；
- 🔧 下一步：① 收集真实用户查询样本 → 调向量阈值/排序权重（`ASSIST_VECTOR_THRESHOLD` 等 7.1 参数）；② 落实 pod 重启后的服务自愈（无 supervisor，需手动/脚本拉起）；③ 完善评测打分表。（注：13.4 的 2180 点等记录为 08-26 旧索引时期的历史数据，已由 `--fresh` 重建取代。）

### 13.6 变更记录：存量 + 发帖/评论自动 embedding（2026-08-28 开发完成；2026-08-31 实测核验已上传服务器并在运行）

> （2026-09-07 注：本节的"容器内自建 bge-m3 + supervisord 自愈"结论已被 §2.7 推翻——平台 embedding 服务 9/6 已上线（`/v1/embeddings`），服务也已迁至 market-rag pod 并全量入库；下方历史验证记录保留备查，最终状态以 §13.7 为准。）

**功能**：

1. **存量 embedding**（全量同步，已有基础上升级）：水位改为**首次全量开始时快照并立即落盘**（断点续跑沿用原水位，中断期间新内容不漏）；`--fresh` 重建时重置水位。







2. **发帖/评论自动 embedding**（新增）：`run_sync.py --watch` 常驻进程，默认 30s 周期增量同步（`>=` 水位兜住同秒写入）+ 每 20 周期私密清理；单周期异常自动重试。**不阻塞发帖**：独立进程、不触碰 Go 后端、不在发帖请求路径上（README「自动同步与发帖的关系」有专门说明）。
3. **Embedding 切换平台 bge-m3**：`embed.py` 重写为 texts/openai 双协议（默认平台 `{"texts":[...]}`），多 schema 响应解析、条数校验、维度≠1024 告警；Qdrant 客户端支持可选 API Key（平台集群备选）。
4. **修复存量 bug**：`REPLIES_SQL`/`SUBREPLIES_SQL` 遗漏 `p.postID`（服务器上帖子同步完即欠费中断，回复/楼中楼尚未跑到该行，未暴露）——由新单测 `test_row_to_items_reply_and_subreply` 捕获。

**验证记录（本地；服务器实测状态见末行）**：

| 项目 | 结果 |
| --- | --- |
| 单元测试（tests/，含新增 test_sync.py 9 项 + test_embed.py 9 项） | ✅ 24/24 通过（Python 3.14 本地 venv） |
| embed 客户端端到端 | ✅ 本地 mock `/embed` 服务（平台格式）实测：请求格式正确、1024 维解析通过 |
| embed 服务不可用路径 | ✅ 连接拒绝 → 重试 3 次 → `EmbeddingUnavailable`，优雅失败（watch 可容忍） |
| 平台 embedding 服务 | ⚠️ 2026-08-31 定位（见 2.7）：不在市场服务器、**从未部署**——平台提供的是可自建服务的 K8s 容器（`group-market-rag`，SSH `ssh -p 22012 cloud@<MARKET_DOMAIN>`），bge-m3 权重齐备但无服务进程与依赖；恢复方案 = **容器内自建 bge-m3**（FastAPI 绑 8080 → 公网 23012，supervisord 随 pod 自愈）；客户端代码按用户提供的 curl 格式（`POST /embed {"texts":[...]}`）实现，无需改动 |
| 平台 Qdrant（25033） | ✅ API Key 验证通过（只读列出 collections）；实际集合前缀 `gxxxxxx_` 与用户提供 `gb1ca3490_` 不一致，**迁移前需确认** |
| 服务器代码与运行状态（2026-08-31 SSH 只读实测） | ✅ 8/28 改造已上传并在运行：`tests/` 含 test_embed.py + test_sync.py、`app/embed.py` 双协议、`scripts/run_sync.py --watch` 均核实存在，服务进程在跑（`pgrep -f main.py` 命中）；⚠️ 服务器与本地 `.env` 的 `EMBEDDING_BASE_URL` 仍为 `http://127.0.0.1:23011`（死隧道），embedding 调用全部失败，服务现以**关键词降级模式**运行（切换 23012 随 §2.7 恢复方案执行）；磁盘 62%（80G 可用） |

**上传状态**：已上传并运行（2026-08-31 SSH 只读实测核验，见上表末行）。

---

### 13.7 变更记录：平台 embedding 上线确认 + 服务迁 market-rag pod + 平台 Qdrant 全量入库（2026-09-06/07）

**背景**：用户拍板改用平台资源（**"用以上qdrant和这个pod"**）——服务迁 market-rag pod、向量写平台 Qdrant 集群；平台 embedding 服务于 2026-09-06 上线（详见 §2.7）。

**完成事项**：

1. **平台 embedding 上线确认**（2026-09-06，本地 + 市场服务器 + pod 三处实测）：`POST https://<MARKET_DOMAIN>:23011/v1/embeddings` `{"model":"bge-m3","input":[...]}` → 200 / 1024 维 / 无鉴权 / 公网有效证书；旧 `POST {base}/embed {"texts":[...]}` 已下线（HTTP 301→HTTPS 且 POST 变 GET→405）。客户端配置改为 `EMBEDDING_MODE=openai` + HTTPS base URL，8/31 的"容器内自建 bge-m3"恢复方案作废。
2. **平台 Qdrant 打通**（2026-09-06）：集群 `qdrant.infra.svc.cluster.local:6333`，API Key 首字符**大写 I**（`I****...`，小写 `l` → 401，系截图 OCR 歧义排查发现）；集合前缀实测 **`gxxxxxx_`**（聊天里给的 `gb1ca3490_` 过时）；集群现存红线集合 `gxxxxxx_docs`（只读不动）；新建 `gxxxxxx_market_assist_v1`（1024/cosine，`{"result":true}`）。
3. **pod 部署**（2026-09-06）：项目上传 `/home/cloud/sse_market_assist` → venv（Python 3.12.3）→ 依赖安装 → `pytest tests/ -q` **24/24 通过** → 服务启动 `/health` 200 → 绑 `0.0.0.0:19084`。MySQL 经公网 `<SERVER_IP>:3506` 直连生产库（root，只读 SQL）。
4. **全量重建**（2026-09-06，约 20 分钟）：`run_sync.py --fresh` → **22,452 点**（帖子 4459 + 回复 10,589 + 楼中楼 7,404；源库 4350 帖/12519 回复/9119 楼中楼，私密、主页分区等过滤后入库）。
5. **常驻增量**（2026-09-06 起）：`run_sync.py --watch` 每 30s 增量同步；首周期即捕获线上新帖 11 点（7 帖 + 4 回复）→ 累计 **22,463 点**；/health 与集合点数均绿。
6. **质量抽测**（2026-09-07，两轮共 42 例，覆盖全部 9 个可选分区）：第一轮 3 分区（学习交流/宿舍生活/实习就业）× 10 例 = **30/30 通过**；第二轮补测其余 6 个分区（技术分享/期末资料/生活日常/社团活动/组团捞人/课程专区/其他）× 2 例 = **12/12 通过**。两轮无 500、无 LLM 降级、固定 5 引用、`data.answer/references/meta` 结构完整、`[n]` 标注与引用链接正常。约 1/3 用例有精准直接答案（含高数真题网盘链接与提取码、食堂窗口推荐、毛概资料、社团招新、数模组队、API 讨论）；其余（含站内确无此类讨论的用例）诚实返回"站内暂无直接讨论"+ 侧面参考，属预期行为。**观察**：最慢单例 took 31s（DeepSeek 偶发慢，阶段 2 前端超时需按 ≥20s 设计，不能按原 3s）；"其他"分区为杂项分区，噪声帖较多但回答仍诚实。

**经验与遗留**：

- `--fresh` 期间 watch 与 fresh 并发写同一集合安全（稳定点 ID 幂等 upsert）；
- 检索参数（`ASSIST_VECTOR_THRESHOLD=0.35`、`ASSIST_TOP_K=5`、权重 0.48/0.27/0.08）为 rag-lab 基线，30/30 通过但直接命中率 ~1/3，待收集真实用户查询样本后调优（见 13.5 下一步 ①）；
- **pod 无 supervisor**（`/entrypoint.sh` 仅重置密码 + exec sshd），pod 重启后服务与 watch 需手动重新拉起（nohup 命令见 §13.3）；
- 阶段 2/3（Nginx/前端/Go）**未执行**，评审通过前不动（红线）。

### 13.8 变更记录：LLM 判断层（小 prompt → qwen，大 prompt → deepseek）（2026-09-12）

**背景**：用户要求"做一个判断层，小的问题用 qwen，大的问题用 deepseek；做完之后测试一下会不会爆上下文，如果会的话，就不做修改直接回滚到当前状态"。

**实现**（`app/llm.py` 判断层 + `app/generate.py` 接入 + `app/config.py` 新配置）：

- `estimate_prompt_tokens`：非 ASCII 1 token/字 + ASCII 3 字符/token（保守上界；实测 est/real ≈ 1.09）；
- `_make_router`：prompt 渲染后按预估 token 与阈值比较——≤ 阈值走 qwen（网关 qwen3.8-27b-awq，`reasoning_effort=none`，挂 DeepSeek 官方兜底），> 阈值走 deepseek（集市网关主 + 官方兜底，即原主通道）；
- 窗口自检 `window_is_safe`：`阈值 + 1536(输出) + 2000(余量) ≤ small_model_window`，越界自动退化为全程主通道（不冒险分流）；
- 新配置：`ASSIST_ROUTE_ENABLED`（回滚开关）/ `ASSIST_ROUTE_SMALL_MAX_TOKENS=20000` / `SSE_MARKET_SMALL_MODEL=qwen3.8-27b-awq` / `ASSIST_SMALL_MODEL_WINDOW=65536`。

**"会不会爆上下文"实测（pod，2026-09-12）**：

| 项 | 实测 |
| --- | --- |
| qwen3.8-27b-awq 上下文窗口 | **65,536 tokens**（128K 字符被 400，报错原文 "maximum context length is 65536 tokens"） |
| deepseek-v4-flash 窗口 | ≥ 90,503 tokens（128K 字符请求 200 通过） |
| 预估器标定（real/est） | 2,944/3,194、23,470/25,643、46,886/51,302 → 保守上界成立，仅高估 ~9% |
| 真实体量分布（近 6 帖完整 prompt） | 2,307~4,653 字符 ≈ 1,716~3,798 est tokens → **全部落 qwen 分支**（远低于阈值 20000） |
| 理论最坏（8 引用全截断） | ≈22,600 字符 ≈ 22,600 est tokens → 超阈值转 deepseek；即便误给 qwen，真实 ≈20.7K tokens 仍在 65,536 内 |
| 端到端（真实用例，同 prompt 双模型） | qwen 12.0s / 906 字；deepseek 19.5s / 1,162 字；均带 [n] 引用、结构达标 |
| qwen 健康度 | `reasoning_effort=none` 生效（22 prompt / 31 completion tokens，无思考浪费）；langchain `extra_body` 链路直达网关 |

**结论**：**不爆上下文**（最坏情形距 qwen 窗口仍有 ~3 倍余量，且超阈值自动转 deepseek）→ 未触发"回滚"分支，按原计划上线。

**上线与验证**：先在 pod 试运行目录（`/home/cloud/assist_route_trial`，与线上目录完全隔离）跑单测 21/21 + 探针（窗口标定/体量调研/端到端）；正式推送前把将被覆盖的三文件备份至 `backup_route_20260912_164909/`；重启后线上冒烟 `POST /api/v1/assist/match` → `[llm] 判断层：prompt 2608 字符 ≈ 2142 tokens ≤ 20000 → qwen3.8-27b-awq`，200 OK、8 引用。**回滚**：还原 `backup_route_20260912_164909/` 三文件 + 重启，或仅改 `.env` 的 `ASSIST_ROUTE_ENABLED=0`。

**观察与遗留**：① 真实流量几乎全部是"小 prompt"，故判断层实际把绝大部分请求导向 qwen——若想让"难题"用 deepseek，需要复杂度信号（会引入一次额外 LLM 调用与延迟），属后续可选项；② 网关 deepseek 在连发请求时偶发失败（试运行期间 6 次查询改写中 2 次切到官方兜底），兜底链工作正常、无用户可见影响。
