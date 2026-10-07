# 华尔街见闻源 + 镜像通道 + 判据4 + 两个新源 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让「业界亲历/行业观察」类爆文（如《我不得不把才华埋葬在昨天》）能以全文模式进入日报——通过把华尔街见闻加为直接源 + wallstreetcn SPA 文章页的内容 API 镜像通道，配合挑选判据第 4 条与量子位/月光博客两个新订阅源。

**Architecture:** wscn information-flow 列表 API 做候选源（实测两天稳定，条目自带 id/标题/作者/摘要/时间/付费标记）；`app/mirror.py` 把 wallstreetcn.com SPA 文章页确定性变换为内容 API 全文（无需任何运行时搜索——2026-09-21 实测数据中心 IP 上一切「按标题搜镜像」端点数小时内衰减，证伪搜索式发现）。订阅源走既有 feeds.py 机制。全部 fail-open，任一新环节失败都降级到现有 digest 导读，绝不阻塞发帖。

**Tech Stack:** Python 3 stdlib + requests（既有），unittest + unittest.mock.patch（既有测试栈），零新依赖。

**Spec:** 设计在本会话中经用户批准（2026-09-21「上镜像链（推荐）」）；本计划是按当日实测证据调整后的最终形态。设计结论同步写入《前沿技术周刊-技术实现文档.md》附录 B（Task 6）。

## Global Constraints

- 零新依赖（stdlib + requests，与仓库现状一致）
- fail-open：新源/镜像通道任何失败只记日志并降级，不得阻塞发帖
- 测试不打真实网络（全部 patch http_get / fetch_article）
- 只读约束：本功能只新增 GET 抓取；发帖仍走 publisher，不绕业务逻辑
- 无新凭据；不触碰 .env 内容（TECH_DIGEST_SOURCES 两端都未显式设置，用 config 默认值）
- AI 文本禁 emoji（strip_emoji 已有兜底，不新增 emoji）
- git commit 不带任何署名行
- 代码风格：`# -*- coding: utf-8 -*-` 头 + 中文 docstring + 模块级常量大写，对齐既有文件

## 实测证据（计划依据，来自 2026-09-21 服务器探测）

| 事实 | 结论 |
|---|---|
| wscn 内容 API `apiv1/content/articles/{id}?extract=0` 两天多轮稳定返回全文 | 镜像通道的取文基础 |
| wscn information-flow 列表 API 稳定（33 条，resource 内层含 id/title/author.display_name/content_short/display_time/is_paid/is_priced/resource_type） | 直接源的候选基础 |
| wscn 搜索 API、搜狗微信、360、DDG、Bing、百度等标题搜索端点在数据中心 IP 上数小时内衰减为 0 命中 | 运行时搜索式发现被证伪，不做 |
| wallstreetcn.com 文章页是 SPA（直抓空壳），知乎 403 有 zse-ck v4 JS 墙 | 前者走镜像通道；后者维持 digest 降级 |

---

### Task 1: `app/mirror.py` 镜像通道（wallstreetcn → 内容 API）

**Files:**
- Create: `tech-digest/app/mirror.py`
- Test: `tech-digest/tests/test_mirror.py`

**Interfaces:**
- Produces: `needs_mirror(url: str) -> bool`；`mirror_fetch(url: str, *, timeout: int = 12) -> dict | None`（返回 `{"text": str, "author": str}`）。Task 2 依赖这两个签名。

- [ ] **Step 1: 写失败测试**

创建 `tech-digest/tests/test_mirror.py`：

```python
# -*- coding: utf-8 -*-
"""镜像通道单测：URL 识别、内容 API 变换、短文/错码/非文章页 fail-open。"""
import unittest
from unittest.mock import patch

from app import mirror

_BODY = "<p>" + "正文内容测试段落。" * 100 + "</p>"   # 提纯后远超 500 字符


class _FakeResp:
    def __init__(self, payload):
        self._payload = payload

    def json(self):
        return self._payload


class TestNeedsMirror(unittest.TestCase):
    def test_wallstreetcn_article_true(self):
        self.assertTrue(mirror.needs_mirror("https://wallstreetcn.com/articles/3781850"))

    def test_other_surfaces_false(self):
        for u in ("https://zhuanlan.zhihu.com/p/1",
                  "https://mp.weixin.qq.com/s/abc",
                  "https://wallstreetcn.com/live/1",   # 非 articles 路径
                  "", None):
            self.assertFalse(mirror.needs_mirror(u))


class TestMirrorFetch(unittest.TestCase):
    def test_returns_text_and_author(self):
        payload = {"code": "20000",
                   "data": {"content": _BODY, "author": {"display_name": "刘胜与"}}}
        with patch.object(mirror, "http_get", return_value=_FakeResp(payload)) as g:
            r = mirror.mirror_fetch("https://wallstreetcn.com/articles/3781850")
        self.assertIn("正文内容测试段落", r["text"])
        self.assertEqual(r["author"], "刘胜与")
        self.assertIn("3781850", g.call_args[0][0])

    def test_author_plain_string(self):
        payload = {"code": "20000", "data": {"content": _BODY, "author": "张三"}}
        with patch.object(mirror, "http_get", return_value=_FakeResp(payload)):
            r = mirror.mirror_fetch("https://wallstreetcn.com/articles/1")
        self.assertEqual(r["author"], "张三")

    def test_short_content_returns_none(self):
        payload = {"code": "20000", "data": {"content": "<p>太短</p>", "author": ""}}
        with patch.object(mirror, "http_get", return_value=_FakeResp(payload)):
            self.assertIsNone(mirror.mirror_fetch("https://wallstreetcn.com/articles/1"))

    def test_bad_code_returns_none(self):
        payload = {"code": "40400", "data": None}
        with patch.object(mirror, "http_get", return_value=_FakeResp(payload)):
            self.assertIsNone(mirror.mirror_fetch("https://wallstreetcn.com/articles/1"))

    def test_non_article_url_returns_none_without_http(self):
        with patch.object(mirror, "http_get") as g:
            self.assertIsNone(mirror.mirror_fetch("https://x.com/1"))
        g.assert_not_called()


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: 跑测试确认失败**

```
cd tech-digest && python -m pytest tests/test_mirror.py -v
```
预期：FAIL，`ModuleNotFoundError: No module named 'app.mirror'`（或 ImportError）。

- [ ] **Step 3: 最小实现**

创建 `tech-digest/app/mirror.py`：

```python
# -*- coding: utf-8 -*-
"""镜像通道：wallstreetcn 文章页是 SPA（直抓只有空壳），改走其内容 API。

背景（2026-09-21 实测）：候选选中华尔街见闻文章时 extract.fetch_article 拿到的是
SPA 空壳（<500 字符）；而 api-one-wscn 内容 API 按文章 id 确定性返回全文 HTML，
两天多轮验证稳定——不需要任何搜索/发现环节。知乎/公众号 403 面不走本模块
（数据中心 IP 上一切「按标题搜镜像」的端点数小时内衰减，实测证伪），它们继续
走 digest 导读降级。
"""
from __future__ import annotations

import re

from app.extract import extract_article

_ARTICLE_URL_RE = re.compile(
    r"https?://(?:www\.)?wallstreetcn\.com/articles/(\d+)", re.I)
CONTENT_API = ("https://api-one-wscn.awtmt.com/apiv1/content/"
               "articles/{id}?extract=0")

MIN_TEXT_CHARS = 500   # 与 extract.fetch_article 同一口径：过短视为不可用


def needs_mirror(url: str) -> bool:
    """该 URL 是否走镜像通道（wallstreetcn 文章页 = SPA 空壳）。"""
    return bool(_ARTICLE_URL_RE.search(url or ""))


def _author_name(raw) -> str:
    """作者字段兼容 dict（{display_name}) 与纯字符串两种返回。"""
    if isinstance(raw, dict):
        return str(raw.get("display_name") or "").strip()
    return str(raw or "").strip()


def mirror_fetch(url: str, *, timeout: int = 12) -> dict | None:
    """wallstreetcn 文章 → 内容 API 全文。返回 {"text", "author"}；不可用 → None。

    fail-open 由调用方处理：本函数只抛网络异常。
    """
    from app.fetcher import http_get   # 延迟导入：与 extract.py 同模式

    m = _ARTICLE_URL_RE.search(url or "")
    if not m:
        return None
    resp = http_get(CONTENT_API.format(id=m.group(1)), timeout=timeout)
    data = resp.json() or {}
    if str(data.get("code")) != "20000":
        return None
    d = data.get("data") or {}
    r = extract_article(d.get("content") or "")
    if len(r["text"]) < MIN_TEXT_CHARS:
        return None
    return {"text": r["text"], "author": _author_name(d.get("author"))}
```

- [ ] **Step 4: 跑测试确认通过**

```
cd tech-digest && python -m pytest tests/test_mirror.py -v
```
预期：全部 PASS。

- [ ] **Step 5: 提交**

```bash
git add tech-digest/app/mirror.py tech-digest/tests/test_mirror.py
git commit -m "feat(tech-digest): wallstreetcn 镜像通道（SPA 文章页→内容 API 全文）"
```

---

### Task 2: `_fetch_body` 接入镜像通道

**Files:**
- Modify: `tech-digest/main.py:35`（import 行）、`tech-digest/main.py:135-160`（`_fetch_body`）
- Test: `tech-digest/tests/test_main.py`（`TestFetchBody` 类内追加）

**Interfaces:**
- Consumes: Task 1 的 `mirror.needs_mirror` / `mirror.mirror_fetch`
- Produces: `_fetch_body(item, has_ai)` 签名不变；wallstreetcn 候选从此能出 `mode="full"`

- [ ] **Step 1: 写失败测试**

在 `tests/test_main.py` 的 `TestFetchBody` 类末尾（`test_unfetchable_falls_to_digest` 之后）追加：

```python
    def test_wallstreetcn_uses_mirror(self):
        """wallstreetcn 文章页是 SPA：extract 返回 None → 镜像通道拿全文。"""
        it = _item("见闻深度", "wallstreetcn")
        it["url"] = "https://wallstreetcn.com/articles/3781850"
        with patch("main.extract.fetch_article", return_value=None), \
             patch("main.mirror.mirror_fetch",
                   return_value={"text": "镜像全文。" * 200, "author": "刘胜与"}) as mf:
            body, mode, author = _fetch_body(it, has_ai=True)
        self.assertEqual((mode, author), ("full", "刘胜与"))
        self.assertIn("镜像全文", body)
        mf.assert_called_once_with("https://wallstreetcn.com/articles/3781850")

    def test_mirror_fail_falls_to_digest(self):
        it = _item("见闻深度", "wallstreetcn")
        it["url"] = "https://wallstreetcn.com/articles/1"
        with patch("main.extract.fetch_article", return_value=None), \
             patch("main.mirror.mirror_fetch", side_effect=RuntimeError("net")):
            body, mode, _ = _fetch_body(it, has_ai=True)
        self.assertEqual((body, mode), ("", "digest"))

    def test_non_mirror_url_skips_mirror(self):
        """知乎 403 面不走镜像（无搜索式发现），维持 digest 降级。"""
        it = _item("知乎文", "zhihu-hot")
        with patch("main.extract.fetch_article", return_value=None), \
             patch("main.mirror.mirror_fetch") as mf:
            body, mode, _ = _fetch_body(it, has_ai=True)
        self.assertEqual((body, mode), ("", "digest"))
        mf.assert_not_called()
```

- [ ] **Step 2: 跑测试确认失败**

```
cd tech-digest && python -m pytest tests/test_main.py::TestFetchBody -v
```
预期：新 3 条 FAIL（`AttributeError: <module 'app.mirror'>` 不存在 / 或 `main` 无 `mirror` 属性），旧 4 条 PASS。

- [ ] **Step 3: 最小实现**

`main.py` import 行（第 35 行）改为：

```python
from app import daily_ai, dedup, extract, marketdb, mirror, titles
```

`_fetch_body` 的开头（`art = None` 的 try/except 之后、`if art:` 之前）插入镜像段，并把 except 日志措辞改为「尝试镜像通道」：

```python
    art = None
    try:
        art = extract.fetch_article(item["url"])
    except Exception as e:  # noqa: BLE001 单篇抓取失败 → 镜像通道
        log.warning("原文抓取失败（%s）→ 尝试镜像通道: %s", item.get("title"), e)
    if art is None and mirror.needs_mirror(item.get("url") or ""):
        # wallstreetcn 文章页是 SPA 空壳 → 内容 API 确定性取全文（app/mirror.py）
        try:
            art = mirror.mirror_fetch(item["url"])
        except Exception as e:  # noqa: BLE001 镜像失败 → 降级导读
            log.warning("镜像通道失败（%s）→ 降级导读: %s", item.get("title"), e)
    if art:
        ...  # 以下原有逻辑不动
```

同步把 `_fetch_body` docstring 第一段补一句：`wallstreetcn 候选走镜像通道（SPA→内容 API），知乎/公众号 403 面维持 digest 降级。`

- [ ] **Step 4: 跑测试确认通过**

```
cd tech-digest && python -m pytest tests/test_main.py -v
```
预期：全部 PASS（含既有用例）。

- [ ] **Step 5: 提交**

```bash
git add tech-digest/main.py tech-digest/tests/test_main.py
git commit -m "feat(tech-digest): _fetch_body 接入镜像通道，见闻候选可出全文模式"
```

---

### Task 3: 华尔街见闻源（`app/sources/wscn.py`）

**Files:**
- Create: `tech-digest/app/sources/wscn.py`
- Modify: `tech-digest/app/sources/__init__.py:49-62`（registry）、`tech-digest/main.py:62-81`（`_daily_candidates`）、`tech-digest/app/config.py:61-63` 与 `config.py:93-96`（默认源列表）、`tech-digest/app/digest.py:31-34`（SOURCE_LABEL）
- Test: `tech-digest/tests/test_wscn.py`（新建）；`tests/test_main.py` 候选配额断言

**Interfaces:**
- Produces: `wscn.fetch() -> (items, "wallstreetcn", detail)`，条目 `source="wallstreetcn"`、`url=https://wallstreetcn.com/articles/{id}`（Task 2 的镜像通道按此 URL 生效）
- Consumes: 既有 `fetcher.http_get`、`sources.new_item`

- [ ] **Step 1: 写失败测试**

创建 `tests/test_wscn.py`：

```python
# -*- coding: utf-8 -*-
"""华尔街见闻源单测：resource 解包、live/付费/缺标题过滤、字段映射、抓取上限。"""
import unittest
from unittest.mock import patch

from app.sources import wscn


def _wrap(res_type="article", **kw):
    res = {"id": kw.get("id", 3781850), "title": kw.get("title", "深度长文标题"),
           "content_short": kw.get("short", "摘要内容"), "display_time": 1789979358,
           "is_paid": kw.get("paid", False), "is_priced": kw.get("priced", False),
           "author": kw.get("author", {"display_name": "刘胜与"})}
    return {"resource_type": res_type, "resource": res}


PAYLOAD = {"code": "20000", "data": {"items": [
    _wrap(),
    _wrap(res_type="live", title="快讯直播"),
    _wrap(title="付费专栏", paid=True),
    _wrap(title="标价文章", priced=True),
    _wrap(id=2, title="", author={"display_name": "x"}),      # 缺标题 → 丢
    _wrap(id=3, title="第二篇", author="字符串作者"),
]}}


class _FakeResp:
    def __init__(self, payload):
        self._payload = payload

    def json(self):
        return self._payload


class TestParseFlow(unittest.TestCase):
    def test_filter_and_fields(self):
        items = wscn.parse_flow(PAYLOAD)
        self.assertEqual(len(items), 2)                       # live/付费/标价/缺题全滤掉
        it = items[0]
        self.assertEqual(it["source"], "wallstreetcn")
        self.assertEqual(it["url"], "https://wallstreetcn.com/articles/3781850")
        self.assertEqual(it["author"], "刘胜与")
        self.assertEqual(it["summary"], "摘要内容")
        self.assertIsNotNone(it["published_at"])              # epoch → 本地 ISO

    def test_author_plain_string(self):
        items = wscn.parse_flow(PAYLOAD)
        self.assertEqual(items[1]["author"], "字符串作者")


class TestFetch(unittest.TestCase):
    def test_caps_to_max_items(self):
        big = {"code": "20000", "data": {"items": [
            _wrap(id=i, title=f"t{i}") for i in range(30)]}}
        with patch.object(wscn, "http_get", return_value=_FakeResp(big)):
            items, label, detail = wscn.fetch()
        self.assertEqual(label, "wallstreetcn")
        self.assertEqual(len(items), wscn.MAX_ITEMS)
        self.assertEqual(detail, {"keep": wscn.MAX_ITEMS})

    def test_empty_raises(self):
        with patch.object(wscn, "http_get",
                          return_value=_FakeResp({"code": "20000", "data": {"items": []}})):
            with self.assertRaises(RuntimeError):
                wscn.fetch()


if __name__ == "__main__":
    unittest.main()
```

再在 `tests/test_main.py` 的 `TestCandidatesOrder` 类内追加配额测试：

```python
    def test_wallstreetcn_quota(self):
        raw = [_item(f"见闻{i}", "wallstreetcn") for i in range(10)]
        cand = _daily_candidates(raw)
        self.assertEqual(sum(1 for c in cand if c["source"] == "wallstreetcn"), 3)
```

- [ ] **Step 2: 跑测试确认失败**

```
cd tech-digest && python -m pytest tests/test_wscn.py tests/test_main.py::TestCandidatesOrder -v
```
预期：`ModuleNotFoundError: No module named 'app.sources.wscn'`；配额测试 FAIL（wallstreetcn 目前落 `rest` 桶只取 5 条且共享）。

- [ ] **Step 3: 最小实现**

创建 `app/sources/wscn.py`：

```python
# -*- coding: utf-8 -*-
"""华尔街见闻源（2026-09-21 v4.5 加源）：行业深度/大厂动态的中文候选。

information-flow 列表 API（实测稳定）→ 外层 resource_type 过滤 + 内层 resource
取 id/title/author/content_short/display_time；is_paid/is_priced（付费/标价）与
非 article 类型（live 快讯）一律跳过。候选 url 用 wallstreetcn.com/articles/{id}，
正文由 app/mirror.py 走内容 API 取（文章页是 SPA，直抓只有空壳）。
"""
from __future__ import annotations

import datetime

from app.fetcher import http_get
from app.sources import new_item

FLOW_API = ("https://api-one-wscn.awtmt.com/apiv1/content/"
            "information-flow?accept=article&limit=30")
MAX_ITEMS = 12


def _iso(ts) -> str | None:
    """epoch 秒 → 本地 ISO；解析不了返回 None 不抛。"""
    try:
        return datetime.datetime.fromtimestamp(int(ts)).astimezone().isoformat(
            timespec="seconds")
    except (TypeError, ValueError, OSError, OverflowError):
        return None


def _author_name(raw) -> str:
    if isinstance(raw, dict):
        return str(raw.get("display_name") or "").strip()
    return str(raw or "").strip()


def parse_flow(payload: dict) -> list[dict]:
    """information-flow JSON → 统一 v2 条目（live/付费/标价/缺标题项跳过）。"""
    items = (payload.get("data") or {}).get("items") or []
    out: list[dict] = []
    for wrap in items:
        res = wrap.get("resource") or {}
        title = str(res.get("title") or "").strip()
        if not title or wrap.get("resource_type") != "article":
            continue
        if res.get("is_paid") or res.get("is_priced"):
            continue
        out.append(new_item(
            type_="news", title=title,
            url=f"https://wallstreetcn.com/articles/{res.get('id')}",
            source="wallstreetcn", author=_author_name(res.get("author")),
            published_at=_iso(res.get("display_time")),
            summary=str(res.get("content_short") or "").strip()[:300]))
    return out


def fetch() -> tuple[list[dict], str, dict]:
    resp = http_get(FLOW_API, timeout=15)
    items = parse_flow(resp.json() or {})[:MAX_ITEMS]
    if not items:
        raise RuntimeError("wscn information-flow 无有效条目")
    return items, "wallstreetcn", {"keep": len(items)}
```

`app/sources/__init__.py`：registry 前的延迟导入行加 `wscn`：

```python
    from app.sources import (devto, feeds, github, hacker_news, sspai,  # 延迟：注册表避免循环
                             wscn, zhihu_hot)
```

registry dict 加一行：

```python
        "wallstreetcn": wscn.fetch,
```

`app/digest.py` SOURCE_LABEL 加：

```python
                "wallstreetcn": "华尔街见闻",
```

`app/config.py`：dataclass 默认列表（第 61-63 行）与 `from_env` 默认字符串（第 93-96 行）都加 `"wallstreetcn"`：

```python
    tech_digest_sources: list[str] = field(default_factory=lambda: [
        "github-trending", "hacker-news", "sspai", "zhihu-hot", "devto",
        "ars-technica", "ieee-spectrum", "freecodecamp", "ruanyifeng",
        "wallstreetcn"])
```
（`from_env` 默认串同理在 `"ruanyifeng"` 后加 `",wallstreetcn"`。）

`main.py` `_daily_candidates`：feeds 元组与 rest 排除元组都加 `"wallstreetcn"`（配额 3，与其他订阅源一致），docstring 的「9 源」改「10 源」：

```python
    for src in ("ars-technica", "ieee-spectrum", "freecodecamp", "ruanyifeng",
                "wallstreetcn"):
        feeds += [it for it in news if it["source"] == src][:3]
    rest = [it for it in news if it["source"] not in (
        "zhihu-hot", "hacker-news", "sspai", "devto",
        "ars-technica", "ieee-spectrum", "freecodecamp", "ruanyifeng",
        "wallstreetcn")][:5]
```

- [ ] **Step 4: 跑测试确认通过**

```
cd tech-digest && python -m pytest tests/test_wscn.py tests/test_main.py tests/test_sources.py -v
```
预期：全部 PASS。

- [ ] **Step 5: 提交**

```bash
git add tech-digest/app/sources/wscn.py tech-digest/app/sources/__init__.py \
        tech-digest/app/digest.py tech-digest/app/config.py tech-digest/main.py \
        tech-digest/tests/test_wscn.py tech-digest/tests/test_main.py
git commit -m "feat(tech-digest): 华尔街见闻源（information-flow 候选 + 付费/快讯过滤）"
```

---

### Task 4: 挑选判据第 4 条（亲历/行业观察）

**Files:**
- Modify: `tech-digest/app/daily_ai.py:192-201`（`_build_pick_prompt` 挑选标准段）
- Test: `tech-digest/tests/test_daily_ai.py`

**Interfaces:**
- Produces: prompt 挑选标准从 6 条变 7 条；新第 4 条「亲历与行业观察」。`sanitize_pick`/`ai_pick` 签名不变。

- [ ] **Step 1: 写失败测试**

在 `tests/test_daily_ai.py` 追加（类外独立用例即可，对齐该文件现有风格）：

```python
class TestPickPromptCriteria(unittest.TestCase):
    """v4.5 判据 4：亲历/行业观察类合格，且编者按按亲历视角写钩子。"""

    def test_prompt_has_industry_essay_criterion(self):
        prompt = daily_ai._build_pick_prompt(
            [{"title": "t", "source": "zhihu-hot", "summary": "s", "author": "",
              "trending": {}, "published_at": None}])
        self.assertIn("亲历", prompt)
        self.assertIn("行业观察", prompt)
        # 新判据插在第 4 位，原 4-6 条顺延为 5-7
        self.assertIn("4. 亲历与行业观察", prompt)
        self.assertIn("5. 学生相关性", prompt)
        self.assertIn("7. 深度长文", prompt)
```

- [ ] **Step 2: 跑测试确认失败**

```
cd tech-digest && python -m pytest tests/test_daily_ai.py::TestPickPromptCriteria -v
```
预期：FAIL（`assertIn` 找不到「4. 亲历与行业观察」）。

- [ ] **Step 3: 最小实现**

`app/daily_ai.py` `_build_pick_prompt` 的挑选标准段改为（新 4 插入，原 4/5/6 改 5/6/7）：

```python
        "挑选标准（按优先级）：\n"
        "1. 时效性：今天榜上的才算热，宁可要今天的热点，不要陈年旧文；\n"
        "2. 影响性：热度信号（HN points / 知乎热榜排名）是全网影响力的硬指标；\n"
        "3. 大厂与人物：涉及知名大厂（OpenAI、英伟达、Google、字节、华为这类）或知名人物"
        "（技术领袖、创始人、知名工程师）的优先选——读者对熟悉的名字更容易点进来；"
        "但时效是前提，再知名、今天没在讨论的过气旧闻也不要；\n"
        "4. 亲历与行业观察：从业者写的圈内亲历、行业深度观察（职业转折、公司内部故事、"
        "行业剖析）天然合格——信息价值和可读性优先，不必是技术教程；\n"
        "5. 学生相关性：课程、实习/秋招、竞赛、做项目、学习路线，至少沾一个；\n"
        "6. 知乎热榜里的娱乐八卦、体育、社会新闻类不要选（除非与科技/学习强相关）；\n"
        "7. 深度长文 > 短讯：这篇是要「全文转载」的，太浅的文章撑不起来。\n\n"
```

写作规则段追加一条：

```python
        "· 亲历/行业观察类文章，编者按用「亲历/观察」视角写钩子（谁、走过了什么、"
        "看清了什么），不要写成教程推荐；\n"
```

- [ ] **Step 4: 跑测试确认通过**

```
cd tech-digest && python -m pytest tests/test_daily_ai.py -v
```
预期：全部 PASS。

- [ ] **Step 5: 提交**

```bash
git add tech-digest/app/daily_ai.py tech-digest/tests/test_daily_ai.py
git commit -m "feat(tech-digest): 挑选判据第4条——亲历/行业观察类合格"
```

---

### Task 5: 量子位 + 月光博客订阅源

**Files:**
- Modify: `tech-digest/app/sources/feeds.py:21-24,116-129`（URL 常量 + fetch 函数）、`tech-digest/app/sources/__init__.py`（registry）、`tech-digest/app/config.py`（默认源列表，两处）、`tech-digest/main.py`（`_daily_candidates` 两个元组 + docstring 计数）、`tech-digest/app/digest.py`（SOURCE_LABEL）
- Test: `tech-digest/tests/test_feeds.py`、`tests/test_main.py`

**Interfaces:**
- Produces: `feeds.fetch_qbitai()` / `feeds.fetch_williamlong()`，签名同既有 fetch_*（`-> (items, source_label, detail)`）
- Consumes: 既有 `fetch_feed(source, url, max_items)`

- [ ] **Step 1: 写失败测试**

`tests/test_feeds.py` 追加：

```python
class TestNewFeedSources(unittest.TestCase):
    """v4.5 新源：量子位/月光博客，走既有 fetch_feed 通道。"""

    def test_fetch_qbitai(self):
        with patch.object(feeds, "fetch_feed", return_value=([], "qbitai", {})) as ff:
            items, label, _ = feeds.fetch_qbitai()
        ff.assert_called_once_with("qbitai", feeds.QBITAI_URL, 10)
        self.assertEqual(label, "qbitai")

    def test_fetch_williamlong(self):
        with patch.object(feeds, "fetch_feed", return_value=([], "williamlong", {})) as ff:
            items, label, _ = feeds.fetch_williamlong()
        ff.assert_called_once_with("williamlong", feeds.WILLIAMLONG_URL, 10)
        self.assertEqual(label, "williamlong")
```

`tests/test_main.py` 的 `TestCandidatesOrder` 追加（与 Task 3 的配额测试并列）：

```python
    def test_qbitai_williamlong_quota(self):
        raw = ([_item(f"量{i}", "qbitai") for i in range(6)]
               + [_item(f"月{i}", "williamlong") for i in range(6)])
        cand = _daily_candidates(raw)
        self.assertEqual(sum(1 for c in cand if c["source"] == "qbitai"), 3)
        self.assertEqual(sum(1 for c in cand if c["source"] == "williamlong"), 3)
```

- [ ] **Step 2: 跑测试确认失败**

```
cd tech-digest && python -m pytest tests/test_feeds.py::TestNewFeedSources tests/test_main.py::TestCandidatesOrder -v
```
预期：`AttributeError: ... QBITAI_URL`；配额测试 FAIL。

- [ ] **Step 3: 最小实现**

`app/sources/feeds.py` 常量区加：

```python
QBITAI_URL = "https://www.qbitai.com/feed"
WILLIAMLONG_URL = "https://www.williamlong.info/rss.xml"
```

文件末尾加：

```python
def fetch_qbitai() -> tuple[list[dict], str, dict]:
    return fetch_feed("qbitai", QBITAI_URL, 10)


def fetch_williamlong() -> tuple[list[dict], str, dict]:
    return fetch_feed("williamlong", WILLIAMLONG_URL, 10)
```

`app/sources/__init__.py` registry 加：

```python
        "qbitai": feeds.fetch_qbitai,
        "williamlong": feeds.fetch_williamlong,
```

`app/digest.py` SOURCE_LABEL 加：

```python
                "qbitai": "量子位", "williamlong": "月光博客",
```

`app/config.py` 两处默认列表在 `"wallstreetcn"` 后加 `"qbitai", "williamlong"`（dataclass 默认与 from_env 默认串）。

`main.py` `_daily_candidates`：两个元组在 `"wallstreetcn"` 后加 `"qbitai", "williamlong"`，docstring「10 源」改「12 源」。

- [ ] **Step 4: 跑测试确认通过**

```
cd tech-digest && python -m pytest tests/test_feeds.py tests/test_main.py tests/test_sources.py tests/test_digest.py -v
```
预期：全部 PASS。

- [ ] **Step 5: 提交**

```bash
git add tech-digest/app/sources/feeds.py tech-digest/app/sources/__init__.py \
        tech-digest/app/digest.py tech-digest/app/config.py tech-digest/main.py \
        tech-digest/tests/test_feeds.py tech-digest/tests/test_main.py
git commit -m "feat(tech-digest): 新增量子位/月光博客订阅源（配额各3）"
```

---

### Task 6: 文档更新（技术实现文档附录 B）

**Files:**
- Modify: `前沿技术周刊-技术实现文档.md`（附录 B）

- [ ] **Step 1: 更新附录 B**

在附录 B 内补三处（沿用该文档现有编号与语气）：
1. **B.1 候选池**：源清单更新为 12 源（新增 wallstreetcn / qbitai / williamlong），写明 wscn 条目字段来源（information-flow resource 内层）与过滤规则（live/is_paid/is_priced/缺标题跳过）；
2. **B.3 AI 护栏**：挑选判据补第 4 条「亲历与行业观察类合格」，注明 2026-09-21 用户决策；
3. **新增 B.5 镜像通道**：wallstreetcn 文章页 SPA → `app/mirror.py` 内容 API 确定性取全文；同一节记录实测否证——数据中心 IP 上标题搜索式镜像发现（wscn search/搜狗微信/360/DDG）数小时内衰减，知乎 403 为 zse-ck v4 JS 墙（z_c0 登录 cookie 亦无效，2026-09-21 双端八组合实测），故不做运行时搜索，知乎/公众号维持 digest 降级。

- [ ] **Step 2: 提交**

```bash
git add "前沿技术周刊-技术实现文档.md"
git commit -m "docs(tech-digest): 附录B——见闻源/镜像通道/判据4与新源"
```

---

### Task 7: 双端全量 pytest + 本地预览 + 部署 + 冒烟

**Files:**
- 无新文件（预览脚本放 `_preview/`，不进 git）

- [ ] **Step 1: 本地全量测试**

```
cd tech-digest && python -m pytest -q
```
预期：全绿（改前基线 354 条）。

- [ ] **Step 2: 本地渲染预览（用户过目后才部署）**

写 `_preview/_render_wscn_preview.py`（仿 `_preview/_gen_weekly_v45.py` 的渲染骨架）：从服务器活数据走 `wscn.fetch()` 取第一条非付费文章 → `mirror.mirror_fetch(url)` → 组 `pick_d`（why 用固定示意文案，glossary 空）→ `render_repost(day, issue_no, ctx)` → 落 `_preview/real-post-mirror.md` 并按既有 html 管线渲染出 `real-post-mirror.html`，无头 Edge 截图。**渲染改动先预览是既定流程；本任务把预览发给用户确认。**

- [ ] **Step 3: 部署**

先读 `tech-digest/scripts/deploy.sh` 头部注释确认用法，然后执行部署；部署脚本完成后在服务器跑全量测试：

```
ssh market-server 'cd /root/market-deploy/agent/tech-digest && .venv/bin/python3 -m pytest -q'
```
预期：全绿（服务器基线 291 条 + 新增）。

- [ ] **Step 4: 服务器活体冒烟**

```
ssh market-server 'cd /root/market-deploy/agent/tech-digest && .venv/bin/python3 -c "
from app.sources import wscn
from app import mirror
items, _, _ = wscn.fetch()
print(\"wscn items:\", len(items))
it = items[0]
r = mirror.mirror_fetch(it[\"url\"])
print(\"mirror text chars:\", len(r[\"text\"]) if r else None, \"| author:\", r[\"author\"] if r else None)
"'
```
预期：`wscn items: 12`，mirror text ≥500。

- [ ] **Step 5: 收尾**

- 冒烟通过后，明天 09:15 daily timer 首次实战；当天复核帖子 mode 是否为 full、新源是否入库（`store.log` 的 source_stats）。
- 清理 `_preview/` 探针脚本（`_wscn_shape*.py`、`_discovery_probe.py`、`_so_probe.py`、`_sg_rehearsal.py`、`_wscn_flow*.py`）与服务器 `/tmp` 残留。
- 本任务无独立提交（预览脚本不进 git）。

---

## Self-Review 记录

- 覆盖检查：判据4（Task 4）、两个新源（Task 5）、镜像链可用形态（Task 1-3）、文档（Task 6）、预览+部署（Task 7）——已批准设计的全部可落地部分均有对应任务；被证伪的「运行时搜索发现」按实测明确不做并记录于 B.5。
- 类型一致性：`mirror_fetch(url) -> dict|None`、`needs_mirror(url) -> bool`、`wscn.fetch()` 三端签名在 Task 1/2/3 间已互相对齐；patch 目标统一 `main.mirror.*` / `app.mirror` / `app.sources.wscn`。
- 占位扫描：所有代码步骤给出完整代码；Task 6 文档步骤给出内容要点（文档为中文行文，不逐字给定）；Task 7 预览脚本指向既有骨架 `_gen_weekly_v45.py`。
