# -*- coding: utf-8 -*-
"""集市自动发帖（M2）：refresh_token 换 access token → POST /api/auth/post。

发帖是真实对外行为：main.py 中仅非 --dry-run 且发帖配置齐全时调用。
重试 ≤1 次，绝不循环调用（防重复发帖）。
后端容器 IP 漂移自愈（2026-09-02）：平台侧重启 docker 后 Go 后端从 172.18.0.5 漂到
172.18.0.6，硬编码 IP 直接弄断发帖 → _base() 每次经 docker inspect 动态解析并回写 .env。
"""
from __future__ import annotations

import json
import logging
import re
import subprocess
from pathlib import Path
from urllib.parse import urlparse

import requests

from app.config import BASE_DIR, settings

log = logging.getLogger("tech-digest")

# 集市 Go 后端容器（docker 名 server，镜像 sse_market_server:latest）的内网地址；
# 容器无宿主机端口映射（docker port 为空），只能走容器 IP —— 由 _base() 动态解析。
DEFAULT_MARKET_BASE = "http://172.18.0.5:8080"
# posts.ptext varchar(10000)，内容留余量
MAX_CONTENT_CHARS = 9000
# pcomments.pctext varchar(3000)，评论留余量
MAX_COMMENT_CHARS = 2800
# docker 自定义 bridge 网段（172.16-172.31）；只有指向该网段的 base 才参与漂移自愈，
# 显式配置的域名/127.0.0.1 不动。
_CONTAINER_IP_RE = re.compile(r"^172\.(1[6-9]|2\d|3[01])\.\d+\.\d+$")


def _docker_server_ip() -> str | None:
    """docker inspect 取集市 Go 后端容器当前 IP；容器重启会漂移（实测 .5 → .6）。"""
    try:
        out = subprocess.run(
            ["docker", "inspect", "-f",
             "{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}", "server"],
            capture_output=True, text=True, timeout=5, check=True)
        ip = out.stdout.strip()
        return ip or None
    except (subprocess.SubprocessError, OSError):
        return None


def _base() -> str:
    """发帖目标 base：显式配置优先；指向容器网段时 docker inspect 动态解析，漂移即自愈。"""
    base = settings.market_base_url or DEFAULT_MARKET_BASE
    host = urlparse(base).hostname or ""
    if _CONTAINER_IP_RE.match(host):
        ip = _docker_server_ip()
        if ip:
            fresh = f"http://{ip}:8080"
            if fresh != base:
                log.warning("集市后端容器 IP 漂移：%s → %s（已自动更新 .env）", base, fresh)
                settings.market_base_url = fresh
                _persist_env_value("MARKET_BASE_URL", fresh)
                base = fresh
    return base


def market_base() -> str:
    """集市后端 base（供 publisher 之外复用，含容器 IP 漂移自愈）。"""
    return _base()


def refresh_access_token() -> tuple[str, str] | None:
    """POST /api/auth/refresh（无鉴权）。返回 (access_token, 新refresh_token)；失败 None。"""
    try:
        resp = requests.post(
            _base() + "/api/auth/refresh",
            json={"refresh_token": settings.market_refresh_token},
            timeout=15,
        )
        if resp.status_code != 200:
            return None
        data = resp.json()
        token = data.get("token") or (data.get("data", {}) or {}).get("token")
        new_refresh = data.get("refresh_token") or (data.get("data", {}) or {}).get("refresh_token")
        if not token:
            return None
        if new_refresh:
            _persist_env_value("MARKET_REFRESH_TOKEN", new_refresh)  # 轮换：立即写回 .env
        return token, new_refresh
    except requests.RequestException:
        return None


def _persist_env_value(key: str, value: str) -> None:
    """原子更新 .env 中指定键所在行（无该行则追加）。

    秋坞 pod 模式下 .env 随 clone 蒸发，真正的持久落点是 state 盘的
    runtime_env.json（config._apply_state_overrides 启动时读回、优先级最高
    ——轮换 token 一次一换，丢了下一班就 401，2026-10-09 daily 首发翻车根因）。
    """
    _persist_state_value(key, value)
    env_path = BASE_DIR / ".env"
    try:
        text = env_path.read_text(encoding="utf-8")
    except OSError:
        return
    newline = f"{key}={value}"
    pattern = re.compile(rf"^{key}=.*$", re.M)
    if pattern.search(text):
        text = pattern.sub(newline, text)
    else:
        text = text.rstrip("\n") + "\n" + newline + "\n"
    tmp = env_path.with_suffix(".env.tmp")
    try:
        tmp.write_text(text, encoding="utf-8")
        try:
            tmp.chmod(env_path.stat().st_mode & 0o777)  # 保留原权限（.env 是 600，tmp.replace 造新文件会变 644）
        except OSError:
            pass  # 权限位复制失败不致命（如 Windows 无 chmod 语义）
        tmp.replace(env_path)
    except OSError:
        pass  # 写回失败不致命：下次 refresh 仍可继续


# 允许进入 state 盘运行时覆盖的键（与 config._apply_state_overrides 的白名单对应）
_STATE_ENV_KEYS = {"MARKET_REFRESH_TOKEN", "MARKET_BASE_URL"}


def _persist_state_value(key: str, value: str) -> None:
    """把轮换出的新值原子写入 state 盘 runtime_env.json（pod 模式的持久落点）。"""
    if key not in _STATE_ENV_KEYS:
        return
    path = settings.data_dir / "runtime_env.json"
    try:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            data = {}
        if not isinstance(data, dict):
            data = {}
        data[key] = value
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
        tmp.replace(path)
    except OSError:
        pass  # best-effort，同 .env 写回：失败不致命（当前进程内 settings 已更新）


def markdown_to_plain(md: str) -> str:
    """发帖内容预处理：集市正文为 Markdown 渲染（markdown-it + github-markdown-css），
    保留原文；仅做长度截断（posts.ptext varchar(10000) 留余量到 9000）。"""
    text = md.strip()
    if len(text) > MAX_CONTENT_CHARS:
        text = text[:MAX_CONTENT_CHARS] + "\n\n...(内容过长已截断)"
    return text


def publish_md(title: str, md: str, private: bool = False) -> tuple[int | None, str | None, str | None]:
    """发布周报。private=True 发私密帖（isPrivate=true，仅作者本人可见）。
    返回 (postID, url, error)。失败时 error 非 None（重试由调用方控制 1 次）。"""
    if not settings.market_refresh_token or not settings.market_user_telephone:
        return None, None, "缺少 MARKET_REFRESH_TOKEN 或 MARKET_USER_TELEPHONE 配置"
    auth = refresh_access_token()
    if not auth:
        return None, None, "refresh_token 换取 access token 失败"
    token, _ = auth
    payload = {
        "content": markdown_to_plain(md),
        "partition": settings.market_post_partition,
        "photos": "",
        "tagList": settings.market_post_tag,
        "title": title[:30],  # 后端 API 限制 30 字（RuneCount）；DB 列宽 90 是下限
        "userTelephone": settings.market_user_telephone,
        "isPrivate": private,
    }
    try:
        resp = requests.post(
            _base() + "/api/auth/post",
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
            json=payload,
            timeout=30,
        )
        data = resp.json()
        if data.get("code") not in (None, 200):
            return None, None, f"发帖失败 code={data.get('code')} msg={data.get('msg')}"
        post_id = (data.get("data") or {}).get("postID") or data.get("postID")
        if post_id:
            return int(post_id), f"/postdetail/{post_id}", None
        # 后端成功时 data 固定为 nil（不返回 postID，2026-08-30 实测确认）。
        # 此时必须视为成功：若当失败重试会发第二篇重复帖子。
        return None, None, None
    except (requests.RequestException, ValueError) as e:
        return None, None, f"发帖异常: {e}"


def post_comment(post_id: int, text: str) -> tuple[bool, str | None]:
    """在指定帖子下发表一级评论（日报的「补楼」：星榜完整版）。

    端点 /api/auth/postPcomment {content, postID, userTelephone}，2026-09-13 从集市
    前端 bundle assets/CommentToolbar-DswEeIFj.js 反查确认（评论创建的唯一入口）。
    返回 (是否成功, 错误信息)。

    判定口径与前端一致：只看 HTTP .ok。正文里带 code!=200 的失败响应也当成功，
    因为「实际发出去了却判失败 → 调用方重试 → 重复评论」比「漏判一次」更糟。
    """
    if post_id <= 0:
        return False, "postID 无效（后端发帖成功时不返回 postID，无法定位帖子）"
    if not settings.market_refresh_token or not settings.market_user_telephone:
        return False, "缺少 MARKET_REFRESH_TOKEN 或 MARKET_USER_TELEPHONE 配置"
    auth = refresh_access_token()
    if not auth:
        return False, "refresh_token 换取 access token 失败"
    token, _ = auth
    content = (text or "").strip()
    if len(content) > MAX_COMMENT_CHARS:
        tail = "\n…（完整榜单见正文）"
        content = content[:MAX_COMMENT_CHARS - len(tail)] + tail
    try:
        resp = requests.post(
            _base() + "/api/auth/postPcomment",
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
            json={"content": content, "postID": post_id,
                  "userTelephone": settings.market_user_telephone},
            timeout=30,
        )
        if not resp.ok:
            return False, f"补楼失败 HTTP {resp.status_code}"
        return True, None
    except Exception as e:   # noqa: BLE001 —— 补楼是锦上添花：任何异常都不能
        return False, f"补楼异常: {e}"   # 让无人值守的日报任务崩在发帖之后
