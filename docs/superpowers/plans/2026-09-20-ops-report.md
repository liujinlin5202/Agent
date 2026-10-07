# 自动巡检报告子系统（ops_report）实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让集市服务器每两天 21:30 自动汇总「发帖机器人 + 发帖自动回复 AI」的运行指标与报错，附知识库/LLM 生成的处置建议，邮件发到 `abc3509429932@163.com`。

**Architecture:** 在 `tech-digest/ops_report/` 内新建一个纯只读子包，三层分离：`collect/*`（采集，每源独立降级）→ `analyze` + `knowledge` + `llm_advice`（聚类 + 处置建议）→ `render` + `mailer`（HTML 邮件 + 归档）。systemd timer 每天 21:30 触发，脚本内按 `state.json` 做 2 天守卫。不改动 tech-digest / assist 的任何现有文件。

**Tech Stack:** Python 3（标准库为主：sqlite3 / subprocess / smtplib / dataclasses）、复用 `app.llm.chat` 与 `app.marketdb._run`、systemd oneshot + timer、`sshpass`（服务器已装，走 `SSHPASS` 环境变量）。

**Spec:** `docs/superpowers/specs/2026-09-20-ops-report-design.md`

## Global Constraints

- **只读硬约束**：对 tech-digest 的 SQLite 用 `mode=ro` 打开；对集市 MySQL 只跑 SELECT；对 pod 只用 `ps/ss/tail`；不改动 tech-digest、sse_market_assist 的任何现有文件。
- **凭据**：163 授权码、pod SSH 口令只写在服务器 `tech-digest/ops_report/.env`（权限 600），**不写入仓库任何文件、不打印进日志、错误信息里不回显**。
- **零新依赖**：不新增 requirements（`requests`/`beautifulsoup4` 已有，其余全走标准库）。
- **时区**：全部按服务器本地时区（CST）计算。
- **失败不静默**：任何单源采集失败 → 该板块标 `unknown` 并注明，报告照发；邮件投递失败 → 写 `data/alerts/`，下次报告开头标红。
- **测试命令**：本地 `cd tech-digest && python -m unittest tests.<模块> -v`；服务器 `cd /root/market-deploy/agent/tech-digest && .venv/bin/python -m pytest tests/ -q`。
- **提交粒度**：每个 Task 一次 commit，commit message 用中文 `feat(ops-report): ...` 风格（与本仓库既有风格一致），不带任何归因脚注。

## File Structure

| 文件 | 职责 |
|---|---|
| `tech-digest/ops_report/__init__.py` | 子包标记，空 |
| `tech-digest/ops_report/model.py` | 三层之间的数据契约：`Collected` / `Section` / `ErrorCluster` / `Advice` / `Report` |
| `tech-digest/ops_report/config.py` | `ops_report/.env` 加载 + `OpsSettings` |
| `tech-digest/ops_report/window.py` | 报告窗口计算 + 2 天守卫 |
| `tech-digest/ops_report/state.py` | `state.json`（last_sent）+ 告警文件读写 |
| `tech-digest/ops_report/collect/__init__.py` | 采集层标记，空 |
| `tech-digest/ops_report/collect/digest.py` | 发帖机器人：run_log（只读）+ 日志文件 + 档期核对 |
| `tech-digest/ops_report/collect/assist.py` | 应答 AI：nginx 日志 + pod 探活/日志 + 集市库业务量 |
| `tech-digest/ops_report/collect/system.py` | 磁盘 / 证书 / systemd 服务状态 |
| `tech-digest/ops_report/analyze.py` | 错误聚类（签名归一化） |
| `tech-digest/ops_report/knowledge.py` | 现象 → 根因 → 处置 知识库 |
| `tech-digest/ops_report/llm_advice.py` | LLM 写分析与建议（失败走模板兜底） |
| `tech-digest/ops_report/render.py` | 主题行 + HTML 邮件 + JSON 归档 |
| `tech-digest/ops_report/mailer.py` | SMTP 465 发送 + 重试 |
| `tech-digest/ops_report/__main__.py` | 编排 + CLI（`run` / `dry-run` / `test-mail`） |
| `tech-digest/deploy/ops-report.service` + `.timer` | systemd 单元 |
| `tech-digest/tests/test_ops_report_*.py` | 各层单测 |

---

### Task 1: 数据契约与配置（model.py + config.py）

**Files:**
- Create: `tech-digest/ops_report/__init__.py`（空文件）
- Create: `tech-digest/ops_report/model.py`
- Create: `tech-digest/ops_report/config.py`
- Test: `tech-digest/tests/test_ops_report_base.py`

**Interfaces:**
- Consumes: 无
- Produces:
  - `Collected(section: Section, raw_errors: list[dict])` — 采集层返回值；`raw_errors` 元素形如 `{"ts": str, "text": str, "source": str}`
  - `Section(key: str, name: str, status: str, metrics: dict, errors: list[ErrorCluster], advice: list[Advice], notes: list[str])`
  - `ErrorCluster(signature, count, first_ts, last_ts, sample, source, expected=False)`
  - `Advice(title, action, origin)`
  - `Report(generated_at, window_start, window_end, sections, pending_alerts)`，属性 `overall` → `"ok"|"warn"|"critical"|"unknown"`
  - `OpsSettings` 数据类与 `load_env()` / `OpsSettings.from_env()`，字段见下方代码

- [ ] **Step 1: 写失败测试**

创建 `tech-digest/tests/test_ops_report_base.py`：

```python
# -*- coding: utf-8 -*-
"""ops_report 基础层单测：数据契约 + 配置加载。

契约测试的意义：collect / analyze / render 三层只通过这些字段名通信，
字段名一变三层同时坏——先钉住。
"""
import os
import tempfile
import unittest
from pathlib import Path

from ops_report import config, model


class TestModel(unittest.TestCase):
    def test_report_overall_picks_worst_section(self):
        r = model.Report(generated_at="t", window_start="a", window_end="b", sections=[
            model.Section(key="digest", name="发帖", status="ok"),
            model.Section(key="assist", name="应答", status="critical"),
            model.Section(key="system", name="系统", status="warn"),
        ])
        self.assertEqual(r.overall, "critical")

    def test_report_overall_unknown_beats_ok(self):
        r = model.Report(generated_at="t", window_start="a", window_end="b", sections=[
            model.Section(key="digest", name="发帖", status="ok"),
            model.Section(key="assist", name="应答", status="unknown"),
        ])
        self.assertEqual(r.overall, "unknown")

    def test_report_overall_empty_is_ok(self):
        r = model.Report(generated_at="t", window_start="a", window_end="b")
        self.assertEqual(r.overall, "ok")

    def test_collected_defaults(self):
        c = model.Collected(section=model.Section(key="x", name="y", status="ok"))
        self.assertEqual(c.raw_errors, [])


class TestConfig(unittest.TestCase):
    def test_dotenv_loads_without_overriding_existing(self):
        with tempfile.TemporaryDirectory() as td:
            env = Path(td) / ".env"
            env.write_text("OPS_TEST_A=from_file\nOPS_TEST_B=\"quoted\"\n"
                           "OPS_TEST_C='single'\n# 注释行\n", encoding="utf-8")
            for k in ("OPS_TEST_A", "OPS_TEST_B", "OPS_TEST_C"):
                os.environ.pop(k, None)
            os.environ["OPS_TEST_B"] = "from_env"
            try:
                config.load_env(env)
                self.assertEqual(os.environ["OPS_TEST_A"], "from_file")
                self.assertEqual(os.environ["OPS_TEST_B"], "from_env")   # 已存在不覆盖
                self.assertEqual(os.environ["OPS_TEST_C"], "single")     # 单引号被剥掉
            finally:
                for k in ("OPS_TEST_A", "OPS_TEST_B", "OPS_TEST_C"):
                    os.environ.pop(k, None)

    def test_defaults_when_no_env(self):
        s = config.OpsSettings()
        self.assertEqual(s.smtp_host, "smtp.163.com")
        self.assertEqual(s.smtp_port, 465)
        self.assertEqual(s.interval_days, 2)
        self.assertEqual(s.pod_ssh_port, 22012)
        self.assertEqual(s.market_db_name, "market")
        self.assertEqual(s.disk_crit_pct, 90)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd tech-digest && python -m unittest tests.test_ops_report_base -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'ops_report'`

- [ ] **Step 3: 实现 model.py**

创建 `tech-digest/ops_report/__init__.py`（空文件），然后 `tech-digest/ops_report/model.py`：

```python
# -*- coding: utf-8 -*-
"""ops_report 的共享数据模型：采集 → 分析 → 渲染 三层之间的契约。

为什么单独一个模块：采集器（collect/*）产出 Section，分析层（analyze/knowledge/
llm_advice）就地补充 errors/advice，渲染层（render）只读不写。三层各自可单测，
换掉任何一层都不影响其他两层——前提是这些字段名不变。
"""
from __future__ import annotations

from dataclasses import dataclass, field

_STATUS_ORDER = {"ok": 0, "unknown": 1, "warn": 2, "critical": 3}


@dataclass
class ErrorCluster:
    """归一化后的同类错误：同一条报错刷 13000 次也只占一行。"""
    signature: str          # 归一化签名（数字/ID/时间戳已换成占位符）
    count: int
    first_ts: str
    last_ts: str
    sample: str             # 原始样例（截断），便于人肉核对
    source: str             # digest_log / nginx / pod / system
    expected: bool = False  # 预期行为（周末跳过、dry-run 等），不计告警


@dataclass
class Advice:
    """一条运维建议：知识库命中或 LLM 产出。"""
    title: str
    action: str             # 处置步骤（可多行）
    origin: str             # knowledge | llm | template


@dataclass
class Section:
    """报告的一个板块（发帖机器人 / 应答 AI / 系统层）。"""
    key: str                # digest | assist | system
    name: str               # 中文板块名
    status: str             # ok | warn | critical | unknown
    metrics: dict = field(default_factory=dict)      # 有序指标（渲染按插入序展示）
    errors: list = field(default_factory=list)       # list[ErrorCluster]
    advice: list = field(default_factory=list)       # list[Advice]
    notes: list = field(default_factory=list)        # 「数据不可得」等说明


@dataclass
class Collected:
    """采集层返回：板块 + 待聚类的原始错误条目。"""
    section: Section
    raw_errors: list = field(default_factory=list)   # [{ts, text, source}]


@dataclass
class Report:
    generated_at: str
    window_start: str
    window_end: str
    sections: list = field(default_factory=list)
    pending_alerts: list = field(default_factory=list)   # 上次投递失败的告警原文

    @property
    def overall(self) -> str:
        """整体状态 = 最差板块状态（unknown 比 ok 差，warn 比 unknown 差）。"""
        worst = "ok"
        for s in self.sections:
            if _STATUS_ORDER.get(s.status, 0) > _STATUS_ORDER.get(worst, 0):
                worst = s.status
        return worst

    def section(self, key: str) -> Section | None:
        for s in self.sections:
            if s.key == key:
                return s
        return None
```

- [ ] **Step 4: 实现 config.py**

创建 `tech-digest/ops_report/config.py`：

```python
# -*- coding: utf-8 -*-
"""ops_report 配置：独立 .env（ops_report/.env），零额外依赖（与 app/config.py 同模式）。

凭据（163 授权码、pod SSH 口令）只存在这里：权限 600、不进 git、不进日志。
这里只在 from_env() 时读取环境变量，不做 import 期单例——单测需要能构造干净实例。
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent          # tech-digest/ops_report
DIGEST_DIR = BASE_DIR.parent                        # tech-digest/
ENV_PATH = BASE_DIR / ".env"


def load_env(path: Path = ENV_PATH) -> None:
    """把 .env 读入 os.environ（已存在的环境变量优先，不覆盖）。文件不存在则跳过。"""
    p = Path(path)
    if not p.exists():
        return
    with open(p, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            if key and key not in os.environ:
                os.environ[key] = value


@dataclass
class OpsSettings:
    # ---- 邮件（163 SMTP，465 SSL；授权码即密码）----
    smtp_host: str = "smtp.163.com"
    smtp_port: int = 465
    smtp_user: str = ""
    smtp_auth_code: str = ""
    mail_to: str = ""
    # ---- 报告节奏 ----
    interval_days: int = 2
    # ---- pod（应答 AI 运行处；从服务器经 127.0.0.1:22012 进）----
    pod_ssh_host: str = "127.0.0.1"
    pod_ssh_port: int = 22012
    pod_ssh_user: str = "cloud"
    pod_ssh_password: str = ""
    pod_app_dir: str = "/home/cloud/sse_market_assist"
    # ---- 采集目标 ----
    nginx_container: str = "sse_market_server-nginx_proxy-1"
    market_db_container: str = "sse_market_db"
    market_db_name: str = "market"
    cert_glob: str = "/root/market-deploy/Nginx/live/*/fullchain.pem"
    disk_warn_pct: int = 80
    disk_crit_pct: int = 90
    # ---- 本地路径 ----
    digest_db: Path = DIGEST_DIR / "data" / "tech-digest.db"
    digest_log: Path = DIGEST_DIR / "log" / "tech-digest.log"
    data_dir: Path = BASE_DIR / "data"

    @property
    def mail_from(self) -> str:
        return self.smtp_user

    @property
    def smtp_ready(self) -> bool:
        return bool(self.smtp_user and self.smtp_auth_code and self.mail_to)

    @classmethod
    def from_env(cls) -> "OpsSettings":
        load_env()
        return cls(
            smtp_host=os.environ.get("OPS_SMTP_HOST", "smtp.163.com"),
            smtp_port=int(os.environ.get("OPS_SMTP_PORT", "465")),
            smtp_user=os.environ.get("OPS_SMTP_USER", ""),
            smtp_auth_code=os.environ.get("OPS_SMTP_AUTH_CODE", ""),
            mail_to=os.environ.get("OPS_MAIL_TO", ""),
            interval_days=int(os.environ.get("OPS_INTERVAL_DAYS", "2")),
            pod_ssh_host=os.environ.get("OPS_POD_SSH_HOST", "127.0.0.1"),
            pod_ssh_port=int(os.environ.get("OPS_POD_SSH_PORT", "22012")),
            pod_ssh_user=os.environ.get("OPS_POD_SSH_USER", "cloud"),
            pod_ssh_password=os.environ.get("OPS_POD_SSH_PASSWORD", ""),
            pod_app_dir=os.environ.get("OPS_POD_APP_DIR", "/home/cloud/sse_market_assist"),
            nginx_container=os.environ.get(
                "OPS_NGINX_CONTAINER", "sse_market_server-nginx_proxy-1"),
            market_db_container=os.environ.get("OPS_MARKET_DB_CONTAINER", "sse_market_db"),
            market_db_name=os.environ.get("OPS_MARKET_DB_NAME", "market"),
            cert_glob=os.environ.get(
                "OPS_CERT_GLOB", "/root/market-deploy/Nginx/live/*/fullchain.pem"),
            disk_warn_pct=int(os.environ.get("OPS_DISK_WARN_PCT", "80")),
            disk_crit_pct=int(os.environ.get("OPS_DISK_CRIT_PCT", "90")),
        )
```

- [ ] **Step 5: 跑测试确认通过**

Run: `cd tech-digest && python -m unittest tests.test_ops_report_base -v`
Expected: PASS（6 个测试）

- [ ] **Step 6: 提交**

```bash
git add tech-digest/ops_report/__init__.py tech-digest/ops_report/model.py tech-digest/ops_report/config.py tech-digest/tests/test_ops_report_base.py
git commit -m "feat(ops-report): 数据契约与配置层（Section/Report/OpsSettings）"
```

---

### Task 2: 报告窗口与 2 天守卫（window.py + state.py）

**Files:**
- Create: `tech-digest/ops_report/window.py`
- Create: `tech-digest/ops_report/state.py`
- Test: `tech-digest/tests/test_ops_report_window.py`

**Interfaces:**
- Consumes: 无（纯函数 + 文件读写）
- Produces:
  - `window.should_send(last_sent: str | None, now: datetime, interval_days: int = 2) -> bool`
  - `window.report_window(last_sent: str | None, now: datetime, interval_days: int = 2) -> tuple[datetime, datetime]`
  - `state.State(data_dir: Path)` → `.last_sent() -> str | None`、`.mark_sent(ts: str | None = None) -> None`
  - `state.add_alert(data_dir, message, now=None) -> Path`
  - `state.pending_alerts(data_dir) -> list[str]`
  - `state.clear_alerts(data_dir) -> None`

- [ ] **Step 1: 写失败测试**

创建 `tech-digest/tests/test_ops_report_window.py`：

```python
# -*- coding: utf-8 -*-
"""窗口与守卫单测。

守卫是「每两天」这个需求的唯一实现处：日历表达式做不到跨月奇偶，
所以用「日跑 + 日期差 >= interval_days」来保证节奏，重启后也能补跑。
"""
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

from ops_report import state, window


class TestShouldSend(unittest.TestCase):
    def test_first_run_always_sends(self):
        self.assertTrue(window.should_send(None, datetime(2026, 9, 20, 21, 30)))

    def test_same_day_after_send_skips(self):
        self.assertFalse(window.should_send(
            "2026-09-20T21:30:00", datetime(2026, 9, 20, 21, 31)))

    def test_next_day_skips(self):
        self.assertFalse(window.should_send(
            "2026-09-20T21:30:00", datetime(2026, 9, 21, 21, 30)))

    def test_two_days_later_sends(self):
        self.assertTrue(window.should_send(
            "2026-09-20T21:30:00", datetime(2026, 9, 22, 21, 30)))

    def test_catches_up_after_downtime(self):
        """服务器停了一周，恢复后应立刻补发，而不是继续等。"""
        self.assertTrue(window.should_send(
            "2026-09-13T21:30:00", datetime(2026, 9, 20, 21, 30)))

    def test_custom_interval(self):
        self.assertTrue(window.should_send(
            "2026-09-20T21:30:00", datetime(2026, 9, 21, 21, 30), interval_days=1))


class TestReportWindow(unittest.TestCase):
    def test_first_run_looks_back_interval_days(self):
        start, end = window.report_window(None, datetime(2026, 9, 20, 21, 30))
        self.assertEqual(start, datetime(2026, 9, 18, 21, 30))
        self.assertEqual(end, datetime(2026, 9, 20, 21, 30))

    def test_window_starts_at_last_sent(self):
        # 起点取上次发送的整点时刻（含秒），不取回看兜底值 —— 用 21:30:05 而非 21:30:00，
        # 正是为了与「回看 interval_days」区分开，避免两种情况同值时断言失真。
        start, end = window.report_window(
            "2026-09-18T21:30:05", datetime(2026, 9, 20, 21, 30))
        self.assertEqual(start.year, 2026)
        self.assertEqual(start, datetime.fromisoformat("2026-09-18T21:30:05"))
        self.assertEqual(end, datetime(2026, 9, 20, 21, 30))

    def test_broken_last_sent_falls_back(self):
        start, end = window.report_window("不是时间", datetime(2026, 9, 20, 21, 30))
        self.assertEqual((end - start).days, 2)


class TestState(unittest.TestCase):
    def test_roundtrip(self):
        with tempfile.TemporaryDirectory() as td:
            st = state.State(Path(td))
            self.assertIsNone(st.last_sent())
            st.mark_sent("2026-09-20T21:30:00")
            self.assertEqual(state.State(Path(td)).last_sent(), "2026-09-20T21:30:00")

    def test_mark_sent_defaults_to_now(self):
        with tempfile.TemporaryDirectory() as td:
            st = state.State(Path(td))
            st.mark_sent()
            self.assertTrue(st.last_sent().startswith("20"))

    def test_corrupt_state_returns_none(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "state.json"
            p.write_text("{坏掉的", encoding="utf-8")
            self.assertIsNone(state.State(Path(td)).last_sent())

    def test_alerts_lifecycle(self):
        with tempfile.TemporaryDirectory() as td:
            d = Path(td)
            self.assertEqual(state.pending_alerts(d), [])
            state.add_alert(d, "第一次投递失败")
            state.add_alert(d, "第二次投递失败")
            self.assertEqual(state.pending_alerts(d), ["第一次投递失败", "第二次投递失败"])
            state.clear_alerts(d)
            self.assertEqual(state.pending_alerts(d), [])


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd tech-digest && python -m unittest tests.test_ops_report_window -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'ops_report.window'`

- [ ] **Step 3: 实现 window.py**

创建 `tech-digest/ops_report/window.py`：

```python
# -*- coding: utf-8 -*-
"""报告窗口与「每两天」守卫。

为什么不用 systemd 的日历表达式做「每两天」：OnCalendar 无法可靠表达跨月奇偶
（*-*-01/2 在 31 天的月份会漏或重）。用「每天 21:30 触发 + 这里判断日期差」：
节奏稳、服务器停机后可补发、改每天/每周只动 OPS_INTERVAL_DAYS 一个数。
"""
from __future__ import annotations

from datetime import date, datetime, timedelta


def should_send(last_sent: str | None, now: datetime, interval_days: int = 2) -> bool:
    """距上次发送不足 interval_days（按自然日）→ False。从未发过 → True。"""
    if not last_sent:
        return True
    try:
        last = datetime.fromisoformat(last_sent).date()
    except (ValueError, TypeError):
        return True
    return (now.date() - last).days >= interval_days


def report_window(last_sent: str | None, now: datetime,
                  interval_days: int = 2) -> tuple[datetime, datetime]:
    """窗口 = (上次发送, 现在]；首次运行（或上次时间坏了）回看 interval_days 天。"""
    fallback = now - timedelta(days=interval_days)
    if not last_sent:
        return fallback, now
    try:
        return datetime.fromisoformat(last_sent), now
    except (ValueError, TypeError):
        return fallback, now
```

- [ ] **Step 4: 实现 state.py**

创建 `tech-digest/ops_report/state.py`：

```python
# -*- coding: utf-8 -*-
"""状态与告警文件。

· state.json 记 last_sent，供 2 天守卫判断；
· alerts/*.txt 是投递失败的唯一留痕处 —— 企业运维里「静默失败」比「失败」危险得多：
  邮件发不出去时没有别的通道能喊人，所以写文件，下次报告开头标红。
"""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path


class State:
    def __init__(self, data_dir: Path):
        self.data_dir = Path(data_dir)
        self.path = self.data_dir / "state.json"

    def last_sent(self) -> str | None:
        if not self.path.exists():
            return None
        try:
            return json.loads(self.path.read_text(encoding="utf-8")).get("last_sent")
        except (ValueError, OSError, AttributeError):
            return None

    def mark_sent(self, ts: str | None = None) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        payload = {"last_sent": ts or datetime.now().isoformat(timespec="seconds")}
        self.path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def alerts_dir(data_dir: Path) -> Path:
    return Path(data_dir) / "alerts"


def add_alert(data_dir: Path, message: str, now: datetime | None = None) -> Path:
    d = alerts_dir(data_dir)
    d.mkdir(parents=True, exist_ok=True)
    # 文件名带微秒（%f）：同一秒内连写两条告警时，秒级时间戳会互相覆盖——
    # 而「失败不静默」正是这个文件存在的理由，覆盖等于把失败藏起来了。
    ts = (now or datetime.now()).strftime("%Y%m%d-%H%M%S-%f")
    path = d / f"{ts}.txt"
    path.write_text(message, encoding="utf-8")
    return path


def pending_alerts(data_dir: Path) -> list[str]:
    """未处理的告警原文（按文件名即时间序）。"""
    d = alerts_dir(data_dir)
    if not d.exists():
        return []
    return [p.read_text(encoding="utf-8") for p in sorted(d.glob("*.txt"))]


def clear_alerts(data_dir: Path) -> None:
    d = alerts_dir(data_dir)
    if not d.exists():
        return
    for p in d.glob("*.txt"):
        p.unlink()
```

- [ ] **Step 5: 跑测试确认通过**

Run: `cd tech-digest && python -m unittest tests.test_ops_report_window -v`
Expected: PASS（15 个测试）

- [ ] **Step 6: 提交**

```bash
git add tech-digest/ops_report/window.py tech-digest/ops_report/state.py tech-digest/tests/test_ops_report_window.py
git commit -m "feat(ops-report): 报告窗口计算与 2 天守卫、投递失败告警文件"
```

---

### Task 3: 发帖机器人采集（collect/digest.py）

**Files:**
- Create: `tech-digest/ops_report/collect/__init__.py`（空文件）
- Create: `tech-digest/ops_report/collect/digest.py`
- Test: `tech-digest/tests/test_ops_report_digest.py`

**Interfaces:**
- Consumes: `model.Collected` / `model.Section`（Task 1）
- Produces:
  - `read_run_log(db_path: Path, since: str) -> list[dict]`，元素 `{"ts","task","status","detail"}`
  - `expected_slots(start: date, end: date) -> list[dict]`，元素 `{"task","day"}`
  - `outcome_kind(row: dict) -> str`，取值 `published|dry_run|expected_skip|degraded|other`
  - `parse_log_errors(log_path: Path, since: datetime) -> list[dict]`，元素 `{"ts","level","text","source"}`
  - `build_section(rows, log_errors, start: date, end: date) -> Collected`
  - `collect(settings, start: datetime, end: datetime) -> Collected`（读 settings.digest_db / digest_log）

- [ ] **Step 1: 写失败测试**

创建 `tech-digest/tests/test_ops_report_digest.py`（fixture 全部照抄 2026-09-20 服务器上的真实行）：

```python
# -*- coding: utf-8 -*-
"""发帖机器人采集单测（fixture 取自 2026-09-20 服务器真实 run_log）。"""
import sqlite3
import tempfile
import unittest
from datetime import date, datetime
from pathlib import Path

from ops_report.collect import digest


def row(ts, task, status, detail):
    return {"ts": ts, "task": task, "status": status, "detail": detail}


COLLECT_OK = row("2026-09-18T09:17:42", "daily", "ok",
                 {"source_stats": {"github-trending": {"fetched": 20},
                                   "hacker-news": {"fetched": 15}}})
PUBLISHED = row("2026-09-18T09:17:42", "daily", "ok",
                {"detail": "published", "post_id": 6291, "title": "Linux"})
WEEKEND_SKIP = row("2026-09-19T09:19:54", "daily", "skipped",
                   {"detail": "weekend", "day": "2026-09-19"})
STAR_PUBLISHED = row("2026-09-19T09:26:13", "star", "ok",
                     {"detail": "published", "post_id": 6311})
DEGRADED = row("2026-09-18T00:09:18", "daily", "degraded",
               {"detail": "publish", "error": "token 校验失败"})
DRY_RUN = row("2026-09-18T00:09:18", "daily", "ok", {"detail": "dry_run_token_ok"})
DUP_SKIP = row("2026-09-17T19:31:45", "daily", "skipped", {"detail": "publish_dup"})


class TestExpectedSlots(unittest.TestCase):
    def test_workday_weekend_mapping(self):
        # 2026-09-18 周五、09-19 周六、09-20 周日
        slots = digest.expected_slots(date(2026, 9, 18), date(2026, 9, 20))
        self.assertEqual(slots, [
            {"task": "daily", "day": "2026-09-18"},
            {"task": "star", "day": "2026-09-19"},
            {"task": "weekly", "day": "2026-09-20"},
        ])

    def test_empty_when_end_before_start(self):
        self.assertEqual(digest.expected_slots(date(2026, 9, 20), date(2026, 9, 19)), [])


class TestOutcomeKind(unittest.TestCase):
    def test_published(self):
        self.assertEqual(digest.outcome_kind(PUBLISHED), "published")

    def test_expected_skips(self):
        self.assertEqual(digest.outcome_kind(WEEKEND_SKIP), "expected_skip")
        self.assertEqual(digest.outcome_kind(DUP_SKIP), "expected_skip")

    def test_degraded(self):
        self.assertEqual(digest.outcome_kind(DEGRADED), "degraded")

    def test_dry_run(self):
        self.assertEqual(digest.outcome_kind(DRY_RUN), "dry_run")


class TestBuildSection(unittest.TestCase):
    def test_success_rate_counts_only_publishing_slots(self):
        """窗口 09-18~09-20：应发 3 档（daily/star/weekly）；发了 2 次（daily+star）→ 67%。"""
        c = digest.build_section(
            [COLLECT_OK, PUBLISHED, WEEKEND_SKIP, STAR_PUBLISHED],
            [], date(2026, 9, 18), date(2026, 9, 20))
        self.assertEqual(c.section.metrics["应发档期"], "3 次")
        self.assertEqual(c.section.metrics["实际发布"], "2 次")
        self.assertEqual(c.section.metrics["发帖成功率"], "67%")
        self.assertEqual(c.section.status, "warn")

    def test_all_published_is_ok(self):
        c = digest.build_section(
            [PUBLISHED, STAR_PUBLISHED, row("2026-09-20T10:03:31", "weekly", "ok",
                                            {"detail": "published", "post_id": 6322})],
            [], date(2026, 9, 18), date(2026, 9, 20))
        self.assertEqual(c.section.metrics["发帖成功率"], "100%")
        self.assertEqual(c.section.status, "ok")

    def test_no_rows_at_all_is_critical(self):
        """整段没有任何运行记录（服务/定时器死了）→ 必须报 critical，不能报 unknown。"""
        c = digest.build_section([], [], date(2026, 9, 18), date(2026, 9, 20))
        self.assertEqual(c.section.status, "critical")
        self.assertTrue(any("没有任何运行记录" in n for n in c.section.notes))

    def test_degraded_marks_warn(self):
        c = digest.build_section([PUBLISHED, DEGRADED], [], date(2026, 9, 18),
                                 date(2026, 9, 18))
        self.assertEqual(c.section.metrics["降级"], "1 次")
        self.assertEqual(c.section.status, "warn")

    def test_expected_skip_not_counted_as_failure(self):
        """周末跳过是设计行为，不能算失败——否则每周都误报。"""
        c = digest.build_section([WEEKEND_SKIP], [], date(2026, 9, 19), date(2026, 9, 19))
        self.assertEqual(c.section.metrics["实际发布"], "0 次")
        self.assertIn("预期跳过", c.section.metrics)

    def test_source_health_listed(self):
        c = digest.build_section([COLLECT_OK, PUBLISHED], [], date(2026, 9, 18),
                                 date(2026, 9, 18))
        self.assertIn("github-trending: 20 条", c.section.metrics["数据源"])


class TestReadRunLog(unittest.TestCase):
    def test_reads_only_and_missing_file_is_empty(self):
        self.assertEqual(digest.read_run_log(Path("/nonexistent/x.db"), "2026-01-01"), [])
        with tempfile.TemporaryDirectory() as td:
            db = Path(td) / "t.db"
            conn = sqlite3.connect(str(db))
            conn.execute("CREATE TABLE run_log(id INTEGER PRIMARY KEY, ts TEXT, task TEXT,"
                         " status TEXT, detail TEXT)")
            conn.execute("INSERT INTO run_log(ts,task,status,detail) VALUES"
                         "('2026-09-18T09:17:42','daily','ok','{\"detail\": \"published\"}')")
            conn.commit()
            conn.close()
            rows = digest.read_run_log(db, "2026-09-01")
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["detail"]["detail"], "published")
            # 只读：写入必须失败
            conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
            with self.assertRaises(sqlite3.OperationalError):
                conn.execute("INSERT INTO run_log(ts,task,status,detail)"
                             " VALUES('x','x','x','{}')")
            conn.close()

    def test_filters_by_since(self):
        with tempfile.TemporaryDirectory() as td:
            db = Path(td) / "t.db"
            conn = sqlite3.connect(str(db))
            conn.execute("CREATE TABLE run_log(id INTEGER PRIMARY KEY, ts TEXT, task TEXT,"
                         " status TEXT, detail TEXT)")
            conn.execute("INSERT INTO run_log(ts,task,status,detail)"
                         " VALUES('2026-09-01T09:00:00','daily','ok','{}')")
            conn.commit()
            conn.close()
            self.assertEqual(digest.read_run_log(db, "2026-09-10"), [])


class TestParseLogErrors(unittest.TestCase):
    def test_extracts_warning_and_error_after_since(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "x.log"
            p.write_text(
                "2026-09-17 18:44:09,523 WARNING 原文抓取失败（某文）→ 降级导读: HTTP 403\n"
                "2026-09-18 09:17:42,000 INFO daily ok: issue=#21\n"
                "2026-09-19 09:26:13,823 INFO star 已发布\n"
                "2026-09-20 09:15:30,000 ERROR 发帖第 3 次失败: boom\n",
                encoding="utf-8")
            got = digest.parse_log_errors(p, datetime(2026, 9, 18, 0, 0))
            self.assertEqual(len(got), 1)
            self.assertEqual(got[0]["level"], "ERROR")
            self.assertIn("发帖第 3 次失败", got[0]["text"])

    def test_missing_file_is_empty(self):
        self.assertEqual(digest.parse_log_errors(Path("/nope/x.log"),
                                                 datetime(2026, 1, 1)), [])


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd tech-digest && python -m unittest tests.test_ops_report_digest -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'ops_report.collect'`

- [ ] **Step 3: 实现 collect/digest.py**

创建 `tech-digest/ops_report/collect/__init__.py`（空文件），然后 `tech-digest/ops_report/collect/digest.py`：

```python
# -*- coding: utf-8 -*-
"""发帖机器人采集：tech-digest 自己的 run_log（结构化）+ 日志文件。

只读硬约束：SQLite 一律 mode=ro 打开，绝不写 tech-digest 的库。
口径（2026-09-20 与服务器真实数据逐条核对过）：
  · 每档期写两行 —— 采集行（带 source_stats）、结果行（带 detail）；
  · 结果行 published=成功；skipped(weekend/not_saturday/publish_dup)=预期跳过；
    degraded=降级（计失败）；
  · 应发档期 = 窗口内「工作日 daily + 周六 star + 周日 weekly」。服务整段没跑时
    该档期在 run_log 里根本没有任何行，成功率如实下降，不做「无数据即满分」的粉饰。
"""
from __future__ import annotations

import json
import logging
import re
import sqlite3
from datetime import date, datetime, timedelta
from pathlib import Path

from ops_report.model import Collected, Section

log = logging.getLogger("tech-digest")

EXPECTED_SKIP_REASONS = {"weekend", "not_saturday", "publish_dup"}
_LOG_LINE = re.compile(
    r"^(?P<ts>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}),\d+ (?P<level>\w+) (?P<msg>.*)$")


def read_run_log(db_path: Path, since: str) -> list[dict]:
    """只读取 run_log；文件不存在 → 空列表（不建库、不抛）。"""
    db = Path(db_path)
    if not db.exists():
        return []
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            "SELECT ts, task, status, detail FROM run_log WHERE ts >= ? ORDER BY ts",
            (since,)).fetchall()
    finally:
        conn.close()
    out = []
    for r in rows:
        try:
            detail = json.loads(r["detail"] or "{}")
        except ValueError:
            detail = {}
        out.append({"ts": r["ts"], "task": r["task"],
                    "status": r["status"], "detail": detail})
    return out


def expected_slots(start: date, end: date) -> list[dict]:
    """应发档期：工作日 daily、周六 star、周日 weekly（周末 daily 属预期跳过，不入账）。"""
    slots, d = [], start
    while d <= end:
        wd = d.weekday()  # 0=周一 … 5=周六 6=周日
        task = "star" if wd == 5 else "weekly" if wd == 6 else "daily"
        slots.append({"task": task, "day": d.isoformat()})
        d += timedelta(days=1)
    return slots


def outcome_kind(row: dict) -> str:
    detail = row.get("detail") or {}
    tag = detail.get("detail")
    if row["status"] == "ok" and tag == "published":
        return "published"
    if row["status"] == "ok" and tag == "dry_run_token_ok":
        return "dry_run"
    if row["status"] == "skipped" and tag in EXPECTED_SKIP_REASONS:
        return "expected_skip"
    if row["status"] == "degraded":
        return "degraded"
    return "other"


def parse_log_errors(log_path: Path, since: datetime) -> list[dict]:
    """日志文件里 since 之后的 WARNING/ERROR 行 → [{ts, level, text, source}]。"""
    p = Path(log_path)
    if not p.exists():
        return []
    out = []
    with open(p, encoding="utf-8", errors="replace") as f:
        for line in f:
            m = _LOG_LINE.match(line.strip())
            if not m or m.group("level") not in ("WARNING", "ERROR"):
                continue
            try:
                ts = datetime.strptime(m.group("ts"), "%Y-%m-%d %H:%M:%S")
            except ValueError:
                continue
            if ts >= since:
                out.append({"ts": m.group("ts"), "level": m.group("level"),
                            "text": m.group("msg"), "source": "digest_log"})
    return out


def build_section(rows: list[dict], log_errors: list[dict],
                  start: date, end: date) -> Collected:
    outcomes = [r for r in rows if "detail" in (r.get("detail") or {})]
    collections = [r for r in rows if "source_stats" in (r.get("detail") or {})]

    published = [r for r in outcomes if outcome_kind(r) == "published"]
    degraded = [r for r in outcomes if outcome_kind(r) == "degraded"]
    expected = [r for r in outcomes if outcome_kind(r) == "expected_skip"]
    skipped_reasons = sorted({(r["detail"].get("detail") or "") for r in expected})

    slots = expected_slots(start, end)
    n_slots = len(slots)
    rate = round(len(published) / n_slots * 100) if n_slots else None

    metrics: dict = {}
    metrics["应发档期"] = f"{n_slots} 次"
    metrics["实际发布"] = f"{len(published)} 次"
    metrics["发帖成功率"] = f"{rate}%" if rate is not None else "无档期"
    if expected:
        metrics["预期跳过"] = f"{len(expected)} 次（{'、'.join(skipped_reasons)}）"
    if degraded:
        metrics["降级"] = f"{len(degraded)} 次"

    # 数据源健康：窗口内最后一次采集行的 source_stats
    if collections:
        stats = collections[-1]["detail"].get("source_stats") or {}
        parts = []
        for name, st in stats.items():
            item = f"{name}: {st.get('fetched', 0)} 条"
            if st.get("error"):
                item += f"（异常：{st['error']}）"
            parts.append(item)
        if parts:
            metrics["数据源"] = "；".join(parts)

    notes = []
    if not rows:
        notes.append(f"{start} ~ {end} 窗口内没有任何运行记录（定时器或服务可能已停）")

    if not rows:
        status = "critical"
    elif degraded or (rate is not None and rate < 100):
        status = "warn"
    else:
        status = "ok"

    section = Section(key="digest", name="自动发帖机器人", status=status,
                      metrics=metrics, notes=notes)
    return Collected(section=section, raw_errors=list(log_errors))


def collect(settings, start: datetime, end: datetime) -> Collected:
    """采一次发帖机器人：run_log + 日志文件。任一路不可得都不影响另一路。"""
    since_iso = start.isoformat(timespec="seconds")
    try:
        rows = read_run_log(settings.digest_db, since_iso)
    except sqlite3.Error as e:
        rows = []
        log.warning("run_log 读取失败: %s", e)
    errors = parse_log_errors(settings.digest_log, start)
    collected = build_section(rows, errors, start.date(), end.date())
    if not Path(settings.digest_db).exists():
        collected.section.notes.append(f"找不到 run_log 数据库：{settings.digest_db}")
    return collected
```

- [ ] **Step 4: 跑测试确认通过**

Run: `cd tech-digest && python -m unittest tests.test_ops_report_digest -v`
Expected: PASS（16 个测试）

- [ ] **Step 5: 提交**

```bash
git add tech-digest/ops_report/collect/ tech-digest/tests/test_ops_report_digest.py
git commit -m "feat(ops-report): 发帖机器人采集（只读 run_log + 档期核对 + 日志错误提取）"
```

---

### Task 4: 应答 AI 采集（collect/assist.py）

**Files:**
- Create: `tech-digest/ops_report/collect/assist.py`
- Test: `tech-digest/tests/test_ops_report_assist.py`

**Interfaces:**
- Consumes: `model.Collected` / `model.Section`（Task 1）；`app.marketdb._run`（既有只读通道）
- Produces:
  - `parse_access_lines(lines: list[str]) -> dict` → `{"total": int, "by_status": dict[int,int], "ok": int, "rate": float | None}`
  - `docker_logs(container: str, since: str, timeout: int = 120) -> list[str] | None`
  - `pod_ssh(settings, remote_cmd: str, timeout: int = 60) -> str | None`
  - `parse_probe(out: str) -> dict` → `{"proc": int, "port": int}`
  - `parse_pod_errors(text: str) -> list[dict]` → `[{"ts","text","source":"pod"}]`
  - `market_counts(since_iso: str) -> dict | None` → `{"新应答数": int, "顶": int, "踩": int}`
  - `build_section(access, probe, pod_errors, business) -> Collected`
  - `collect(settings, start: datetime, end: datetime) -> Collected`

- [ ] **Step 1: 写失败测试**

创建 `tech-digest/tests/test_ops_report_assist.py`（nginx 行与 pod 日志行取自 2026-09-20 真实抓取）：

```python
# -*- coding: utf-8 -*-
"""应答 AI 采集单测（样本行取自 2026-09-20 线上真实日志）。"""
import unittest
from datetime import datetime

from ops_report import config
from ops_report.collect import assist


NGINX_502 = ('120.239.196.134 - - [20/Sep/2026:05:48:28 +0000] '
             '"POST /api/v1/assist/match HTTP/1.1" 502 559 "-" "Mozilla/5.0"')
NGINX_421 = ('120.239.196.134 - - [20/Sep/2026:05:48:28 +0000] '
             '"POST /api/v1/assist/match HTTP/2.0" 421 575 "-" "Mozilla/5.0"')
NGINX_200 = ('1.2.3.4 - - [20/Sep/2026:05:48:28 +0000] '
             '"GET /api/v1/assist/post/6325 HTTP/1.1" 200 1234 "-" "curl"')
NGINX_OTHER = ('1.2.3.4 - - [20/Sep/2026:05:48:28 +0000] '
               '"GET /api/v1/posts/hot HTTP/1.1" 200 10 "-" "curl"')


class TestParseAccessLines(unittest.TestCase):
    def test_counts_by_status_and_computes_rate(self):
        got = assist.parse_access_lines([NGINX_502, NGINX_421, NGINX_200, NGINX_OTHER])
        self.assertEqual(got["total"], 3)          # 非 assist 请求不计
        self.assertEqual(got["by_status"], {502: 1, 421: 1, 200: 1})
        self.assertEqual(got["ok"], 1)             # 只有 2xx 算成功
        self.assertEqual(got["server_errors"], 1)  # 5xx 单独计
        self.assertAlmostEqual(got["rate"], 100 / 3, places=2)

    def test_all_failed_is_zero_rate(self):
        got = assist.parse_access_lines([NGINX_502, NGINX_502])
        self.assertEqual(got["rate"], 0.0)
        self.assertEqual(got["server_errors"], 2)

    def test_empty_is_none_rate(self):
        got = assist.parse_access_lines([])
        self.assertEqual(got["total"], 0)
        self.assertIsNone(got["rate"])


class TestParseProbe(unittest.TestCase):
    def test_parses_proc_and_port(self):
        self.assertEqual(assist.parse_probe("PROC=1\nPORT=1\nUPTIME=x\n"),
                         {"proc": 1, "port": 1})

    def test_missing_output_is_down(self):
        self.assertEqual(assist.parse_probe(""), {"proc": 0, "port": 0})
        self.assertEqual(assist.parse_probe(None), {"proc": 0, "port": 0})


class TestParsePodErrors(unittest.TestCase):
    def test_picks_error_lines_only(self):
        text = (
            "== service.log ==\n"
            "INFO:     127.0.0.1:55056 - \"GET /health/live HTTP/1.1\" 200 OK\n"
            "[api] 检索失败，降级为空结果: ConnectionError('qdrant refused')\n"
            "[llm] 主通道失败，已切换备用\n"
            "== sync.log ==\n"
            "[sync] 第 11098 周期失败（30.0s 后重试）: EmbeddingUnavailable(\"HTTPError 502\")\n")
        got = assist.parse_pod_errors(text)
        self.assertEqual(len(got), 3)
        self.assertTrue(all(e["source"] == "pod" for e in got))
        self.assertIn("EmbeddingUnavailable", got[2]["text"])


class TestBuildSection(unittest.TestCase):
    DOWN_ACCESS = {"total": 14577, "by_status": {502: 13223, 421: 1333, 499: 19, 404: 2},
                   "ok": 0, "server_errors": 13223, "rate": 0.0}

    def test_service_down_is_critical_with_evidence(self):
        c = assist.build_section(self.DOWN_ACCESS, {"proc": 0, "port": 0}, [], None,
                                 datetime(2026, 9, 18, 21, 30), datetime(2026, 9, 20, 21, 30))
        self.assertEqual(c.section.status, "critical")
        self.assertEqual(c.section.metrics["请求成功率"], "0%")
        self.assertEqual(c.section.metrics["请求总数"], "14577 次")
        self.assertEqual(c.section.metrics["进程/端口"], "未运行 / 未监听")
        self.assertIn("502", c.section.metrics["状态码分布"])

    def test_healthy_service_is_ok(self):
        access = {"total": 120, "by_status": {200: 118, 404: 2}, "ok": 118,
                  "server_errors": 0, "rate": 98.33}
        c = assist.build_section(access, {"proc": 1, "port": 1}, [], {"新应答数": 30, "顶": 4, "踩": 1},
                                 datetime(2026, 9, 18), datetime(2026, 9, 20))
        self.assertEqual(c.section.status, "ok")
        self.assertEqual(c.section.metrics["新应答数"], "30 条")
        self.assertEqual(c.section.metrics["用户反馈"], "顶 4 / 踩 1")

    def test_no_data_at_all_is_unknown(self):
        """连 nginx 都读不到（docker 不可用）且探活失败 → unknown，而不是 critical。"""
        c = assist.build_section(None, None, [], None,
                                 datetime(2026, 9, 18), datetime(2026, 9, 20))
        self.assertEqual(c.section.status, "unknown")
        self.assertTrue(any("nginx" in n for n in c.section.notes))

    def test_access_only_no_requests_is_warn(self):
        access = {"total": 0, "by_status": {}, "ok": 0, "server_errors": 0, "rate": None}
        c = assist.build_section(access, {"proc": 1, "port": 1}, [], None,
                                 datetime(2026, 9, 18), datetime(2026, 9, 20))
        self.assertEqual(c.section.status, "warn")
        self.assertEqual(c.section.metrics["请求总数"], "0 次")

    def test_partial_server_errors_is_warn(self):
        """服务活着但有 5xx → warn（不是 critical）。"""
        access = {"total": 100, "by_status": {200: 95, 502: 5}, "ok": 95,
                  "server_errors": 5, "rate": 95.0}
        c = assist.build_section(access, {"proc": 1, "port": 1}, [], None,
                                 datetime(2026, 9, 18), datetime(2026, 9, 20))
        self.assertEqual(c.section.status, "warn")


class TestPodSshGuard(unittest.TestCase):
    def test_no_password_returns_none_without_calling_ssh(self):
        """未配 pod 口令时必须直接返回 None，不能去连（否则报错刷屏）。"""
        s = config.OpsSettings(pod_ssh_password="")
        self.assertIsNone(assist.pod_ssh(s, "echo hi"))


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd tech-digest && python -m unittest tests.test_ops_report_assist -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'ops_report.collect.assist'`

- [ ] **Step 3: 实现 collect/assist.py**

创建 `tech-digest/ops_report/collect/assist.py`：

```python
# -*- coding: utf-8 -*-
"""应答 AI 采集：请求侧（nginx）+ 存活性（pod）+ 链路侧（pod 日志）+ 业务侧（集市库）。

四路里任意一路拿不到都不影响其余——服务整挂时「请求侧 + 存活性」照样出数据，
这正是本报告存在的意义（2026-09-20 实测：48h 内 14577 次请求 0 次成功）。
全部只读：docker logs / ssh 只跑 ps、ss、tail；MySQL 只 SELECT。
"""
from __future__ import annotations

import logging
import os
import re
import subprocess
from datetime import datetime

from ops_report.model import Collected, Section

log = logging.getLogger("tech-digest")

ACCESS_RE = re.compile(r'"(?:GET|POST) (?P<path>/api/v1/assist/[^ "\s]*)[^"]*" (?P<status>\d{3})')
_POD_ERR_MARKERS = ("失败", "异常", "错误", "ERROR", "Traceback", "Timeout", "refused")
_POD_ERR_SKIP = ("INFO:", "[llm] 主通道（market-deploy）失败，尝试备用通道")  # 预期内的降级提示

PROBE_CMD = ("echo PROC=$(pgrep -fc '[m]ain\\.py' 2>/dev/null || echo 0); "
             "echo PORT=$(ss -ltn 2>/dev/null | grep -c ':8080' || echo 0)")
POD_LOGS_CMD = ("for f in service.log assist.log sync.log; do "
                "p=\"$HOME/sse_market_assist/logs/$f\"; "
                "[ -f \"$p\" ] && echo \"== $f ==\" && tail -n 300 \"$p\"; done")


def parse_access_lines(lines: list[str]) -> dict:
    """nginx 访问日志行 → assist 请求的成败分布。

    口径：成功 = 2xx（用户真的拿到了回答）；5xx = 服务端故障。两者分开统计——
    421/499 既不是成功也不是服务故障，混进任何一边都会让数字失真
    （2026-09-20 实测：13223×502 + 1333×421 + 19×499 + 2×404 → 2xx 为 0，即 0 次成功）。
    """
    by_status: dict = {}
    for line in lines or []:
        m = ACCESS_RE.search(line)
        if not m:
            continue
        st = int(m.group("status"))
        by_status[st] = by_status.get(st, 0) + 1
    total = sum(by_status.values())
    ok = sum(c for s, c in by_status.items() if 200 <= s < 300)
    server_errors = sum(c for s, c in by_status.items() if s >= 500)
    rate = round(ok / total * 100, 2) if total else None
    return {"total": total, "by_status": by_status, "ok": ok,
            "server_errors": server_errors, "rate": rate}


def docker_logs(container: str, since: str, timeout: int = 120) -> list[str] | None:
    """docker logs --since（访问日志在 stdout、error_log 在 stderr，两路都收）。"""
    try:
        p = subprocess.run(["docker", "logs", container, "--since", since],
                           capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.SubprocessError):
        return None
    if p.returncode != 0:
        return None
    return (p.stdout + "\n" + p.stderr).splitlines()


def pod_ssh(settings, remote_cmd: str, timeout: int = 60) -> str | None:
    """经 sshpass 进 pod。口令走 SSHPASS 环境变量（不出现在 ps 命令行里）。"""
    if not settings.pod_ssh_password:
        return None
    env = dict(os.environ, SSHPASS=settings.pod_ssh_password)
    cmd = ["sshpass", "-e", "ssh",
           "-o", "StrictHostKeyChecking=no", "-o", "ConnectTimeout=8",
           "-o", "BatchMode=no", "-p", str(settings.pod_ssh_port),
           f"{settings.pod_ssh_user}@{settings.pod_ssh_host}", remote_cmd]
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, env=env)
    except (OSError, subprocess.SubprocessError):
        return None
    if p.returncode != 0:
        return None
    return p.stdout


def parse_probe(out: str | None) -> dict:
    """PROC=/PORT= → {"proc": int, "port": int}；无输出视为未运行。"""
    d = {}
    for line in (out or "").splitlines():
        if "=" in line:
            k, _, v = line.partition("=")
            d[k.strip()] = v.strip()
    def _int(key: str) -> int:
        try:
            return int(d.get(key, "0") or 0)
        except ValueError:
            return 0
    return {"proc": _int("PROC"), "port": _int("PORT")}


def parse_pod_errors(text: str | None) -> list[dict]:
    """pod 日志里的报错行 → 原始错误条目（真正的聚类在 analyze.py）。"""
    out = []
    for line in (text or "").splitlines():
        s = line.strip()
        if not s or s.startswith("=="):
            continue
        if any(skip in s for skip in _POD_ERR_SKIP):
            continue
        if any(marker in s for marker in _POD_ERR_MARKERS):
            out.append({"ts": datetime.now().isoformat(timespec="seconds"),
                        "text": s[:400], "source": "pod"})
    return out


def market_counts(since_iso: str) -> dict | None:
    """集市库 assist_responses 的窗口内业务量（只读）。docker 不可用 → None。

    复用 app.marketdb._run：那是本项目既有的「docker exec + 只读 SELECT」通道，
    口径与 content_report.py 的浏览采样完全一致；改它会影响线上追踪脚本，所以不加包装。
    """
    from app import marketdb
    sql = ("SELECT COUNT(*), COALESCE(SUM(feedback_upvotes),0), "
           "COALESCE(SUM(feedback_downvotes),0) FROM assist_responses "
           f"WHERE created_at >= '{since_iso}'")
    try:
        rows = marketdb._run(sql)
    except Exception as e:  # noqa: BLE001 — 采集失败绝不能让报告挂掉
        log.warning("集市库应答数查询失败（按不可得降级）: %s", e)
        return None
    if not rows or not rows[0] or len(rows[0]) < 3:
        return None
    try:
        return {"新应答数": int(rows[0][0]), "顶": int(rows[0][1]), "踩": int(rows[0][2])}
    except (ValueError, IndexError):
        return None


def build_section(access: dict | None, probe: dict | None,
                  pod_errors: list[dict], business: dict | None,
                  start: datetime, end: datetime) -> Collected:
    section = Section(key="assist", name="自动回复 AI（应答服务）", status="unknown")
    metrics = {}

    if access is not None:
        metrics["请求总数"] = f"{access['total']} 次"
        if access["rate"] is not None:
            metrics["请求成功率"] = f"{access['rate']:g}%"
        else:
            metrics["请求成功率"] = "窗口内无请求"
        if access["by_status"]:
            dist = "、".join(f"{s}×{c}" for s, c in sorted(access["by_status"].items()))
            metrics["状态码分布"] = dist
    else:
        section.notes.append("nginx 访问日志不可得（docker logs 读取失败）")

    if probe is not None:
        metrics["进程/端口"] = ("运行中 / 已监听" if probe["proc"] and probe["port"]
                              else f"未运行 / {'已监听' if probe['port'] else '未监听'}")
    else:
        section.notes.append("pod 探活不可得（SSH 未配置或不可达）")

    if business is not None:
        metrics["新应答数"] = f"{business['新应答数']} 条"
        metrics["用户反馈"] = f"顶 {business['顶']} / 踩 {business['踩']}"
    else:
        section.notes.append("集市库应答数不可得")

    section.metrics = metrics

    # ---- 状态判定 ----
    down = probe is not None and (probe["proc"] == 0 or probe["port"] == 0)
    if down:
        section.status = "critical"
    elif access is None and probe is None:
        section.status = "unknown"
    elif access is not None and access["total"] > 0 and access["ok"] == 0:
        section.status = "critical"
    elif access is not None and access["total"] == 0:
        section.status = "warn"
    elif access is not None and (access["server_errors"] > 0
                                 or (access["rate"] or 0) < 95):
        section.status = "warn"
    elif pod_errors:
        section.status = "warn"
    else:
        section.status = "ok"

    if down:
        section.notes.append("pod 上应答进程不在：所有 /api/v1/assist/ 请求都会 502")
    if access is not None and access["total"] == 0 and probe and probe["proc"]:
        section.notes.append("服务在跑但窗口内没有任何请求：检查 nginx 路由是否仍指向 13012")

    return Collected(section=section, raw_errors=list(pod_errors))


def collect(settings, start: datetime, end: datetime) -> Collected:
    """四路采集：任一失败返回 None 交 build_section 降级，绝不抛。"""
    since_iso = start.isoformat(timespec="seconds")
    access = None
    lines = docker_logs(settings.nginx_container, since_iso)
    if lines is not None:
        access = parse_access_lines(lines)

    probe = None
    out = pod_ssh(settings, PROBE_CMD)
    if out is not None:
        probe = parse_probe(out)

    pod_errors = []
    if out is not None:
        pod_errors = parse_pod_errors(pod_ssh(settings, POD_LOGS_CMD, timeout=90))

    business = market_counts(since_iso)
    return build_section(access, probe, pod_errors, business, start, end)
```

- [ ] **Step 4: 跑测试确认通过**

Run: `cd tech-digest && python -m unittest tests.test_ops_report_assist -v`
Expected: PASS（12 个测试）

- [ ] **Step 5: 提交**

```bash
git add tech-digest/ops_report/collect/assist.py tech-digest/tests/test_ops_report_assist.py
git commit -m "feat(ops-report): 应答 AI 采集（nginx 请求成功率 + pod 探活/日志 + 集市库业务量）"
```

---

### Task 5: 系统层采集（collect/system.py）

**Files:**
- Create: `tech-digest/ops_report/collect/system.py`
- Test: `tech-digest/tests/test_ops_report_system.py`

**Interfaces:**
- Consumes: `model.Collected` / `model.Section`（Task 1）
- Produces:
  - `parse_df(out: str) -> dict | None` → `{"pct": int, "used": str, "size": str}`
  - `parse_enddate(line: str) -> datetime | None`（带 UTC 时区）
  - `scan_certs(pattern: str, now: datetime, runner=subprocess.run) -> dict | None` → `{"domain","expires","days_left"}`
  - `disk_usage(runner=subprocess.run) -> dict | None`
  - `_systemctl_show(unit: str, prop: str, runner=subprocess.run) -> str | None`
  - `timer_status(units=None, runner=subprocess.run) -> dict | None` → `{"tech-digest-daily.timer": {"state": "active", "last": "..."}, ...}`
  - `build_section(disk, cert, warn_pct: int, crit_pct: int, cert_warn_days: int = 14, timers=None) -> Collected`（raw_errors 恒为空，信号走 `notes`/`metrics`）
  - `collect(settings) -> Collected`

- [ ] **Step 1: 写失败测试**

创建 `tech-digest/tests/test_ops_report_system.py`：

```python
# -*- coding: utf-8 -*-
"""系统层采集单测：磁盘、证书、阈值判定。"""
import unittest
from datetime import datetime, timezone

from ops_report.collect import system


DF_OUT = ("Filesystem     1024-blocks      Used Available Capacity Mounted on\n"
          "/dev/vda1        221247488 175859712  34119680      82% /\n")


class TestParseDf(unittest.TestCase):
    def test_parses_capacity(self):
        self.assertEqual(system.parse_df(DF_OUT),
                         {"pct": 82, "used": "175859712", "size": "221247488"})

    def test_garbage_is_none(self):
        self.assertIsNone(system.parse_df(""))
        self.assertIsNone(system.parse_df("only one line"))


class TestParseEnddate(unittest.TestCase):
    def test_parses_gmt(self):
        dt = system.parse_enddate("notAfter=Nov 22 02:29:45 2026 GMT")
        self.assertEqual(dt.year, 2026)
        self.assertEqual(dt.month, 11)
        self.assertEqual(dt.tzinfo, timezone.utc)

    def test_garbage_is_none(self):
        self.assertIsNone(system.parse_enddate("nonsense"))


class TestBuildSection(unittest.TestCase):
    def test_disk_below_warn_is_ok(self):
        c = system.build_section({"pct": 50, "used": "1G", "size": "2G"}, None, 80, 90)
        self.assertEqual(c.section.status, "ok")
        self.assertEqual(c.section.metrics["磁盘使用率"], "50%")

    def test_disk_above_warn_is_warn(self):
        c = system.build_section({"pct": 82, "used": "168G", "size": "216G"}, None, 80, 90)
        self.assertEqual(c.section.status, "warn")

    def test_disk_above_crit_is_critical(self):
        c = system.build_section({"pct": 95, "used": "a", "size": "b"}, None, 80, 90)
        self.assertEqual(c.section.status, "critical")
        self.assertTrue(any("磁盘" in n for n in c.section.notes))

    def test_cert_near_expiry_warns(self):
        cert = {"domain": "<MARKET_DOMAIN>", "expires": "2026-09-25T00:00:00+00:00", "days_left": 5}
        c = system.build_section({"pct": 10, "used": "a", "size": "b"}, cert, 80, 90)
        self.assertEqual(c.section.status, "warn")
        self.assertEqual(c.section.metrics["最近到期证书"], "<MARKET_DOMAIN>（剩 5 天）")

    def test_both_unavailable_is_unknown(self):
        c = system.build_section(None, None, 80, 90)
        self.assertEqual(c.section.status, "unknown")

    def test_inactive_timer_is_critical(self):
        """定时器没激活 = 那个任务根本不会跑，属于 critical，不是「提醒一下」。"""
        timers = {"tech-digest-daily.timer": {"state": "inactive", "last": "未触发"},
                  "tech-digest-star.timer": {"state": "active", "last": "2026-09-19 09:25:55"}}
        c = system.build_section({"pct": 10, "used": "a", "size": "b"}, None, 80, 90,
                                 timers=timers)
        self.assertEqual(c.section.status, "critical")
        self.assertTrue(any("tech-digest-daily" in n for n in c.section.notes))

    def test_all_timers_active_is_ok(self):
        timers = {"tech-digest-daily.timer": {"state": "active", "last": "2026-09-20 09:15:29"}}
        c = system.build_section({"pct": 10, "used": "a", "size": "b"}, None, 80, 90,
                                 timers=timers)
        self.assertEqual(c.section.status, "ok")
        self.assertIn("定时器", c.section.metrics)


class TestSystemctlShow(unittest.TestCase):
    class P:
        def __init__(self, out, rc=0):
            self.stdout = out
            self.returncode = rc

    def test_returns_value(self):
        got = system._systemctl_show("x.timer", "ActiveState",
                                     runner=lambda *a, **k: self.P("active\n"))
        self.assertEqual(got, "active")

    def test_nonzero_rc_is_none(self):
        got = system._systemctl_show("x.timer", "ActiveState",
                                     runner=lambda *a, **k: self.P("", rc=1))
        self.assertIsNone(got)

    def test_missing_systemctl_is_none(self):
        def boom(*a, **k):
            raise FileNotFoundError("systemctl not found")

        self.assertIsNone(system._systemctl_show("x.timer", "ActiveState", runner=boom))


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd tech-digest && python -m unittest tests.test_ops_report_system -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'ops_report.collect.system'`

- [ ] **Step 3: 实现 collect/system.py**

创建 `tech-digest/ops_report/collect/system.py`：

```python
# -*- coding: utf-8 -*-
"""系统层采集：磁盘水位、证书到期。

磁盘这两项不是凑数：2026-08-30 磁盘满 97% 整机挂死过一次，阈值必须进定期报告。
证书路径实测在 /root/market-deploy/Nginx/live/*/fullchain.pem（certbot 容器挂载目录），
不是 /etc/letsencrypt —— 按 glob 扫，取最早到期的一张。
"""
from __future__ import annotations

import glob
import logging
import subprocess
from datetime import datetime, timezone

from ops_report.model import Collected, Section

log = logging.getLogger("tech-digest")

CERT_WARN_DAYS = 14
# 只盯发帖机器人的三个 timer：报告的 timer 若挂掉，症状是「收不到邮件」本身即可见，
# 把它列进来反而会在首次部署（timer 还没装）时误报 critical。
TIMER_UNITS = ["tech-digest-daily.timer", "tech-digest-star.timer",
               "tech-digest-weekly.timer"]


def parse_df(out: str) -> dict | None:
    """df -P 输出 → {"pct": 82, "used": "165G", "size": "216G"}。"""
    lines = [l for l in (out or "").splitlines() if l.strip()]
    if len(lines) < 2:
        return None
    parts = lines[1].split()
    if len(parts) < 6:
        return None
    try:
        pct = int(parts[4].rstrip("%"))
    except ValueError:
        return None
    return {"pct": pct, "used": parts[2], "size": parts[1]}


def parse_enddate(line: str) -> datetime | None:
    """openssl 的 'notAfter=Nov 22 02:29:45 2026 GMT' → 带时区 datetime。"""
    if "=" not in (line or ""):
        return None
    raw = line.split("=", 1)[1].strip()
    try:
        return datetime.strptime(raw, "%b %d %H:%M:%S %Y %Z").replace(
            tzinfo=timezone.utc)
    except ValueError:
        return None


def disk_usage(path: str = "/", runner=subprocess.run) -> dict | None:
    try:
        p = runner(["df", "-P", path], capture_output=True, text=True, timeout=15)
    except (OSError, subprocess.SubprocessError):
        return None
    if getattr(p, "returncode", 1) != 0:
        return None
    return parse_df(p.stdout)


def scan_certs(pattern: str, now: datetime, runner=subprocess.run) -> dict | None:
    """glob 出证书文件，取最早到期的一张。openssl 不可用/无证书 → None。"""
    best = None
    for path in sorted(glob.glob(pattern)):
        try:
            p = runner(["openssl", "x509", "-enddate", "-noout", "-in", path],
                       capture_output=True, text=True, timeout=15)
        except (OSError, subprocess.SubprocessError):
            continue
        dt = parse_enddate(getattr(p, "stdout", "") or "")
        if dt and (best is None or dt < best[1]):
            best = (path, dt)
    if not best:
        return None
    days = (best[1] - now).days
    domain = best[0].rstrip("/").split("/")[-2] if "/" in best[0] else best[0]
    return {"domain": domain, "expires": best[1].isoformat(), "days_left": days}


def _systemctl_show(unit: str, prop: str, runner=subprocess.run) -> str | None:
    """systemctl show -p <prop> --value；不可用/单元不存在 → None。"""
    try:
        p = runner(["systemctl", "show", unit, "-p", prop, "--value"],
                   capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return None
    if getattr(p, "returncode", 1) != 0:
        return None
    return (getattr(p, "stdout", "") or "").strip()


def timer_status(units: list[str] | None = None, runner=subprocess.run) -> dict | None:
    """各 timer 的 ActiveState 与上次触发时间。一条都拿不到 → None（降级）。"""
    out = {}
    for unit in (units or TIMER_UNITS):
        state = _systemctl_show(unit, "ActiveState", runner)
        if state is None:
            continue
        last = _systemctl_show(unit, "LastTriggerUSec", runner) or ""
        out[unit] = {"state": state,
                     "last": "未触发" if (not last or last == "n/a") else last[:24]}
    return out or None


def build_section(disk: dict | None, cert: dict | None,
                  warn_pct: int, crit_pct: int,
                  cert_warn_days: int = CERT_WARN_DAYS,
                  timers: dict | None = None) -> Collected:
    section = Section(key="system", name="系统层", status="ok")
    metrics, notes = {}, []

    if disk is not None:
        metrics["磁盘使用率"] = f"{disk['pct']}%"
        metrics["磁盘用量"] = f"{disk['used']} / {disk['size']}"
    else:
        notes.append("磁盘信息不可得")

    if cert is not None:
        metrics["最近到期证书"] = f"{cert['domain']}（剩 {cert['days_left']} 天）"
    else:
        notes.append("证书信息不可得（glob 无匹配或 openssl 不可用）")

    if timers:
        metrics["定时器"] = "；".join(
            f"{u.replace('.timer', '')}: {v['state']}" for u, v in timers.items())

    status = "ok"
    if disk is None and cert is None:
        status = "unknown"
    if disk is not None:
        if disk["pct"] >= crit_pct:
            status = "critical"
            notes.append(f"磁盘使用率 {disk['pct']}% 已达告警线 {crit_pct}%："
                         "参考 2026-08-30 磁盘满导致整机挂死，立即清理")
        elif disk["pct"] >= warn_pct and status == "ok":
            status = "warn"
            notes.append(f"磁盘使用率 {disk['pct']}% 超过提醒线 {warn_pct}%")
    if cert is not None and cert["days_left"] <= cert_warn_days and status in ("ok", "warn"):
        status = "warn"
        notes.append(f"证书 {cert['domain']} 剩 {cert['days_left']} 天到期（certbot 自动续期需确认）")
    if timers:
        dead = [u for u, v in timers.items() if v["state"] != "active"]
        if dead:
            status = "critical" if status == "ok" else status
            notes.append("以下定时器未激活：" + "、".join(dead)
                         + "（systemctl list-timers 确认，enable --now 拉起）")

    section.status = status
    section.metrics = metrics
    section.notes = notes
    return Collected(section=section, raw_errors=[])


def collect(settings) -> Collected:
    now = datetime.now(timezone.utc)
    disk = disk_usage()
    cert = scan_certs(settings.cert_glob, now)
    timers = timer_status()
    return build_section(disk, cert, settings.disk_warn_pct, settings.disk_crit_pct,
                         timers=timers)
```

- [ ] **Step 4: 跑测试确认通过**

Run: `cd tech-digest && python -m unittest tests.test_ops_report_system -v`
Expected: PASS（15 个测试）

- [ ] **Step 5: 提交**

```bash
git add tech-digest/ops_report/collect/system.py tech-digest/tests/test_ops_report_system.py
git commit -m "feat(ops-report): 系统层采集（磁盘水位、证书到期）"
```

---

### Task 6: 错误聚类（analyze.py）

**Files:**
- Create: `tech-digest/ops_report/analyze.py`
- Test: `tech-digest/tests/test_ops_report_analyze.py`

**Interfaces:**
- Consumes: `model.ErrorCluster`（Task 1）
- Produces:
  - `signature(text: str) -> str`
  - `cluster(entries: list[dict]) -> list[ErrorCluster]`（按次数降序；同签名合并，保留最早/最晚时间与一条样例）
  - `annotate(section, clusters) -> None`（把聚类结果写回 `section.errors`，标记 `expected`）

- [ ] **Step 1: 写失败测试**

创建 `tech-digest/tests/test_ops_report_analyze.py`：

```python
# -*- coding: utf-8 -*-
"""错误聚类单测：13000 条同类报错必须聚成一行。"""
import unittest

from ops_report import analyze, model


class TestSignature(unittest.TestCase):
    def test_numbers_and_ids_become_placeholders(self):
        a = analyze.signature("发帖第 3 次失败: postID=6311 boom")
        b = analyze.signature("发帖第 5 次失败: postID=6322 boom")
        self.assertEqual(a, b)

    def test_timestamps_removed(self):
        a = analyze.signature("2026-09-20 09:15:30,123 ERROR x")
        b = analyze.signature("2026-09-21 10:16:31,999 ERROR x")
        self.assertEqual(a, b)

    def test_long_hex_removed(self):
        a = analyze.signature("trace 550cf88bfbb9 failed")
        b = analyze.signature("trace 6126631a9236 failed")
        self.assertEqual(a, b)

    def test_different_errors_stay_different(self):
        self.assertNotEqual(analyze.signature("embedding 502"),
                            analyze.signature("qdrant refused"))


class TestCluster(unittest.TestCase):
    def test_merges_same_signature_and_counts(self):
        entries = [
            {"ts": "2026-09-20T05:00:00", "text": "postID=1 failed", "source": "pod"},
            {"ts": "2026-09-20T06:00:00", "text": "postID=2 failed", "source": "pod"},
            {"ts": "2026-09-20T07:00:00", "text": "postID=3 failed", "source": "pod"},
        ]
        got = analyze.cluster(entries)
        self.assertEqual(len(got), 1)
        self.assertEqual(got[0].count, 3)
        self.assertEqual(got[0].first_ts, "2026-09-20T05:00:00")
        self.assertEqual(got[0].last_ts, "2026-09-20T07:00:00")
        self.assertIn("postID=1", got[0].sample)

    def test_sorted_by_count_desc(self):
        entries = (
            [{"ts": "t", "text": "b error", "source": "pod"}] * 2 +
            [{"ts": "t", "text": "a error", "source": "nginx"}] * 5)
        got = analyze.cluster(entries)
        self.assertEqual([c.count for c in got], [5, 2])

    def test_empty(self):
        self.assertEqual(analyze.cluster([]), [])


class TestAnnotate(unittest.TestCase):
    def test_writes_clusters_into_section(self):
        sec = model.Section(key="digest", name="发帖", status="warn")
        analyze.annotate(sec, [{"ts": "t", "text": "weekend skip", "source": "digest_log"}])
        self.assertEqual(len(sec.errors), 1)
        self.assertIsInstance(sec.errors[0], model.ErrorCluster)

    def test_expected_marker_from_text(self):
        sec = model.Section(key="digest", name="发帖", status="ok")
        analyze.annotate(sec, [
            {"ts": "t", "text": "周末只抓取入库、不发帖（数据仍进周报池）", "source": "digest_log"},
            {"ts": "t", "text": "发帖第 3 次失败: token 过期", "source": "digest_log"},
        ])
        flags = sorted(e.expected for e in sec.errors)
        self.assertEqual(flags, [False, True])


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd tech-digest && python -m unittest tests.test_ops_report_analyze -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'ops_report.analyze'`

- [ ] **Step 3: 实现 analyze.py**

创建 `tech-digest/ops_report/analyze.py`：

```python
# -*- coding: utf-8 -*-
"""分析层：错误聚类。

聚类的意义：同一类报错在窗口里刷 13223 次也只占一行——人要看的是「有几类问题」，
不是「刷了多少屏」。签名归一化把数字/ID/时间戳替换成占位符，同类自然合并。
"""
from __future__ import annotations

import re

from ops_report.model import ErrorCluster

_TS = re.compile(r"\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2}(?:[,.]\d+)?")
_HEX = re.compile(r"\b[0-9a-fA-F]{12,}\b")
_NUM = re.compile(r"\d+")
_WS = re.compile(r"\s+")

# 预期内的行为：命中即标 expected=True，不进告警口径
EXPECTED_MARKERS = ("周末只抓取入库", "跳过发帖", "dry-run", "dry_run", "publish_dup")


def signature(text: str) -> str:
    s = _TS.sub("<ts>", text or "")
    s = _HEX.sub("<hex>", s)
    s = _NUM.sub("<n>", s)
    return _WS.sub(" ", s).strip()[:220]


def is_expected(text: str) -> bool:
    low = (text or "").lower()
    return any(m.lower() in low for m in EXPECTED_MARKERS)


def cluster(entries: list[dict]) -> list[ErrorCluster]:
    """[{ts, text, source}] → 按签名聚合，次数降序（同数按首次时间）。"""
    agg: dict = {}
    for e in entries or []:
        text = e.get("text") or ""
        sig = signature(text)
        item = agg.get(sig)
        if item is None:
            agg[sig] = {"count": 1, "first_ts": e.get("ts", ""), "last_ts": e.get("ts", ""),
                        "sample": text[:300], "source": e.get("source", ""),
                        "expected": is_expected(text)}
        else:
            item["count"] += 1
            item["last_ts"] = e.get("ts", item["last_ts"])
    out = [ErrorCluster(signature=sig, **data) for sig, data in agg.items()]
    out.sort(key=lambda c: (-c.count, c.first_ts))
    return out


def annotate(section, entries: list[dict]) -> None:
    """把原始错误条目聚类后写入 section.errors。"""
    section.errors = cluster(entries)


def actionable(section) -> list[ErrorCluster]:
    """剔除预期行为的错误（报告正文只列真正要看的）。"""
    return [c for c in section.errors if not c.expected]
```

- [ ] **Step 4: 跑测试确认通过**

Run: `cd tech-digest && python -m unittest tests.test_ops_report_analyze -v`
Expected: PASS（9 个测试）

- [ ] **Step 5: 提交**

```bash
git add tech-digest/ops_report/analyze.py tech-digest/tests/test_ops_report_analyze.py
git commit -m "feat(ops-report): 错误聚类（签名归一化 + 预期行为标记）"
```

---

### Task 7: 运维知识库（knowledge.py）

**Files:**
- Create: `tech-digest/ops_report/knowledge.py`
- Test: `tech-digest/tests/test_ops_report_knowledge.py`

**Interfaces:**
- Consumes: `model.Advice`
- Produces:
  - `Rule(id, contains: list[str], title: str, cause: str, action: str)` 数据类（`expected` 标记不在 Rule 上——预期行为由 analyze.EXPECTED_MARKERS 按文本判定）
  - `RULES: list[Rule]`
  - `match(signals: list[str]) -> list[Advice]`（`contains` 全部命中才算；去重保序）
  - `signals_from(sections: list) -> list[str]`（把各板块的错误签名与指标提示汇成信号串）

- [ ] **Step 1: 写失败测试**

创建 `tech-digest/tests/test_ops_report_knowledge.py`：

```python
# -*- coding: utf-8 -*-
"""知识库单测：每条规则的命中/不命中都要有断言——知识库错配会给出错处置。"""
import unittest

from ops_report import knowledge


POD_DOWN_SIG = ("upstream prematurely closed connection while reading response header "
                "from upstream, client: <n>.<n>.<n>.<n>")
DISK_SIG = "磁盘使用率 92% 已达告警线 90%：参考 <ts> 磁盘满导致整机挂死，立即清理"
EMBED_SIG = "[embed] HTTP <n> (<n>次): <html> <head><title><n> Bad Gateway</title>"
TOKEN_SIG = "发帖第 <n> 次失败: refresh_token 已过期 <n>"


class TestMatch(unittest.TestCase):
    def test_pod_down_matched(self):
        got = knowledge.match([POD_DOWN_SIG])
        self.assertTrue(any(a.title.startswith("应答服务进程不在") for a in got))

    def test_disk_matched(self):
        got = knowledge.match([DISK_SIG])
        self.assertTrue(any("磁盘" in a.title for a in got))

    def test_embedding_matched(self):
        got = knowledge.match([EMBED_SIG])
        self.assertTrue(any("向量" in a.title or "embedding" in a.title.lower() for a in got))

    def test_token_matched(self):
        got = knowledge.match([TOKEN_SIG])
        self.assertTrue(any("token" in a.title.lower() or "凭据" in a.title for a in got))

    def test_unrelated_signal_matches_nothing(self):
        self.assertEqual(knowledge.match(["完全无关的一行日志"]), [])

    def test_partial_match_does_not_fire(self):
        """规则要求多关键词同时命中：只命中一半不能给处置建议（错配=给错处置）。"""
        half = knowledge.match(["磁盘使用率 95%"])              # 缺「已达告警线」
        self.assertEqual([a for a in half if "磁盘" in a.title], [])
        full = knowledge.match(["磁盘使用率 95% 已达告警线 90%"])   # 两词齐了才命中
        self.assertTrue(any("磁盘" in a.title for a in full))

    def test_duplicate_signals_yield_one_advice(self):
        got = knowledge.match([POD_DOWN_SIG, POD_DOWN_SIG])
        self.assertEqual(len([a for a in got if a.title.startswith("应答服务进程不在")]), 1)

    def test_every_advice_has_nonempty_action(self):
        for rule in knowledge.RULES:
            self.assertTrue(rule.action.strip(), f"规则 {rule.id} 缺处置步骤")
            self.assertTrue(rule.title.strip(), f"规则 {rule.id} 缺标题")


class TestSignalsFrom(unittest.TestCase):
    def test_collects_error_signatures_and_notes(self):
        from ops_report import model
        sec = model.Section(key="system", name="系统层", status="warn",
                            metrics={"磁盘使用率": "92%"},
                            notes=["磁盘使用率 92% 已达告警线 90%：立即清理"])
        sec.errors = [model.ErrorCluster(signature=POD_DOWN_SIG, count=1, first_ts="t",
                                         last_ts="t", sample="s", source="nginx")]
        sigs = knowledge.signals_from([sec])
        self.assertIn(POD_DOWN_SIG, sigs)
        self.assertTrue(any("磁盘使用率" in s for s in sigs))


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd tech-digest && python -m unittest tests.test_ops_report_knowledge -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'ops_report.knowledge'`

- [ ] **Step 3: 实现 knowledge.py**

创建 `tech-digest/ops_report/knowledge.py`：

```python
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

    匹配是**单信号内**全关键词：跨信号拼串会让「发帖成功率 100%」的「发帖」
    和另一条报错里的「失败」凑成 publish_failed——错误建议高频出现，
    教会用户忽略建议层（终审发现，2026-09-22 修复）。
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
```

- [ ] **Step 4: 跑测试确认通过**

Run: `cd tech-digest && python -m unittest tests.test_ops_report_knowledge -v`
Expected: PASS（9 个测试）

- [ ] **Step 5: 提交**

```bash
git add tech-digest/ops_report/knowledge.py tech-digest/tests/test_ops_report_knowledge.py
git commit -m "feat(ops-report): 运维知识库（现象→根因→处置，首批来自真实事故）"
```

---

### Task 8: LLM 分析与建议（llm_advice.py）

**Files:**
- Create: `tech-digest/ops_report/llm_advice.py`
- Test: `tech-digest/tests/test_ops_report_llm_advice.py`

**Interfaces:**
- Consumes: `model.Report`、`app.llm.chat(prompt, system, max_tokens, timeout) -> str | None`
- Produces:
  - `build_prompt(report) -> str`
  - `fallback_text(section) -> str`
  - `write_advice(report, chat=None) -> dict[str, str]`（键为 `section.key`；LLM 失败时值为 `fallback_text`，且 `section.advice` 里追加 `origin="llm"`/`"template"` 的 `Advice`）

- [ ] **Step 1: 写失败测试**

创建 `tech-digest/tests/test_ops_report_llm_advice.py`：

```python
# -*- coding: utf-8 -*-
"""LLM 建议层单测：LLM 挂了也必须出报告（走模板兜底）。"""
import unittest

from ops_report import llm_advice, model


def make_report():
    digest = model.Section(key="digest", name="自动发帖机器人", status="ok",
                           metrics={"应发档期": "2 次", "发帖成功率": "100%"})
    assist = model.Section(key="assist", name="自动回复 AI", status="critical",
                           metrics={"请求成功率": "0%", "请求总数": "14577 次"},
                           notes=["pod 上应答进程不在：所有 /api/v1/assist/ 请求都会 502"])
    assist.errors = [model.ErrorCluster(signature="upstream prematurely closed connection",
                                        count=13223, first_ts="2026-09-18T21:30:00",
                                        last_ts="2026-09-20T21:30:00",
                                        sample="upstream prematurely closed connection",
                                        source="nginx")]
    return model.Report(generated_at="2026-09-20T21:30:00",
                        window_start="2026-09-18T21:30:00",
                        window_end="2026-09-20T21:30:00",
                        sections=[digest, assist])


class TestBuildPrompt(unittest.TestCase):
    def test_prompt_contains_metrics_and_errors(self):
        p = llm_advice.build_prompt(make_report())
        self.assertIn("14577", p)
        self.assertIn("upstream prematurely closed connection", p)
        self.assertIn("自动回复 AI", p)


class TestFallback(unittest.TestCase):
    def test_fallback_lists_status_and_notes(self):
        txt = llm_advice.fallback_text(model.Section(
            key="x", name="板块", status="critical", metrics={"请求成功率": "0%"},
            notes=["原因说明"]))
        self.assertIn("critical", txt)
        self.assertIn("请求成功率 0%", txt)
        self.assertIn("原因说明", txt)


class TestWriteAdvice(unittest.TestCase):
    def test_uses_llm_when_available(self):
        calls = []

        def fake_chat(prompt, system=None, max_tokens=0, timeout=0):
            calls.append(prompt)
            return "LLM 分析结论"

        rep = make_report()
        got = llm_advice.write_advice(rep, chat=fake_chat)
        self.assertEqual(got["assist"], "LLM 分析结论")
        self.assertEqual(len(calls), 1)          # 两次调用合并为一次，省 token
        self.assertTrue(any(a.origin == "llm" for a in rep.section("assist").advice))

    def test_falls_back_when_llm_returns_none(self):
        rep = make_report()
        got = llm_advice.write_advice(rep, chat=lambda *a, **k: None)
        self.assertIn("critical", got["assist"])         # 模板兜底仍有内容
        self.assertTrue(any(a.origin == "template" for a in rep.section("assist").advice))

    def test_falls_back_when_chat_raises(self):
        def boom(*a, **k):
            raise RuntimeError("network down")

        rep = make_report()
        got = llm_advice.write_advice(rep, chat=boom)
        self.assertTrue(got["digest"])
        self.assertTrue(got["assist"])


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd tech-digest && python -m unittest tests.test_ops_report_llm_advice -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'ops_report.llm_advice'`

- [ ] **Step 3: 实现 llm_advice.py**

创建 `tech-digest/ops_report/llm_advice.py`：

```python
# -*- coding: utf-8 -*-
"""LLM 分析与建议：把「指标 + 错误聚类 + 知识库命中」交给 LLM 写成运维叙事。

为什么需要它：知识库只能覆盖踩过的坑，新错误得靠 LLM 给候选根因。
复用 tech-digest 已验证的 app.llm.chat（集市网关主 + DeepSeek 备，双通道都挂才 None）。
LLM 不可用 → 模板兜底：报告只下降「文采」，不下降「事实」。
"""
from __future__ import annotations

import logging

from app import llm
from ops_report import analyze
from ops_report.model import Advice

log = logging.getLogger("tech-digest")

SYSTEM = (
    "你是资深 SRE，为中文运维巡检报告写「分析与运维建议」。要求："
    "只依据给定的指标与报错，不编造未出现的事实；"
    "先说结论（是否需要立刻处理），再给按优先级排序的具体动作（命令级）；"
    "对预期行为（周末跳过发帖、dry-run 降级）不要当成故障；"
    "总长控制在 300 字内，分点陈述。"
)


def build_prompt(report) -> str:
    lines = [f"报告窗口：{report.window_start} ~ {report.window_end}",
             f"整体状态：{report.overall}", ""]
    for sec in report.sections:
        lines.append(f"## {sec.name}（状态 {sec.status}）")
        for k, v in (sec.metrics or {}).items():
            lines.append(f"- {k}：{v}")
        for note in sec.notes or []:
            lines.append(f"- 说明：{note}")
        real = analyze.actionable(sec)
        if real:
            lines.append("- 报错聚类（次数降序）：")
            for c in real[:8]:
                lines.append(f"  · ×{c.count} {c.signature[:160]}")
                lines.append(f"    样例：{c.sample[:160]}")
        for a in sec.advice or []:
            if a.origin == "knowledge":
                lines.append(f"- 已知处置：{a.title}")
        lines.append("")
    lines.append("请针对以上内容写出「分析与运维建议」，逐板块给结论与动作。")
    return "\n".join(lines)


def fallback_text(section) -> str:
    """LLM 不可用时的模板兜底：事实照样完整，只是没有提炼。"""
    parts = [f"（模板兜底：LLM 不可用）当前状态 {section.status}。"]
    for k, v in (section.metrics or {}).items():
        parts.append(f"{k} {v}。")
    for note in section.notes or []:
        parts.append(f"{note}。")
    real = analyze.actionable(section)
    if real:
        parts.append("待处理报错：" + "；".join(f"×{c.count} {c.signature[:80]}" for c in real[:5]))
    for a in section.advice or []:
        if a.origin == "knowledge":
            parts.append(f"处置参考：{a.title}")
    return " ".join(parts)


def write_advice(report, chat=None) -> dict:
    """一次 LLM 调用覆盖全报告（省 token），失败则逐板块模板兜底。"""
    chat = chat or llm.chat
    text = None
    try:
        text = chat(build_prompt(report), SYSTEM, 1200, 120)
    except Exception as e:  # noqa: BLE001 — LLM 任何异常都不能让报告失败
        log.warning("LLM 建议生成异常（走模板兜底）: %s", e)

    out = {}
    for sec in report.sections:
        if text and text.strip():
            out[sec.key] = text.strip()
            sec.advice.append(Advice(title="LLM 分析与建议", action=text.strip(), origin="llm"))
        else:
            tpl = fallback_text(sec)
            out[sec.key] = tpl
            sec.advice.append(Advice(title="模板分析与建议", action=tpl, origin="template"))
    return out
```

- [ ] **Step 4: 跑测试确认通过**

Run: `cd tech-digest && python -m unittest tests.test_ops_report_llm_advice -v`
Expected: PASS（5 个测试）

- [ ] **Step 5: 提交**

```bash
git add tech-digest/ops_report/llm_advice.py tech-digest/tests/test_ops_report_llm_advice.py
git commit -m "feat(ops-report): LLM 分析与建议层（双通道 + 模板兜底）"
```

---

### Task 9: 渲染（render.py）

**Files:**
- Create: `tech-digest/ops_report/render.py`
- Test: `tech-digest/tests/test_ops_report_render.py`

**Interfaces:**
- Consumes: `model.Report`、`analyze.actionable`
- Produces:
  - `subject(report) -> str`
  - `render_html(report) -> str`（纯内联样式，邮件客户端可直接渲染）
  - `archive_paths(data_dir, when: datetime) -> tuple[Path, Path]`（html、json）
  - `write_archive(report, data_dir, when) -> tuple[Path, Path]`

- [ ] **Step 1: 写失败测试**

创建 `tech-digest/tests/test_ops_report_render.py`：

```python
# -*- coding: utf-8 -*-
"""渲染单测：主题行要能一眼看出好坏；HTML 必须包含全部板块与建议。"""
import unittest
from datetime import datetime
from pathlib import Path
import tempfile

from ops_report import model, render


def make_report(assist_status="critical"):
    rep = model.Report(generated_at="2026-09-20T21:30:00",
                       window_start="2026-09-18T21:30:00",
                       window_end="2026-09-20T21:30:00",
                       pending_alerts=["上次邮件发送失败：SMTP 认证错误"])
    d = model.Section(key="digest", name="自动发帖机器人", status="ok",
                      metrics={"应发档期": "2 次", "发帖成功率": "100%"})
    a = model.Section(key="assist", name="自动回复 AI", status=assist_status,
                      metrics={"请求成功率": "0%", "请求总数": "14577 次"})
    a.errors = [model.ErrorCluster(signature="upstream prematurely closed connection",
                                   count=13223, first_ts="t1", last_ts="t2",
                                   sample="upstream prematurely closed connection",
                                   source="nginx")]
    a.advice = [model.Advice(title="进程不在", action="1) 进 pod 2) 起服务",
                              origin="knowledge")]
    rep.sections = [d, a]
    return rep


class TestSubject(unittest.TestCase):
    def test_subject_has_headline_metrics_and_flag(self):
        s = render.subject(make_report())
        self.assertIn("发帖成功率 100%", s)
        self.assertIn("请求成功率 0%", s)
        self.assertIn("🔴", s)

    def test_ok_report_has_no_flag(self):
        s = render.subject(make_report(assist_status="ok"))
        self.assertNotIn("🔴", s)
        self.assertNotIn("⚠️", s)


class TestRenderHtml(unittest.TestCase):
    def test_contains_sections_metrics_errors_advice(self):
        html = render.render_html(make_report())
        self.assertIn("自动发帖机器人", html)
        self.assertIn("自动回复 AI", html)
        self.assertIn("发帖成功率", html)
        self.assertIn("×13223", html)
        self.assertIn("1) 进 pod 2) 起服务", html)

    def test_pending_alert_is_highlighted(self):
        html = render.render_html(make_report())
        self.assertIn("上次邮件发送失败", html)

    def test_llm_advice_rendered_once_not_per_section(self):
        """LLM 叙事全局一份（T8 单次调用设计）——板块下重复 3 次会淹没真正的信息。"""
        rep = make_report()
        for s in rep.sections:
            s.advice.append(model.Advice(title="LLM 分析与建议",
                                         action="同一份 LLM 文本", origin="llm"))
        html = render.render_html(rep)
        self.assertEqual(html.count("同一份 LLM 文本"), 1)

    def test_expected_errors_are_not_listed_as_problems(self):
        rep = make_report()
        rep.sections[1].errors.append(model.ErrorCluster(
            signature="周末只抓取入库、不发帖", count=5, first_ts="t", last_ts="t",
            sample="周末只抓取入库、不发帖", source="digest_log", expected=True))
        html = render.render_html(rep)
        self.assertNotIn("周末只抓取入库、不发帖", html)

    def test_html_is_escaped(self):
        rep = make_report()
        rep.sections[0].metrics["标题"] = "<script>alert(1)</script>"
        html = render.render_html(rep)
        self.assertNotIn("<script>alert(1)</script>", html)
        self.assertIn("&lt;script&gt;", html)


class TestArchive(unittest.TestCase):
    def test_writes_html_and_json(self):
        with tempfile.TemporaryDirectory() as td:
            rep = make_report()
            h, j = render.write_archive(rep, Path(td), datetime(2026, 9, 22, 21, 30))
            self.assertTrue(h.exists() and j.exists())
            self.assertEqual(h.name, "2026-09-22.html")
            self.assertEqual(j.name, "2026-09-22.json")
            self.assertIn("自动回复 AI", h.read_text(encoding="utf-8"))
            self.assertIn("assist", j.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd tech-digest && python -m unittest tests.test_ops_report_render -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'ops_report.render'`

- [ ] **Step 3: 实现 render.py**

创建 `tech-digest/ops_report/render.py`：

```python
# -*- coding: utf-8 -*-
"""渲染层：主题行 + HTML 邮件 + JSON 归档。

HTML 全内联样式（邮件客户端不认外部 CSS）；正文只列「要看的问题」——
预期行为（周末跳过、dry-run）聚类里保留、正文里不出现，避免每周误报制造疲劳。
"""
from __future__ import annotations

import html
import json
from datetime import datetime
from pathlib import Path

from ops_report import analyze

_FLAG = {"ok": "", "warn": " ⚠️", "critical": " 🔴", "unknown": " ❓"}
_COLOR = {"ok": "#1a7f37", "warn": "#9a6700", "critical": "#cf222e", "unknown": "#57606a"}


def _esc(v) -> str:
    return html.escape(str(v), quote=True)


def subject(report) -> str:
    def metric(key: str, name: str) -> str:
        sec = report.section(key)
        return (sec.metrics.get(name, "—") if sec else "—")

    return (f"【集市运维报告】{report.window_start[5:10]} ~ {report.window_end[5:10]} ｜ "
            f"发帖成功率 {metric('digest', '发帖成功率')} ｜ "
            f"请求成功率 {metric('assist', '请求成功率')}{_FLAG.get(report.overall, '')}")


def _section_html(sec) -> str:
    color = _COLOR.get(sec.status, "#57606a")
    rows = "".join(
        f'<tr><td style="padding:4px 12px 4px 0;color:#57606a;">{_esc(k)}</td>'
        f'<td style="padding:4px 0;"><b>{_esc(v)}</b></td></tr>'
        for k, v in (sec.metrics or {}).items())

    notes = ""
    if sec.notes:
        items = "".join(f"<li>{_esc(n)}</li>" for n in sec.notes)
        notes = (f'<div style="margin:8px 0;padding:8px 12px;background:#fff8c5;'
                 f'border-left:3px solid #d4a72c;">{items}</div>')

    errs = ""
    real = analyze.actionable(sec)
    if real:
        items = "".join(
            f'<li style="margin-bottom:6px;">×{c.count} '
            f'<code style="background:#f6f8fa;padding:1px 4px;">{_esc(c.signature[:160])}</code>'
            f'<br><span style="color:#57606a;font-size:12px;">首次 {_esc(c.first_ts)} ／ '
            f'末次 {_esc(c.last_ts)} · 来源 {_esc(c.source)}</span></li>'
            for c in real[:8])
        errs = (f'<div style="margin:10px 0;"><b>报错聚类</b>'
                f'<ul style="margin:6px 0 0 0;padding-left:18px;">{items}</ul></div>')
    else:
        errs = '<div style="margin:10px 0;color:#1a7f37;">窗口内无异常报错</div>'

    advice = ""
    # LLM 叙事（origin=="llm"）全局只渲染一次（见 render_html 的综合分析块），
    # 板块下只保留知识库/模板建议——同一份文本重复 3 次只会淹没真正的信息。
    own = [a for a in (sec.advice or []) if a.origin != "llm"]
    if own:
        items = "".join(
            f'<div style="margin:8px 0;padding:8px 12px;background:#f6f8fa;'
            f'border-left:3px solid {color};">'
            f'<b>{_esc(a.title)}</b>'
            f'<pre style="white-space:pre-wrap;font-family:inherit;margin:6px 0 0 0;'
            f'font-size:13px;">{_esc(a.action)}</pre></div>'
            for a in own)
        advice = f'<div style="margin:10px 0;"><b>运维建议</b>{items}</div>'

    return (f'<h2 style="font-size:16px;margin:22px 0 6px 0;padding-bottom:4px;'
            f'border-bottom:2px solid {color};">{_esc(sec.name)} '
            f'<span style="color:{color};font-size:13px;">[{_esc(sec.status)}]</span></h2>'
            f'<table style="border-collapse:collapse;font-size:14px;">{rows}</table>'
            f'{notes}{errs}{advice}')


def render_html(report) -> str:
    alerts = ""
    if report.pending_alerts:
        items = "".join(f"<li>{_esc(a[:300])}</li>" for a in report.pending_alerts)
        alerts = (f'<div style="margin:0 0 14px 0;padding:10px 14px;background:#ffebe9;'
                  f'border-left:4px solid #cf222e;"><b>⚠️ 上次投递未成功，以下告警未送达：</b>'
                  f'<ul style="margin:6px 0 0 0;">{items}</ul></div>')

    summary = "".join(
        f'<li>{_esc(s.name)}：<b style="color:{_COLOR.get(s.status, "#57606a")};">'
        f'{_esc(s.status)}</b></li>' for s in report.sections)

    # LLM 叙事（origin=="llm"）挂在每个板块上（T8 的单次调用设计），渲染时去重只出一次
    llm_texts = []
    for s in report.sections:
        for a in (s.advice or []):
            if a.origin == "llm" and a.action not in llm_texts:
                llm_texts.append(a.action)

    llm_block = ""
    if llm_texts:
        items = "".join(
            f'<div style="margin:8px 0;padding:8px 12px;background:#f6f8fa;'
            f'border-left:3px solid #0969da;"><b>综合分析与建议</b>'
            f'<pre style="white-space:pre-wrap;font-family:inherit;margin:6px 0 0 0;'
            f'font-size:13px;">{_esc(t)}</pre></div>' for t in llm_texts)
        llm_block = f'<div style="margin:0 0 14px 0;">{items}</div>'

    body = "".join(_section_html(s) for s in report.sections)

    return (
        '<!DOCTYPE html><html><head><meta charset="utf-8"></head>'
        '<body style="margin:0;padding:0;background:#ffffff;">'
        '<div style="max-width:760px;margin:0 auto;padding:20px;'
        'font-family:-apple-system,\'PingFang SC\',\'Microsoft YaHei\',sans-serif;'
        'color:#1f2328;font-size:14px;line-height:1.6;">'
        f'<h1 style="font-size:18px;margin:0 0 4px 0;">集市运维巡检报告</h1>'
        f'<div style="color:#57606a;font-size:13px;margin-bottom:14px;">'
        f'窗口 {_esc(report.window_start)} ~ {_esc(report.window_end)} ｜ '
        f'生成于 {_esc(report.generated_at)}</div>'
        f'{alerts}{llm_block}'
        f'<div style="margin-bottom:14px;"><b>结论摘要</b>'
        f'<ul style="margin:6px 0 0 0;">{summary}</ul></div>'
        f'{body}'
        '<div style="margin-top:24px;padding-top:10px;border-top:1px solid #d0d7de;'
        'color:#57606a;font-size:12px;">由 tech-digest/ops_report 自动生成（只读采集）。'
        '归档见 ops_report/data/reports/。</div>'
        '</div></body></html>')


def archive_paths(data_dir: Path, when: datetime) -> tuple[Path, Path]:
    d = Path(data_dir) / "reports"
    day = when.strftime("%Y-%m-%d")
    return d / f"{day}.html", d / f"{day}.json"


def write_archive(report, data_dir: Path, when: datetime) -> tuple[Path, Path]:
    html_path, json_path = archive_paths(data_dir, when)
    html_path.parent.mkdir(parents=True, exist_ok=True)
    html_path.write_text(render_html(report), encoding="utf-8")
    payload = {
        "generated_at": report.generated_at,
        "window_start": report.window_start,
        "window_end": report.window_end,
        "overall": report.overall,
        "pending_alerts": report.pending_alerts,
        "sections": [
            {
                "key": s.key, "name": s.name, "status": s.status,
                "metrics": s.metrics, "notes": s.notes,
                "errors": [c.__dict__ for c in s.errors],
                "advice": [a.__dict__ for a in s.advice],
            } for s in report.sections
        ],
    }
    json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2),
                         encoding="utf-8")
    return html_path, json_path
```

- [ ] **Step 4: 跑测试确认通过**

Run: `cd tech-digest && python -m unittest tests.test_ops_report_render -v`
Expected: PASS（8 个测试）

- [ ] **Step 5: 提交**

```bash
git add tech-digest/ops_report/render.py tech-digest/tests/test_ops_report_render.py
git commit -m "feat(ops-report): HTML 邮件渲染与 JSON 归档"
```

---

### Task 10: 邮件投递（mailer.py）

**Files:**
- Create: `tech-digest/ops_report/mailer.py`
- Test: `tech-digest/tests/test_ops_report_mailer.py`

**Interfaces:**
- Consumes: `config.OpsSettings`
- Produces:
  - `send(settings, subject: str, html: str, retries: int = 3, delay: int = 60, sleep=time.sleep, smtp_factory=None) -> tuple[bool, str]`（成功 → `(True, "已发送")`；失败 → `(False, 最后一次错误)`；`smtp_factory` 便于单测注入假 SMTP）

- [ ] **Step 1: 写失败测试**

创建 `tech-digest/tests/test_ops_report_mailer.py`：

```python
# -*- coding: utf-8 -*-
"""邮件投递单测：重试与「失败必须留痕」是本模块的全部价值。"""
import unittest

from ops_report import config, mailer


class FakeSMTP:
    """记录调用；按排队脚本决定第几次成功。"""

    def __init__(self, fail_times=0, exc=None):
        self.fail_times = fail_times
        self.exc = exc
        self.calls = 0
        self.sent = []
        self.logged_in = None

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def login(self, user, code):
        self.logged_in = (user, code)

    def send_message(self, msg):
        self.calls += 1
        if self.calls <= self.fail_times:
            raise self.exc or OSError("smtp 连接被重置")
        self.sent.append(msg)


def _settings():
    return config.OpsSettings(smtp_user="a@163.com", smtp_auth_code="CODE",
                              mail_to="b@163.com")


class TestSend(unittest.TestCase):
    def test_success_first_try(self):
        box = {}
        ok, msg = mailer.send(_settings(), "主题", "<p>正文</p>",
                              smtp_factory=lambda s, box=box: box.setdefault("c", FakeSMTP()))
        self.assertTrue(ok)
        self.assertEqual(box["c"].logged_in, ("a@163.com", "CODE"))
        self.assertEqual(len(box["c"].sent), 1)

    def test_retries_then_succeeds(self):
        made = []
        # 每次重试都是新建连接（实现按次重连，不复用已重置的连接），
        # 所以失败脚本按「连接」排队：第 1 条连接首发失败，第 2 条直接成功。
        # 若每条连接都 fail_times=1，任何重试结构下都不可能第二次成功。
        plans = [1, 0]

        def factory(s, **kw):
            c = FakeSMTP(fail_times=plans.pop(0))
            made.append(c)
            return c

        slept = []
        ok, _ = mailer.send(_settings(), "主题", "<p>x</p>", retries=3, delay=7,
                            sleep=slept.append, smtp_factory=factory)
        self.assertTrue(ok)
        self.assertEqual(len(made), 2)          # 第一次失败、第二次成功
        self.assertEqual(slept, [7])            # 失败后才等

    def test_all_failures_reported(self):
        made = []

        def factory(s, **kw):
            c = FakeSMTP(fail_times=99, exc=OSError("认证失败"))
            made.append(c)
            return c

        ok, msg = mailer.send(_settings(), "主题", "<p>x</p>", retries=3, delay=0,
                              sleep=lambda s: None, smtp_factory=factory)
        self.assertFalse(ok)
        self.assertIn("认证失败", msg)
        self.assertEqual(len(made), 3)

    def test_missing_credentials_skips_network(self):
        called = []
        ok, msg = mailer.send(config.OpsSettings(), "主题", "<p>x</p>",
                              smtp_factory=lambda s, **k: called.append(1))
        self.assertFalse(ok)
        self.assertIn("未配置", msg)
        self.assertEqual(called, [])

    def test_subject_and_html_headers(self):
        box = {}

        def factory(s, **kw):
            box["c"] = FakeSMTP()
            return box["c"]

        mailer.send(_settings(), "【集市运维报告】x", "<p>正文</p>", smtp_factory=factory)
        msg = box["c"].sent[0]
        self.assertEqual(msg["To"], "b@163.com")
        # From 经 formataddr 编码为「=?utf-8?…?= <a@163.com>」（带显示名），
        # 断言收件地址在 From 里即可；精确等于裸地址会误报。
        self.assertIn("a@163.com", msg["From"])
        self.assertIn("集市运维报告", str(msg["Subject"]))


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd tech-digest && python -m unittest tests.test_ops_report_mailer -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'ops_report.mailer'`

- [ ] **Step 3: 实现 mailer.py**

创建 `tech-digest/ops_report/mailer.py`：

```python
# -*- coding: utf-8 -*-
"""邮件投递：163 SMTP（465 SSL）+ 失败重试。

为什么重试 3 次而不是 1 次：163 偶发限流/连接重置是常态，一次失败就放弃等于把
「报告没发出去」变成静默事故。重试仍失败 → 返回 False，由编排层落告警文件。
"""
from __future__ import annotations

import logging
import smtplib
import time
from email.header import Header
from email.mime.text import MIMEText
from email.utils import formataddr, formatdate

log = logging.getLogger("tech-digest")


def _build_message(sender: str, to: str, subject: str, html: str) -> MIMEText:
    msg = MIMEText(html, "html", "utf-8")
    msg["Subject"] = Header(subject, "utf-8")
    msg["From"] = formataddr((str(Header("集市运维巡检", "utf-8")), sender))
    msg["To"] = to
    msg["Date"] = formatdate(localtime=True)
    return msg


def send(settings, subject: str, html: str, retries: int = 3, delay: int = 60,
         sleep=time.sleep, smtp_factory=None) -> tuple[bool, str]:
    """SMTP_SSL 发送；（成功?, 说明）。任何异常都被收在这里，不外抛。"""
    if not settings.smtp_ready:
        return False, "未配置 SMTP（OPS_SMTP_USER / OPS_SMTP_AUTH_CODE / OPS_MAIL_TO）"

    factory = smtp_factory or (lambda s: smtplib.SMTP_SSL(
        s.smtp_host, s.smtp_port, timeout=30))
    last_err = ""
    for attempt in range(1, retries + 1):
        try:
            with factory(settings) as client:
                client.login(settings.smtp_user, settings.smtp_auth_code)
                client.send_message(_build_message(
                    settings.mail_from, settings.mail_to, subject, html))
            return True, "已发送"
        except Exception as e:  # noqa: BLE001 — 网络/SMTP/编码异常都算投递失败
            last_err = f"{type(e).__name__}: {e}"
            log.warning("邮件发送第 %d/%d 次失败: %s", attempt, retries, last_err)
            if attempt < retries:
                sleep(delay)
    return False, last_err
```

- [ ] **Step 4: 跑测试确认通过**

Run: `cd tech-digest && python -m unittest tests.test_ops_report_mailer -v`
Expected: PASS（5 个测试）

- [ ] **Step 5: 提交**

```bash
git add tech-digest/ops_report/mailer.py tech-digest/tests/test_ops_report_mailer.py
git commit -m "feat(ops-report): 163 SMTP 投递（重试 + 未配置短路）"
```

---

### Task 11: 编排与 CLI（__main__.py）

**Files:**
- Create: `tech-digest/ops_report/__main__.py`
- Test: `tech-digest/tests/test_ops_report_main.py`

**Interfaces:**
- Consumes: 全部上游模块
- Produces:
  - `build_report(settings, now: datetime | None = None, collectors: dict | None = None) -> tuple[model.Report, dict]`（`collectors` 可注入，便于单测；返回 `(report, advice_map)`）
  - `main(argv: list[str] | None = None) -> int`，子命令 `run` / `dry-run` / `test-mail`
  - 退出码：`0` 成功或按守卫跳过；`1` 失败

- [ ] **Step 1: 写失败测试**

创建 `tech-digest/tests/test_ops_report_main.py`：

```python
# -*- coding: utf-8 -*-
"""编排层单测：守卫、降级、投递失败留痕——用注入的假采集器，不碰网络。"""
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

from ops_report import __main__ as cli
from ops_report import config, model


def fake_collectors(section_status="ok"):
    def digest(settings, start, end):
        return model.Collected(model.Section(key="digest", name="自动发帖机器人",
                                             status=section_status,
                                             metrics={"发帖成功率": "100%"}))
    def assist(settings, start, end):
        return model.Collected(model.Section(key="assist", name="自动回复 AI",
                                             status="critical",
                                             metrics={"请求成功率": "0%"}))
    def system(settings):
        return model.Collected(model.Section(key="system", name="系统层", status="ok",
                                             metrics={"磁盘使用率": "82%"}))
    return {"digest": digest, "assist": assist, "system": system}


class TestBuildReport(unittest.TestCase):
    def test_builds_all_sections_and_advice(self):
        with tempfile.TemporaryDirectory() as td:
            s = config.OpsSettings(data_dir=Path(td))
            rep, advice = cli.build_report(
                s, now=datetime(2026, 9, 20, 21, 30),
                collectors=fake_collectors(), chat=lambda *a, **k: None)
            self.assertEqual([x.key for x in rep.sections], ["digest", "assist", "system"])
            self.assertEqual(rep.overall, "critical")
            self.assertTrue(advice["assist"])

    def test_collector_exception_degrades_to_unknown(self):
        cols = fake_collectors()

        def boom(settings, start, end):
            raise RuntimeError("采集炸了")

        cols["assist"] = boom
        with tempfile.TemporaryDirectory() as td:
            s = config.OpsSettings(data_dir=Path(td))
            rep, _ = cli.build_report(s, now=datetime(2026, 9, 20, 21, 30),
                                      collectors=cols, chat=lambda *a, **k: None)
            sec = rep.section("assist")
            self.assertEqual(sec.status, "unknown")
            self.assertTrue(any("采集异常" in n for n in sec.notes))


class TestMainGuard(unittest.TestCase):
    def test_dry_run_writes_archive_and_does_not_touch_state(self):
        with tempfile.TemporaryDirectory() as td:
            s = config.OpsSettings(data_dir=Path(td))
            rc = cli.main(["dry-run"], settings=s, now=datetime(2026, 9, 20, 21, 30),
                          collectors=fake_collectors(), chat=lambda *a, **k: None)
            self.assertEqual(rc, 0)
            self.assertTrue((Path(td) / "reports" / "2026-09-20.html").exists())
            self.assertFalse((Path(td) / "state.json").exists())

    def test_run_sends_and_marks_sent(self):
        sent = []
        with tempfile.TemporaryDirectory() as td:
            s = config.OpsSettings(data_dir=Path(td), smtp_user="a@163.com",
                                   smtp_auth_code="x", mail_to="b@163.com")
            rc = cli.main(["run"], settings=s, now=datetime(2026, 9, 20, 21, 30),
                          collectors=fake_collectors(), chat=lambda *a, **k: None,
                          send=lambda *a, **k: (sent.append(a), (True, "已发送"))[1])
            self.assertEqual(rc, 0)
            self.assertEqual(len(sent), 1)
            self.assertEqual((Path(td) / "state.json").exists(), True)

    def test_run_skips_when_within_interval(self):
        with tempfile.TemporaryDirectory() as td:
            s = config.OpsSettings(data_dir=Path(td))
            (Path(td) / "state.json").write_text('{"last_sent": "2026-09-20T21:30:00"}',
                                                 encoding="utf-8")
            called = []
            rc = cli.main(["run"], settings=s, now=datetime(2026, 9, 21, 21, 30),
                          collectors=fake_collectors(),
                          send=lambda *a, **k: called.append(1))
            self.assertEqual(rc, 0)
            self.assertEqual(called, [])            # 守卫生效：没发
            self.assertFalse((Path(td) / "reports" / "2026-09-21.html").exists())

    def test_send_failure_writes_alert_and_returns_one(self):
        with tempfile.TemporaryDirectory() as td:
            s = config.OpsSettings(data_dir=Path(td), smtp_user="a@163.com",
                                   smtp_auth_code="x", mail_to="b@163.com")
            rc = cli.main(["run"], settings=s, now=datetime(2026, 9, 20, 21, 30),
                          collectors=fake_collectors(), chat=lambda *a, **k: None,
                          send=lambda *a, **k: (False, "认证失败"))
            self.assertEqual(rc, 1)
            alerts = list((Path(td) / "alerts").glob("*.txt"))
            self.assertEqual(len(alerts), 1)
            self.assertIn("认证失败", alerts[0].read_text(encoding="utf-8"))
            self.assertFalse((Path(td) / "state.json").exists())   # 未发成功不推进节奏

    def test_pending_alert_is_cleared_after_successful_send(self):
        with tempfile.TemporaryDirectory() as td:
            s = config.OpsSettings(data_dir=Path(td), smtp_user="a@163.com",
                                   smtp_auth_code="x", mail_to="b@163.com")
            d = Path(td) / "alerts"
            d.mkdir(parents=True)
            (d / "20260920-000000.txt").write_text("旧的失败", encoding="utf-8")
            rc = cli.main(["run"], settings=s, now=datetime(2026, 9, 20, 21, 30),
                          collectors=fake_collectors(), chat=lambda *a, **k: None,
                          send=lambda *a, **k: (True, "已发送"))
            self.assertEqual(rc, 0)
            self.assertEqual(list(d.glob("*.txt")), [])             # 送出去了就清掉


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd tech-digest && python -m unittest tests.test_ops_report_main -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'ops_report.__main__'`

- [ ] **Step 3: 实现 __main__.py**

创建 `tech-digest/ops_report/__main__.py`：

```python
# -*- coding: utf-8 -*-
"""ops_report 入口：编排三层 + CLI。

子命令：
  run       正式执行（守卫 → 采集 → 分析 → 渲染 → 发送 → 记状态）
  dry-run   只生成不发送（落归档，供人先看样式）
  test-mail 发一封最小测试邮件（验证 SMTP 通路，不跑采集）

任何单源采集异常 → 该板块降级为 unknown 并注明，报告照发。这是刻意的：
运维报告最没用的形态就是「自己挂了所以什么都没说」。
"""
from __future__ import annotations

import logging
import sys
from datetime import datetime

from ops_report import analyze, config, knowledge, llm_advice, mailer, render, state, window
from ops_report.collect import assist as collect_assist
from ops_report.collect import digest as collect_digest
from ops_report.collect import system as collect_system
from ops_report.model import Collected, Report, Section


def _setup_logging() -> None:
    # Windows 控制台常见 GBK，编不了主题里的状态符号（🔴/⚠️/❓）——
    # 不可编码时降级为 ?，而不是让整条 CLI 崩在 print 上（UTF-8 环境不受影响）。
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            try:
                stream.reconfigure(errors="replace")
            except (ValueError, OSError):
                pass
    root = logging.getLogger()
    if not root.handlers:
        root.addHandler(logging.StreamHandler(sys.stdout))
    root.setLevel(logging.INFO)


def default_collectors() -> dict:
    return {"digest": collect_digest.collect,
            "assist": collect_assist.collect,
            "system": collect_system.collect}


SECTION_NAMES = {"digest": "自动发帖机器人", "assist": "自动回复 AI", "system": "系统层"}


def build_report(settings, now: datetime | None = None,
                 collectors: dict | None = None, chat=None) -> tuple[Report, dict]:
    """采集三层 → 聚类 → 知识库 → LLM 建议 → Report。

    窗口只算一次（来自 state 的 last_sent），采集与报告头用同一个窗口，
    避免两头各自计算导致「报告说 2 天、采集只取了 1 天」。
    """
    now = now or datetime.now()
    collectors = collectors or default_collectors()
    start, end = window.report_window(state.State(settings.data_dir).last_sent(),
                                      now, settings.interval_days)

    sections, raw = [], {}
    for key in ("digest", "assist", "system"):
        fn = collectors.get(key)
        try:
            collected = fn(settings) if key == "system" else fn(settings, start, end)
        except Exception as e:  # noqa: BLE001 — 采集异常必须降级而不是中断
            collected = Collected(Section(key=key, name=SECTION_NAMES[key], status="unknown",
                                          notes=[f"采集异常：{type(e).__name__}: {e}"]))
        sections.append(collected.section)
        raw[key] = collected.raw_errors

    for sec in sections:
        analyze.annotate(sec, raw.get(sec.key, []))

    report = Report(generated_at=now.isoformat(timespec="seconds"),
                    window_start=start.isoformat(timespec="seconds"),
                    window_end=end.isoformat(timespec="seconds"),
                    sections=sections,
                    pending_alerts=state.pending_alerts(settings.data_dir))

    for adv in knowledge.match(knowledge.signals_from(sections)):
        _guess_section(sections, adv).advice.append(adv)

    advice = llm_advice.write_advice(report, chat=chat)
    return report, advice


def _guess_section(sections: list, advice) -> Section:
    """知识库建议归属：按建议标题里的关键词找板块，找不到挂到第一个非 ok 板块。"""
    text = advice.title + advice.action
    for sec in sections:
        if sec.key == "assist" and ("应答" in text or "pod" in text or "nginx" in text):
            return sec
        if sec.key == "digest" and ("发帖" in text or "refresh_token" in text or "token" in text):
            return sec
        if sec.key == "system" and ("磁盘" in text or "证书" in text):
            return sec
    for sec in sections:
        if sec.status != "ok":
            return sec
    return sections[0]


def main(argv: list[str] | None = None, settings=None, now: datetime | None = None,
         collectors: dict | None = None, chat=None, send=None) -> int:
    _setup_logging()
    argv = list(sys.argv[1:] if argv is None else argv)
    cmd = argv[0] if argv else "run"
    settings = settings or config.OpsSettings.from_env()
    now = now or datetime.now()

    if cmd == "test-mail":
        ok, msg = (send or mailer.send)(
            settings, "【集市运维报告】SMTP 测试",
            "<p>这是一封测试邮件：若你看到它，说明巡检报告的投递链路已打通。</p>")
        print(("测试邮件已发送" if ok else f"测试邮件发送失败：{msg}"))
        return 0 if ok else 1

    if cmd not in ("run", "dry-run"):
        print(f"未知子命令：{cmd}（可用：run / dry-run / test-mail）")
        return 1

    st = state.State(settings.data_dir)
    if cmd == "run" and not window.should_send(st.last_sent(), now, settings.interval_days):
        print(f"距上次发送不足 {settings.interval_days} 天，跳过")
        return 0

    report, _advice = build_report(settings, now=now, collectors=collectors, chat=chat)
    html = render.render_html(report)
    html_path, json_path = render.write_archive(report, settings.data_dir, now)
    subj = render.subject(report)
    print(f"报告已生成：{html_path}")
    print(f"主题：{subj}")

    if cmd == "dry-run":
        print("dry-run：未发送、未更新 state.json")
        return 0

    ok, msg = (send or mailer.send)(settings, subj, html)
    if ok:
        st.mark_sent(now.isoformat(timespec="seconds"))
        state.clear_alerts(settings.data_dir)
        print("邮件已发送")
        return 0

    state.add_alert(settings.data_dir,
                    f"运维报告投递失败（{now.isoformat(timespec='seconds')}）：{msg}\n"
                    f"报告已归档：{html_path}")
    print(f"邮件发送失败：{msg}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 4: 跑测试确认通过**

Run: `cd tech-digest && python -m unittest tests.test_ops_report_main -v`
Expected: PASS（7 个测试）

- [ ] **Step 5: 跑全部既有测试确认无回归**

Run: `cd tech-digest && python -m unittest tests.test_report tests.test_store tests.test_main tests.test_ops_report_base tests.test_ops_report_window tests.test_ops_report_digest tests.test_ops_report_assist tests.test_ops_report_system tests.test_ops_report_analyze tests.test_ops_report_knowledge tests.test_ops_report_llm_advice tests.test_ops_report_render tests.test_ops_report_mailer tests.test_ops_report_main`
Expected: 全部 OK

- [ ] **Step 6: 提交**

```bash
git add tech-digest/ops_report/__main__.py tech-digest/tests/test_ops_report_main.py
git commit -m "feat(ops-report): 编排与 CLI（守卫/降级/投递留痕）"
```

---

### Task 12: systemd 单元与 .gitignore

**Files:**
- Create: `tech-digest/deploy/ops-report.service`
- Create: `tech-digest/deploy/ops-report.timer`
- Create: `tech-digest/ops_report/.gitignore`（新文件；不改动 tech-digest 既有文件——Global Constraints 禁止）
- Test: 无单测（配置文件），用 `systemd-analyze verify` 在服务器验证

**Interfaces:**
- Consumes: `python -m ops_report run`
- Produces: 定时器（每天 21:30 触发，脚本内守卫决定是否真发）

- [ ] **Step 1: 写 service 单元**

创建 `tech-digest/deploy/ops-report.service`：

```ini
[Unit]
Description=ops-report 运维巡检报告（每两天 21:30 邮件汇总）
After=network-online.target docker.service
Wants=network-online.target

[Service]
Type=oneshot
WorkingDirectory=/root/market-deploy/agent/tech-digest
ExecStart=/root/market-deploy/agent/tech-digest/.venv/bin/python -m ops_report run
# 采集（docker logs 48h + ssh pod + 集市库）+ LLM + SMTP 重试（3×60s）
TimeoutStartSec=600
Nice=10
```

- [ ] **Step 2: 写 timer 单元**

创建 `tech-digest/deploy/ops-report.timer`：

```ini
[Unit]
Description=ops-report timer（每天 21:30 触发；脚本内 2 天守卫决定是否发送）

[Timer]
OnCalendar=*-*-* 21:30
Persistent=true
RandomizedDelaySec=60
Unit=ops-report.service

[Install]
WantedBy=timers.target
```

- [ ] **Step 3: 新增 ops_report/.gitignore**

创建 `tech-digest/ops_report/.gitignore`（**新建**而不是改 tech-digest 既有的 .gitignore——Global Constraints 要求不动现有文件；目录级 .gitignore 效果相同且更local）：

```gitignore
# ops_report 运行时产物（归档、状态、告警）与凭据
data/
.env
```

- [ ] **Step 4: 本地语法自检**

Run: `cd tech-digest && python -c "import configparser; [configparser.ConfigParser(strict=False).read(f) for f in ['deploy/ops-report.service','deploy/ops-report.timer']]; print('ini 可解析')"`
Expected: `ini 可解析`

- [ ] **Step 5: 提交**

```bash
git add tech-digest/deploy/ops-report.service tech-digest/deploy/ops-report.timer tech-digest/ops_report/.gitignore
git commit -m "feat(ops-report): systemd timer（每天 21:30 触发 + 脚本内 2 天守卫）"
```

---

### Task 13: 部署到集市服务器并跑真实 dry-run

**Files:**
- 无仓库改动（部署动作）
- 服务器产生：`/root/market-deploy/agent/tech-digest/ops_report/`（代码 + `.env` + `data/`）

**Interfaces:**
- Consumes: Task 1–12 的全部产物
- Produces: 服务器上可运行的 `python -m ops_report`，以及一份真实数据的 dry-run 报告

- [ ] **Step 1: 推送代码到服务器**

用仓库既有的 SFTP 方式（`_sftp_put.py` 同款 paramiko）推送新增文件，或直接 tar + scp。逐文件推送 `ops_report/` 整包与 `deploy/ops-report.*`：

```bash
cd "c:/Users/<user>/Desktop/自动巡检agent/tech-digest" && tar czf /tmp/ops_report.tgz ops_report deploy/ops-report.service deploy/ops-report.timer && scp /tmp/ops_report.tgz root@<SERVER_IP>:/tmp/ && ssh root@<SERVER_IP> "cd /root/market-deploy/agent/tech-digest && tar xzf /tmp/ops_report.tgz && ls -la ops_report/"
```

- [ ] **Step 2: 写服务器 .env（授权码与 pod 口令，权限 600）**

在服务器上创建 `/root/market-deploy/agent/tech-digest/ops_report/.env`：

```bash
ssh root@<SERVER_IP> 'cat > /root/market-deploy/agent/tech-digest/ops_report/.env <<EOF
OPS_SMTP_USER=abc3509429932@163.com
OPS_SMTP_AUTH_CODE=<用户提供的授权码>
OPS_MAIL_TO=abc3509429932@163.com
OPS_INTERVAL_DAYS=2
OPS_POD_SSH_HOST=127.0.0.1
OPS_POD_SSH_PORT=22012
OPS_POD_SSH_USER=cloud
OPS_POD_SSH_PASSWORD=<pod 口令>
EOF
chmod 600 /root/market-deploy/agent/tech-digest/ops_report/.env
ls -l /root/market-deploy/agent/tech-digest/ops_report/.env'
```

Expected: `-rw------- 1 root root ... .env`

- [ ] **Step 3: 服务器跑全量测试**

```bash
ssh root@<SERVER_IP> 'cd /root/market-deploy/agent/tech-digest && .venv/bin/python -m pytest tests/ -q 2>&1 | tail -5'
```
Expected: `287 passed` 加上 ops_report 的新测试（共 300+ passed），0 failed

- [ ] **Step 4: 真实 dry-run（采集真数据，不发送）**

```bash
ssh root@<SERVER_IP> 'cd /root/market-deploy/agent/tech-digest && .venv/bin/python -m ops_report dry-run 2>&1 | tail -20'
```
Expected: 打印「报告已生成：.../ops_report/data/reports/2026-09-20.html」与主题行；主题里应答侧应体现 **请求成功率 0% + 🔴**

- [ ] **Step 5: 人工核对报告内容**

```bash
ssh root@<SERVER_IP> 'cd /root/market-deploy/agent/tech-digest && head -c 3000 ops_report/data/reports/2026-09-20.html'
```
核对要点：
- 发帖板块成功率与 run_log 一致
- 应答板块「请求总数」与 `docker logs` 实际条数吻合（14577 量级）、状态码分布含 502/421
- 「进程/端口」显示**未运行 / 未监听**
- 系统层磁盘显示 82%
- 运维建议里出现「应答服务进程不在（pod 上 main.py 消失）」的处置步骤

- [ ] **Step 6: 提交（如核对中发现需要修的代码）**

若上一步发现问题需改代码，回到对应 Task 修 → 重跑单测 → 重新推送 → 重新 dry-run；若无改动，本 Task 无提交。

---

### Task 14: 测试邮件 + 激活定时器 + 收尾验证

**Files:**
- 服务器：`/etc/systemd/system/ops-report.{service,timer}`（由 deploy/ 复制）

**Interfaces:**
- Consumes: Task 13 的部署
- Produces: 已激活的 systemd timer + 用户邮箱里的一封测试邮件

- [ ] **Step 1: 发测试邮件**

```bash
ssh root@<SERVER_IP> 'cd /root/market-deploy/agent/tech-digest && .venv/bin/python -m ops_report test-mail'
```
Expected: `测试邮件已发送`，且 `abc3509429932@163.com` 收到邮件

> 用户确认收到后再继续下一步；未收到则先排查（授权码是否正确、是否需要开启 SMTP 服务）。

- [ ] **Step 2: 安装并激活 systemd 单元**

```bash
ssh root@<SERVER_IP> 'cd /root/market-deploy/agent/tech-digest && cp deploy/ops-report.service deploy/ops-report.timer /etc/systemd/system/ && systemctl daemon-reload && systemctl enable --now ops-report.timer && systemctl list-timers ops-report.timer --no-pager'
```
Expected: `ops-report.timer` 在列表里，NEXT 显示次日 21:30

- [ ] **Step 3: 验证单元语法与依赖**

```bash
ssh root@<SERVER_IP> 'systemd-analyze verify /etc/systemd/system/ops-report.service 2>&1 | head -5; systemctl is-enabled ops-report.timer'
```
Expected: 无 error 输出；`enabled`

- [ ] **Step 4: 手工触发一次真实运行（验证 run 路径与守卫）**

```bash
ssh root@<SERVER_IP> 'systemctl start ops-report.service; sleep 5; systemctl status ops-report.service --no-pager | head -12; journalctl -u ops-report.service -n 20 --no-pager'
```
Expected: 首次运行无 `state.json` → 发送成功 → 打印「邮件已发送」；
紧接着再触发一次应打印「距上次发送不足 2 天，跳过」（验证守卫）。

```bash
ssh root@<SERVER_IP> 'systemctl start ops-report.service; journalctl -u ops-report.service -n 3 --no-pager'
```
Expected: `距上次发送不足 2 天，跳过`

- [ ] **Step 5: 确认运维闭环**

```bash
ssh root@<SERVER_IP> 'cat /root/market-deploy/agent/tech-digest/ops_report/data/state.json; ls -la /root/market-deploy/agent/tech-digest/ops_report/data/reports/'
```
Expected: `state.json` 含 today 的 `last_sent`；`reports/` 下有当日 html + json

- [ ] **Step 6: 提交收尾（更新设计文档的实施记录）**

在 `docs/superpowers/specs/2026-09-20-ops-report-design.md` 的状态行改为「已实现并部署（含首次发送时间）」，并提交：

```bash
git add docs/superpowers/specs/2026-09-20-ops-report-design.md
git commit -m "docs(ops-report): 标记为已部署，记录首次发送时间与验证结果"
```

---

## 附录：服务器 .env 模板（部署时写入，权限 600）

```text
OPS_SMTP_USER=abc3509429932@163.com
OPS_SMTP_AUTH_CODE=<163 授权码>
OPS_MAIL_TO=abc3509429932@163.com
OPS_INTERVAL_DAYS=2
OPS_POD_SSH_HOST=127.0.0.1
OPS_POD_SSH_PORT=22012
OPS_POD_SSH_USER=cloud
OPS_POD_SSH_PASSWORD=<pod 口令>
OPS_NGINX_CONTAINER=sse_market_server-nginx_proxy-1
OPS_CERT_GLOB=/root/market-deploy/Nginx/live/*/fullchain.pem
OPS_DISK_WARN_PCT=80
OPS_DISK_CRIT_PCT=90
```
