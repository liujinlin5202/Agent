# -*- coding: utf-8 -*-
"""content pool（编辑部 M1）：跨班次素材池的数据访问层。

决策留底：docs/superpowers/plans/2026-10-08-editorial-m1-content-pool.md
  · D1 与现有表同库；连接复用 store.connect_db（WAL/busy_timeout 全仓单点）
  · D3 raw 列存完整 v2 条目 JSON——候选行对 gate/ai_pick/渲染零适配
  · D4 粗排 rank_score = heat + source_weight*10 - pool_age_days*5
  · D5 去重权威键=规范化 URL（UNIQUE）；标题模糊 0.85 查近 14 天任意状态行
  · D7 trending 行独立生命周期（最新一班语义 + 7 天清理）
  · D9 反爬冷却 24h 记 meta 表
"""
from __future__ import annotations

import json
import logging
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path

from app.config import settings
from app.dedup import is_duplicate, normalize_url
from app.store import SCHEMA, connect_db

log = logging.getLogger("tech-digest.pool")

# 标题查重回看窗口：对齐 main._dedup_history 的 14 期口径（v4 起的防重发标准）
TITLE_DEDUP_WINDOW_DAYS = 14
# trending「最新一班」判定窗：ingest 每 2h 一班，3h 窗含正常抖动
TRENDING_BATCH_WINDOW_H = 3

SOURCE_WEIGHT = {
    "zhihu-hot": 8, "hacker-news": 8, "devto": 8, "sspai": 8,
    "wallstreetcn": 6, "ars-technica": 6, "ieee-spectrum": 6,
    "freecodecamp": 6, "ruanyifeng": 6,
}
DEFAULT_SOURCE_WEIGHT = 5
HEAT_DEFAULT = 50.0          # 无热度信号的源（订阅系）保底可见分

POOL_SCHEMA = """
CREATE TABLE IF NOT EXISTS pool_items (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  kind          TEXT NOT NULL DEFAULT 'news',   -- news | trending
  url           TEXT NOT NULL,                  -- normalize_url 后的规范化 URL
  title         TEXT NOT NULL,
  source        TEXT NOT NULL,
  author        TEXT NOT NULL DEFAULT '',
  published_at  TEXT,
  raw           TEXT NOT NULL,                  -- 原始 v2 条目 JSON（无损）
  heat          REAL NOT NULL DEFAULT 0,
  source_weight REAL NOT NULL DEFAULT 0,
  age_days      REAL NOT NULL DEFAULT 0,
  status        TEXT NOT NULL DEFAULT 'candidate',
  score         REAL,
  score_detail  TEXT,
  research      TEXT,
  review        TEXT,
  digest_date   TEXT,
  first_seen    TEXT NOT NULL,
  last_seen     TEXT NOT NULL,
  -- 身份=（kind, url）：同一 GitHub 仓库可以同时以 news（HN 直链）与 trending
  -- 两种身份在池，两条泳道的生命周期互不干扰
  UNIQUE(kind, url)
);
CREATE INDEX IF NOT EXISTS idx_pool_kind_status ON pool_items(kind, status);
CREATE INDEX IF NOT EXISTS idx_pool_last_seen ON pool_items(last_seen);
"""


def _heat_signal(it: dict) -> float | None:
    """源内热度信号（只用于排序，量纲无所谓）；None = 无信号 → 固定保底分。"""
    src = it.get("source") or ""
    t = it.get("trending") or {}
    if src in ("hacker-news", "devto"):
        pts = it.get("points") or 0
        return float(pts) if pts else None
    if src == "zhihu-hot":
        if t.get("heat"):
            return float(t["heat"])
        rank = t.get("rank")
        return -float(rank) if rank else None   # rank 越小越热 → 取负降序排
    if src == "github-trending":
        ts = t.get("today_stars") or 0
        return float(ts) if ts else None
    return None


def normalize_heat(items: list[dict]) -> dict[str, float]:
    """每源批内分位归一到 0-100（D4：粗排只要序不要绝对值，分位跨源可比）。

    返回 {规范化 url: heat}。同源内有信号的按序线性铺满 0-100（唯一有信号的
    条目=100）；无信号的条目固定 HEAT_DEFAULT。
    """
    by_src: dict[str, list[dict]] = defaultdict(list)
    for it in items:
        if it.get("url") and it.get("title"):
            by_src[it.get("source") or ""].append(it)
    out: dict[str, float] = {}
    for group in by_src.values():
        with_sig = [(it, s) for it in group if (s := _heat_signal(it)) is not None]
        n = len(with_sig)
        for i, (it, _) in enumerate(sorted(with_sig, key=lambda p: -p[1])):
            score = 100.0 if n == 1 else round(100.0 * (n - 1 - i) / (n - 1), 1)
            out[normalize_url(it["url"])] = score
        for it in group:
            u = normalize_url(it["url"] or "")
            if u and u not in out:
                out[u] = HEAT_DEFAULT
    return out


def _age_days(item: dict) -> float:
    """入池时的真实年龄（published_at 可考才有意义；不可考=0，接受热榜偏袒）。"""
    p = item.get("published_at") or ""
    try:
        dt = datetime.fromisoformat(p[:19])
        return max(0.0, (datetime.now() - dt).total_seconds() / 86400)
    except ValueError:
        return 0.0


def _source_weight(item: dict) -> float:
    return float(SOURCE_WEIGHT.get(item.get("source") or "", DEFAULT_SOURCE_WEIGHT))


def _item_row(item: dict, kind: str, heat: float, now: str) -> tuple:
    return (kind, normalize_url(item["url"]), item["title"], item.get("source") or "",
            item.get("author") or "", item.get("published_at"),
            json.dumps(item, ensure_ascii=False), heat, _source_weight(item),
            round(_age_days(item), 2), now, now)


class PoolStore:
    """pool_items 表的读写入口。ingest/daily/star 各自短期开用即关。"""

    def __init__(self, db_path: Path | None = None, conn=None):
        path = db_path or (settings.data_dir / "tech-digest.db")
        self.db_path = path
        self.conn = conn or connect_db(path)
        # SCHEMA 随取：meta（冷却键）与 pool_items 的 DDL 唯一来源仍是各自模块
        self.conn.executescript(SCHEMA)
        self.conn.executescript(POOL_SCHEMA)

    def close(self) -> None:
        self.conn.close()

    # ---------- 入池（ingest 专用） ----------

    def upsert_news(self, items: list[dict],
                    history: list[tuple[str, dict]] | None = None) -> dict:
        """news 条目入池（D5 三路：新增 / URL 刷新 / 标题撞弃）。

        history：已发布历史 [(date, item)]（main._dedup_history 口径）。池子冷启动
        时库里还没有这些文章的任何痕迹，不对齐会造成「换 URL 复活已发文章」；
        title 模糊与 URL 精确共用 dedup.is_duplicate，语义与老路径逐字一致。
        """
        now = datetime.now().isoformat(timespec="seconds")
        cutoff = (datetime.now() - timedelta(days=TITLE_DEDUP_WINDOW_DAYS)).isoformat()
        known = self.conn.execute(
            "SELECT url, title FROM pool_items WHERE kind='news' AND first_seen >= ?",
            (cutoff,)).fetchall()
        known_items = [{"url": r["url"], "title": r["title"]} for r in known]
        history = history or []
        heat_map = normalize_heat(items)
        res = {"inserted": 0, "refreshed": 0, "dup_dropped": 0, "dup_samples": []}
        for it in items:
            url = normalize_url(it.get("url") or "")
            if not url or not (it.get("title") or "").strip():
                continue
            row = self.conn.execute(
                "SELECT id FROM pool_items WHERE url=? AND kind='news'", (url,)).fetchone()
            if row:
                self.conn.execute(
                    "UPDATE pool_items SET raw=?, heat=?, last_seen=?, title=?, "
                    "published_at=? WHERE id=?",
                    (json.dumps(it, ensure_ascii=False), heat_map.get(url, HEAT_DEFAULT),
                     now, it["title"], it.get("published_at"), row["id"]))
                res["refreshed"] += 1
                continue
            if any(is_duplicate({"url": url, "title": it["title"]}, old)
                   for old in known_items) or \
               any(is_duplicate(it, old) for _, old in history):
                res["dup_dropped"] += 1
                if len(res["dup_samples"]) < 5:
                    res["dup_samples"].append({"title": it["title"], "url": url})
                continue
            known_items.append({"url": url, "title": it["title"]})
            self.conn.execute(
                "INSERT INTO pool_items(kind, url, title, source, author, published_at,"
                " raw, heat, source_weight, age_days, first_seen, last_seen)"
                " VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                _item_row(it, "news", heat_map.get(url, HEAT_DEFAULT), now))
            res["inserted"] += 1
        self.conn.commit()
        return res

    def upsert_trending(self, items: list[dict]) -> dict:
        """trending 条目入池：同仓库（同 URL）隔班重抓 → 刷新 raw（今日星数滚动）。"""
        now = datetime.now().isoformat(timespec="seconds")
        heat_map = normalize_heat(items)
        res = {"inserted": 0, "refreshed": 0}
        for it in items:
            url = normalize_url(it.get("url") or "")
            if not url or not (it.get("title") or "").strip():
                continue
            row = self.conn.execute(
                "SELECT id FROM pool_items WHERE url=? AND kind='trending'", (url,)).fetchone()
            if row:
                self.conn.execute(
                    "UPDATE pool_items SET raw=?, heat=?, last_seen=? WHERE id=?",
                    (json.dumps(it, ensure_ascii=False), heat_map.get(url, HEAT_DEFAULT),
                     now, row["id"]))
                res["refreshed"] += 1
            else:
                self.conn.execute(
                    "INSERT INTO pool_items(kind, url, title, source, author, published_at,"
                    " raw, heat, source_weight, age_days, first_seen, last_seen)"
                    " VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                    _item_row(it, "trending", heat_map.get(url, HEAT_DEFAULT), now))
                res["inserted"] += 1
        self.conn.commit()
        return res

    # ---------- 取材（daily / star） ----------

    def top_candidates(self, limit: int = 24,
                       window_days: int | None = None) -> list[dict]:
        """粗排 candidate（D4 公式），返回 v2 条目（附 _pool_url/_pool_id/_pool_score）。

        空池返回 []——调用方据此回退老路径，永不因池空停刊（D6）。
        """
        window = window_days or settings.pool_ttl_days
        cutoff = (datetime.now() - timedelta(days=window)).isoformat()
        rows = self.conn.execute(
            "SELECT id, url, raw, heat, source_weight, first_seen FROM pool_items "
            "WHERE kind='news' AND status='candidate' AND first_seen >= ?", (cutoff,)).fetchall()
        now = datetime.now()
        scored = []
        for r in rows:
            try:
                age = (now - datetime.fromisoformat(r["first_seen"])).total_seconds() / 86400
            except ValueError:
                age = 0.0
            score = r["heat"] + r["source_weight"] * 10 - age * 5
            scored.append((score, r))
        scored.sort(key=lambda p: -p[0])
        out = []
        for score, r in scored[:limit]:
            it = json.loads(r["raw"])
            it["_pool_url"] = r["url"]
            it["_pool_id"] = r["id"]
            it["_pool_score"] = round(score, 1)
            out.append(it)
        return out

    def latest_trending(self, limit: int = 15,
                        max_age_h: float = 48) -> list[dict]:
        """「最新一班」trending（D7）：只取最近一班 ingest 触碰过的行，rank 升序。

        以 MAX(last_seen) 为基准往回看一班窗（3h）——隔天掉榜的仓库不会混进
        「今日榜单」；最新一班整体老于 max_age_h 视为池子失效，返回 [] 让调用方
        回退即时抓取。
        """
        row = self.conn.execute(
            "SELECT MAX(last_seen) AS m FROM pool_items WHERE kind='trending'").fetchone()
        newest = row["m"] if row else None
        if not newest:
            return []
        ref = datetime.fromisoformat(newest)
        if datetime.now() - ref > timedelta(hours=max_age_h):
            return []
        cutoff = (ref - timedelta(hours=TRENDING_BATCH_WINDOW_H)).isoformat()
        rows = self.conn.execute(
            "SELECT raw FROM pool_items WHERE kind='trending' AND last_seen >= ?",
            (cutoff,)).fetchall()
        items = []
        for r in rows:
            it = json.loads(r["raw"])
            rank = (it.get("trending") or {}).get("rank")
            items.append(((rank if isinstance(rank, (int, float)) else 10**9), it))
        items.sort(key=lambda p: p[0])
        return [it for _, it in items[:limit]]

    # ---------- 流水线落库（M2 编辑部：断点续跑的存储面） ----------

    _STAGE_COLUMNS = ("score", "score_detail", "research", "review")

    def get_item(self, url: str) -> dict | None:
        """按规范化 URL 取池行（流水线各步读既有产物，决定跳过或重烧）。"""
        row = self.conn.execute(
            "SELECT * FROM pool_items WHERE url=? AND kind='news'", (url,)).fetchone()
        return dict(row) if row else None

    def save_stage(self, url: str, column: str, payload) -> bool:
        """写流水线产物列（列名白名单）；返回行是否存在。"""
        if column not in self._STAGE_COLUMNS:
            raise ValueError(f"非法产物列: {column}")
        text = payload if isinstance(payload, str) else json.dumps(
            payload, ensure_ascii=False)
        cur = self.conn.execute(
            f"UPDATE pool_items SET {column}=? WHERE url=? AND kind='news'",
            (text, url))
        self.conn.commit()
        return cur.rowcount > 0

    def mark_reserved(self, urls: list[str]) -> int:
        """candidate → reserved（score≥沉淀线且未入选，weekly 深读段 M3 消费）。"""
        n = 0
        for url in urls:
            cur = self.conn.execute(
                "UPDATE pool_items SET status='reserved' "
                "WHERE url=? AND kind='news' AND status='candidate'", (url,))
            n += cur.rowcount
        self.conn.commit()
        return n

    # ---------- 状态迁移 ----------

    def mark_consumed(self, urls: list[str], day: str) -> int:
        """candidate → daily_used 并记 digest_date（反馈环地基，上位计划 §5.4）。

        只动 candidate——reserved/daily_used 不该被 daily 消费（M2 起口径不变）。
        """
        n = 0
        for url in urls:
            cur = self.conn.execute(
                "UPDATE pool_items SET status='daily_used', digest_date=? "
                "WHERE url=? AND kind='news' AND status='candidate'", (day, url))
            n += cur.rowcount
        self.conn.commit()
        return n

    def sweep(self) -> dict:
        """生命周期（上位计划 §4）：candidate 超池龄→expired；reserved 超期→expired；
        expired 超保留期→DELETE；trending 超 7 天→DELETE（防榜单无限填充）。"""
        now = datetime.now()
        cand_cut = (now - timedelta(days=settings.pool_ttl_days)).isoformat()
        resv_cut = (now - timedelta(days=settings.reserved_ttl_days)).isoformat()
        purge_cut = (now - timedelta(days=settings.daily_retain_days)).isoformat()
        trend_cut = (now - timedelta(days=7)).isoformat()
        c1 = self.conn.execute(
            "UPDATE pool_items SET status='expired' "
            "WHERE status='candidate' AND kind='news' AND first_seen < ?", (cand_cut,)).rowcount
        c2 = self.conn.execute(
            "UPDATE pool_items SET status='expired' "
            "WHERE status='reserved' AND kind='news' AND first_seen < ?", (resv_cut,)).rowcount
        c3 = self.conn.execute(
            "DELETE FROM pool_items WHERE status='expired' AND first_seen < ?", (purge_cut,)).rowcount
        c4 = self.conn.execute(
            "DELETE FROM pool_items WHERE kind='trending' AND last_seen < ?", (trend_cut,)).rowcount
        self.conn.commit()
        return {"expired_candidate": c1, "expired_reserved": c2,
                "purged": c3, "trending_purged": c4}

    # ---------- 反爬冷却（D9） ----------

    def set_cooldown(self, source: str, hours: float) -> str:
        until = (datetime.now() + timedelta(hours=hours)).isoformat(timespec="seconds")
        self.conn.execute(
            "INSERT INTO meta(key, value) VALUES('pool_cooldown:' || ?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (source, until))
        self.conn.commit()
        return until

    def cooldowns(self) -> dict[str, str]:
        """生效中的冷却 {source: until_iso}；到期的顺手清掉。"""
        rows = self.conn.execute(
            "SELECT key, value FROM meta WHERE key LIKE 'pool_cooldown:%'").fetchall()
        now = datetime.now()
        out: dict[str, str] = {}
        for r in rows:
            source = r["key"].split(":", 1)[1]
            try:
                until = datetime.fromisoformat(r["value"])
            except ValueError:
                until = None
            if until and until > now:
                out[source] = r["value"]
            else:
                self.conn.execute("DELETE FROM meta WHERE key=?", (r["key"],))
        self.conn.commit()
        return out

    # ---------- 观测 ----------

    def stats(self) -> dict:
        rows = self.conn.execute(
            "SELECT kind, status, COUNT(*) AS n FROM pool_items GROUP BY kind, status").fetchall()
        return {f"{r['kind']}:{r['status']}": r["n"] for r in rows}
