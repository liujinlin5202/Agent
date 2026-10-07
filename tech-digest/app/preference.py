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
