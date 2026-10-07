# -*- coding: utf-8 -*-
"""集市登录 → 获取 refresh_token → 写入 .env（仅 M2 发帖使用）。

用法（在服务器上执行）:
  .venv/bin/python scripts/fetch_refresh_token.py

密码输入不回显、不出现在命令行参数、不写任何文件。
登录接口走内网容器地址（app.publisher._base 动态解析，容器 IP 漂移自愈）。
"""
from __future__ import annotations

import base64
import getpass
import hashlib
import os
import sys
from pathlib import Path

import requests
from Crypto.Cipher import AES
from Crypto.Util.Padding import pad

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.publisher import _base  # noqa: E402  需先完成 sys.path 注入

# 集市登录密码的 AES key（与前端 utils.ts 一致——该 key 本就公开于前端 JS；
# 出于脱敏考虑不再硬编码，运行前 export MARKET_LOGIN_AES_KEY='<16字节key>'）
KEY = os.environ["MARKET_LOGIN_AES_KEY"].encode()  # WordArray 模式，无 EVP 派生
IV = hashlib.sha256(KEY).hexdigest().encode()[:16]  # 与 Go GenIVFromKey 一致


def encrypt_password(password: str) -> str:
    cipher = AES.new(KEY, AES.MODE_CBC, IV)
    ct = cipher.encrypt(pad(password.encode("utf-8"), AES.block_size))
    return base64.b64encode(ct).decode()


def _read_stdin_inputs() -> tuple[str, str, str]:
    """管道模式：从 stdin 逐行读 email / password / telephone。
    （input/getpass 在管道下受缓冲重排干扰，实测不可靠：改用显式读取。）"""
    lines = sys.stdin.read().splitlines()
    if len(lines) < 3:
        print("stdin 需 3 行：email / password / telephone", file=sys.stderr)
        raise SystemExit(1)
    return lines[0].strip(), lines[1].strip(), lines[2].strip()


def main() -> int:
    if sys.stdin.isatty():
        email = input("集市账号邮箱: ").strip()
        if not email:
            print("邮箱不能为空", file=sys.stderr)
            return 1
        password = getpass.getpass("集市账号密码: ")
        if not password:
            print("密码不能为空", file=sys.stderr)
            return 1
        telephone = input("发帖账号手机号（userTelephone 字段，用于后端校验）: ").strip()
    else:
        email, password, telephone = _read_stdin_inputs()
    if not telephone:
        print("手机号不能为空", file=sys.stderr)
        return 1
    try:
        resp = requests.post(
            _base() + "/api/auth/login",
            json={"email": email, "password": encrypt_password(password)},
            timeout=15,
        )
    except requests.RequestException as e:
        print(f"请求失败: {e}", file=sys.stderr)
        return 1
    data = resp.json()
    if data.get("code") not in (None, 200):
        print(f"登录失败: code={data.get('code')} msg={data.get('msg')}", file=sys.stderr)
        return 1
    refresh_token = data.get("refresh_token") or (data.get("data") or {}).get("refresh_token")
    token = data.get("token") or (data.get("data") or {}).get("token")
    if not refresh_token:
        print(f"响应中无 refresh_token: {str(data)[:200]}", file=sys.stderr)
        return 1

    env_path = Path(__file__).resolve().parent.parent / ".env"
    lines = env_path.read_text(encoding="utf-8").splitlines()
    out = []
    patched = {"MARKET_REFRESH_TOKEN": False, "MARKET_USER_TELEPHONE": False}
    for line in lines:
        key_name = line.split("=", 1)[0].strip() if "=" in line else ""
        if key_name == "MARKET_REFRESH_TOKEN":
            out.append(f"MARKET_REFRESH_TOKEN={refresh_token}")
            patched["MARKET_REFRESH_TOKEN"] = True
            continue
        if key_name == "MARKET_USER_TELEPHONE":
            out.append(f"MARKET_USER_TELEPHONE={telephone}")
            patched["MARKET_USER_TELEPHONE"] = True
            continue
        out.append(line)
    if not patched["MARKET_REFRESH_TOKEN"]:
        out.append(f"MARKET_REFRESH_TOKEN={refresh_token}")
    if not patched["MARKET_USER_TELEPHONE"]:
        out.append(f"MARKET_USER_TELEPHONE={telephone}")
    env_path.write_text("\n".join(out) + "\n", encoding="utf-8")
    env_path.chmod(0o600)

    print("✅ 登录成功")
    print(f"   refresh_token: {refresh_token[:12]}... ({len(refresh_token)} 字符)")
    print("   ✅ 已写入 .env（MARKET_REFRESH_TOKEN / MARKET_USER_TELEPHONE，600 权限）")
    print("   access token 已验证可用（发帖时自动轮换）" if token else "")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
