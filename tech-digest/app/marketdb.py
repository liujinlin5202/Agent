# -*- coding: utf-8 -*-
"""集市 MySQL 只读查询（docker exec）——定位自己的 postID、采样帖子表现。

2026-09-13 新增。两件事都卡在同一个缺口上：集市后端发帖成功时返回 data=nil，
拿不到 postID（2026-08-30 实测），于是「1 楼补楼」和「表现追踪」都没有目标可指。

部署机与集市同机（Go 后端容器 `server`、MySQL 容器 `sse_market_db`，库 `market`），
publisher._base() 本来就用 docker inspect 解析后端 IP —— docker 依赖已经存在，
这里沿用同一台机器上的 docker exec，不新增网络暴露面、不新增 Python 依赖。

**只读是硬约束**：本模块只跑 SELECT。发帖与评论一律走 publisher 的 API，
不绕过业务逻辑改库（否则会与集市用户表/热度/通知等副作用脱节）。

开发机没有 docker/容器 → 每个函数返回 None/{}，调用方据此降级（补楼跳过、
追踪只在部署机上跑），不抛异常。
"""
from __future__ import annotations

import logging
import subprocess
from datetime import datetime

log = logging.getLogger("tech-digest")

CONTAINER = "sse_market_db"
DB = "market"
_TIMEOUT = 15


def _lit(s: str) -> str:
    """MySQL 字符串字面量（转义反斜杠与单引号）。"""
    return "'" + str(s).replace("\\", "\\\\").replace("'", "\\'") + "'"


def _split_rows(stdout: str | None) -> list[list[str]]:
    """mysql -N -B 输出 → 行列表。制表符分隔、无表头、末尾空行丢弃。"""
    out: list[list[str]] = []
    for line in (stdout or "").splitlines():
        if not line.strip():
            continue
        out.append(line.split("\t"))
    return out


def _run(sql: str) -> list[list[str]] | None:
    """docker exec 跑一条 SELECT。不可用/失败 → None（调用方降级，不抛）。"""
    try:
        pw = subprocess.run(
            ["docker", "exec", CONTAINER, "printenv", "MYSQL_ROOT_PASSWORD"],
            capture_output=True, text=True, timeout=_TIMEOUT, check=True).stdout.strip()
        if not pw:
            return None
        out = subprocess.run(
            ["docker", "exec", CONTAINER, "mysql", "--default-character-set=utf8mb4",
             "-uroot", f"-p{pw}", "-N", "-B", "-e", f"USE {DB}; {sql}"],
            capture_output=True, text=True, timeout=_TIMEOUT, check=True)
        return _split_rows(out.stdout)
    except (subprocess.SubprocessError, OSError) as e:
        log.warning("集市库查询失败（按不可用降级）: %s", e)
        return None


def available(runner=None) -> bool:
    """集市库是否可查（开发机为 False）。"""
    return (runner or _run)("SELECT 1") is not None


# ---------------- SQL 构造（纯函数，便于单测）----------------

def find_post_id_sql(user_id: int, title: str, since: datetime) -> str:
    """按「本人 + 标题逐字一致 + 发帖时间窗」定位刚发的帖。

    标题是我们自己发出去的那一串（publisher 已按 30 字截断），精确匹配即可；
    同一标题重发（--force）取最新一帖 —— ORDER BY postID DESC LIMIT 1。
    """
    return ("SELECT postID FROM posts "
            f"WHERE userID={int(user_id)} AND title={_lit(title)} "
            f"AND post_time>='{since.strftime('%Y-%m-%d %H:%M:%S')}' "
            "ORDER BY postID DESC LIMIT 1")


def stats_sql(post_ids: list[int]) -> str:
    """批量取浏览/赞/评/热度。空列表返回空串（调用方跳过查询）。"""
    if not post_ids:
        return ""
    ids = ",".join(str(int(i)) for i in post_ids)
    return ("SELECT postID, browse_num, like_num, comment_num, heat "
            f"FROM posts WHERE postID IN ({ids})")


def user_id_sql(phone: str) -> str:
    return f"SELECT userID FROM users WHERE phone={_lit(phone)} ORDER BY userID LIMIT 1"


def posts_since_sql(user_id: int, since_day: str) -> str:
    """取本人自某日起的公开帖（表现采样 / 历史基线）。"""
    return ("SELECT postID, post_time, browse_num, like_num, comment_num, heat, title "
            f"FROM posts WHERE userID={int(user_id)} AND post_time>='{since_day} 00:00:00' "
            "AND is_private=0 ORDER BY postID")


def post_visibility_sql(post_id: int) -> str:
    """回读刚发的帖的可见性（2026-09-20 星榜帖被写私密事故的校验查询）。"""
    return f"SELECT is_private FROM posts WHERE postID={int(post_id)}"


# ---------------- 对外接口 ----------------

def user_id_of(phone: str, runner=None) -> int | None:
    """手机号 → userID（.env 里配的是手机号，库里存的是 userID）。"""
    if not phone:
        return None
    rows = (runner or _run)(user_id_sql(phone))
    if not rows:
        return None
    try:
        return int(rows[0][0])
    except (ValueError, IndexError):
        return None


def find_post_id(user_id: int, title: str, since: datetime, runner=None) -> int | None:
    """刚发的帖的 postID；查不到（或库不可用）→ None。"""
    if not user_id or not title:
        return None
    rows = (runner or _run)(find_post_id_sql(user_id, title, since))
    if not rows:
        return None
    try:
        return int(rows[0][0])
    except (ValueError, IndexError):
        return None


def post_stats(post_ids: list[int], runner=None) -> dict[int, dict]:
    """{postID: {browse, likes, comments, heat}}；查不到的 id 不在结果里。"""
    sql = stats_sql(post_ids)
    if not sql:
        return {}
    rows = (runner or _run)(sql)
    out: dict[int, dict] = {}
    for r in rows or []:
        try:
            out[int(r[0])] = {"browse": int(r[1]), "likes": int(r[2]),
                              "comments": int(r[3]), "heat": float(r[4])}
        except (ValueError, IndexError):
            continue
    return out


def post_is_private(post_id: int, runner=None) -> bool | None:
    """帖子是否私密；查不到/库不可用/字段异常 → None（未知，调用方按「无法确认」处理）。"""
    rows = (runner or _run)(post_visibility_sql(post_id))
    if not rows:
        return None
    try:
        return bool(int(rows[0][0]))
    except (ValueError, IndexError):
        return None


def posts_since(user_id: int, since_day: str, runner=None) -> list[dict]:
    """本人自 since_day（含）起的公开帖，按 postID 升序。

    返回 [{post_id, day, browse, likes, comments, heat, title}]；
    行格式不对的直接跳过（列被改过时不至于整个脚本炸掉）。
    """
    if not user_id or not since_day:
        return []
    rows = (runner or _run)(posts_since_sql(user_id, since_day))
    out: list[dict] = []
    for r in rows or []:
        try:
            out.append({"post_id": int(r[0]), "day": r[1][:10],
                        "browse": int(r[2]), "likes": int(r[3]),
                        "comments": int(r[4]), "heat": float(r[5]), "title": r[6]})
        except (ValueError, IndexError):
            continue
    return out
