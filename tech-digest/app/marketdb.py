# -*- coding: utf-8 -*-
"""集市 MySQL 只读查询——定位自己的 postID、采样帖子表现、回读可见性。

2026-09-13 新增。两件事都卡在同一个缺口上：集市后端发帖成功时返回 data=nil，
拿不到 postID（2026-08-30 实测），于是「1 楼补楼」和「表现追踪」都没有目标可指。

传输（缺口⑤，2026-10-13 评审）：
  · docker（默认，宿主机语义不变）：与集市同机时 `docker exec sse_market_db mysql`
    ——沿袭 publisher._base() 的 docker 依赖，不新增网络暴露面；
  · tcp / auto（秋坞 pod 用）：pod 里没有 docker，但与集市同机（k3s 同宿主），
    MySQL 3506 端口对本机 pod 可达 → pymysql 直连**只读账号**（SELECT-only 授权，
    比宿主机 root 更贴合只读硬约束）。pymysql 延迟导入：docker 模式零新依赖。

**只读是硬约束**：本模块只跑 SELECT（传输入口统一把守，非 SELECT 直接拒绝）。
发帖与评论一律走 publisher 的 API，不绕过业务逻辑改库。

查不到/不可用 → None/{}（调用方降级：补楼跳过、追踪记日志），不抛异常。
"""
from __future__ import annotations

import logging
import subprocess
from datetime import datetime

from app.config import settings

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


def _run_docker(sql: str) -> list[list[str]] | None:
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


def _connect_tcp():
    """TCP 连接（独立函数便于测试注入）。pymysql 延迟导入：仅 TCP 传输需要。"""
    import pymysql   # noqa: PLC0415 纯 Python 客户端；docker 模式永远不触发
    return pymysql.connect(
        host=settings.market_mysql_host, port=settings.market_mysql_port,
        user=settings.market_mysql_user, password=settings.market_mysql_password,
        database=DB, charset="utf8mb4",
        connect_timeout=_TIMEOUT, read_timeout=_TIMEOUT, write_timeout=_TIMEOUT)


def _run_tcp(sql: str) -> list[list[str]] | None:
    """pymysql 直连只读账号跑一条 SELECT。未配密码/失败 → None（降级，不抛）。"""
    if not settings.market_mysql_password:
        return None   # 未配置只读账号 → 视为不可用，与 docker 缺失同语义
    try:
        conn = _connect_tcp()
    except Exception as e:  # noqa: BLE001 连接失败按不可用降级
        log.warning("集市库 TCP 连接失败（按不可用降级）: %s", type(e).__name__)
        return None
    try:
        with conn.cursor() as cur:
            cur.execute(sql)
            rows = cur.fetchall()
        # 与 docker -N -B 输出同形：全字符串化，None → ""（_split_rows 消费方零适配）
        return [[("" if c is None else str(c)) for c in row] for row in rows]
    except Exception as e:  # noqa: BLE001
        log.warning("集市库 TCP 查询失败（按不可用降级）: %s", type(e).__name__)
        return None
    finally:
        try:
            conn.close()
        except Exception:  # noqa: BLE001
            pass


def _run(sql: str) -> list[list[str]] | None:
    """传输入口：按 settings.market_read 分发（docker|tcp|auto）。

    只读硬约束在这里统一把守：非 SELECT 一律拒绝（两条传输都只该见到查询）。
    auto = 先 docker，不可用自动落 tcp——宿主机与 pod 同一份代码各自工作。
    """
    if not sql.lstrip().upper().startswith("SELECT"):
        log.warning("marketdb 只读硬约束：拒绝非 SELECT 查询")
        return None
    mode = settings.market_read
    if mode in ("docker", "auto"):
        rows = _run_docker(sql)
        if rows is not None or mode == "docker":
            return rows
    if mode in ("tcp", "auto"):
        return _run_tcp(sql)
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
