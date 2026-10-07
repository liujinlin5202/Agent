# 集市 AI 应答：对话模型切至集市网关（主备双通道）Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把 sse_market_assist 的对话模型从「DeepSeek 官方直连」改为「集市自有网关为主 + DeepSeek 官方为备」的双通道自动切换。

**Architecture:** 只改 LLM 接入层——`app/config.py` 新增网关主通道配置（`SSE_MARKET_*`），DeepSeek 降为可选备通道；`app/llm.py` 用 LangChain `with_fallbacks` 组合两个 `ChatOpenAI`。检索改写（`retrieve.py`）与回答生成（`generate.py`）都经 `get_llm()`，自动获得双通道，**这两个文件不改**。Embedding、Qdrant、MySQL、WebSearch 全部不动。

**Tech Stack:** Python 3.14 / FastAPI / LangChain（langchain-openai 1.6.0、langchain-core 1.6.1）/ 本地 venv `.venv_local`

**Spec:** [集市分区智能检索与AI应答功能-技术实现文档.md](../../../集市分区智能检索与AI应答功能-技术实现文档.md)

## Global Constraints

- **网关主通道**：`https://api.<MARKET_DOMAIN>/v1`，模型 `deepseek-v4-flash`，OpenAI 兼容协议。
- **网关 key**：与本机 cc-switch 里 market-deploy provider 的 `ANTHROPIC_AUTH_TOKEN` 同一把（`sk-****…` 开头）。**任何文档、计划、提交中都不得出现明文 key**；服务器 .env 由部署步骤注入。
- **不要给网关设 `reasoning_effort`**：实测该网关 deepseek 系模型**拒绝** `reasoning_effort="none"`（HTTP 400 `ModelArts.81001`，仅接受 low/medium/high/xhigh/max）。若将来把主通道换成 `qwen3.8-27b-awq`，则**必须**显式设 `reasoning_effort="none"`，否则思考烧光 token 输出空内容（2026-09-10 实测）。
- **未采用的模型**：`ling-3.0-flash`（502 上游故障）、`kimi-k2.6`（实测 12.4s，明显慢于 deepseek-v4-flash 的 ~4s）。
- **生成参数两通道保持一致**：temperature 0.2、max_completion_tokens 1536、timeout 100s、max_retries 0（沿用现网已验证参数，勿改）。
- **日志风格**：本服务无 logging 配置，统一 `print(..., flush=True)`（见 `app/sync.py`、`app/embed.py`）。
- **降级链不变**：双通道都失败 → `api.py` 现有 except 兜底（返回 `answer: null` + 引用列表）。
- 部署目标：`cloud@<SERVER_IP>:22012`，服务目录 `/home/cloud/sse_market_assist`。

---

### Task 1: 配置层接入网关主通道

**Files:**
- Modify: `sse_market_assist/app/config.py:35-38`
- Modify: `sse_market_assist/.env`（本地，gitignored）
- Modify: `sse_market_assist/.env.example`
- Test: `sse_market_assist/tests/test_core.py`

**Interfaces:**
- Consumes: 无
- Produces: `settings.sse_market_api_key: str`（必填，缺失时 import 即 KeyError）、`settings.sse_market_base_url: str`、`settings.sse_market_model: str`；`settings.deepseek_api_key: str`（由必填改为**可空**，Task 2 据此决定是否挂备用通道）

- [ ] **Step 1: 先写失败的测试**

在 `sse_market_assist/tests/test_core.py` 顶部 import 行改为：

```python
from app.config import EXCLUDE_PARTITIONS, PARTITIONS, settings
```

文件末尾追加：

```python
def test_llm_channels_config():
    """2026-09-10 起：主通道=集市网关，备通道=DeepSeek 官方。"""
    assert settings.sse_market_base_url.startswith("https://api.<MARKET_DOMAIN>")
    assert settings.sse_market_model
    assert settings.sse_market_api_key  # 必填项，缺失时 import config 即 KeyError
    assert settings.deepseek_base_url.startswith("https://api.deepseek.com")
    assert isinstance(settings.deepseek_api_key, str)  # 备通道可空，但不能缺失该属性
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd sse_market_assist && ../.venv_local/Scripts/python.exe -m pytest tests/test_core.py::test_llm_channels_config -q`
Expected: FAIL —— `AttributeError: type object 'Settings' has no attribute 'sse_market_base_url'`

- [ ] **Step 3: 改 config.py**

把 `sse_market_assist/app/config.py` 第 35-38 行：

```python
    # DeepSeek 对话模型
    deepseek_api_key: str = os.environ["DEEPSEEK_API_KEY"]
    deepseek_base_url: str = os.environ.get("DEEPSEEK_BASE_URL", "https://api.deepseek.com/v1")
    deepseek_model: str = os.environ.get("DEEPSEEK_MODEL", "deepseek-v4-flash")
```

替换为：

```python
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
```

- [ ] **Step 4: 更新本地 .env（追加主通道，保留备通道）**

在 `sse_market_assist/.env` 的 DeepSeek 段之前插入（key 从本机 cc-switch 取：`python -c "import sqlite3,json,os;con=sqlite3.connect(os.path.expanduser('~/.cc-switch/cc-switch.db'));print(json.loads(con.execute(\"SELECT settings_config FROM providers WHERE id='57869bb0-c7bc-4fba-8ac6-c2c2f37159f9'\").fetchone()[0])['env']['ANTHROPIC_AUTH_TOKEN'])"`）：

```
# ---- 主通道：集市自有网关（2026-09-10 起）----
SSE_MARKET_API_KEY=sk-****...（真 key 从 cc-switch 取，勿写入任何提交物）
SSE_MARKET_BASE_URL=https://api.<MARKET_DOMAIN>/v1
SSE_MARKET_MODEL=deepseek-v4-flash

# ---- 备用通道：DeepSeek 官方（主通道失败时自动切换）----
```

- [ ] **Step 5: 同步 .env.example 模板**

`sse_market_assist/.env.example` 第 5-8 行改为：

```
# ---- 主通道：集市自有网关（OpenAI 兼容；2026-09-10 起为主用）----
SSE_MARKET_API_KEY=sk-xxx
SSE_MARKET_BASE_URL=https://api.<MARKET_DOMAIN>/v1
SSE_MARKET_MODEL=deepseek-v4-flash

# ---- 备用通道：DeepSeek 官方（主通道失败时自动切换；留空则退化为单通道）----
DEEPSEEK_API_KEY=sk-xxx
DEEPSEEK_BASE_URL=https://api.deepseek.com/v1
DEEPSEEK_MODEL=deepseek-v4-flash
```

- [ ] **Step 6: 跑全量测试确认通过**

Run: `cd sse_market_assist && ../.venv_local/Scripts/python.exe -m pytest tests -q`
Expected: 全部 PASS（含新增 `test_llm_channels_config`）

- [ ] **Step 7: 提交**

```bash
git add sse_market_assist/app/config.py sse_market_assist/.env.example sse_market_assist/tests/test_core.py
git commit -m "feat(assist): 对话模型主通道切至集市网关（配置层）"
```

（`.env` 被 .gitignore 忽略，不入库——确认 `git status` 无 .env）

---

### Task 2: LLM 客户端主备双通道 + 切换日志

**Files:**
- Modify: `sse_market_assist/app/llm.py`（整体重写，约 60 行）
- Modify: 措辞订正 `app/generate.py:2`、`app/retrieve.py:2,24`、`app/context.py:113,133`、`app/api.py:191`（把「DeepSeek」改为「LLM」，不含逻辑改动）
- Test: `sse_market_assist/tests/test_llm.py`（新建）

**Interfaces:**
- Consumes: Task 1 的 `settings.sse_market_*` / `settings.deepseek_*`
- Produces: `_make_llm(model, base_url, api_key) -> ChatOpenAI`；`_notify_failover(runnable, label) -> RunnableLambda`；`_build_llm(primary_key, primary_url, primary_model, fallback_key, fallback_url, fallback_model) -> Runnable`（无 fallback_key 时返回 `ChatOpenAI`，否则返回 `RunnableWithFallbacks`）；`get_llm() -> Runnable`（lru_cache 单例，调用方 `PROMPT | get_llm() | StrOutputParser()` 不变）

- [ ] **Step 1: 写失败的测试**

新建 `sse_market_assist/tests/test_llm.py`：

```python
# -*- coding: utf-8 -*-
"""LLM 双通道单测：构造、参数一致性、主通道失败时的切换与日志。"""
from __future__ import annotations

import sys
from pathlib import Path

from langchain_core.runnables import RunnableLambda
from langchain_core.runnables.fallbacks import RunnableWithFallbacks
from langchain_openai import ChatOpenAI

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.llm import _build_llm, _make_llm, _notify_failover


def test_make_llm_params():
    llm = _make_llm("m1", "https://gw.example/v1", "k1")
    assert llm.model_name == "m1"
    assert llm.openai_api_base == "https://gw.example/v1"
    assert llm.temperature == 0.2
    assert llm.max_tokens == 1536
    assert llm.request_timeout == 100
    assert llm.max_retries == 0


def test_single_channel_when_no_fallback_key():
    """备通道未配 key（空串）时退化为单通道，返回裸 ChatOpenAI。"""
    llm = _build_llm("k1", "https://gw.example/v1", "m1", "", "", "")
    assert isinstance(llm, ChatOpenAI)
    assert llm.model_name == "m1"


def test_dual_channel_structure():
    llm = _build_llm("k1", "https://gw.example/v1", "m1",
                     "k2", "https://ds.example/v1", "m2")
    assert isinstance(llm, RunnableWithFallbacks)
    primary = llm.runnable
    assert isinstance(primary, ChatOpenAI)
    assert primary.model_name == "m1"
    assert primary.openai_api_base == "https://gw.example/v1"
    assert len(llm.fallbacks) == 1  # 备通道已挂上


def test_failover_switches_and_logs(capsys):
    """主通道抛异常 → 真实落到备通道，且 stdout 留下可观测日志。"""
    def boom(_):
        raise RuntimeError("主通道挂了")

    primary = RunnableLambda(boom)
    backup = RunnableLambda(lambda _: "来自备用通道")
    chain = primary.with_fallbacks([_notify_failover(backup, "备用通道(DeepSeek)")])

    out = chain.invoke("hi")

    assert out == "来自备用通道"
    assert "已切换到备用通道(DeepSeek)" in capsys.readouterr().out
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd sse_market_assist && ../.venv_local/Scripts/python.exe -m pytest tests/test_llm.py -q`
Expected: FAIL —— `ImportError: cannot import name '_build_llm' from 'app.llm'`

- [ ] **Step 3: 重写 app/llm.py**

```python
# -*- coding: utf-8 -*-
"""LLM 客户端：主通道集市网关（OpenAI 兼容）+ 备用通道 DeepSeek 官方，失败自动切换。

2026-09-10 起主通道由 DeepSeek 官方直连改为集市自有网关（api.<MARKET_DOMAIN>/v1，
与 tech-digest 同网关同 key）；主通道任何异常由 LangChain with_fallbacks 兜到
DeepSeek 官方，并打印切换日志——静默降级会让网关故障无人察觉（2026-09-01 教训）。

Embedding 不走 langchain（兼容性实测不通过），见 app/embed.py。
"""
from __future__ import annotations

from functools import lru_cache

from langchain_core.runnables import Runnable, RunnableLambda
from langchain_openai import ChatOpenAI

from app.config import settings

# 生成参数（两通道保持一致，沿用现网已验证值）
_TEMPERATURE = 0.2
_MAX_TOKENS = 1536
# 单次 100s 上限、不重试：基线实测（42 例）最慢 ~31s；重试只会叠时长，保持 0。
_TIMEOUT = 100
_MAX_RETRIES = 0


def _make_llm(model: str, base_url: str, api_key: str) -> ChatOpenAI:
    """按统一参数构造一个 OpenAI 兼容客户端。"""
    return ChatOpenAI(
        model=model,
        base_url=base_url,
        api_key=api_key,
        temperature=_TEMPERATURE,
        max_completion_tokens=_MAX_TOKENS,
        timeout=_TIMEOUT,
        max_retries=_MAX_RETRIES,
    )


def _notify_failover(runnable: Runnable, label: str) -> Runnable:
    """包在备通道外层：被调用即说明主通道已失败，先打日志再执行。"""
    def _invoke(input, **kwargs):
        print(f"[llm] 主通道（集市网关）失败，已切换到{label}", flush=True)
        return runnable.invoke(input, **kwargs)
    return RunnableLambda(_invoke)


def _build_llm(primary_key: str, primary_url: str, primary_model: str,
               fallback_key: str, fallback_url: str, fallback_model: str) -> Runnable:
    """主通道必配；备通道配了 key 才挂上（未配则单通道返回）。"""
    primary = _make_llm(primary_model, primary_url, primary_key)
    if not fallback_key:
        return primary
    backup = _make_llm(fallback_model, fallback_url, fallback_key)
    return primary.with_fallbacks([_notify_failover(backup, "备用通道(DeepSeek)")])


@lru_cache(maxsize=1)
def get_llm() -> Runnable:
    """对话模型单例：集市网关为主，DeepSeek 官方为备。检索回答场景温度调低，减少幻觉。"""
    return _build_llm(
        settings.sse_market_api_key, settings.sse_market_base_url, settings.sse_market_model,
        settings.deepseek_api_key, settings.deepseek_base_url, settings.deepseek_model,
    )
```

- [ ] **Step 4: 跑测试确认通过**

Run: `cd sse_market_assist && ../.venv_local/Scripts/python.exe -m pytest tests/test_llm.py -q`
Expected: 4 passed

- [ ] **Step 5: 措辞订正（纯注释，不改逻辑）**

把下列位置的「DeepSeek」改为「LLM」或「主通道」：
- `app/generate.py:2` → `"""AI 回答生成（LCEL 链）：LLM 基于检索材料生成带引用回答（约束规则见文档 4.3）。`
- `app/retrieve.py:2` → `"""检索层：LLM 查询改写 + MySQL 关键词检索 + Qdrant 分区语义检索 + 合并重排。`
- `app/retrieve.py:24` → `# 1. 查询改写（LLM；失败回退原文）`
- `app/context.py:113` / `:133` → `...供 LLM prompt 使用。`
- `app/api.py:191` → `# noqa: BLE001  LLM 双通道均故障 → 降级：只给引用列表`

- [ ] **Step 6: 真实网关连通性自测（不依赖服务器）**

Run:
```bash
cd sse_market_assist && ../.venv_local/Scripts/python.exe -c "
from app.llm import get_llm
from langchain_core.output_parsers import StrOutputParser
from langchain_core.prompts import ChatPromptTemplate
chain = ChatPromptTemplate.from_messages([('user','只回复两个字：收到')]) | get_llm() | StrOutputParser()
print('输出:', chain.invoke({}))
"
```
Expected: 输出「收到」之类短文本，无异常（证明主通道真实可用；若打印了切换日志，说明网关侧有问题，需停下来查）

- [ ] **Step 7: 跑全量测试**

Run: `cd sse_market_assist && ../.venv_local/Scripts/python.exe -m pytest tests -q`
Expected: 全部 PASS

- [ ] **Step 8: 提交**

```bash
git add sse_market_assist/app/llm.py sse_market_assist/app/generate.py sse_market_assist/app/retrieve.py sse_market_assist/app/context.py sse_market_assist/app/api.py sse_market_assist/tests/test_llm.py
git commit -m "feat(assist): LLM 双通道（集市网关主 + DeepSeek 备）与切换日志"
```

---

### Task 3: 技术实现文档同步

**Files:**
- Modify: `集市分区智能检索与AI应答功能-技术实现文档.md`

**Interfaces:**
- Consumes: Task 1/2 的最终配置与代码
- Produces: 无（文档）

- [ ] **Step 1: 更新选型表（第 74、80 行附近）**

把第 74 行「DeepSeek 对话模型」行的用途说明改为「**主通道**：集市自有网关 `https://api.<MARKET_DOMAIN>/v1`，模型 `deepseek-v4-flash`（2026-09-10 起主用；与 tech-digest 同网关）」；把第 80 行「平台 LLM 网关（备选）」改为「**备用通道**：DeepSeek 官方 `https://api.deepseek.com/v1`，主通道失败时自动切换」。

- [ ] **Step 2: 更新 §4.3 生成层（第 344-353 行附近）**

标题与代码示例改为双通道：示例改用 `with_fallbacks`，并保留「网关不接受 reasoning_effort=none；换 qwen 必须设 none」的实测注记。

- [ ] **Step 3: 更新 §5.2 LLM 客户端（第 486-496 行附近）**

代码块同步为 `app/llm.py` 的实际结构（`_build_llm` + `get_llm`）。

- [ ] **Step 4: 更新降级链描述（第 397 行附近）**

「DeepSeek 失败 → 返回 answer: null」改为「**双通道（网关→DeepSeek）均失败** → 返回 `answer: null` + 仅引用列表」。

- [ ] **Step 5: 更新 §7 环境变量示例（第 649 行附近）**

补 `SSE_MARKET_*` 三行（key 用 `sk-xxx` 占位，**不得写明文**）。

- [ ] **Step 6: 提交**

```bash
git add "集市分区智能检索与AI应答功能-技术实现文档.md"
git commit -m "docs(assist): 技术文档同步双通道 LLM 方案"
```

---

### Task 4: 部署到服务器并端到端验证

**Files:**
- Modify（服务器）: `/home/cloud/sse_market_assist/app/{config,llm,generate,retrieve,context,api}.py`
- Modify（服务器）: `/home/cloud/sse_market_assist/.env`
- 复用脚本: `_pod_sync.py`（pull/push/exec）、`_pod_restart.py`

**Interfaces:**
- Consumes: Task 1/2 的本地代码、Task 1 的本地 .env 中的 key
- Produces: 线上运行的双通道服务

- [ ] **Step 1: 拉取服务器现状并 diff（防覆盖热修）**

Run:
```bash
python _pod_sync.py pull
for f in config llm generate retrieve context api; do
  echo "=== $f.py ==="; diff -u _pod_check/$f.py sse_market_assist/app/$f.py | head -40
done
```
Expected: diff 只包含本次改动（config/llm）与措辞订正（generate/retrieve/context/api）。**若出现意外差异，停下来人工核对，不要 push。**

- [ ] **Step 2: 备份服务器代码**

Run: `python _pod_sync.py exec "cp -r /home/cloud/sse_market_assist/app /home/cloud/sse_market_assist/app.bak.20260910_gateway && ls -d /home/cloud/sse_market_assist/app.bak.*"`

- [ ] **Step 3: 服务器 .env 追加主通道配置**

`_pod_sync.py exec` 不易安全传 key，改用 paramiko 直写（key 从本地 .env 读，只在内存里搬运）：
```bash
./.venv_local/Scripts/python.exe -c "
import paramiko
key = [l.split('=',1)[1].strip() for l in open('sse_market_assist/.env',encoding='utf-8') if l.startswith('SSE_MARKET_API_KEY=')][0]
c = paramiko.SSHClient(); c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
c.connect('<SERVER_IP>', port=22012, username='cloud', password='<PWD>', timeout=20)
p = '/home/cloud/sse_market_assist/.env'
sftp = c.open_sftp(); f = sftp.file(p, 'r'); cur = f.read().decode('utf-8'); f.close()
lines = [l for l in cur.splitlines() if not l.startswith('SSE_MARKET_')]
out = ('# ---- 主通道：集市自有网关（2026-09-10 起）----\n'
       f'SSE_MARKET_API_KEY={key}\n'
       'SSE_MARKET_BASE_URL=https://api.<MARKET_DOMAIN>/v1\n'
       'SSE_MARKET_MODEL=deepseek-v4-flash\n') + '\n'.join(lines) + '\n'
f = sftp.file(p, 'w'); f.write(out); f.close(); sftp.close(); c.close()
print('env updated')
"
```
（PWD 见 `_pod_sync.py` 顶部常量；执行时勿回显。改动仅「删除旧的 SSE_MARKET_* 行 + 顶部插入三行」，其余行原样保留。）

验证：`python _pod_sync.py exec "grep -c '^SSE_MARKET_API_KEY=sk-' /home/cloud/sse_market_assist/.env; grep -c '^DEEPSEEK_API_KEY=' /home/cloud/sse_market_assist/.env"`
Expected: `1` 和 `1`（主备两把 key 都在，且 DEEPSEEK 未被删）

- [ ] **Step 4: 推送代码**

Run: `python _pod_sync.py push`
Expected: 逐文件 `pushed xxx.py`，无 MISSING

- [ ] **Step 5: 重启服务**

Run: `python _pod_restart.py`
Expected: `port released after N s` → `HEALTH OK: {"ok":true,...}` → FINAL 里只有一个 `main.py` 进程

- [ ] **Step 6: 端到端真实生成（走主通道）**

Run:
```bash
python _pod_sync.py exec "curl -s -m 120 -X POST http://127.0.0.1:8080/api/v1/assist/match -H 'Content-Type: application/json' -d '{\"partition\":\"生活日常\",\"title\":\"有人一起打羽毛球吗\",\"content\":\"想找人周末打球\",\"postID\":0}' | head -c 800"
```
Expected: 返回含 `answer` 的完整回答（非 null、非错误），引用格式 [n] 正常

- [ ] **Step 7: 确认日志未出现切换（主通道健康）**

Run: `python _pod_sync.py exec "grep -c '已切换到备用通道' /home/cloud/sse_market_assist/logs/service.log || true"`
Expected: 本次重启后为 `0`（有旧记录则看时间戳；出现新记录说明主通道有问题）

- [x] **Step 8: ~~故障切换实测（故障注入）~~ —— 用户决定不做（2026-09-10）**

跳过生产故障注入演练。切换机制的验证改由 Task 2 的 `test_failover_switches_and_logs`（单测级，真实走 with_fallbacks + 日志断言）承担；上线后如遇真实网关故障，日志中的 `[llm] 主通道（集市网关）失败，已切换到备用通道(DeepSeek)` 即为切换发生的信号。

- [ ] **Step 9: 清理临时探针文件**

Run: `rm -f _gw_probe.py _gw_probe_result.json _gw_rag_probe.py _gw_rag_probe_result.json`

（已被 `/_-*` 规则 gitignore，删除即可，避免遗留）

---

## 验证清单（全部完成后逐条确认）

- [ ] 本地全量测试绿
- [ ] 服务器 `/health` ok，只有一个 main.py 进程
- [ ] 真实生成请求返回完整回答，走主通道（无切换日志）
- [ ] 故障注入时自动切到 DeepSeek 备用，且有日志
- [ ] 恢复后主通道正常
- [ ] 服务器 .env 中 DEEPSEEK_* 仍保留
- [ ] 回滚方式明确：`cp -r app.bak.20260910_gateway/* app/` + 还原 .env + `python _pod_restart.py`
