# 日报人工倾向功能 — 设计文档

日期：2026-09-22
状态：已与需求方逐节确认（自由文本倾向、白名单成员可见、三阶段灰度、不叠加私密帖灰度）；**2026-09-23 修订——入口呈现从「侧边栏 + 独立管理页」改为「最新一期日报帖详情页内按钮 + 弹窗」（需求方选定）**

## 1. 背景与目标

tech-digest 每日由 LLM 从热点榜候选中挑 1 篇全文转载（`app/daily_ai.py:_build_pick_prompt`，挑选标准硬编码：时效 > 影响 > 大厂/人物 > 学生相关…）。运营侧需要一个**人工倾向**能力：

- 平时**默认模式**，行为与现状完全一致；
- 需要**倾向模式**时（例：「jev 相关内容优先」），倾向作为**软偏置**注入挑选——倾向不是必定生效：倾向话题当天的候选若质量差（过气、太浅、擦边、与学习无关），必须放弃倾向，仍按既有标准挑最高价值一篇；
- 倾向内容为**自由文本一段话**，可随时改，下一次 daily 运行（timer 09:15 或手动触发）自动生效；
- 管理入口在集市网页**最新一期日报帖详情页内**（帖内按钮 + 弹窗，2026-09-23 修订），**仅白名单成员可见**（登录态识别，无密码），**三阶段灰度上线**。

范围：仅 daily 选文。star（整榜渲染）/ weekly（周汇总）不涉及「挑一篇」，不引入倾向。

## 2. 总体架构与数据流

```text
白名单成员浏览器（集市登录态，浏览最新一期日报帖）
   │  GET/POST /api/v1/digest-pref（携带 Bearer JWT）
   ▼
nginx（<MARKET_DOMAIN> 主站同域，仅此一条路径，仿 /api/v1/assist 先例）
   ▼
tech-digest 常驻面板进程 127.0.0.1:19086（manual_server.py 扩展）
   │  1. 拿 JWT 调集市后端 {market_base_url}/api/auth/info 解析 username
   │  2. 比对服务端白名单 DIGEST_PREF_ADMINS → 403 或放行
   │  3. GET 时本机读 store → 附带 daily_post_id（最近一期日报帖 ID）
   ▼
tech-digest/data/preference.json（唯一真源）
   │  daily 运行开始时现场读
   ▼
ai_pick 软倾向注入 → 挑 1 篇 → 发帖
```

两条铁律：

1. **前端白名单只管「看不看得见入口」，服务端白名单才是权威**——直达 URL、伪造请求都被服务端 403。
2. **preference.json 是唯一真源**——写路径（面板进程/nginx）故障只影响「改」，daily 读文件照跑，互不耦合。

## 3. tech-digest 侧：偏好存储与选文软注入

### 3.1 新增 `app/preference.py`

```jsonc
// data/preference.json
{
  "mode": "default",            // "default" | "prefer"
  "text": "",                   // prefer 模式的自由文本，≤200 字
  "updated_at": "2026-09-22T10:00:00",
  "updated_by": "Rimuru"
}
```

`load_preference() -> Preference`（dataclass：mode/text/updated_at/updated_by）容错规则——以下情况一律回退默认模式并记 warning，绝不抛异常打断主流程：

- 文件不存在；
- JSON 解析失败；
- `mode` 非 `default`/`prefer`；
- `mode == "prefer"` 但 `text` 去空白后为空或超 200 字。

### 3.2 prompt 注入（`app/daily_ai.py`）

`_build_pick_prompt(candidates, preference: Preference | None = None)`。`preference.mode == "prefer"` 时，在现有 6 条挑选标准之后追加一段：

> 7. 编辑当前倾向：{text}。这是**加分项，不是硬性条件**：倾向话题的候选若今天质量差（过气、太浅、擦边、与学习无关），必须放弃倾向，仍按上述 1-6 条标准挑今天最高价值的一篇。宁可发非倾向的高价值文章，不发倾向的低质量文章。

候选行不做任何关键词标记（不加「命中倾向」之类标签），命中与否由 LLM 依据语义判断——软生效即不做硬匹配。

- **默认模式下 prompt 与现状逐字一致**（回归保障）。
- `sanitize_pick` 不校验「是否倾向命中」；`main.py` 的 AI 挑选失败降级路径（首条候选）也不掺倾向——降级只保命不加分。

### 3.3 接线与可观测（`main.py`）

`run_daily` 生成候选后、调用 `ai_pick` 前 `load_preference()`：

- 日志一行：`倾向模式: prefer（jev 相关内容优先…）/ 默认模式`；
- store 的 daily ok 记录 detail 增加 `pref_mode` 与 `pref_text`（截 30 字）；
- 传给 `ai_pick(candidates, day, preference=...)`。

## 4. 偏好 API（`manual_server.py` 扩展）

面板进程已常驻（`scripts/manual-panel.sh` 启停现成），在其上新增两个端点：

### 4.1 契约

- 两个端点均需通过鉴权，鉴权失败一律 `403`；身份解析网络异常/集市后端不可用一律拒（**fail closed，不降级放行**）。
- `GET /api/v1/digest-pref` → `200 {"mode","text","updated_at","updated_by","daily_post_id"}`（未初始化时返回 default 空文本；`daily_post_id`=最近一期日报帖 ID——先查当天、无则查前一日，均无为 null。帖内入口靠它与当前帖 ID 比对决定按钮显隐，前端无需硬编码日报账号）
- `POST /api/v1/digest-pref`，body `{"mode","text"}`：
  - 校验：`mode ∈ {default, prefer}`；`prefer` 时 `text` 必填、去空白后 1–200 字；`default` 时 text 忽略并清空；
  - 通过：原子写入 preference.json（tmp + rename），带 `updated_at`（当前时间）与 `updated_by`（解析出的 username），并向 `log/tech-digest.log` 写一行审计（谁、何时、mode、text 全文）；
  - 不通过：`400`（参数）。

### 4.2 鉴权：JWT → username → 白名单

- 取请求头 `Authorization: Bearer <JWT>`；
- 调集市后端 `GET {market_base_url}/api/auth/info`（复用 `app.config.settings.market_base_url`，默认 `http://127.0.0.1:8080`，与 publisher.py 的 `/api/auth/post` 同前缀；若该路径 404，实现时探测 `/auth/info` 并固定之），携带同一 Bearer；
- 响应 `data.user.name` 即 username；
- `username ∈ DIGEST_PREF_ADMINS` 才放行。服务端名单读 env `DIGEST_PREF_ADMINS`（逗号分隔），**默认 `Rimuru`**。
- 身份解析封装为独立函数（HTTP 客户端可注入），便于单测。

### 4.3 暴露面控制

nginx 仅反代 `/api/v1/digest-pref` 一条路径到 `127.0.0.1:19086`；面板原有 `/api/run`、`/api/file`、`/api/log` 不暴露、维持仅 127.0.0.1 可达。

## 5. nginx 反代（服务器一次性配置）

主站 server 块（与 `/api/v1/assist` 同一 server）追加：

```nginx
location /api/v1/digest-pref {
    proxy_pass http://127.0.0.1:19086;
    proxy_set_header Authorization $http_authorization;
}
```

（路径不重写，面板端按原路径匹配；`proxy_set_header` 其余按 assist 现有写法对齐。）改前 `nginx -t`，reload 生效；遵循磁盘/备份防复发清单，不产生大文件操作。

## 6. 集市前端（`_fe/` Vue 源码）

### 6.1 可见性开关 `src/config/digestPref.ts`（新增）

```ts
export const DIGEST_PREF_ADMINS: string[] = ['Rimuru']

export function isDigestPrefEnabled(username: string): boolean {
  if (import.meta.env.VITE_DIGEST_PREF_ENABLED !== 'true')
    return false
  if (DIGEST_PREF_ADMINS.includes(username))
    return true
  try {
    return localStorage.getItem('digestPref.force') === '1'
  }
  catch {
    return false
  }
}

export function digestPrefBaseUrl(): string {
  return import.meta.env.VITE_DIGEST_PREF_BASE_URL || '/api/v1/digest-pref'
}
```

与 `assist.ts` 完全同构（编译开关 + 账号白名单 + localStorage 调试后门）。

### 6.2 API 封装 `src/api/digestPref.ts`（新增）

`getDigestPref()` / `saveDigestPref({mode, text})`：经 `ensureAccessToken()` 续期后，向 `digestPrefBaseUrl()` 发 GET/POST，携带 `Authorization: Bearer <userStore.token>`（对齐 `api/opsagent/headers.ts` 写法）；错误经 toast 展示。`DigestPrefData` 含 `daily_post_id: number | null`。

### 6.3 管理弹窗 `src/components/DigestPrefModal.vue`（新增，2026-09-23 修订）

- **弹窗即全部管理 UI**（无独立管理页、无路由）：标题「调整日报倾向」；当前生效状态（最后修改人/时间；从未设置过显示「默认模式（尚未设置过倾向）」）；单选（默认模式 / 倾向模式）+ 文本框（prefer 时必填，≤200 字，带计数）+ 保存按钮（成功 toast「已保存，下一次日报生成（每日 09:15）生效」并关闭弹窗）；固定一行软性说明（「倾向只是加分项：倾向话题当天没有高质量文章时，日报仍会选其他最高价值的一篇」）；
- 手写 fixed 遮罩弹窗（集市无第三方 UI 库，与现有组件风格一致）；`defineAsyncComponent` 懒加载，非白名单用户零加载成本；
- 弹窗关闭后父组件重拉一次 GET，保持回显与按钮显隐同步。

### 6.4 帖内入口（`src/views/PostDetailView.vue` 挂点，2026-09-23 修订）

- 不建路由、不动侧边栏/头部组件；入口 = **最新一期日报帖详情页内的「调整日报倾向」按钮**（帖子正文之后、评论区之前），点击打开 6.3 弹窗；
- 按钮显隐条件（**同时满足**才显示）：`isDigestPrefEnabled(username)` **且** 当前帖 ID === GET 返回的 `daily_post_id`；
- 判定方式取舍：
  - 不用「作者 == 日报账号」——前端要硬编码账号 ID，账号换号即静默失效，且周末 star/weekly 帖也会挂按钮；
  - 不用「仅当天帖子」——次日 00:00–09:15 不存在当天帖，恰是赶在 09:15 生成前改倾向的黄金窗口，按钮反而消失；
  - 「最近一期」语义即「看完这期、调下一期」；且 PostDetailView 为 PC/PWA 共用，**天然覆盖移动端**（侧边栏方案的移动端本无入口）；
- 非白名单用户在挂点逻辑**提前返回**：不发 digest-pref 请求、不加载弹窗 chunk；帖内改动自包含（一个 v-if 按钮 + 懒加载弹窗），不触碰既有渲染分支。

### 6.5 构建部署

沿用 `_fe_deploy_whitelist.py` 既定流程：本地改 `_fe/` 源码（`config/digestPref.ts`、`api/digestPref.ts`、`components/DigestPrefModal.vue`、`views/PostDetailView.vue` 四文件）→ SFTP 上传变更文件 → 服务器 `/root/market-deploy/sse_market_new_client/newSSE` 内 `npm run build` → dist 就地备份（`dist.bak.<TS>`）→ 公网 hash 确认。`VITE_DIGEST_PREF_ENABLED` 通过服务器构建 env 控制（对齐 `VITE_ASSIST_ENABLED` 现有管理方式）。

## 7. 灰度三阶段

| 阶段 | 动作 | 效果 |
|---|---|---|
| ① 后端先行 | tech-digest 上线 preference.py + prompt 注入 + API（含 daily_post_id）；nginx 加反代；前端以 `VITE_DIGEST_PREF_ENABLED=false` 构建（帖内入口在网页上不存在） | 帖子详情页零变化；SSH 直改 preference.json 或直接 curl API（带 JWT）验证软生效与日志倾向行 |
| ② 白名单灰度 | 开 `VITE_DIGEST_PREF_ENABLED=true`，前端名单 `['Rimuru']` | 仅 Rimuru 登录在最新一期日报帖内可见按钮、可用弹窗，其余成员无感；观察几天选文，重点看「该弃倾向时弃倾向」是否符合预期 |
| ③ 扩量 | 按需把成员加进前后端两处名单，重建前端 | — |

不叠加「帖子先私密审后公开」（需求方已明确排除）；dry-run 验证用面板现有能力即可。

## 8. 测试计划（TDD）

### 8.1 tech-digest 单测（`tests/`）

1. `load_preference`：正常 prefer / 文件缺失 / 坏 JSON / mode 非法 / prefer+空 text / prefer+超长 text → 各自回退或原样；
2. `_build_pick_prompt`：prefer 模式含「编辑当前倾向」「加分项，不是硬性条件」「宁可发非倾向」与倾向原文；default（及 None）模式输出与不传参时**逐字一致**；
3. 偏好 API handler：白名单过（200 并正确写文件+审计行）/ 非白名单（403）/ JWT 缺失（403）/ 身份端点网络失败（403，fail closed）/ POST 参数非法（400）/ 原子写与 updated_by 注入。身份解析用注入的假客户端。GET 响应另验 `daily_post_id`：store 正常（当天命中 / 前一日回退）、store 异常返回 null（GET 不因此 500）。

### 8.2 前端

`_fe/` 无既有单测设施，走灰度人工验证：`localStorage.setItem('digestPref.force','1')` 调试开关 + Rimuru 账号实测帖内按钮显隐（最新一期日报帖出现；旧日报 / star / weekly / 非白名单 / flag 关均不出现）与弹窗读写。

### 8.3 端到端（阶段②验收）

在最新一期日报帖弹窗设倾向 → 「运行 daily（dry-run）」→ 审产物 md 与日志倾向行 → 切回默认模式再 dry-run 确认 prompt 复原 → 真跑观察线上帖。

## 9. 风险与边界

- 面板进程挂：只丢「改偏好」（帖内按钮因 GET 失败不出现，属优雅降级），daily 读文件照跑；重启脚本现成。
- 帖内入口挂集市核心页 PostDetailView：改动自包含（一个 v-if 按钮 + defineAsyncComponent 懒加载弹窗），非白名单用户零请求零加载；`daily_post_id` 由服务端下发，前端无硬编码账号。
- 集市后端不可用：写接口 fail closed。
- 倾向文本进 prompt：仅白名单成员可写、200 字上限、审计可查，注入面可控。
- 最坏后果上限：偏好被滥用也只影响选文软倾向，不触碰发帖执行链。
- 兼容性：默认模式 prompt 逐字不变；不部署本功能时 tech-digest 行为与现状完全一致。

## 10. 明确不做

- star / weekly 的倾向；
- sanitize 层的倾向硬校验、候选关键词标记；
- 降级路径（首条候选）掺倾向；
- 密码/token 面板鉴权体系（成员白名单替代）；
- 帖子私密帖灰度层；
- 侧边栏入口与独立管理页/路由（2026-09-23 修订：呈现改为帖内按钮 + 弹窗）；公共导航/其他头部组件一律不动；
- 非最新一期日报帖（旧日报 / star / weekly）上的入口。

## 11. 实施记录

### 2026-09-27 阶段②灰度上线 + 端到端验收（Task 11）

### 三阶段执行情况

- 阶段①（休眠版）：2026-09-24 上线（flag=false，编译开关关闭，公网 entry `index-Pv9CvQC8.js`）。**勘误（2026-09-27 终审 Minor⑥）**：原文误记为 `index-Cym18yY6.js`——那是同日 19:03 兄弟会话在服务器外部重建的产物（事故复删之后又重建过一次），非 T10 部署产物；阶段①真正的部署产物是 09-24 11:13 T10 上线的 `index-Pv9CvQC8.js`。时序：11:13 T10 部署（Pv9CvQC8）→ 15:48 兄弟会话复删事故块（源文件修复）→ 19:03 兄弟会话外部重建（hash 变为 Cym18yY6）。
- 阶段②（本日）：flag=true 重建上线，Rimuru 白名单灰度生效。新公网 entry `assets/index-BdHogEgI.js`（dist 与公网 md5 相等 `d1d91cb9…`，构建 `✓ built in 21.12s` 无 error TS，引用清单 REF_LIST_MATCH）。最终态：flag=true + preference.json mode=default（日报行为与现状一致）+ 审计链完整。
- 阶段③（扩量）：未启动，按需执行——前端 `DIGEST_PREF_ADMINS` 常量 + 服务器 env `DIGEST_PREF_ADMINS` 两处同改后重跑 `_fe_deploy_digest_pref.py`。

### reconcile 事实（09-24 并行修复并入）

09-24 有并行会话对服务器 `PostDetailView.vue` 做了 assist「重删点击生成」修复（只读留底、删除 `handleAssistClick`/`aiAssistLoading`/`assistResolvers`/触发区块/CSS，净删除 3423B）。Task 11 Step 0 三方对照（A=09a3964 / B=103ae9e / S=服务器版）证实 S = B − 点击生成块且 digest-pref 挂点逐行相同（DigestPrefModal×6、digest×24 与 B 一致），cmp 后采纳为基线并入库（commit `6c37f54`）；其余 3 个 digest 文件服务器与 worktree md5 逐字节相等（40062156…/e3e0fb30…/a33dcc37…）。

### 端到端结果（API 链路，2026-09-27）

- 无 token GET 公网 `/api/v1/digest-pref` → 403（fail closed）✓
- bot JWT GET → 200 `mode=default`；POST prefer「jev 相关内容优先」→ 200；GET 回显 prefer + Rimuru + 时间戳 ✓
- 服务器核对：`data/preference.json` 同步为 prefer；日志 `[pref-audit] Rimuru mode=prefer text=jev 相关内容优先` ✓
- dry-run：日志 `倾向模式: prefer（jev 相关内容优先…）`，全流程 rc=0、产物 md 生成（AI 选非 jev 文章属软生效预期）✓
- POST default → 200；再 dry-run：`倾向模式: 默认`；审计行 `[pref-audit] Rimuru mode=default` ✓（完整链：默认→prefer→dry-run prefer→default→dry-run 默认）

### 已证设计事实

- 发帖账号 user.name=Rimuru，= 默认白名单唯一成员。
- POST 响应 `daily_post_id` 恒 null 属设计内（写接口不下发）。
- `refresh_access_token()` 返回 `(access_token, new_refresh_token)` 元组（非字符串）；Task 11 本体验收共轮换 3 次（原上限 3）；fix round 1（回看窗口裁定重部署 + GET 复验）新增第 4 次轮换（超原上限属本修复必要动作，已裁定）。
- **周末边界（已裁定，2026-09-27 fix round 1）**：周末 daily 只入库不发帖（「周末只抓取入库、不发帖」）。原实现 `_latest_daily_post_id()` 仅回看当日+前一日 → 周五帖的帖内按钮周六可见、周日起消失。**已裁定回看窗口 2→4 天**：正常周循环最坏需 3 天（周五帖 → 周一 09:15 前仍可见），余量 1 天；store 异常仍一律 None（GET 不因此 500）。已随本修复（Task 11 fix round 1）重部署生效。

### 观察期指引

- ≥3 天：每天 09:15 timer 默认模式跑（行为与现状一致）；期间可在最新日报帖弹窗随时切倾向试运行，重点看「该弃倾向时弃倾向」案例（倾向是加分项、质量差必须弃）。
- 前端验收（Rimuru 登录 <MARKET_DOMAIN>，建议周一后做）：最新一期日报帖详情见「调整日报倾向」按钮→弹窗回显当前模式；旧日报/star/weekly 帖无按钮；非白名单账号或无痕窗口无按钮（API 403）；弹窗切倾向保存→toast→重开回显；localStorage `digestPref.force` 为非白名单账号调试后门（可留可清）。

## 12. 部署产物抄档（终审 Minor④，2026-09-27 从生产只读取回归档）

> 以下三件生产配置不入版本库（房内既有风格：服务器配置留在服务器），抄档仅供灾备重建时对照。2026-09-27 由本会话 ssh 只读（cat / grep / sed -n）从生产 <SERVER_IP> 取回，逐字原样。

### digest-pref-proxy.service（`/etc/systemd/system/digest-pref-proxy.service` 全文）

```ini
[Unit]
Description=tech-digest digest-pref Same-Origin Proxy
After=network.target

[Service]
Type=simple
WorkingDirectory=/root/market-deploy/agent/tech-digest
Environment=PREF_PROXY_UPSTREAM=http://127.0.0.1:19086
Environment=PREF_PROXY_HOST=172.17.0.1
Environment=PREF_PROXY_PORT=19087
ExecStart=/root/market-deploy/agent/tech-digest/.venv/bin/python3 /root/market-deploy/agent/tech-digest/scripts/pref_public_proxy.py
Restart=on-failure
RestartSec=3

[Install]
WantedBy=multi-user.target
```

### 桥接代理关键 env（以上 unit 文件 Environment 行为准）

- `PREF_PROXY_UPSTREAM=http://127.0.0.1:19086`（上游 = 宿主机 manual_server）
- `PREF_PROXY_HOST=172.17.0.1`（docker0 网关，容器内 nginx 经此回连宿主机）
- `PREF_PROXY_PORT=19087`（代理监听端口；nginx `proxy_pass` 指向 `host.docker.internal:19087`）

### 容器 nginx 的 digest-pref location 块（`/root/market-deploy/nginx_proxy/nginx.conf` 602-613 行）

```nginx
    location /api/v1/digest-pref {
      proxy_pass http://host.docker.internal:19087;
      proxy_http_version 1.1;
      proxy_set_header Host $host;
      proxy_set_header X-Real-IP $remote_addr;
      proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
      proxy_set_header X-Forwarded-Proto $scheme;
      proxy_set_header Authorization $http_authorization;
      proxy_connect_timeout 10s;
      proxy_send_timeout 60s;
      proxy_read_timeout 60s;
    }
```
