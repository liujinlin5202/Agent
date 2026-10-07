# 日报人工倾向功能 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** tech-digest 日报支持「默认/倾向」双模式：倾向为自由文本软注入挑选 prompt（质量差必须弃倾向），管理入口挂集市前端最新一期日报帖详情页（帖内按钮 + 弹窗，白名单成员可见，无需密码），三阶段灰度上线。

**Architecture:** `data/preference.json` 是唯一真源；daily 运行现场读取并注入 `ai_pick` prompt；写路径 = 集市前端（白名单 UI）→ nginx 同域反代 `/api/v1/digest-pref` → 常驻面板进程（JWT 解析身份 + 服务端白名单 + 原子写 + 审计）。前端白名单只管入口可见性，服务端白名单权威，fail closed。

**Tech Stack:** Python 3 stdlib（面板/偏好模块，urllib 原子写）、Vue 3 + TS + Vite（`_fe/`）、nginx 反代、unittest（经 pytest 运行）。

**Spec:** `docs/superpowers/specs/2026-09-22-digest-preference-design.md`（实现时先读 spec 与本计划）

## Global Constraints

- 服务器：`root@<SERVER_IP>`（密钥 `C:\Users/<user>\.ssh\id_ed25519`）。tech-digest=`/root/market-deploy/agent/tech-digest`，集市前端=`/root/market-deploy/sse_market_new_client/newSSE`（同一台机器）。
- 本地测试：`cd tech-digest && .venv/Scripts/python.exe -m pytest tests/test_xxx.py -v`；服务器用 `.venv/bin/python3`。测试风格 unittest + `mock.patch.object`（照抄 tests/ 现有用例风格）。
- **默认模式下 `_build_pick_prompt` 输出必须与现状逐字一致**；偏好文件缺失/损坏/非法一律回退默认模式，绝不影响发帖主流程。
- 服务端白名单权威：env `DIGEST_PREF_ADMINS`（逗号分隔），默认 `Rimuru`。身份解析失败一律 403（fail closed）。
- `refresh_token` 单次轮换：只准经 `publisher.refresh_access_token()`（它自己持久化新 token）。
- Windows 坑（既往踩实）：复杂远程脚本一律 Write→scp→跑，别用 heredoc 传 `\n`；scp 逐文件传；服务器根目录勿放 test_*.py（污染 pytest）。
- `_fe/` 是服务器源码的本地工作副本，**不入 git**；前端改动经部署脚本上传 + 服务器构建 + dist 就地备份（仿 `_fe_deploy_whitelist.py`）。
- git 提交信息用中文、风格对齐仓库历史（`feat(digest-pref): ...`）。

---

### Task 1: `app/preference.py` — 偏好读取与容错

**Files:**
- Create: `tech-digest/app/preference.py`
- Test: `tech-digest/tests/test_preference.py`

**Interfaces:**
- Produces: `Preference` dataclass（`mode: str = "default"`、`text: str = ""`、`updated_at: str = ""`、`updated_by: str = ""`，属性 `active -> bool`）；`preference_path() -> Path`；`load_preference(path: Path | None = None) -> Preference`；常量 `MAX_TEXT_CHARS = 200`。后续所有任务都依赖这些名字。

- [ ] **Step 1: 写失败测试**

创建 `tech-digest/tests/test_preference.py`：

```python
# -*- coding: utf-8 -*-
"""日报人工倾向：preference.json 读取与容错（任何异常 → 默认模式，不炸主流程）。"""
import json
import tempfile
import unittest
from pathlib import Path

from app.preference import Preference, load_preference


class TestLoadPreference(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "preference.json"

    def _write(self, content: str) -> None:
        self.path.write_text(content, encoding="utf-8")

    def test_missing_file_returns_default(self):
        pref = load_preference(self.path)
        self.assertEqual(pref.mode, "default")
        self.assertEqual(pref.text, "")

    def test_valid_prefer_roundtrip(self):
        self._write(json.dumps({"mode": "prefer", "text": "jev 相关内容优先",
                                "updated_at": "2026-09-22T10:00:00",
                                "updated_by": "Rimuru"}, ensure_ascii=False))
        pref = load_preference(self.path)
        self.assertEqual(pref.mode, "prefer")
        self.assertEqual(pref.text, "jev 相关内容优先")
        self.assertEqual(pref.updated_by, "Rimuru")
        self.assertTrue(pref.active)

    def test_broken_json_falls_back_to_default(self):
        self._write("{不是 JSON")
        pref = load_preference(self.path)
        self.assertEqual(pref.mode, "default")
        self.assertFalse(pref.active)

    def test_illegal_mode_falls_back_to_default(self):
        self._write(json.dumps({"mode": "always-jev", "text": "x"}))
        self.assertEqual(load_preference(self.path).mode, "default")

    def test_prefer_with_empty_text_falls_back_to_default(self):
        self._write(json.dumps({"mode": "prefer", "text": "   "}))
        self.assertEqual(load_preference(self.path).mode, "default")

    def test_prefer_with_overlong_text_falls_back_to_default(self):
        self._write(json.dumps({"mode": "prefer", "text": "好" * 201}))
        self.assertEqual(load_preference(self.path).mode, "default")

    def test_default_mode_ignores_text(self):
        self._write(json.dumps({"mode": "default", "text": "残留文本"}))
        pref = load_preference(self.path)
        self.assertEqual(pref.mode, "default")
        self.assertFalse(pref.active)

    def test_json_array_falls_back_to_default(self):
        self._write("[1, 2]")
        self.assertEqual(load_preference(self.path).mode, "default")

    def test_fallback_logs_warning(self):
        self._write("{坏")
        with self.assertLogs("tech-digest.preference", level="WARNING") as cm:
            load_preference(self.path)
        self.assertTrue(any("回退默认模式" in m for m in cm.output))
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd tech-digest && .venv/Scripts/python.exe -m pytest tests/test_preference.py -v`
Expected: FAIL（`ModuleNotFoundError: No module named 'app.preference'`）

- [ ] **Step 3: 最小实现**

创建 `tech-digest/app/preference.py`：

```python
# -*- coding: utf-8 -*-
"""日报人工倾向（2026-09-22）：data/preference.json 是唯一真源。

- daily 每次运行现场读（main.run_daily → ai_pick 软注入）；读不到/读坏一律回退
  默认模式，绝不影响发帖主流程。
- 管理入口（manual_server /api/v1/digest-pref）只负责写这个文件，与 daily 解耦：
  写路径挂了只影响「改」，不影响 daily 照跑。
"""
from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from app.config import settings

log = logging.getLogger("tech-digest.preference")

PREF_FILENAME = "preference.json"
MAX_TEXT_CHARS = 200
DEFAULT_ADMINS = "Rimuru"


@dataclass
class Preference:
    mode: str = "default"          # "default" | "prefer"
    text: str = ""
    updated_at: str = ""
    updated_by: str = ""

    @property
    def active(self) -> bool:
        return self.mode == "prefer" and bool(self.text.strip())


def preference_path() -> Path:
    return settings.data_dir / PREF_FILENAME


def load_preference(path: Path | None = None) -> Preference:
    """读偏好。缺失/坏 JSON/字段非法 → 默认模式 + warning（fail open to default）。"""
    p = path or preference_path()
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            raise ValueError("preference.json 顶层不是对象")
        mode = str(raw.get("mode") or "default")
        text = str(raw.get("text") or "").strip()
        if mode not in ("default", "prefer"):
            raise ValueError(f"mode 非法: {mode}")
        if mode == "prefer" and not text:
            raise ValueError("prefer 模式 text 为空")
        if len(text) > MAX_TEXT_CHARS:
            raise ValueError(f"text 超长（>{MAX_TEXT_CHARS} 字）")
        return Preference(mode=mode, text=text,
                          updated_at=str(raw.get("updated_at") or ""),
                          updated_by=str(raw.get("updated_by") or ""))
    except FileNotFoundError:
        return Preference()
    except (ValueError, OSError, TypeError) as e:
        log.warning("preference.json 不可用（%s），回退默认模式", e)
        return Preference()


def admin_accounts() -> list[str]:
    """服务端权威白名单（env 逗号分隔），默认 Rimuru。"""
    raw = os.environ.get("DIGEST_PREF_ADMINS", DEFAULT_ADMINS)
    return [a.strip() for a in raw.split(",") if a.strip()]


def is_admin(username: str | None) -> bool:
    return bool(username) and username in admin_accounts()


def save_preference(mode: str, text: str, updated_by: str,
                    path: Path | None = None) -> Preference:
    """写偏好（tmp+rename 原子写），带 updated_at/by。非法参数抛 ValueError。"""
    text = (text or "").strip()
    if mode not in ("default", "prefer"):
        raise ValueError(f"mode 非法: {mode}")
    if mode == "prefer":
        if not text:
            raise ValueError("prefer 模式 text 必填")
        if len(text) > MAX_TEXT_CHARS:
            raise ValueError(f"text 超长（>{MAX_TEXT_CHARS} 字）")
    else:
        text = ""
    p = path or preference_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    pref = Preference(mode=mode, text=text,
                      updated_at=datetime.now().isoformat(timespec="seconds"),
                      updated_by=updated_by)
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(pref.__dict__, ensure_ascii=False, indent=2),
                   encoding="utf-8")
    tmp.replace(p)
    return pref
```

注意：`save_preference` 本任务只要求实现（Task 2 一并测试），此处先落代码是为了 Task 2 测试直接可用。

- [ ] **Step 4: 跑测试确认通过**

Run: `cd tech-digest && .venv/Scripts/python.exe -m pytest tests/test_preference.py -v`
Expected: 全部 PASS

- [ ] **Step 5: 全量回归**

Run: `cd tech-digest && .venv/Scripts/python.exe -m pytest tests/ -q`
Expected: 原有全绿 + 新增 9 例全绿

- [ ] **Step 6: Commit**

```bash
git add tech-digest/app/preference.py tech-digest/tests/test_preference.py
git commit -m "feat(digest-pref): 偏好存储读取——preference.json 唯一真源，异常一律回退默认模式"
```

---

### Task 2: 身份解析（JWT → username → 白名单）+ 原子写测试

**Files:**
- Modify: `tech-digest/app/preference.py`（追加 `resolve_member` 与 `_urllib_get_json`）
- Modify: `tech-digest/app/publisher.py`（追加公开封装 `market_base()`）
- Test: `tech-digest/tests/test_preference.py`（追加）

**Interfaces:**
- Consumes: Task 1 的 `is_admin`、`Preference`。
- Produces: `resolve_member(token: str, base_url: str, timeout: int = 5, fetch_json=None) -> str | None`（失败/网络异常/非白名单前不在这里判——只解析身份，None=解析失败）；`AUTH_INFO_PATHS: tuple[str, ...]`。`publisher.market_base() -> str`。

- [ ] **Step 1: 写失败测试（追加到 tests/test_preference.py）**

```python
class TestResolveMember(unittest.TestCase):
    def _resolve(self, fetch, token="tok", base="http://127.0.0.1:8080"):
        from app.preference import resolve_member
        return resolve_member(token, base, fetch_json=fetch)

    def test_resolves_name_from_data_user(self):
        def fetch(url, token, timeout):
            self.assertEqual(url, "http://127.0.0.1:8080/api/auth/info")
            self.assertEqual(token, "tok")
            return {"code": 0, "data": {"user": {"name": "Rimuru"}}}
        self.assertEqual(self._resolve(fetch), "Rimuru")

    def test_falls_back_to_alt_path_on_404(self):
        def fetch(url, token, timeout):
            if url.endswith("/api/auth/info"):
                return None          # 404
            return {"data": {"user": {"name": "Rimuru"}}}
        self.assertEqual(self._resolve(fetch), "Rimuru")

    def test_empty_token_returns_none_without_calling(self):
        calls = []
        def fetch(url, token, timeout):
            calls.append(url)
            return {"data": {"user": {"name": "Rimuru"}}}
        self.assertIsNone(self._resolve(fetch, token=""))
        self.assertEqual(calls, [])

    def test_network_error_returns_none(self):
        def fetch(url, token, timeout):
            raise OSError("connection refused")
        self.assertIsNone(self._resolve(fetch))

    def test_http_403_returns_none(self):
        # 非 404 的 HTTPError 由 _urllib_get_json 抛出——这里模拟其效果
        def fetch(url, token, timeout):
            raise OSError("HTTP 403")
        self.assertIsNone(self._resolve(fetch))

    def test_empty_name_returns_none(self):
        def fetch(url, token, timeout):
            return {"data": {"user": {"name": ""}}}
        self.assertIsNone(self._resolve(fetch))


class TestSavePreference(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "preference.json"

    def test_save_prefer_roundtrip_atomic(self):
        from app.preference import save_preference
        pref = save_preference("prefer", "jev 相关内容优先", "Rimuru", path=self.path)
        self.assertEqual(pref.mode, "prefer")
        on_disk = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual(on_disk["text"], "jev 相关内容优先")
        self.assertEqual(on_disk["updated_by"], "Rimuru")
        self.assertTrue(on_disk["updated_at"])
        self.assertFalse(self.path.with_suffix(".json.tmp").exists())

    def test_save_default_clears_text(self):
        from app.preference import save_preference
        save_preference("default", "残留", "Rimuru", path=self.path)
        on_disk = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual(on_disk["mode"], "default")
        self.assertEqual(on_disk["text"], "")

    def test_save_rejects_bad_params(self):
        from app.preference import save_preference
        with self.assertRaises(ValueError):
            save_preference("always", "x", "Rimuru", path=self.path)
        with self.assertRaises(ValueError):
            save_preference("prefer", "  ", "Rimuru", path=self.path)
        with self.assertRaises(ValueError):
            save_preference("prefer", "好" * 201, "Rimuru", path=self.path)
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd tech-digest && .venv/Scripts/python.exe -m pytest tests/test_preference.py -v`
Expected: 新增两类 FAIL（`ImportError: cannot import name 'resolve_member'`）

- [ ] **Step 3: 最小实现**

`app/preference.py` 追加（放在 `is_admin` 之后）：

```python
AUTH_INFO_PATHS = ("/api/auth/info", "/auth/info")


def _urllib_get_json(url: str, token: str, timeout: int) -> dict | None:
    """GET url with Bearer。200 → dict；404 → None（试下一前缀）；其他 HTTPError → 抛。"""
    import urllib.error
    import urllib.request

    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return None
        raise


def resolve_member(token: str, base_url: str, timeout: int = 5,
                   fetch_json=None) -> str | None:
    """集市 Bearer JWT → username。任何失败 → None（fail closed，调用方一律 403）。"""
    if not token:
        return None
    fetch = fetch_json or _urllib_get_json
    for path in AUTH_INFO_PATHS:
        try:
            data = fetch(f"{base_url}{path}", token, timeout)
        except Exception:  # noqa: BLE001 网络坏/非 404 HTTP 错 → 拒
            return None
        if data is None:
            continue                       # 404 → 试下一个前缀
        if not isinstance(data, dict):
            return None
        data_obj = data.get("data") if isinstance(data.get("data"), dict) else {}
        user = data_obj.get("user") or data.get("user") or {}
        name = str(user.get("name") or "").strip()
        return name or None
    return None
```

`app/publisher.py` 追加（放在 `_base()` 之后）：

```python
def market_base() -> str:
    """集市后端 base（供 publisher 之外复用，含容器 IP 漂移自愈）。"""
    return _base()
```

- [ ] **Step 4: 跑测试确认通过**

Run: `cd tech-digest && .venv/Scripts/python.exe -m pytest tests/test_preference.py tests/test_publisher.py -v`
Expected: 全 PASS（test_publisher 既有用例不受影响）

- [ ] **Step 5: Commit**

```bash
git add tech-digest/app/preference.py tech-digest/app/publisher.py tech-digest/tests/test_preference.py
git commit -m "feat(digest-pref): JWT→username 身份解析（fail closed）+ 原子写 + publisher.market_base 封装"
```

---

### Task 3: prompt 软注入（`app/daily_ai.py`）

**Files:**
- Modify: `tech-digest/app/daily_ai.py`（`_build_pick_prompt` 约 179 行、`ai_pick` 约 274 行）
- Test: `tech-digest/tests/test_daily_ai.py`（追加）

**Interfaces:**
- Consumes: Task 1 的 `Preference`（只用 `.active` 与 `.text`）。
- Produces: `_build_pick_prompt(candidates: list[dict], preference: Preference | None = None) -> str`；`ai_pick(candidates: list[dict], day: str = "", preference: Preference | None = None) -> dict | None`（签名向后兼容，Task 4 调用）。

- [ ] **Step 1: 写失败测试（追加到 tests/test_daily_ai.py，文件末尾前）**

```python
class TestPickPromptPreference(unittest.TestCase):
    """人工倾向软注入（2026-09-22 spec）：加分项非硬条件，默认模式 prompt 逐字不变。"""

    def test_prefer_injects_soft_clause(self):
        from app.preference import Preference
        pref = Preference(mode="prefer", text="jev 相关内容优先")
        prompt = daily_ai._build_pick_prompt(CANDS, pref)
        self.assertIn("编辑当前倾向：jev 相关内容优先", prompt)
        self.assertIn("加分项，不是硬性条件", prompt)
        self.assertIn("必须放弃倾向", prompt)
        self.assertIn("宁可发非倾向的高价值文章", prompt)
        # 软规则在 6 条标准之后、写作规则之前
        self.assertGreater(prompt.index("深度长文 > 短讯"), -1)
        self.assertLess(prompt.index("编辑当前倾向"), prompt.index("写作规则"))

    def test_default_mode_prompt_unchanged(self):
        from app.preference import Preference
        baseline = daily_ai._build_pick_prompt(CANDS)
        self.assertEqual(daily_ai._build_pick_prompt(CANDS, None), baseline)
        self.assertEqual(daily_ai._build_pick_prompt(CANDS, Preference()), baseline)
        self.assertEqual(
            daily_ai._build_pick_prompt(CANDS, Preference(mode="prefer", text="")),
            baseline)

    def test_ai_pick_passes_preference_into_prompt(self):
        captured = {}
        real_build = daily_ai._build_pick_prompt

        def spy(candidates, preference=None):
            captured["preference"] = preference
            return real_build(candidates, preference)

        pref = Preference(mode="prefer", text="jev 相关内容优先")
        with mock.patch.object(daily_ai, "_build_pick_prompt", spy), _ai(GOOD_JSON):
            daily_ai.ai_pick(CANDS, "2026-09-22", pref)
        self.assertIs(captured["preference"], pref)
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd tech-digest && .venv/Scripts/python.exe -m pytest tests/test_daily_ai.py::TestPickPromptPreference -v`
Expected: FAIL（`_build_pick_prompt() takes 1 positional argument but 2 were given`）

- [ ] **Step 3: 最小实现**

`app/daily_ai.py` 顶部 import 区加：

```python
from app.preference import Preference
```

`_build_pick_prompt` 改为（注意只在 criteria 6 与「写作规则」之间插入，其余逐字不动）：

```python
def _build_pick_prompt(candidates: list[dict],
                       preference: Preference | None = None) -> str:
    prefer_clause = ""
    if preference is not None and preference.active:
        prefer_clause = (
            f"7. 编辑当前倾向：{preference.text}。这是**加分项，不是硬性条件**："
            "倾向话题的候选若今天质量差（过气、太浅、擦边、与学习无关），"
            "必须放弃倾向，仍按上述 1-6 条标准挑今天最高价值的一篇。"
            "宁可发非倾向的高价值文章，不发倾向的低质量文章。\n"
        )
    return (
        ...  # 原有 f-string 主体完全不动，仅在
        "6. 深度长文 > 短讯：这篇是要「全文转载」的，太浅的文章撑不起来。\n"
        f"{prefer_clause}"
        "\n写作规则：\n"
        ...  # 其余原样
    )
```

（实施时以编辑而非重写方式进行：在 `"6. 深度长文 > 短讯...\n"` 行与 `"\n写作规则：\n"` 行之间插入 `f"{prefer_clause}"` 一行，函数签名与开头 `prefer_clause` 构造按上文。）

`ai_pick` 改为：

```python
def ai_pick(candidates: list[dict], day: str = "",
            preference: Preference | None = None) -> dict | None:
    """挑 1 篇爆文。LLM 失败/校验不过 → None。"""
    if not candidates:
        return None
    content = llm.chat(_build_pick_prompt(candidates, preference), system=SYSTEM_PROMPT)
    ...  # 其余原样
```

- [ ] **Step 4: 跑测试确认通过**

Run: `cd tech-digest && .venv/Scripts/python.exe -m pytest tests/test_daily_ai.py -v`
Expected: 全 PASS（含既有 TestAiPick/TestPrompt 用例——它们证明默认路径未变）

- [ ] **Step 5: Commit**

```bash
git add tech-digest/app/daily_ai.py tech-digest/tests/test_daily_ai.py
git commit -m "feat(digest-pref): 挑选 prompt 软注入第 7 条——倾向是加分项，质量差必须弃倾向"
```

---

### Task 4: `main.py` 接线（读偏好 + 日志 + store 记录）

**Files:**
- Modify: `tech-digest/main.py`（`_pick_article` 约 122 行、`run_daily` 约 279-335 行）
- Test: `tech-digest/tests/test_main.py`（追加）

**Interfaces:**
- Consumes: Task 1 `load_preference/Preference`、Task 3 `ai_pick(..., preference=)`。
- Produces: `_pick_article(candidates: list[dict], day: str, preference: Preference | None = None)`；store daily 记录 detail 新增 `pref_mode: str`、`pref_text: str`（截 30 字）。

- [ ] **Step 1: 写失败测试（追加到 tests/test_main.py）**

```python
class TestPickArticlePreference(unittest.TestCase):
    """倾向透传：_pick_article 必须把 preference 原样交给 ai_pick。"""

    def test_preference_passed_to_ai_pick(self):
        from app import daily_ai, preference
        captured = {}

        def fake_pick(candidates, day, preference=None):
            captured["preference"] = preference
            return None

        pref = preference.Preference(mode="prefer", text="jev 相关内容优先")
        with patch.object(daily_ai, "ai_pick", fake_pick):
            main._pick_article([{"title": "T", "source": "hacker-news"}],
                               "2026-09-22", pref)
        self.assertIs(captured["preference"], pref)

    def test_none_preference_still_calls_ai_pick(self):
        from app import daily_ai
        captured = {}

        def fake_pick(candidates, day, preference=None):
            captured["preference"] = preference
            return None

        with patch.object(daily_ai, "ai_pick", fake_pick):
            main._pick_article([{"title": "T", "source": "hacker-news"}],
                               "2026-09-22", None)
        self.assertIsNone(captured["preference"])
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd tech-digest && .venv/Scripts/python.exe -m pytest tests/test_main.py::TestPickArticlePreference -v`
Expected: FAIL（`_pick_article() takes 2 positional arguments but 3 were given`）

- [ ] **Step 3: 最小实现**

`main.py`：

1. import 区加 `from app import preference`（与现有 `from app import ...` 并列）。
2. `_pick_article` 改签名并透传：

```python
def _pick_article(candidates: list[dict], day: str,
                  preference: preference.Preference | None = None) -> tuple[dict | None, dict | None, bool]:
    """AI 挑 1 篇爆文。失败 → 首条候选 + 空按语（不缺席，只降级）。降级路径不掺倾向。"""
    pick = None
    if _has_ai() and candidates:
        try:
            pick = daily_ai.ai_pick(candidates, day, preference=preference)
        except Exception as e:  # noqa: BLE001 AI 是增强项，失败不阻塞发帖
            log.warning("AI 挑选失败（降级首条候选）: %s", e)
            pick = None
    if pick:
        item = candidates[pick["idx"] - 1]
        return item, pick, True
    return candidates[0] if candidates else None, None, False
```

3. `run_daily` 中 `candidates = _daily_candidates(news)` 之前加：

```python
        pref = preference.load_preference()
        if pref.active:
            log.info("倾向模式: prefer（%s…）", pref.text[:30])
        else:
            log.info("倾向模式: 默认")
```

并把 `item, pick, ai_used = _pick_article(candidates, day)` 改为：

```python
        item, pick, ai_used = _pick_article(candidates, day, pref)
```

4. `store.log("daily", ...)` 的 detail dict 里加两个字段（放在 `"pick_title"` 旁）：

```python
            "pref_mode": "prefer" if pref.active else "default",
            "pref_text": pref.text[:30],
```

- [ ] **Step 4: 跑测试确认通过 + 全量回归**

Run: `cd tech-digest && .venv/Scripts/python.exe -m pytest tests/test_main.py -v && .venv/Scripts/python.exe -m pytest tests/ -q`
Expected: 全 PASS

- [ ] **Step 5: Commit**

```bash
git add tech-digest/main.py tech-digest/tests/test_main.py
git commit -m "feat(digest-pref): run_daily 读偏好透传 ai_pick，日志与 store 记录带倾向状态"
```

---

### Task 5: 面板偏好 API（`manual_server.py` GET/POST + 审计）

**Files:**
- Modify: `tech-digest/manual_server.py`（Handler `do_GET` 约 132 行、`do_POST` 约 158 行；文件级新增 6 个辅助函数）
- Test: `tech-digest/tests/test_pref_api.py`

**Interfaces:**
- Consumes: Task 1/2 的 `load_preference/save_preference/resolve_member/is_admin`、`publisher.market_base()`、`app.store.Store.daily_post_id`。
- Produces: HTTP 契约——`GET /api/v1/digest-pref` → `200 {"mode","text","updated_at","updated_by","daily_post_id"}`（`daily_post_id`=最近一期日报帖 ID：先查当天、无则查前一日，均无为 null）或 `403`；`POST`（body `{"mode","text"}`）→ `200`（回写后全量 JSON，`daily_post_id` 为 null）或 `400`（参数）或 `403`（鉴权）。模块级可测函数：`handle_pref_get(auth_header: str) -> tuple[int, str]`、`handle_pref_post(auth_header: str, body: bytes) -> tuple[int, str]`、`_latest_daily_post_id() -> int | None`（store 经 `_store_for_pref()` 注入）。

- [ ] **Step 1: 写失败测试**

创建 `tech-digest/tests/test_pref_api.py`：

```python
# -*- coding: utf-8 -*-
"""偏好 API 单测：白名单 fail closed + 原子写 + 审计行。"""
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import manual_server
from app import preference


class PrefApiTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.pref_path = Path(self.tmp.name) / "preference.json"
        self.log_path = Path(self.tmp.name) / "tech-digest.log"
        p1 = mock.patch.object(manual_server, "LOG_FILE", self.log_path)
        p1.start()
        self.addCleanup(p1.stop)
        p2 = mock.patch.object(preference, "preference_path",
                               return_value=self.pref_path)
        p2.start()
        self.addCleanup(p2.stop)
        # daily_post_id 查询默认打桩为 None，避免单测触碰真实 store
        p3 = mock.patch.object(manual_server, "_store_for_pref")
        m_store = p3.start()
        m_store.return_value.daily_post_id.return_value = None
        self.addCleanup(p3.stop)

    def _allow(self, name="Rimuru"):
        p = mock.patch.object(preference, "resolve_member", return_value=name)
        p.start()
        self.addCleanup(p.stop)
        e = mock.patch.dict(os.environ, {"DIGEST_PREF_ADMINS": name})
        e.start()
        self.addCleanup(e.stop)

    def _deny(self):
        p = mock.patch.object(preference, "resolve_member", return_value=None)
        p.start()
        self.addCleanup(p.stop)

    # ---- GET ----

    def test_get_403_without_token(self):
        code, _ = manual_server.handle_pref_get("")
        self.assertEqual(code, 403)

    def test_get_403_when_resolve_fails(self):
        self._deny()
        code, _ = manual_server.handle_pref_get("Bearer bad")
        self.assertEqual(code, 403)

    def test_get_403_when_not_admin(self):
        p = mock.patch.object(preference, "resolve_member", return_value="路人")
        p.start()
        self.addCleanup(p.stop)
        with mock.patch.dict(os.environ, {"DIGEST_PREF_ADMINS": "Rimuru"}):
            code, _ = manual_server.handle_pref_get("Bearer ok")
        self.assertEqual(code, 403)

    def test_get_returns_default_when_uninitialized(self):
        self._allow()
        code, body = manual_server.handle_pref_get("Bearer ok")
        self.assertEqual(code, 200)
        data = json.loads(body)
        self.assertEqual(data["mode"], "default")
        self.assertEqual(data["text"], "")

    def test_get_includes_latest_daily_post_id(self):
        self._allow()
        with mock.patch.object(manual_server, "_latest_daily_post_id",
                               return_value=12345):
            code, body = manual_server.handle_pref_get("Bearer ok")
        self.assertEqual(code, 200)
        self.assertEqual(json.loads(body)["daily_post_id"], 12345)

    def test_latest_daily_post_id_falls_back_to_yesterday(self):
        from datetime import date, timedelta
        today = date.today().isoformat()
        yesterday = (date.today() - timedelta(days=1)).isoformat()
        with mock.patch.object(manual_server, "_store_for_pref") as mf:
            store = mock.MagicMock()
            store.daily_post_id.side_effect = lambda d: 777 if d == yesterday else None
            mf.return_value = store
            self.assertEqual(manual_server._latest_daily_post_id(), 777)
            store.daily_post_id.assert_any_call(today)
            store.daily_post_id.assert_any_call(yesterday)

    def test_latest_daily_post_id_none_when_store_raises(self):
        with mock.patch.object(manual_server, "_store_for_pref",
                               side_effect=RuntimeError("db broken")):
            self.assertIsNone(manual_server._latest_daily_post_id())

    # ---- POST ----

    def test_post_roundtrip_and_audit(self):
        self._allow()
        code, body = manual_server.handle_pref_post(
            "Bearer ok", json.dumps({"mode": "prefer", "text": "jev 相关"}).encode("utf-8"))
        self.assertEqual(code, 200)
        on_disk = json.loads(self.pref_path.read_text(encoding="utf-8"))
        self.assertEqual(on_disk["mode"], "prefer")
        self.assertEqual(on_disk["updated_by"], "Rimuru")
        audit = self.log_path.read_text(encoding="utf-8")
        self.assertIn("[pref-audit] Rimuru mode=prefer text=jev 相关", audit)

    def test_post_400_bad_mode(self):
        self._allow()
        code, msg = manual_server.handle_pref_post(
            "Bearer ok", json.dumps({"mode": "always", "text": "x"}).encode("utf-8"))
        self.assertEqual(code, 400)
        self.assertIn("mode", msg)

    def test_post_400_prefer_empty_text(self):
        self._allow()
        code, _ = manual_server.handle_pref_post(
            "Bearer ok", json.dumps({"mode": "prefer", "text": "  "}).encode("utf-8"))
        self.assertEqual(code, 400)

    def test_post_400_non_json_body(self):
        self._allow()
        code, _ = manual_server.handle_pref_post("Bearer ok", b"not json")
        self.assertEqual(code, 400)

    def test_post_403_when_resolve_raises(self):
        p = mock.patch.object(preference, "resolve_member",
                              side_effect=RuntimeError("boom"))
        p.start()
        self.addCleanup(p.stop)
        code, _ = manual_server.handle_pref_post(
            "Bearer ok", json.dumps({"mode": "prefer", "text": "x"}).encode("utf-8"))
        self.assertEqual(code, 403)

    def test_post_403_when_not_admin(self):
        p = mock.patch.object(preference, "resolve_member", return_value="路人")
        p.start()
        self.addCleanup(p.stop)
        with mock.patch.dict(os.environ, {"DIGEST_PREF_ADMINS": "Rimuru"}):
            code, _ = manual_server.handle_pref_post(
                "Bearer ok", json.dumps({"mode": "prefer", "text": "x"}).encode("utf-8"))
        self.assertEqual(code, 403)
        self.assertFalse(self.pref_path.exists())   # 拒绝时绝不落盘
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd tech-digest && .venv/Scripts/python.exe -m pytest tests/test_pref_api.py -v`
Expected: FAIL（`AttributeError: module 'manual_server' has no attribute 'handle_pref_get'`）

- [ ] **Step 3: 最小实现**

`manual_server.py` 顶部 import 区补 `from datetime import datetime`，并在 `run_task` 定义之后、`class Handler` 之前插入：

```python
PREF_PATH = "/api/v1/digest-pref"


def _pref_base_url() -> str:
    """集市后端 base：复用 publisher 的容器 IP 漂移自愈；导入失败兜底本机直连。"""
    try:
        from app.publisher import market_base
        return market_base()
    except Exception:  # noqa: BLE001
        return "http://127.0.0.1:8080"


def _pref_member(auth_header: str) -> str | None:
    """Bearer JWT → 白名单成员名；任何不通过 → None。"""
    token = (auth_header or "")[7:].strip() if (auth_header or "").startswith("Bearer ") else ""
    from app.preference import is_admin, resolve_member
    try:
        name = resolve_member(token, _pref_base_url())
    except Exception:  # noqa: BLE001 双保险 fail closed
        return None
    return name if is_admin(name) else None


def _pref_audit(line: str) -> None:
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    try:
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(f"{stamp} [pref-audit] {line}\n")
    except OSError:
        print(f"[pref-audit] {line}")


def _pref_payload(pref, daily_post_id: int | None = None) -> str:
    return json.dumps({"mode": pref.mode, "text": pref.text,
                       "updated_at": pref.updated_at,
                       "updated_by": pref.updated_by,
                       "daily_post_id": daily_post_id}, ensure_ascii=False)


def _store_for_pref():
    from app.store import Store
    return Store()


def _latest_daily_post_id() -> int | None:
    """最近一期日报帖 ID：先查当天、无则查前一日；store 异常一律 None（GET 不因此 500）。"""
    try:
        from datetime import date, timedelta
        store = _store_for_pref()
        today = date.today()
        return (store.daily_post_id(today.isoformat())
                or store.daily_post_id((today - timedelta(days=1)).isoformat()))
    except Exception:  # noqa: BLE001
        return None


def handle_pref_get(auth_header: str) -> tuple[int, str]:
    if _pref_member(auth_header) is None:
        return 403, "无权限"
    from app.preference import load_preference
    return 200, _pref_payload(load_preference(), _latest_daily_post_id())


def handle_pref_post(auth_header: str, body: bytes) -> tuple[int, str]:
    member = _pref_member(auth_header)
    if member is None:
        return 403, "无权限"
    try:
        raw = json.loads((body or b"").decode("utf-8"))
        if not isinstance(raw, dict):
            raise ValueError("body 必须为 JSON 对象")
    except (ValueError, UnicodeDecodeError):
        return 400, "body 必须为 JSON 对象"
    from app.preference import save_preference
    try:
        pref = save_preference(str(raw.get("mode") or ""),
                               str(raw.get("text") or ""), updated_by=member)
    except ValueError as e:
        return 400, str(e)
    _pref_audit(f"{member} mode={pref.mode} text={pref.text}")
    return 200, _pref_payload(pref)
```

`Handler.do_GET` 在 `if path == "/api/log":` 块之前插入：

```python
        if path == PREF_PATH:
            code, resp = handle_pref_get(self.headers.get("Authorization", ""))
            return self._send(code, resp)
```

`Handler.do_POST` 整体改为（原 /api/run 逻辑原样保留在后半段）：

```python
    def do_POST(self):  # noqa: N802
        path = urlparse(self.path).path
        if path == PREF_PATH:
            length = int(self.headers.get("Content-Length") or 0)
            body = self.rfile.read(min(length, 8192)) if length > 0 else b""
            code, resp = handle_pref_post(self.headers.get("Authorization", ""), body)
            return self._send(code, resp)
        if path != "/api/run":
            return self._send(404, "not found")
        qs = parse_qs(urlparse(self.path).query)
        task = qs.get("task", ["daily"])[0]
        force = qs.get("force", ["0"])[0] == "1"
        if task not in ("daily", "weekly"):
            return self._send(400, "task 必须为 daily/weekly")
        result = run_task(task, force)
        return self._send(200, json.dumps(result, ensure_ascii=False))
```

- [ ] **Step 4: 跑测试确认通过 + 全量回归**

Run: `cd tech-digest && .venv/Scripts/python.exe -m pytest tests/test_pref_api.py -v && .venv/Scripts/python.exe -m pytest tests/ -q`
Expected: 全 PASS

- [ ] **Step 5: Commit**

```bash
git add tech-digest/manual_server.py tech-digest/tests/test_pref_api.py
git commit -m "feat(digest-pref): 面板进程新增偏好 API——JWT 白名单鉴权 fail closed + 原子写 + 审计 + daily_post_id"
```

---

### Task 6: tech-digest 服务器部署 + 真实冒烟

**Files:** 无新代码。部署 Task 1-5 的产物到服务器。

**Interfaces:**
- Consumes: 本地已全绿的 tech-digest 代码。
- Produces: 服务器上 API 可用（仅 127.0.0.1），`data/preference.json` 可写。

- [ ] **Step 1: 本地全量测试**

Run: `cd tech-digest && .venv/Scripts/python.exe -m pytest tests/ -q`
Expected: 全绿（基线 432 + 新增 ≈25）

- [ ] **Step 2: scp 逐文件上传**

```bash
cd "C:/Users/<user>/Desktop/自动巡检agent/tech-digest"
scp app/preference.py root@<SERVER_IP>:/root/market-deploy/agent/tech-digest/app/
scp app/daily_ai.py app/publisher.py root@<SERVER_IP>:/root/market-deploy/agent/tech-digest/app/
scp main.py manual_server.py root@<SERVER_IP>:/root/market-deploy/agent/tech-digest/
scp tests/test_preference.py tests/test_pref_api.py tests/test_daily_ai.py tests/test_main.py root@<SERVER_IP>:/root/market-deploy/agent/tech-digest/tests/
```

- [ ] **Step 3: 服务器全量测试**

```bash
ssh root@<SERVER_IP> "cd /root/market-deploy/agent/tech-digest && .venv/bin/python3 -m pytest tests/ -q"
```
Expected: 全绿。红了先修再继续，不得带病部署。

- [ ] **Step 4: 重启面板进程**

```bash
ssh root@<SERVER_IP> "cd /root/market-deploy/agent/tech-digest && bash scripts/manual-panel.sh stop; bash scripts/manual-panel.sh start && sleep 1 && bash scripts/manual-panel.sh status"
```
Expected: status 显示运行中。

- [ ] **Step 5: 真实 JWT 冒烟（走 publisher 的轮换通道）**

```bash
ssh root@<SERVER_IP> "cd /root/market-deploy/agent/tech-digest && TOKEN=\$(.venv/bin/python3 -c \"from app.publisher import refresh_access_token; print(refresh_access_token()[0])\") && curl -s -o /dev/null -w 'no-token:%{http_code}\n' http://127.0.0.1:19086/api/v1/digest-pref && curl -s -H \"Authorization: Bearer \$TOKEN\" http://127.0.0.1:19086/api/v1/digest-pref && echo && curl -s -X POST -H \"Authorization: Bearer \$TOKEN\" -H 'Content-Type: application/json' -d '{\"mode\":\"prefer\",\"text\":\"测试倾向，稍后恢复\"}' http://127.0.0.1:19086/api/v1/digest-pref && echo && curl -s -H \"Authorization: Bearer \$TOKEN\" http://127.0.0.1:19086/api/v1/digest-pref"
```
Expected 依次：`no-token:403`；GET 返回 `{"mode": "default", ..., "daily_post_id": <数字或null>}`；POST 返回 prefer 全量 JSON；再 GET 返回 prefer。若 POST 返回 403，说明该账号不在服务端白名单——检查 env `DIGEST_PREF_ADMINS`（默认 Rimuru，发帖机器人账号不是 Rimuru 的话本步 403 属预期，改用 `-d '{"mode":"default","text":""}'` 确认 400/200 逻辑即可，身份校验正确性已由单测覆盖）。

- [ ] **Step 6: 恢复默认 + 验证审计行**

```bash
ssh root@<SERVER_IP> "cd /root/market-deploy/agent/tech-digest && TOKEN=\$(.venv/bin/python3 -c \"from app.publisher import refresh_access_token; print(refresh_access_token()[0])\") && curl -s -X POST -H \"Authorization: Bearer \$TOKEN\" -H 'Content-Type: application/json' -d '{\"mode\":\"default\",\"text\":\"\"}' http://127.0.0.1:19086/api/v1/digest-pref && echo && tail -2 log/tech-digest.log"
```
Expected: 返回 `mode=default`；日志尾有 `[pref-audit] ... mode=default` 行。

注意：此步会多轮换一次 refresh_token——`refresh_access_token()` 自带持久化（600 权限），是仓库钦定通道，安全。

---

### Task 7: nginx 反代（公网路径，仅此一条）

**Files:** 服务器 nginx 配置（只改一处，不动仓库文件）。

- [ ] **Step 1: 定位 assist 反代所在 server 块**

```bash
ssh root@<SERVER_IP> "grep -rn 'api/v1/assist' /etc/nginx/ 2>/dev/null | head -5"
```

- [ ] **Step 2: 在同一 server 块、assist location 旁边追加**

先备份：`ssh root@<SERVER_IP> "cp -a /etc/nginx/nginx.conf /etc/nginx/nginx.conf.bak.digestpref"`（若 assist 配置在 sites-available/conf.d 子文件，则备份并改那个文件）。追加：

```nginx
    location /api/v1/digest-pref {
        proxy_pass http://127.0.0.1:19086;
        proxy_set_header Authorization $http_authorization;
    }
```

（路径不重写——面板端按 `/api/v1/digest-pref` 原样匹配；面板其余端点不经 nginx，继续仅 127.0.0.1 可达。）

- [ ] **Step 3: 语法检查 + reload + 公网冒烟**

```bash
ssh root@<SERVER_IP> "nginx -t && systemctl reload nginx && curl -s -o /dev/null -w 'public-no-token:%{http_code}\n' https://<MARKET_DOMAIN>/api/v1/digest-pref && curl -s -o /dev/null -w 'public-run-endpoint:%{http_code}\n' https://<MARKET_DOMAIN>/api/run"
```
Expected: `nginx -t` OK；`public-no-token:403`；`public-run-endpoint:404`（/api/run 不可从公网达，证明暴露面只有偏好路径）。

---

### Task 8: 前端配置与 API 模块（`_fe/`）

**Files:**
- Create: `_fe/src/config/digestPref.ts`
- Create: `_fe/src/api/digestPref.ts`

**Interfaces:**
- Produces: `isDigestPrefEnabled(username: string): boolean`、`digestPrefBaseUrl(): string`、`getDigestPref(): Promise<DigestPrefData | null>`、`saveDigestPref(mode: 'default' | 'prefer', text: string): Promise<string | null>`（null=成功，否则错误文案）、`DigestPrefData { mode, text, updated_at, updated_by, daily_post_id: number | null }`。Task 9 的弹窗与帖内挂点消费这些名字。

- [ ] **Step 1: 创建 `src/config/digestPref.ts`（与 assist.ts 完全同构）**

```ts
/**
 * 日报人工倾向 —— 灰度开关与接口地址（2026-09-22 spec）。
 *
 * 全量上线前由 VITE_DIGEST_PREF_ENABLED（编译开关）+ 账号白名单控制可见性；
 * 调试体验：控制台执行 localStorage.setItem('digestPref.force', '1') 后刷新即可，无需重建。
 * 服务端另有权威白名单（DIGEST_PREF_ADMINS env），此处只管入口可见性。
 */
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

- [ ] **Step 2: 创建 `src/api/digestPref.ts`**

```ts
import { ensureAccessToken } from '@/api/req'
import { digestPrefBaseUrl } from '@/config/digestPref'
import { useUserStore } from '@/store/userStore'

export interface DigestPrefData {
  mode: 'default' | 'prefer'
  text: string
  updated_at: string
  updated_by: string
  daily_post_id: number | null
}

async function authHeaders(): Promise<Record<string, string>> {
  await ensureAccessToken()
  const store = useUserStore()
  return {
    Authorization: `Bearer ${store.token.value || ''}`,
    'Content-Type': 'application/json',
  }
}

/** 读当前倾向。失败返回 null（调用方展示默认态）。 */
export async function getDigestPref(): Promise<DigestPrefData | null> {
  try {
    const res = await fetch(digestPrefBaseUrl(), { method: 'GET', headers: await authHeaders() })
    if (!res.ok)
      return null
    return (await res.json()) as DigestPrefData
  }
  catch {
    return null
  }
}

/** 保存倾向。返回 null=成功，否则错误文案。 */
export async function saveDigestPref(mode: 'default' | 'prefer', text: string): Promise<string | null> {
  try {
    const res = await fetch(digestPrefBaseUrl(), {
      method: 'POST',
      headers: await authHeaders(),
      body: JSON.stringify({ mode, text }),
    })
    if (!res.ok) {
      const detail = (await res.text()).trim()
      return detail && detail.length < 200 ? detail : `保存失败（${res.status}）`
    }
    return null
  }
  catch (e) {
    return `保存失败：${e instanceof Error ? e.message : '网络错误'}`
  }
}
```

- [ ] **Step 3: Commit（部署脚本随 Task 10 提交；_fe 本身不入 git）**

本任务无 git 提交。自查两个文件能通过 TS 编译即可（Task 10 服务器构建会跑 `vue-tsc`）。

---

### Task 9: 帖内入口按钮 + 管理弹窗（PostDetailView 挂点）

**Files:**
- Create: `_fe/src/components/DigestPrefModal.vue`
- Modify: `_fe/src/views/PostDetailView.vue`（帖内挂点：正文后按钮 + script 装配）

**Interfaces:**
- Consumes: Task 8 全部导出（`isDigestPrefEnabled`、`getDigestPref`、`DigestPrefData`）；`useUserStore().userInfo.name`；`showMsg(msg: string)`（`@/components/MessageBox`，单参数）。
- Produces: `DigestPrefModal.vue`（props：`data: DigestPrefData | null`；emits：`close`）；PostDetailView 内三个 ref：`digestBtnVisible` / `digestPrefData` / `showDigestPrefModal`。

**呈现逻辑（2026-09-23 spec 修订）：** 按钮=可见性，服务端白名单=权威。仅当 `isDigestPrefEnabled(username)` 且 当前帖 ID === GET 返回的 `daily_post_id` 时显示「调整日报倾向」按钮；点击懒加载弹窗，弹窗内完成查看/修改/保存。独立管理页、路由、侧边栏入口全部取消。

- [ ] **Step 0: 摸挂点（PostDetailView 源码探查）**

`_fe/src/views/PostDetailView.vue` 当前不在本地工作副本（部署时同步或从服务器取回）。开工先探明三件事：

1. 帖子 ID 的取法：route 参数（`/postdetail/:id`）或 `post.postID`，以源码实际为准；
2. 按钮插入位置：帖子正文容器之后、评论区之前（或现有操作按钮容器），PC/PWA 共用同一视图；
3. 弹窗不依赖 UI 库：集市组件均为手写（MessageBox toast、drawer-mask 风格），弹窗按同风格手写 fixed 遮罩。

Step 0 探查完成后，以下代码为**意图示例**（变量名以源码实际为准），按意图落地。

- [ ] **Step 1: 创建 `src/components/DigestPrefModal.vue`**

```vue
<script lang="ts" setup>
import { ref } from 'vue'
import { saveDigestPref } from '@/api/digestPref'
import type { DigestPrefData } from '@/api/digestPref'
import { showMsg } from '@/components/MessageBox'

const props = defineProps<{ data: DigestPrefData | null }>()
const emit = defineEmits<{ (e: 'close'): void }>()

const saving = ref(false)
const mode = ref<'default' | 'prefer'>(props.data?.mode === 'prefer' ? 'prefer' : 'default')
const text = ref(props.data?.text || '')
const meta = ref({
  updated_at: props.data?.updated_at || '',
  updated_by: props.data?.updated_by || '',
})

async function onSave() {
  if (mode.value === 'prefer' && !text.value.trim()) {
    showMsg('倾向内容不能为空')
    return
  }
  saving.value = true
  const err = await saveDigestPref(mode.value, text.value.trim())
  saving.value = false
  if (err)
    showMsg(err)
  else
    showMsg('已保存，下一次日报生成（每日 09:15）生效')
  emit('close')
}
</script>

<template>
  <div class="dpref-mask" @click.self="emit('close')">
    <div class="dpref-box">
      <div class="dpref-head">
        <span>调整日报倾向</span>
        <button class="dpref-x" type="button" @click="emit('close')">
          ✕
        </button>
      </div>
      <p v-if="meta.updated_by" class="dpref-meta">
        当前生效：最后由 {{ meta.updated_by }} 于 {{ meta.updated_at }} 修改
      </p>
      <p v-else class="dpref-meta">
        当前生效：默认模式（尚未设置过倾向）
      </p>
      <label class="dpref-opt">
        <input v-model="mode" type="radio" value="default">
        默认模式（按既有标准挑最高价值文章）
      </label>
      <label class="dpref-opt">
        <input v-model="mode" type="radio" value="prefer">
        倾向模式（优先考虑下方倾向内容）
      </label>
      <textarea
        v-model="text"
        :disabled="mode !== 'prefer'"
        maxlength="200"
        rows="4"
        placeholder="例：jev 相关内容优先"
      />
      <div class="dpref-counter">
        {{ text.length }}/200
      </div>
      <button class="dpref-save" type="button" :disabled="saving" @click="onSave">
        {{ saving ? '保存中…' : '保存' }}
      </button>
      <p class="dpref-hint">
        ⓘ 倾向只是加分项：倾向话题当天没有高质量文章时，日报仍会选其他最高价值的一篇。
      </p>
    </div>
  </div>
</template>

<style scoped>
.dpref-mask {
  position: fixed;
  inset: 0;
  background: rgba(0, 0, 0, 0.45);
  z-index: 1000;
  display: flex;
  align-items: center;
  justify-content: center;
  padding: 16px;
}
.dpref-box {
  background: #fff;
  border-radius: 12px;
  max-width: 420px;
  width: 100%;
  padding: 16px;
  box-sizing: border-box;
}
.dpref-head {
  display: flex;
  justify-content: space-between;
  align-items: center;
  font-size: 16px;
  font-weight: 600;
  margin-bottom: 8px;
}
.dpref-x {
  background: none;
  border: 0;
  font-size: 14px;
  cursor: pointer;
  color: #8b93a5;
}
.dpref-meta {
  color: #8b93a5;
  font-size: 12px;
  margin: 0 0 12px;
}
.dpref-opt {
  display: block;
  margin: 8px 0;
  cursor: pointer;
  font-size: 14px;
}
textarea {
  width: 100%;
  box-sizing: border-box;
  margin-top: 4px;
  border: 1px solid #d1d5db;
  border-radius: 8px;
  padding: 10px;
  font-size: 14px;
  resize: vertical;
}
.dpref-counter {
  text-align: right;
  color: #8b93a5;
  font-size: 12px;
  margin: 4px 0 12px;
}
.dpref-save {
  background: #2d6cdf;
  color: #fff;
  border: 0;
  border-radius: 8px;
  padding: 10px 24px;
  font-size: 14px;
  cursor: pointer;
}
.dpref-save:disabled {
  opacity: 0.5;
}
.dpref-hint {
  color: #8b93a5;
  font-size: 12px;
  margin: 12px 0 0;
}
</style>
```

（深色模式：若集市为 `body.dark-mode` 全局方案，`.dpref-box` 可按源码现有暗色写法加深底适配；白底弹窗 + 深遮罩首版可接受。）

- [ ] **Step 2: PostDetailView 挂点（script 区）**

import + 装配（帖子 ID 的取法以 Step 0 探查为准，下例假设 `route.params.id`，备选 `post.postID`）：

```ts
import { defineAsyncComponent, onMounted, ref } from 'vue'
import { useRoute } from 'vue-router'
import { getDigestPref } from '@/api/digestPref'
import type { DigestPrefData } from '@/api/digestPref'
import { isDigestPrefEnabled } from '@/config/digestPref'
import { useUserStore } from '@/store/userStore'

const DigestPrefModal = defineAsyncComponent(() => import('@/components/DigestPrefModal.vue'))

const route = useRoute()
const { userInfo } = useUserStore()

const digestBtnVisible = ref(false)
const digestPrefData = ref<DigestPrefData | null>(null)
const showDigestPrefModal = ref(false)

function currentPostId(): number | null {
  const raw = route.params.id ?? route.params.postId
    ?? (post as any)?.value?.postID ?? (post as any)?.postID
  const n = Number(raw)
  return Number.isFinite(n) && n > 0 ? n : null
}

async function refreshDigestPref() {
  if (!isDigestPrefEnabled(userInfo.name))
    return
  const data = await getDigestPref()
  digestPrefData.value = data
  const pid = currentPostId()
  digestBtnVisible.value = !!data?.daily_post_id && pid === Number(data.daily_post_id)
}

onMounted(refreshDigestPref)
```

关键点：非白名单用户在此**提前返回**——不发任何 digest-pref 请求、不加载弹窗 chunk（defineAsyncComponent 惰性）；GET 失败（面板挂）→ `data` 为 null → 按钮不出现（优雅降级）。

- [ ] **Step 3: PostDetailView 挂点（template 区）**

帖子正文容器之后、评论区之前插入（容器类名以 Step 0 探查为准）：

```vue
    <button
      v-if="digestBtnVisible"
      class="digest-pref-entry"
      type="button"
      @click="showDigestPrefModal = true"
    >
      调整日报倾向
    </button>
    <DigestPrefModal
      v-if="showDigestPrefModal"
      :data="digestPrefData"
      @close="onPrefModalClose"
    />
```

script 区补关闭处理（重拉一次，保存后的回显/按钮状态保持同步）：

```ts
async function onPrefModalClose() {
  showDigestPrefModal.value = false
  await refreshDigestPref()
}
```

CSS：`.digest-pref-entry` 与帖子现有次级操作风格对齐（描边/浅底，弱于主操作），写法照源码现有变量。

- [ ] **Step 4: 代码自查**

核对：flag=false 或非白名单 → 无请求、无按钮、无弹窗 chunk 加载；`daily_post_id` 为 null（首期日报前）→ 按钮不出现；旧日报帖 / star / weekly 帖 → ID 不匹配 → 按钮不出现；弹窗关闭后回显与按钮状态已刷新。

- [ ] **Step 5: Commit**

`_fe/` 不入 git（现有工作流如此）。本任务无 git 提交。

---

### Task 10: 前端部署阶段①（编译开关关闭）+ 公网验证

**Files:**
- Create: `_fe_deploy_digest_pref.py`（仓库根目录，仿 `_fe_deploy_whitelist.py`）

**Interfaces:**
- Consumes: Task 8/9 的 4 个本地文件路径。
- Produces: 公网构建产物（flag=false，功能对所有人不存在/无权限）；Task 11 直接改构建 env 复用本脚本。

- [ ] **Step 1: 写部署脚本 `_fe_deploy_digest_pref.py`**

以 `C:\Users/<user>\Desktop\自动巡检agent\_fe_deploy_whitelist.py` 为模板（paramiko + 同 HOST/PORT/USER/KEY、同 REMOTE_SRC），上传以下文件到远端对应相对路径（保持目录结构）：

- `_fe/src/config/digestPref.ts` → `src/config/digestPref.ts`
- `_fe/src/api/digestPref.ts` → `src/api/digestPref.ts`
- `_fe/src/components/DigestPrefModal.vue` → `src/components/DigestPrefModal.vue`
- `_fe/src/views/PostDetailView.vue` → `src/views/PostDetailView.vue`

脚本流程（与模板一致）：路径确认 → sftp 逐文件上传 → `cp -a dist dist.bak.<TS>`（TS 如 `20260922_digestpref_stage1`）→ `cd REMOTE_SRC && VITE_DIGEST_PREF_ENABLED=false npm run build`（timeout 600）→ dist 资产 hash + `curl -s https://<MARKET_DOMAIN>/new/ | grep -oE 'assets/[^"]+\.js'` 公网确认。

注意：先在服务器上执行 `grep -rn "VITE_ASSIST_ENABLED\|VITE_DIGEST_PREF" REMOTE_SRC/.env* REMOTE_SRC/src 2>/dev/null` 看 assist 的编译开关管理方式；若 assist 是通过 `.env.production` 文件管理，则同文件追加一行 `VITE_DIGEST_PREF_ENABLED=false`，构建命令就不带前缀 env（两种方式二选一，与 assist 保持一致）。

- [ ] **Step 2: 跑部署脚本**

Run: `cd "C:/Users/<user>/Desktop/自动巡检agent" && python _fe_deploy_digest_pref.py`
（与模板 `_fe_deploy_whitelist.py` 相同的运行方式，过去已跑通；本地若 python 不带 paramiko，用既往装好 paramiko 的解释器。）
Expected: BUILD OUT 显示 vite build 完成、无 TS 报错（vue-tsc 会校验 Task 8/9 的类型）；公网 hash 与 dist 一致。

- [ ] **Step 3: 阶段①验证（浏览器，Rimuru 账号登录 <MARKET_DOMAIN>）**

1. Rimuru 登录打开任意帖子详情 → 无「调整日报倾向」按钮（flag=false 时不渲染）；
2. 控制台执行 `localStorage.setItem('digestPref.force','1')` 后刷新 → **仍无按钮**（编译开关在白名单之前）；
3. 直达 `https://<MARKET_DOMAIN>/digest-pref` → 404（路由已按新方案取消，属预期）；
4. 验证完执行 `localStorage.removeItem('digestPref.force')` 清掉调试开关；
5. 集市其余页面（首页/发帖/小秋）与帖子详情页本身回归正常。

- [ ] **Step 4: Commit 部署脚本**

```bash
git add _fe_deploy_digest_pref.py
git commit -m "chore(digest-pref): 前端部署脚本——上传偏好管理 4 文件+构建（阶段①编译开关关闭）"
```

---

### Task 11: 阶段②开白名单灰度 + 端到端验收

**Files:** 无新代码。改构建 env 后重建前端。

- [ ] **Step 1: 打开编译开关重建**

按 Task 10 确认的管理方式把 `VITE_DIGEST_PREF_ENABLED` 置 `true`（改 .env 文件，或把部署脚本构建命令的前缀 env 改为 `true`；同时把脚本内 dist 备份常量改为 `20260922_digestpref_stage2`），重跑 `_fe_deploy_digest_pref.py`。Expected: 构建成功、公网 hash 更新。

- [ ] **Step 2: 可见性验证（浏览器）**

1. Rimuru 登录 → 打开**最新一期日报帖**详情 → 帖内出现「调整日报倾向」按钮 → 点击弹窗加载，显示「默认模式（尚未设置过倾向）」；
2. 旧一期日报帖 / star / weekly 帖 → 无按钮（ID 不匹配）；
3. 非白名单账号（或无痕窗口）打开同一最新日报帖 → 无按钮；同状态 curl 公网 API → 403。

- [ ] **Step 3: 端到端——设倾向**

Rimuru 在弹窗切「倾向模式」、输入 `jev 相关内容优先`、保存 → toast「已保存，下一次日报生成（每日 09:15）生效」、弹窗关闭；重开弹窗回显 prefer + Rimuru + 时间戳。

服务器侧核对：

```bash
ssh root@<SERVER_IP> "cat /root/market-deploy/agent/tech-digest/data/preference.json && tail -2 /root/market-deploy/agent/tech-digest/log/tech-digest.log"
```
Expected: `mode=prefer, text=jev 相关内容优先, updated_by=Rimuru`；日志有 `[pref-audit] Rimuru mode=prefer` 行。

- [ ] **Step 4: 端到端——dry-run 验证软生效（不发帖）**

```bash
ssh root@<SERVER_IP> "cd /root/market-deploy/agent/tech-digest && .venv/bin/python3 main.py daily --dry-run --force 2>&1 | tail -20 && grep -E '倾向模式|pref-audit' log/tech-digest.log | tail -3 && ls -la output/$(date +%F)-daily.md"
```
Expected: 运行日志出现 `倾向模式: prefer（jev 相关内容优先…）`；产物 md 生成、全流程不炸。若 AI 恰好选了非 jev 文章，属软生效预期（prompt 注入本身已由 Task 3 单测覆盖，此处只验链路）。

- [ ] **Step 5: 端到端——切回默认模式回归**

页面切回「默认模式」保存 → 再跑一次 `main.py daily --dry-run --force` → 日志出现 `倾向模式: 默认`；store 记录核对：

```bash
ssh root@<SERVER_IP> "cd /root/market-deploy/agent/tech-digest && .venv/bin/python3 -c \"from app.store import Store; s=Store(); print([r for r in s.recent_logs('daily', 2)])\" 2>/dev/null || tail -5 log/tech-digest.log"
```
（store 查询 API 名若不符，以 `log/tech-digest.log` 尾部为准。）Expected: `pref_mode=default` 或日志「倾向模式: 默认」。

- [ ] **Step 6: 灰度观察与收尾**

- 此后每天 09:15 timer 正常跑（默认模式，行为与现状一致）；
- 观察期（建议 ≥3 天）内在最新日报帖弹窗随时切倾向试运行，重点看「该弃倾向时弃倾向」的案例；
- 扩量（spec 阶段③）：按需把成员加进前后端两处名单——前端 `DIGEST_PREF_ADMINS` 常量 + 服务器 env `DIGEST_PREF_ADMINS`——改完重跑 `_fe_deploy_digest_pref.py` 重建；
- 把验收结论与观察期记录写进 `docs/superpowers/specs/2026-09-22-digest-preference-design.md` 的文末「实施记录」小节并 commit。
