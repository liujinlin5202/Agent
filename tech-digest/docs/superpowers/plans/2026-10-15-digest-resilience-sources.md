# 日报韧性优化 + 源扩展 Implementation Plan

> **进度（2026-10-15）**：Task 1-10 全部完成——Agent 仓 main `c84e15e..2a67de9` 已推 GitHub
> （9 commits：池配额/五 RSS 源/掘金源/注册表声明化+知乎下线/抓取缓存/可行性预检/薄池补齐/rc 契约）；
> qiudock 仓 `07d6d92` 已推 gitee（死人开关）。全量回归 809 绿（基线 797+新增 12）。
> 本机实测：6 新源 URL 均 200（solidot 直连+代理均 000 弃、HF 被墙弃）。
> **Task 11（生产部署+闭环验证）待用户确认后执行。**

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 收掉 tech-digest「skip_quality 静默 rc=0」的假成功缺陷（含发帖失败 rc=0），加池级容灾（单源配额/薄池补齐/可行性预检），下线知乎死源并新增 6 个可靠源。

**Architecture:** rc 契约改为「当天未发布即 rc≠0」（dup/周末关/显式关/凭据缺失/dry-run 除外）；告警侧（qiudock 仓 `qdock_cron_alert.sh`）改真·死人开关——只判「今日无 ok+post_id」，与过程状态无关。取材侧三层容灾：池粗排单源配额、薄池 live 补齐、编辑部 LLM 前可行性预检（带进程内抓取缓存避免重复抓）。

**Tech Stack:** Python 3 stdlib + requests + unittest（仓库既有零依赖风格）；告警脚本为 POSIX sh + 内嵌 python3。

**背景（2026-10-15 事故）**：知乎 403 死候选占池 13/24，剩 11 条质检全灭（missing_source/empty_markdown/invalid_final_title），skip_quality 落库为 `skipped`，cronwatch 只看 rc 记 passed，`qdock_cron_alert.sh:61` 只扫 degraded/error——四层观测全瞎。业界对标：dead man's switch（期望驱动告警）、dbt source freshness（源可用性一等公民）、症状告警（SRE Workbook Ch5）。

**用户裁决（2026-10-15）**：①load 熔断跳过日也 rc≠0；②不做自动降级出版（P2 否决）；③新源只要国外业界网站 + 国内技术性网站，不要营销号源。**知乎处理=下线**（RSSHub 社区实锤数据中心 IP 永久 403，mirror.py 已证伪搜镜像方案）。

---

### Task 1: pool 粗排单源配额（防单源霸榜）

**Files:**
- Modify: `app/pool.py`（`top_candidates`，约 232-267 行）
- Test: `tests/test_pool.py`

- [ ] **Step 1: 写失败测试**（追加到 `tests/test_pool.py`，沿用该文件既有的 `_news` 构造器与 setUp 建库方式；若类名不同以实际为准）

```python
    def test_top_candidates_per_source_quota(self):
        # 12 条 HN + 4 条 sspai：单源配额 8 → HN 最多 8 条入粗排结果
        items = ([_news(f"hn{i}", source="hacker-news", heat=float(100 - i))
                  for i in range(12)]
                 + [_news(f"sspai{i}", source="sspai", heat=float(i)) for i in range(4)])
        self.store.upsert_news(items)
        cands = self.store.top_candidates(limit=24)
        by_src = {}
        for c in cands:
            by_src[c["source"]] = by_src.get(c["source"], 0) + 1
        self.assertEqual(by_src.get("hacker-news"), 8)
        self.assertEqual(by_src.get("sspai"), 4)
```

- [ ] **Step 2: 跑测试确认失败**

```bash
cd C:/Users/ASUS/Desktop/SSE-Agent/Agent/tech-digest && python -m unittest tests.test_pool -k quota -v
```
Expected: FAIL（当前 HN 会占满 12 条）

- [ ] **Step 3: 实现**——`app/pool.py` 常量区（`HEAT_DEFAULT = 50.0` 之后）加：

```python
# 单源配额（2026-10-15）：防单源霸榜挤掉其他源的合格名额（当日知乎死候选 13/24 实证）
MAX_PER_SOURCE_CANDIDATES = 8
```

`top_candidates` 选择循环改为：

```python
        out, per_src = [], defaultdict(int)
        for score, r in scored:
            it = json.loads(r["raw"])
            if excluded_candidate(it):
                continue
            src = it.get("source") or ""
            if per_src[src] >= MAX_PER_SOURCE_CANDIDATES:
                continue
            per_src[src] += 1
            it["_pool_url"] = r["url"]
            it["_pool_id"] = r["id"]
            it["_pool_score"] = round(score, 1)
            out.append(it)
            if len(out) >= limit:
                break
        return out
```

（`defaultdict` 已在该文件顶部 import。）

- [ ] **Step 4: 跑测试确认通过**

```bash
python -m unittest tests.test_pool -v
```
Expected: 全部 PASS（既有用例不受影响——既有用例单源条数 <8）

- [ ] **Step 5: Commit**

```bash
cd C:/Users/ASUS/Desktop/SSE-Agent/Agent && git add tech-digest/app/pool.py tech-digest/tests/test_pool.py && git commit -m "feat(pool): top_candidates 单源配额 8，防单源霸榜"
```

---

### Task 2: 源注册表声明化 + 知乎下线 + 默认源清单扩展

**Files:**
- Modify: `app/sources/__init__.py`（`fetch_all`）
- Modify: `app/config.py`（`tech_digest_sources` 两处默认值）
- Test: `tests/test_sources.py`

知乎处理：从默认源清单移除 `zhihu-hot`（不再抓取入池）。`app/sources/zhihu_hot.py` 模块与 `main._pool_daily_materials` 的读取侧知乎过滤**保留**（老池行 7 天 TTL 内仍会被过滤掉）。`_daily_candidates` 的知乎桶保留不动（live 回退路径不再有 zhihu 条目，桶自然恒空，测试也不破）。

- [ ] **Step 1: 写失败测试**（追加到 `tests/test_sources.py`）

```python
class TestRegistryContract(unittest.TestCase):
    def test_default_sources_all_registered(self):
        from app.config import settings
        from app.sources import KNOWN_SOURCES
        self.assertNotIn("zhihu-hot", settings.tech_digest_sources)
        self.assertTrue(set(settings.tech_digest_sources) <= set(KNOWN_SOURCES))

    def test_known_sources_resolvable(self):
        import importlib
        from app.sources import KNOWN_SOURCES
        for name, (mod, fn) in KNOWN_SOURCES.items():
            f = getattr(importlib.import_module(f"app.sources.{mod}"), fn)
            self.assertTrue(callable(f), f"{name} -> {mod}.{fn}")
```

- [ ] **Step 2: 跑测试确认失败**

```bash
python -m unittest tests.test_sources -v
```
Expected: FAIL（KNOWN_SOURCES 不存在；zhihu 仍在默认清单）

- [ ] **Step 3: 实现**——`app/sources/__init__.py`：模块级加声明表（`fetch_all` 上方）：

```python
# 源注册声明表（2026-10-15 声明化：测试可断言完整性，fetch_all 惰性解析避免循环 import）
KNOWN_SOURCES = {
    "github-trending": ("github", "fetch"),
    "hacker-news": ("hacker_news", "fetch"),
    "sspai": ("sspai", "fetch"),
    "zhihu-hot": ("zhihu_hot", "fetch"),   # 已下线（默认清单移除）；保留注册便于历史 env 回切
    "devto": ("devto", "fetch"),
    "wallstreetcn": ("wscn", "fetch"),
    "ars-technica": ("feeds", "fetch_ars"),
    "ieee-spectrum": ("feeds", "fetch_ieee"),
    "freecodecamp": ("feeds", "fetch_fcc"),
    "ruanyifeng": ("feeds", "fetch_ruanyifeng"),
    "lobste-rs": ("feeds", "fetch_lobsters"),
    "the-verge": ("feeds", "fetch_verge"),
    "techcrunch": ("feeds", "fetch_techcrunch"),
    "infoq": ("feeds", "fetch_infoq"),
    "oschina": ("feeds", "fetch_oschina"),
    "juejin": ("juejin", "fetch"),
}
```

`fetch_all` 的 registry 构建段整体替换为：

```python
    import importlib
    registry: dict[str, FetchFn] = {
        name: getattr(importlib.import_module(f"app.sources.{mod}"), fn)
        for name, (mod, fn) in KNOWN_SOURCES.items()}
```

（删除原函数内的 `from app.sources import (...)` 与手写 registry。）

`app/config.py` 两处默认值同步：dataclass 字段默认

```python
    tech_digest_sources: list[str] = field(default_factory=lambda: [
        "github-trending", "hacker-news", "sspai", "devto", "wallstreetcn",
        "ars-technica", "ieee-spectrum", "freecodecamp", "ruanyifeng",
        "lobste-rs", "the-verge", "techcrunch", "infoq", "oschina", "juejin"])
```

`from_env` 的 env 缺省串：

```python
            tech_digest_sources=[s.strip() for s in os.environ.get(
                "TECH_DIGEST_SOURCES",
                "github-trending,hacker-news,sspai,devto,wallstreetcn,"
                "ars-technica,ieee-spectrum,freecodecamp,ruanyifeng,"
                "lobste-rs,the-verge,techcrunch,infoq,oschina,juejin").split(",") if s.strip()],
```

- [ ] **Step 4: 检查部署 spec 是否显式覆盖源清单**（若qiudock spec env 有 TECH_DIGEST_SOURCES 则 qiudock 仓同步改，留到 Task 8 一起提交）

```bash
grep -n "TECH_DIGEST_SOURCES" C:/Users/ASUS/Desktop/SSE-Agent/qiudock/agents/tech-digest.yaml
```
Expected: 无输出（用默认）或有输出（记录下来 Task 8 同步）

- [ ] **Step 5: 跑测试确认通过**

```bash
python -m unittest tests.test_sources tests.test_ingest -v
```
Expected: PASS（test_ingest 用显式源名清单，不受默认值影响）

- [ ] **Step 6: Commit**

```bash
cd C:/Users/ASUS/Desktop/SSE-Agent/Agent && git add tech-digest/app/sources/__init__.py tech-digest/app/config.py tech-digest/tests/test_sources.py && git commit -m "feat(sources): 注册表声明化；知乎下线；新增 6 源进默认清单"
```

---

### Task 3: feeds.py 加 5 个 RSS 源（lobste.rs / The Verge / TechCrunch / InfoQ / OSCHINA）

**Files:**
- Modify: `app/sources/feeds.py`（URL 常量 + 一行函数）
- Test: `tests/test_feeds.py`

说明：`fetch_feed(source, url, max_items)` 已处理 RSS2/Atom 双格式与上限，新源只是一行函数。五源 URL 均 2026-10-15 实测 200。solidot 本机直连+代理均 000 不加；HF daily papers 被墙不加。

- [ ] **Step 1: 写失败测试**（追加到 `tests/test_feeds.py`，复用文件顶部的 `RSS_FULL`/`ATOM` 样例）

```python
class TestNewFeeds(unittest.TestCase):
    def test_new_feed_functions(self):
        cases = [
            (feeds.fetch_lobsters, "lobste-rs", feeds.LOBSTERS_URL),
            (feeds.fetch_verge, "the-verge", feeds.VERGE_URL),
            (feeds.fetch_techcrunch, "techcrunch", feeds.TECHCRUNCH_URL),
            (feeds.fetch_infoq, "infoq", feeds.INFOQ_URL),
            (feeds.fetch_oschina, "oschina", feeds.OSCHINA_URL),
        ]
        for fn, source, url in cases:
            with self.subTest(source=source):
                with patch("app.sources.feeds.http_get") as m:
                    m.return_value.text = RSS_FULL
                    items, label, _ = fn()
                self.assertEqual(label, source)
                self.assertEqual(items[0]["source"], source)
                self.assertEqual(m.call_args.args[0], url)

    def test_fetch_feed_respects_max_items(self):
        xml = RSS_FULL.replace("</item>", "</item>" + RSS_FULL.split("<item>", 1)[1].split("</item>", 1)[0] + "</item>")
        with patch("app.sources.feeds.http_get") as m:
            m.return_value.text = xml
            items, _, _ = feeds.fetch_feed("x", "http://x", 1)
        self.assertEqual(len(items), 1)
```

- [ ] **Step 2: 跑测试确认失败**

```bash
python -m unittest tests.test_feeds -v
```
Expected: FAIL/AttributeError（fetch_lobsters 等不存在）

- [ ] **Step 3: 实现**——`app/sources/feeds.py` 常量区加：

```python
LOBSTERS_URL = "https://lobste.rs/rss"
VERGE_URL = "https://www.theverge.com/rss/index.xml"
TECHCRUNCH_URL = "https://techcrunch.com/feed/"
INFOQ_URL = "https://feed.infoq.com"
OSCHINA_URL = "https://www.oschina.net/news/rss"
```

文件尾部追加：

```python
def fetch_lobsters() -> tuple[list[dict], str, dict]:
    return fetch_feed("lobste-rs", LOBSTERS_URL, 10)


def fetch_verge() -> tuple[list[dict], str, dict]:
    return fetch_feed("the-verge", VERGE_URL, 10)


def fetch_techcrunch() -> tuple[list[dict], str, dict]:
    return fetch_feed("techcrunch", TECHCRUNCH_URL, 10)


def fetch_infoq() -> tuple[list[dict], str, dict]:
    return fetch_feed("infoq", INFOQ_URL, 10)


def fetch_oschina() -> tuple[list[dict], str, dict]:
    return fetch_feed("oschina", OSCHINA_URL, 10)
```

并更新文件 docstring 的源清单一句（四源→九源）。

- [ ] **Step 4: 跑测试确认通过**

```bash
python -m unittest tests.test_feeds -v
```

- [ ] **Step 5: Commit**

```bash
cd C:/Users/ASUS/Desktop/SSE-Agent/Agent && git add tech-digest/app/sources/feeds.py tech-digest/tests/test_feeds.py && git commit -m "feat(sources): feeds 新增 lobste-rs/the-verge/techcrunch/infoq/oschina 五源"
```

---

### Task 4: 掘金源（自定义 JSON API 模块）+ 池权重/热度信号

**Files:**
- Create: `app/sources/juejin.py`
- Modify: `app/pool.py`（`SOURCE_WEIGHT`、`_heat_signal`）
- Test: `tests/test_juejin.py`（新建）

掘金官方 recommend API POST JSON 无需登录，2026-10-15 实测返回 `err_no:0` + `data[].item_info.article_info{title, article_id, brief_content, ...}`；文章页 `https://juejin.cn/post/{article_id}` SSR 可抓。热信号取 `hot_index`（缺则 `view_count`）。

- [ ] **Step 1: 写失败测试**（新建 `tests/test_juejin.py`）

```python
# -*- coding: utf-8 -*-
import unittest

from app.sources.juejin import parse_payload

FIXTURE = {
    "err_no": 0, "err_msg": "success",
    "data": [
        {"item_info": {"article_info": {
            "article_id": "7637856870833635343",
            "title": "Cursor 转 Codex 大半个月，聊聊我的真实感受",
            "brief_content": "AI 编程的震撼……",
            "hot_index": 420, "view_count": 3100}},
         "author_user_info": {"user_name": "someone"}},
        {"item_info": {"article_info": {"article_id": "", "title": "无 id 条目"}}},
        {"item_info": {}},
    ],
}


class TestParsePayload(unittest.TestCase):
    def test_fields(self):
        items = parse_payload(FIXTURE)
        self.assertEqual(len(items), 1)          # 无 id / 无 info 的条目丢弃
        it = items[0]
        self.assertEqual(it["title"], "Cursor 转 Codex 大半个月，聊聊我的真实感受")
        self.assertEqual(it["url"], "https://juejin.cn/post/7637856870833635343")
        self.assertEqual(it["source"], "juejin")
        self.assertEqual(it["points"], 420)      # hot_index 优先
        self.assertEqual(it["summary"], "AI 编程的震撼……")

    def test_points_falls_back_to_view_count(self):
        payload = {"err_no": 0, "data": [{"item_info": {"article_info": {
            "article_id": "1", "title": "t", "view_count": 99}}}]}
        self.assertEqual(parse_payload(payload)[0]["points"], 99)

    def test_api_error_raises(self):
        with self.assertRaises(RuntimeError):
            parse_payload({"err_no": 1, "err_msg": "fail"})
```

- [ ] **Step 2: 跑测试确认失败**

```bash
python -m unittest tests.test_juejin -v
```
Expected: FAIL（模块不存在）

- [ ] **Step 3: 实现**——新建 `app/sources/juejin.py`：

```python
# -*- coding: utf-8 -*-
"""掘金热文源：官方 recommend API（POST JSON，无需登录；2026-10-15 实测）。

热文取 recommend_all_feed（sort_type=200 热度序）；热信号 hot_index（缺则
view_count），normalize_heat 只用批内分位、量纲无所谓。文章 URL =
https://juejin.cn/post/{article_id}，文章页 SSR、extract 直抓全文。
"""
from __future__ import annotations

import requests

from app.fetcher import UA
from app.sources import new_item

API_URL = "https://api.juejin.cn/recommend_api/v1/article/recommend_all_feed"
MAX_ITEMS = 20


def parse_payload(payload: dict) -> list[dict]:
    if not isinstance(payload, dict) or payload.get("err_no") != 0:
        raise RuntimeError(f"掘金 API 异常: {str(payload)[:120]}")
    items: list[dict] = []
    for entry in payload.get("data") or []:
        info = ((entry.get("item_info") or {}).get("article_info") or {})
        title = (info.get("title") or "").strip()
        aid = str(info.get("article_id") or "").strip()
        if not title or not aid:
            continue
        it = new_item(
            type_="news", title=title, url=f"https://juejin.cn/post/{aid}",
            source="juejin",
            published_at=None,
            summary=(info.get("brief_content") or "").strip())
        it["points"] = int(info.get("hot_index") or info.get("view_count") or 0)
        items.append(it)
        if len(items) >= MAX_ITEMS:
            break
    return items


def fetch() -> tuple[list[dict], str, dict]:
    resp = requests.post(API_URL, timeout=15, headers={"User-Agent": UA},
                         json={"id_type": 2, "sort_type": 200,
                               "cursor": "0", "limit": MAX_ITEMS})
    resp.raise_for_status()
    items = parse_payload(resp.json())
    if not items:
        raise RuntimeError("掘金 feed 无条目")
    return items, "juejin", {"keep": len(items)}
```

`app/pool.py` 两处：

```python
SOURCE_WEIGHT = {
    "zhihu-hot": 8, "hacker-news": 8, "devto": 8, "sspai": 8,
    "wallstreetcn": 6, "ars-technica": 6, "ieee-spectrum": 6,
    "freecodecamp": 6, "ruanyifeng": 6,
    # 2026-10-15 新源：技术社区 7，业界媒体 6
    "lobste-rs": 7, "juejin": 7,
    "the-verge": 6, "techcrunch": 6, "infoq": 6, "oschina": 6,
}
```

`_heat_signal` 的 HN/devto 分支并入 juejin：

```python
    if src in ("hacker-news", "devto", "juejin"):
        pts = it.get("points") or 0
        return float(pts) if pts else None
```

- [ ] **Step 4: 跑测试确认通过**

```bash
python -m unittest tests.test_juejin tests.test_pool -v
```

- [ ] **Step 5: Commit**

```bash
cd C:/Users/ASUS/Desktop/SSE-Agent/Agent && git add tech-digest/app/sources/juejin.py tech-digest/app/pool.py tech-digest/tests/test_juejin.py && git commit -m "feat(sources): 掘金热文源 + 新源池权重/热度信号"
```

---

### Task 5: extract 抓取缓存（可行性预检与 deep_research 共享，避免重复抓）

**Files:**
- Modify: `app/extract.py`（`fetch_with_mirror` 附近）
- Test: `tests/test_extract.py`

- [ ] **Step 1: 写失败测试**（追加到 `tests/test_extract.py`）

```python
class TestFetchCache(unittest.TestCase):
    def setUp(self):
        extract.clear_fetch_cache()

    def test_same_url_fetched_once(self):
        calls = []
        def fake(item):
            calls.append(item["url"])
            return {"text": "正文" * 10}
        with patch("app.extract.fetch_with_mirror", side_effect=fake):
            a = extract.fetch_cached({"url": "https://x.com/a", "title": "t"})
            b = extract.fetch_cached({"url": "https://x.com/a", "title": "t"})
        self.assertEqual(calls, ["https://x.com/a"])
        self.assertIs(a, b)

    def test_failed_fetch_not_poisoned_but_recorded(self):
        with patch("app.extract.fetch_with_mirror", return_value=None) as m:
            extract.fetch_cached({"url": "https://x.com/f", "title": "t"})
            extract.fetch_cached({"url": "https://x.com/f", "title": "t"})
        self.assertEqual(m.call_count, 1)   # 同一运行内死候选不重复抓（None 也缓存）
```

（`from unittest.mock import patch` 与 `from app import extract` 若文件顶部没有则补。）

- [ ] **Step 2: 跑测试确认失败**

```bash
python -m unittest tests.test_extract -k cache -v
```
Expected: FAIL（clear_fetch_cache/fetch_cached 不存在）

- [ ] **Step 3: 实现**——`app/extract.py` 在 `fetch_with_mirror` 定义之后加：

```python
# 运行内抓取缓存（2026-10-15）：可行性预检先抓一遍，deep_research 命中缓存
# 零重复抓取；None 也缓存（同运行内死候选不重复烧超时）。进程即单次任务，无失效问题。
_FETCH_CACHE: dict[str, dict | None] = {}


def clear_fetch_cache() -> None:
    _FETCH_CACHE.clear()


def fetch_cached(item: dict) -> dict | None:
    url = item.get("_pool_url") or item.get("url") or ""
    if not url:
        return fetch_with_mirror(item)
    if url not in _FETCH_CACHE:
        _FETCH_CACHE[url] = fetch_with_mirror(item)
    return _FETCH_CACHE[url]
```

`app/editorial.py` 的 `deep_research`（约 185 行）取文改为缓存版：

```python
            art = extract.fetch_cached(item)
```

- [ ] **Step 4: 跑测试确认通过**

```bash
python -m unittest tests.test_extract tests.test_editorial -v
```
Expected: PASS（既有 editorial 测试 patch 的是 `fetch_with_mirror` 本体，经 `fetch_cached` 全局名查找仍被命中；若个别用例因缓存串味，在该用例 setUp 加 `extract.clear_fetch_cache()`）

- [ ] **Step 5: Commit**

```bash
cd C:/Users/ASUS/Desktop/SSE-Agent/Agent && git add tech-digest/app/extract.py tech-digest/app/editorial.py tech-digest/tests/test_extract.py && git commit -m "perf(extract): 运行内抓取缓存，预检与研究阶段共享"
```

---

### Task 6: 编辑部可行性预检（LLM 前先验源可抓，0 可行快速失败）

**Files:**
- Modify: `app/editorial.py`（`run_pipeline` 开头）
- Test: `tests/test_editorial.py`

语义切分：预检全灭 = **取材层故障**（generation_error，rc≠0）；预检通过后 judge 全拒 = 内容判断（skip_quality）。今天的事故形态（11 条结构性死候选烧完整条管线后静默 skip）被此闸拦下。

- [ ] **Step 1: 写失败测试**（追加到 `tests/test_editorial.py`，候选构造沿用该文件既有 fixture/构造方式）

```python
class TestViabilityGate(unittest.TestCase):
    def _cand(self, n):
        return {"type": "news", "title": f"候选{n}", "url": f"https://x.com/{n}",
                "source": "hacker-news", "author": "a", "published_at": None,
                "fetched_at": "2026-10-15T09:15:00", "summary": f"摘要{n}",
                "ai_summary": None, "confidence": None, "trending": None}

    def test_all_unfetchable_fails_fast_without_llm(self):
        with patch.object(editorial.extract, "fetch_cached", return_value=None), \
             patch.object(editorial.llmpool, "map_chat",
                          side_effect=AssertionError("0 可行候选不得调用 LLM")):
            result = editorial.run_pipeline([self._cand(1), self._cand(2)])
        self.assertEqual(result["status"], "generation_error")
        self.assertIn("no_viable_candidates", result["reasons"])

    def test_dead_candidates_noted(self):
        captured = {}
        with patch.object(editorial.extract, "fetch_cached", return_value=None):
            result = editorial.run_pipeline([self._cand(1)])
        # attempts 里有 viability 轨迹（run_log 归因面）
        self.assertTrue(result["attempts"])
        self.assertEqual(result["attempts"][0]["stage"], "viability")
```

- [ ] **Step 2: 跑测试确认失败**

```bash
python -m unittest tests.test_editorial -k viability -v
```
Expected: FAIL（现在全死候选会走到 judge 并返回 skip_quality/generation_error 之外的老路径）

- [ ] **Step 3: 实现**——`app/editorial.py` 模块级（`TOP_N = 3` 附近）加：

```python
# 可行性预检（2026-10-15）：编辑部烧 LLM 前先验源可抓。全灭=取材层故障，
# 快速失败 rc≠0（死人开关会报），而不是 judge/skip_quality 静默吞掉。
```

`run_pipeline` 的 `try:` 之后、`scores = judge_score(...)` 之前插入：

```python
        viable, dead_count = [], 0
        for it in candidates:
            try:
                art = extract.fetch_cached(it)
            except Exception:
                art = None
            if isinstance(art, dict) and isinstance(art.get("text"), str) and art["text"].strip():
                viable.append(it)
            else:
                dead_count += 1
        if dead_count:
            attempts.append({"stage": "viability", "status": "skip_quality",
                             "reasons": ["source_unfetchable"], "dead": dead_count,
                             "total": len(candidates)})
        if not viable:
            result.update(status="generation_error", reasons=["no_viable_candidates"])
            return result
        scores = judge_score(viable, pool, preference, dry_run)
        top = [it for it in viable if _qualified(scores.get(_url(it), {}))]
```

（即原 `candidates` 变量在此之后一律改用 `viable`。）

- [ ] **Step 4: 跑测试确认通过**

```bash
python -m unittest tests.test_editorial -v
```
Expected: PASS。**注意**：既有用例若 mock 了 `fetch_with_mirror` 返回合法文（走 `fetch_cached` 仍命中）应全绿；若有用例 patch 的是 `editorial.extract` 属性对象，同样命中。若发现既有用例走 run_pipeline 且未 mock 抓取（真实试图抓 x.com），给该用例补 `patch.object(editorial.extract, "fetch_cached", return_value={"text": "正文", "blocks": []})`——逐个看失败输出决定，不许改断言放水。

- [ ] **Step 5: Commit**

```bash
cd C:/Users/ASUS/Desktop/SSE-Agent/Agent && git add tech-digest/app/editorial.py tech-digest/tests/test_editorial.py && git commit -m "feat(editorial): LLM 前可行性预检；全灭快速失败 no_viable_candidates"
```

---

### Task 7: 薄池 live 补齐（健康候选 < 12 时合并即时抓取）

**Files:**
- Modify: `main.py`（`run_daily` 取材段 + 常量）
- Test: `tests/test_main.py`

- [ ] **Step 1: 写失败测试**（追加到 `tests/test_main.py`）

```python
class TestThinPoolTopup(unittest.TestCase):
    def test_thin_pool_merges_live_candidates(self):
        import tempfile
        from tests.test_main_reader import ready_result
        tmp = tempfile.mkdtemp()
        pool_cands = [{"type": "news", "title": f"池内{i}",
                       "url": f"https://pool.example/{i}", "source": "sspai",
                       "author": "", "published_at": None,
                       "fetched_at": "2026-10-15T08:00:00", "summary": "s",
                       "ai_summary": None, "confidence": None, "trending": None}
                      for i in range(3)]
        live_news = [main._item_shape(f"live{i}", "hacker-news") if hasattr(main, "_item_shape")
                     else {"type": "news", "title": f"live{i}",
                           "url": f"https://live.example/{i}", "source": "hacker-news",
                           "author": "", "published_at": None,
                           "fetched_at": "2026-10-15T09:00:00", "summary": "s",
                           "ai_summary": None, "confidence": None, "trending": None}
                     for i in range(10)]
        captured = {}
        store = FakeStore()
        result = ready_result()

        def pipeline(candidates, **kw):
            captured.update(candidates=candidates)
            return result

        with patch("main.Store", return_value=store), \
             patch("main._pool_daily_materials", return_value=(pool_cands, [])), \
             patch("main.fetch_all", return_value=(live_news, {})), \
             patch("main._mark_pool_consumed"), patch("main._sample_views"), \
             patch("main.should_skip", return_value=(False, "")), \
             patch("main.preference.load_preference", return_value=main.preference.Preference()), \
             patch("main.settings.sse_market_api_key", "offline-model"), \
             patch("main.settings.deepseek_api_key", ""), \
             patch("main.settings.output_dir", Path(tmp)), \
             patch("main.settings.data_dir", Path(tmp)), \
             patch("main.settings.market_refresh_token", ""), \
             patch("main.settings.market_user_telephone", ""), \
             patch("main.settings.publish_weekend", True), \
             patch("main.editorial.run_pipeline", side_effect=pipeline):
            rc = run_daily(force=True, dry_run=True)
        self.assertEqual(rc, 0)
        urls = [c["url"] for c in captured["candidates"]]
        self.assertTrue(any(u.startswith("https://pool.example/") for u in urls))
        self.assertTrue(any(u.startswith("https://live.example/") for u in urls))
        self.assertLessEqual(len(urls), main.POOL_CANDIDATE_LIMIT)
```

- [ ] **Step 2: 跑测试确认失败**

```bash
python -m unittest tests.test_main -k ThinPool -v
```
Expected: FAIL（当前薄池不补齐，candidates 只有池内 3 条）

- [ ] **Step 3: 实现**——`main.py` 常量区（`POOL_CANDIDATE_LIMIT = 24` 之后）：

```python
# 池模式保底线（2026-10-15）：去死源后健康候选低于此数时合并 live 抓取补齐。
# 当日 11 条候选（知乎剔后）全灭的形态在此被兜住。
POOL_MIN_CANDIDATES = 12
```

`run_daily` 取材段改为：

```python
        try:
            material = _pool_daily_materials()
            pool_mode = material is not None
            if material is not None:
                news, trending = material
                if len(news) < POOL_MIN_CANDIDATES:
                    topup_items, _ = fetch_all()
                    live_news = [it for it in topup_items if it["type"] == "news"]
                    seen = {normalize_url(c.get("url") or "") for c in news}
                    added = [c for c in _daily_candidates(live_news)
                             if normalize_url(c.get("url") or "") not in seen]
                    log.info("池内候选 %d < 保底线 %d，live 补齐 +%d",
                             len(news), POOL_MIN_CANDIDATES, len(added))
                    news = news + added[:POOL_CANDIDATE_LIMIT - len(news)]
            else:
                items, stats = fetch_all()
                trending = [it for it in items if it["type"] == "trending"]
                raw_news = [it for it in items if it["type"] == "news"]
                news, dedup_info = dedup.dedupe_news(raw_news, _dedup_history(store, day))
```

- [ ] **Step 4: 跑测试确认通过**

```bash
python -m unittest tests.test_main -v
```
Expected: PASS（既有 `_run` 走 `_pool_daily_materials=None` 的 live 路径，不触补齐）

- [ ] **Step 5: Commit**

```bash
cd C:/Users/ASUS/Desktop/SSE-Agent/Agent && git add tech-digest/main.py tech-digest/tests/test_main.py && git commit -m "feat(daily): 薄池 live 补齐（健康候选<12 兜底）"
```

---

### Task 8: rc 契约对齐「每天必发」（假成功根治，INC-QD03 挂账关闭）

**Files:**
- Modify: `main.py`（`_publish_md`、`run_daily`、`run_star`、`run_weekly`）
- Modify: `tests/test_main_reader.py`（skip_quality 期望 rc 0→1）
- Test: `tests/test_main.py`（新增 rc 契约用例）

新契约：**当天未发布 → rc=1**，豁免四类：dup（今日已发）、weekend 关（publish_weekend=0 的既定模式）、显式关/凭据缺失（运营态，保持 rc=0 与 dry-run 门禁兼容）、dry-run。load 熔断跳过 → rc=1（用户裁决 2026-10-15：熔断=没发=该叫人）。

- [ ] **Step 1: 写失败测试**（追加到 `tests/test_main.py`）

```python
class TestRcContract(unittest.TestCase):
    def test_skip_quality_returns_nonzero(self):
        import tempfile
        from tests.test_main_reader import ready_result
        tmp = tempfile.mkdtemp()
        result = ready_result()
        result.update(status="skip_quality", reasons=["no_qualified_candidates"])
        rc, _, store, _ = _run(tmp, result=result)
        self.assertEqual(rc, 1)

    def test_load_skip_returns_nonzero(self):
        import tempfile
        tmp = tempfile.mkdtemp()
        store = FakeStore()
        with patch("main.Store", return_value=store), \
             patch("main.should_skip", return_value=(True, "load 9.9")):
            rc = run_daily(force=True, dry_run=False)
        self.assertEqual(rc, 1)

    def test_publish_failure_returns_nonzero(self):
        import tempfile
        from tests.test_main_reader import ready_result
        tmp = tempfile.mkdtemp()
        result = ready_result()
        store = FakeStore()
        raw = [result["item"]]

        def pipeline(candidates, **kw):
            return result

        with patch("main.Store", return_value=store), \
             patch("main.fetch_all", return_value=(raw, {})), \
             patch("main._pool_daily_materials", return_value=None), \
             patch("main._mark_pool_consumed"), patch("main._sample_views"), \
             patch("main.should_skip", return_value=(False, "")), \
             patch("main.preference.load_preference", return_value=main.preference.Preference()), \
             patch("main.settings.output_dir", Path(tmp)), \
             patch("main.settings.data_dir", Path(tmp)), \
             patch("main.settings.market_refresh_token", "tok"), \
             patch("main.settings.market_user_telephone", "13800000000"), \
             patch("main.settings.publish_weekend", True), \
             patch("main.editorial.run_pipeline", side_effect=pipeline), \
             patch("app.publisher.publish_md", return_value=(None, None, "HTTP 500")):
            rc = run_daily(force=True, dry_run=False)
        self.assertEqual(rc, 1)
```

（`publisher.publish_md` 的实际调用签名若为 `publish_md(title, md, private=...)` 与 mock 返回三元组 `(post_id, url, err)` 对齐即可；以 `app/publisher.py` 实际为准。）

- [ ] **Step 2: 跑测试确认失败**

```bash
python -m unittest tests.test_main -k RcContract -v
```
Expected: 三个用例全 FAIL（现在分别返回 0/0/0）

- [ ] **Step 3: 实现 `_publish_md`**——返回值由 bool 改为结果串。签名行改为：

```python
def _publish_md(store: Store, task: str, day: str, title: str, md: str,
                dry_run: bool, private: bool = False, content_meta: dict | None = None) -> str:
    """published|failed|weekend|dup|disabled|no_credentials|dryrun 之一。"""
```

各分支 return 改为：weekend → `return "weekend"`；daily_publish=0 → `return "disabled"`；无凭据 → `return "no_credentials"`；已发 dup → `return "dup"`；dry-run 段末（token ok/fail 两路之后）→ `return "dryrun"`；发帖循环两败 → `return "failed"`；成功 → `return True` 处改 `return "published"`。

`run_daily` 发布段（原 518-527 行）改为：

```python
        publish_result = _publish_md(store, "daily", day, title, md, dry_run, private,
                                     content_meta={"article_type": draft["article_type"],
                                                   "learning_question": draft["learning_question"],
                                                   "evidence_count": len(draft["evidence_ids"]),
                                                   "source_hash": source["source_hash"],
                                                   "draft_hash": review["draft_hash"]})
        if publish_result == "published" and not dry_run:
            _mark_pool_consumed(item, day)
        _sample_views(store)
        return 1 if publish_result == "failed" else 0
```

`run_daily` 决策非 ready 段（原 493 行）：`return 1 if status == "generation_error" else 0` 改为 `return 1`；load 段（原 456 行）`return 0` 改 `return 1`（log 仍记 `skipped/load` 留痕）。

`run_star`：load 段 `return 0` → `return 1`；末尾 `_publish_md(...)` 后：

```python
        publish_result = _publish_md(store, "star", day, title, md, dry_run, private)
        return 1 if publish_result == "failed" else 0
```

`run_weekly`：load 段 `return 0` → `return 1`；发帖循环两败（`attempt == 2` 分支）改为：

```python
            if attempt == 2:
                store.log("weekly", "degraded", {"detail": "publish", "error": err})
                store.cleanup()
                return 1
```

- [ ] **Step 4: 更新 test_main_reader 期望**——`assert_blocked` 加关键字参数并把全部 skip_quality 期望 rc 0→1：

```python
    def assert_blocked(self, result, expected_rc, *, material="pool",
                       expected_status="generation_error"):
        ...
        self.assertEqual(record["status"], expected_status)
```

调用点变换规则（grep `assert_blocked(result,` 全量过一遍）：
- `assert_blocked(result, 0, material=material)`（skip_quality 类：low_score、incomplete_source 等期望 0 的）→ `assert_blocked(result, 1, material=material, expected_status="skip_quality")`
- 其余 `assert_blocked(result, 1, ...)` → 补 `expected_status="generation_error"`（或靠默认值）
- `test_producer_skip_and_failure_cannot_be_overridden_by_passing_gate`（192 行）改为：

```python
    def test_producer_skip_and_failure_cannot_be_overridden_by_passing_gate(self):
        for status in ("skip_quality", "generation_error"):
            with self.subTest(status=status):
                self.store = TrackedStore()
                result = ready_result()
                result.update(status=status, reasons=["producer_declined"])
                calls = self.assert_blocked(result, 1, expected_status=status)
                self.assertEqual(calls["gate"].call_count, 1)
```

- [ ] **Step 5: 全量跑 main 相关测试确认通过**

```bash
python -m unittest tests.test_main tests.test_main_reader tests.test_publish_state tests.test_ingest -v
```
Expected: PASS。凡因 rc 收紧而红的既有用例：判断其语义——若该用例描述的是旧契约（skip_quality=健康），改期望；若暴露的是新契约引入的真 bug，修实现。**不许删用例。**

- [ ] **Step 6: Commit**

```bash
cd C:/Users/ASUS/Desktop/SSE-Agent/Agent && git add tech-digest/main.py tech-digest/tests/test_main.py tech-digest/tests/test_main_reader.py && git commit -m "fix(daily): rc 契约=当天未发布即失败；关闭 INC-QD03 发帖失败 rc=0 挂账"
```

---

### Task 9: 死人开关——qdock_cron_alert.sh 改期望驱动（qiudock 仓）

**Files:**
- Modify: `C:/Users/ASUS/Desktop/SSE-Agent/qiudock/scripts/qdock_cron_alert.sh`（run_log 扫描段，56-64 行）

判定从「扫 degraded/error」改为「今日无 ok+post_id 即报，附最新条目」——与过程状态无关，覆盖 skipped 及一切未来未知失败形态。契约前提：daily 每天必发（WEEKEND_PUBLISH=1 武装中）、star 周六、weekly 周日。

- [ ] **Step 1: 本地逻辑演练**（改前先验证新判定逻辑，用临时 sqlite 复现三种日子）

```bash
python3 - <<'EOF'
import sqlite3, tempfile, os
db = tempfile.mktemp(suffix=".db")
conn = sqlite3.connect(db)
conn.execute("CREATE TABLE run_log(id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, task TEXT, status TEXT, detail TEXT)")
rows = [("2026-10-15T09:20:00", "daily", "skipped", '{"detail":"skip_quality","reasons":["no_qualified_candidates"]}'),
        ("2026-10-14T09:20:00", "daily", "ok", '{"detail":"published","post_id":6884}')]
conn.executemany("INSERT INTO run_log(ts, task, status, detail) VALUES(?,?,?,?)", rows)
conn.commit()
for day in ("2026-10-15", "2026-10-14"):
    entries = conn.execute("SELECT ts, status, detail FROM run_log WHERE task='daily' AND ts >= ? ORDER BY id", (day,)).fetchall()
    published = any(st == "ok" and '"post_id"' in dt for _, st, dt in entries)
    verdict = "healthy" if published else (f"ALERT 未发布（最新: {entries[-1][1]} @{entries[-1][0]}）" if entries else "MISSING")
    print(day, "->", verdict)
os.remove(db)
EOF
```
Expected:
```
2026-10-15 -> ALERT 未发布（最新: skipped @2026-10-15T09:20:00）
2026-10-14 -> healthy
```

- [ ] **Step 2: 修改脚本**——`by_task` 内层循环（56-62 行）替换为：

```bash
    for t in check:
        entries = by_task.get(t, [])
        if any(st == 'ok' and '"post_id"' in dt for _, st, dt in entries):
            continue
        # 死人开关（2026-10-15）：契约=当天必发；只判「今日是否发布过」，与过程
        # 状态无关——skipped（skip_quality/负载）等一切未发布形态照报。旧版只扫
        # degraded/error，skip_quality rc=0 曾四层观测全瞎（10-15 事故）。
        if not entries:
            continue  # 无运行记录=缺勤面，前面 runs 检查已报 MISSING
        ts, st, dt = entries[-1]
        out.append(f'  {t}: 未发布（最新: {st} @{ts} {dt[:120]}）')
```

- [ ] **Step 3: 部署后服务器 dryrun 验证**（健康日应 0 输出；留待部署阶段执行）

```bash
ssh <集市机> 'DRYRUN=1 bash /root/qiudock/scripts/qdock_cron_alert.sh'
```
Expected: 无 FAILS 输出（若当日已正常发布）

- [ ] **Step 4: Commit（qiudock 仓）**

```bash
cd C:/Users/ASUS/Desktop/SSE-Agent/qiudock && git add scripts/qdock_cron_alert.sh && git commit -m "fix(alert): cron 告警改死人开关——只判今日未发布，收掉 skipped 盲区"
```

---

### Task 10: 全量回归 + 推送

- [ ] **Step 1: 全量测试**

```bash
cd C:/Users/ASUS/Desktop/SSE-Agent/Agent/tech-digest && python -m unittest discover -s tests -v 2>&1 | tail -5
```
Expected: OK（全绿；基线 614+，新增约 12 用例）

- [ ] **Step 2: 推送 Agent 仓（本机需代理）**

```bash
cd C:/Users/ASUS/Desktop/SSE-Agent/Agent && git -c http.proxy=http://127.0.0.1:7890 -c https.proxy=http://127.0.0.1:7890 push origin main
```

- [ ] **Step 3: 推送 qiudock 仓**

```bash
cd C:/Users/ASUS/Desktop/SSE-Agent/qiudock && git -c http.proxy=http://127.0.0.1:7890 -c https.proxy=http://127.0.0.1:7890 push origin main
```

---

### Task 11: 生产部署与闭环验证（需用户确认后执行）

- [ ] **Step 1**: 集市机/秋坞发布新平台版本（门禁 dry-run 会吃新 rc 契约：dry-run 不过线会拒版本——这是期望行为）。遵循发布通道 v2：一次只动一个部署。
- [ ] **Step 2**: 部署后在服务器跑一次 `daily --dry-run` 观察：可行性预检日志（viability dead 数）、池补齐日志、6 新源 fetch 统计。
- [ ] **Step 3**: 等次日 09:15 cron + 10:45 告警窗口：确认发布正常、无邮件；再人为观察一次失败形态的告警路径（如需，可用临时 suspend cron + 补跑制造）。
- [ ] **Step 4**: 结果回写本计划顶部进度标注。

---

## Self-Review 结论

- 规格覆盖：P0-A→Task 9、P0-B→Task 8、P1-A→Task 1/2/7、P1-B→Task 5/6、源扩展→Task 2/3/4。无缺。
- 类型一致性：`_publish_md -> str` 的 7 个结果串在 Task 8 内自洽；`fetch_cached`/`clear_fetch_cache` 命名在 Task 5/6 一致；`KNOWN_SOURCES` 值为 `(module, function)` 二元组，Task 2 测试与实现一致。
- 已知风险：①既有 editorial/main 用例可能依赖旧 rc 或未 mock 抓取——Task 6 Step 4 / Task 8 Step 5 给了处置规则；②部署 spec 若显式配 TECH_DIGEST_SOURCES 需 Task 8 同步（Task 2 Step 4 检查）；③掘金 API 字段以实测为准（Task 4 fixture 来自 2026-10-15 实抓）。
