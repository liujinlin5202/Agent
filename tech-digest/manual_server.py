# -*- coding: utf-8 -*-
"""手动测试面板（MVP 用，非生产）：网页按钮触发 daily/weekly、查看产物与日志。

- 仅绑定 127.0.0.1:19086（不经公网；本机用 SSH 隧道访问，见 manual-panel.ps1）
- 每次任务以子进程运行 main.py，与生产 timer 完全隔离；不写任何发帖操作（weekly 恒为 --dry-run）
- 启动: nohup .venv/bin/python manual_server.py &   （或 scripts/manual-panel.sh start）
- 停止: scripts/manual-panel.sh stop
"""
from __future__ import annotations

import json
import subprocess
import sys
import threading
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

BASE = Path(__file__).resolve().parent
OUTPUT_DIR = BASE / "output"
LOG_FILE = BASE / "log" / "tech-digest.log"
PORT = 19086

PAGE = """<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>tech-digest 手动测试面板</title>
<style>
  :root { --bg:#0f1115; --card:#171a21; --line:#262b36; --fg:#e6e8ee; --muted:#8b93a5;
          --ok:#3fb96f; --warn:#e5a13f; --err:#e05555; --btn:#2d6cdf; }
  * { box-sizing:border-box; }
  body { margin:0; background:var(--bg); color:var(--fg);
         font:14px/1.6 "Microsoft YaHei",system-ui,sans-serif; }
  .wrap { max-width:960px; margin:0 auto; padding:24px 16px 60px; }
  h1 { font-size:20px; margin:0 0 4px; }
  .sub { color:var(--muted); margin-bottom:20px; }
  .card { background:var(--card); border:1px solid var(--line); border-radius:10px;
          padding:16px; margin-bottom:16px; }
  .card h2 { font-size:15px; margin:0 0 12px; }
  .row { display:flex; gap:10px; flex-wrap:wrap; }
  button { background:var(--btn); color:#fff; border:0; border-radius:8px;
           padding:10px 18px; font-size:14px; cursor:pointer; }
  button:hover { filter:brightness(1.15); }
  button.ghost { background:transparent; border:1px solid var(--line); color:var(--muted); }
  button:disabled { opacity:.5; cursor:wait; }
  pre { background:#0b0d12; border:1px solid var(--line); border-radius:8px;
        padding:12px; overflow:auto; max-height:420px; white-space:pre-wrap; word-break:break-all; }
  .file { display:flex; justify-content:space-between; padding:8px 0;
          border-bottom:1px dashed var(--line); }
  .file a { color:#7ab3ff; text-decoration:none; }
  .file small { color:var(--muted); }
  .tag { display:inline-block; font-size:12px; padding:2px 8px; border-radius:6px; margin-right:6px; }
  .ok { background:#1d3a2a; color:var(--ok); } .warn { background:#3a301d; color:var(--warn); }
  .err { background:#3a1d1d; color:var(--err); }
  .spin { display:inline-block; width:14px; height:14px; border:2px solid #ffffff55;
          border-top-color:#fff; border-radius:50%; margin-right:8px;
          animation:r 1s linear infinite; vertical-align:-2px; }
  @keyframes r { to { transform:rotate(360deg); } }
</style></head><body><div class="wrap">
  <h1>tech-digest · 手动测试面板</h1>
  <div class="sub">每日星榜 / AI 周报 · 只生成与查看，不会自动发帖（weekly 恒为 --dry-run）</div>

  <div class="card"><h2>任务触发</h2><div class="row">
    <button onclick="run('daily',0)">▶ 生成今日榜单</button>
    <button class="ghost" onclick="run('daily',1)">⟳ 强制重建</button>
    <button onclick="run('weekly',0)">▶ 生成 AI 周报（不发帖）</button>
    <button class="ghost" onclick="refresh()">刷新列表</button>
  </div>
  <pre id="result" style="margin-top:12px;display:none"></pre></div>

  <div class="card"><h2>产物（output/）</h2><div id="files">加载中...</div></div>

  <div class="card"><h2>最近日志</h2><pre id="log">加载中...</pre></div>
</div>
<script>
const $ = s => document.querySelector(s);
const esc = s => String(s).replace(/[&<>]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;'}[c]));

async function run(task, force) {
  const btn = event.target; btn.disabled = true;
  const r = $('#result'); r.style.display = 'block';
  r.innerHTML = '<span class="spin"></span>执行中（约 15~60s）...';
  try {
    const res = await fetch('/api/run?task=' + task + '&force=' + force, {method:'POST'});
    const data = await res.json();
    r.textContent = (data.tail || '').split('\\n').slice(-12).join('\\n');
  } catch (e) { r.textContent = '请求失败: ' + e; }
  btn.disabled = false; refresh();
}

async function refresh() {
  try {
    const res = await fetch('/api/status'); const d = await res.json();
    $('#files').innerHTML = d.files.length
      ? d.files.map(f => '<div class="file"><a href="/api/file?name=' + f.name + '">📄 ' + f.name +
        '</a><small>' + f.size + '</small></div>').join('')
      : '（暂无产物，点上方按钮生成）';
    $('#log').textContent = (d.log || '（暂无日志）').split('\\n').slice(-25).join('\\n');
  } catch (e) {}
}
refresh(); setInterval(refresh, 15000);
</script></body></html>
"""


def run_task(task: str, force: bool) -> dict:
    """子进程执行 main.py，返回 (rc, stdout 尾部)。"""
    cmd = [sys.executable, str(BASE / "main.py"), task]
    if force:
        cmd.append("--force")
    if task == "weekly":
        cmd.append("--dry-run")
    try:
        proc = subprocess.run(cmd, cwd=str(BASE), capture_output=True, text=True, timeout=240)
        tail = (proc.stdout or "") + (proc.stderr or "")
        return {"rc": proc.returncode, "tail": tail[-4000:]}
    except subprocess.TimeoutExpired:
        return {"rc": -1, "tail": "任务超时（>240s）"}


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
        return name if name and is_admin(name) else None
    except Exception:  # noqa: BLE001 双保险 fail closed（含 is_admin 本身）
        return None


def _pref_audit(line: str) -> None:
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    safe = line.replace("\\", "\\\\").replace("\n", "\\n").replace("\r", "\\r")
    try:
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(f"{stamp} [pref-audit] {safe}\n")
    except OSError:
        print(f"[pref-audit] {safe}")


def _pref_payload(pref, daily_post_id: int | None = None) -> str:
    return json.dumps({"mode": pref.mode, "text": pref.text,
                       "updated_at": pref.updated_at,
                       "updated_by": pref.updated_by,
                       "daily_post_id": daily_post_id}, ensure_ascii=False)


def _store_for_pref():
    from app.store import Store
    return Store()


def _latest_daily_post_id() -> int | None:
    """最近一期日报帖 ID：从今天回看 4 天（周末不发帖时周五帖仍可命中到周一早）；
    store 异常一律 None（GET 不因此 500）。"""
    from datetime import date, timedelta
    store = None
    try:
        store = _store_for_pref()
        today = date.today()
        for offset in range(4):
            pid = store.daily_post_id((today - timedelta(days=offset)).isoformat())
            if pid:
                return pid
        return None
    except Exception:  # noqa: BLE001
        return None
    finally:
        if store is not None:
            store.close()  # 显式关闭（终审 Minor①）：不依赖 CPython 引用计数


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


class Handler(BaseHTTPRequestHandler):
    def _send(self, code: int, body: str, ctype: str = "application/json; charset=utf-8") -> None:
        data = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):  # noqa: N802
        path = urlparse(self.path).path
        if path == "/":
            return self._send(200, PAGE, "text/html; charset=utf-8")
        if path == "/api/status":
            files = []
            if OUTPUT_DIR.is_dir():
                for f in sorted(OUTPUT_DIR.iterdir(), key=lambda p: p.name, reverse=True)[:20]:
                    if f.is_file() and f.suffix == ".md":
                        files.append({"name": f.name, "size": f"{f.stat().st_size // 1024}KB"})
            log_text = ""
            if LOG_FILE.exists():
                log_text = LOG_FILE.read_text(encoding="utf-8", errors="replace")[-6000:]
            return self._send(200, json.dumps({"files": files, "log": log_text}, ensure_ascii=False))
        if path == "/api/file":
            # 白名单：仅 output/ 下的 .md（防目录穿越）
            name = parse_qs(urlparse(self.path).query).get("name", [""])[0]
            target = (OUTPUT_DIR / name).resolve()
            if OUTPUT_DIR.resolve() not in target.parents or target.suffix != ".md" or not target.is_file():
                return self._send(404, "未找到文件")
            return self._send(200, target.read_text(encoding="utf-8"), "text/plain; charset=utf-8")
        if path == PREF_PATH:
            code, resp = handle_pref_get(self.headers.get("Authorization", ""))
            return self._send(code, resp)
        if path == "/api/log":
            log_text = LOG_FILE.read_text(encoding="utf-8", errors="replace")[-6000:] if LOG_FILE.exists() else ""
            return self._send(200, json.dumps({"log": log_text}, ensure_ascii=False))
        return self._send(404, "not found")

    def do_POST(self):  # noqa: N802
        path = urlparse(self.path).path
        if path == PREF_PATH:
            try:
                length = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                return self._send(400, "Content-Length 非法")
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

    def log_message(self, fmt, *args):  # noqa: A002 面板日志走 stdout 即可
        pass


def main() -> None:
    srv = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    print(f"manual panel: http://127.0.0.1:{PORT}  (Ctrl+C 停止)")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
