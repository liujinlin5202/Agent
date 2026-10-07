#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""网页测试页：浏览器里输入 分区/标题/正文 → 调用正式版服务 /api/v1/assist/match 展示结果。

- 本页只做代理，检索与生成全部由 sse-market-assist（127.0.0.1:19082）完成；
- 监听 0.0.0.0:19083，访问 http://<服务器IP>:19083/ （或 SSH 隧道后 http://127.0.0.1:19083/）；
- 需要 TEST_TOKEN（本目录 .env 中配置），防止公网被随意调用消耗额度。
"""

from __future__ import annotations

import json
import os
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))
TOKEN = os.environ.get("TEST_TOKEN", "sse-mvp-test")
ASSIST_URL = os.environ.get("ASSIST_URL", "http://127.0.0.1:19084/api/v1/assist/match")

PARTITIONS = [
    "学习交流", "实习就业", "宿舍生活", "技术分享", "期末资料",
    "生活日常", "社团活动", "组团捞人", "课程专区", "其他",
]

PAGE = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>集市分区智能检索 + AI 应答 · 正式版测试页</title>
<style>
  body { font-family: -apple-system, "Microsoft YaHei", sans-serif; max-width: 860px;
         margin: 24px auto; padding: 0 16px; color: #222; background: #f7f8fa; }
  h1 { font-size: 20px; }
  .box { background: #fff; border-radius: 10px; padding: 18px; margin: 12px 0;
         box-shadow: 0 1px 4px rgba(0,0,0,.08); }
  label { display: block; margin: 10px 0 4px; font-size: 14px; color: #555; }
  input, select, textarea { width: 100%; box-sizing: border-box; padding: 8px 10px;
         font-size: 14px; border: 1px solid #d0d4da; border-radius: 6px; }
  textarea { min-height: 80px; resize: vertical; }
  button { margin-top: 14px; padding: 9px 26px; font-size: 15px; border: 0;
           border-radius: 6px; background: #2f6fed; color: #fff; cursor: pointer; }
  button:disabled { background: #9db8ef; cursor: wait; }
  #result { white-space: pre-wrap; line-height: 1.7; font-size: 14px; }
  .meta { color: #888; font-size: 12px; margin-top: 6px; }
  .err { color: #c0392b; }
  a { color: #2f6fed; }
  .hint { font-size: 12px; color: #999; margin-top: 4px; }
</style>
</head>
<body>
<h1>🧪 集市「分区智能检索 + AI 应答」正式版测试页</h1>
<div class="box">
  <label>测试口令（mvp/.env 中 TEST_TOKEN）</label>
  <input id="token" type="password" placeholder="输入测试口令">
  <label>分区</label>
  <select id="partition"></select>
  <label>标题</label>
  <input id="title" placeholder="例如：大三要不要主动找老师做项目">
  <label>正文</label>
  <textarea id="content" placeholder="例如：对保研和就业有没有帮助？该怎么开口联系老师？"></textarea>
  <button id="go" onclick="doTest()">开始测试</button>
  <div class="hint">正式版管线：DeepSeek 查询改写 → 分区内关键词 + 向量混合检索 → 合并重排 → AI 回答，单次约 5~10 秒。</div>
</div>
<div class="box" id="outbox" style="display:none">
  <div class="meta" id="meta"></div>
  <div id="result"></div>
</div>
<script>
const PARTS = %PARTS%;
const sel = document.getElementById('partition');
PARTS.forEach(p => { const o = document.createElement('option'); o.value = p; o.textContent = p; sel.appendChild(o); });
if (localStorage.getItem('mvp_token')) document.getElementById('token').value = localStorage.getItem('mvp_token');

async function doTest() {
  const token = document.getElementById('token').value.trim();
  const partition = sel.value;
  const title = document.getElementById('title').value.trim();
  const content = document.getElementById('content').value.trim();
  if (!token) { alert('请先输入测试口令'); return; }
  if (!title || !content) { alert('标题和正文都要填'); return; }
  localStorage.setItem('mvp_token', token);
  const btn = document.getElementById('go'); btn.disabled = true; btn.textContent = '检索生成中…';
  const outbox = document.getElementById('outbox'); outbox.style.display = 'none';
  try {
    const resp = await fetch('/test', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({token, partition, title, content}),
    });
    const data = await resp.json();
    outbox.style.display = 'block';
    const meta = document.getElementById('meta');
    const result = document.getElementById('result');
    if (!data.ok) { meta.textContent = ''; result.innerHTML = '<span class="err">错误：' + escapeHtml(data.error || '未知错误') + '</span>'; return; }
    const m = data.meta || {};
    meta.textContent = '改写问句：' + (data.keywords || []).join('；') +
      ' | MySQL 关键词命中 ' + (m.mysql_hits ?? 0) + ' 帖 | 向量命中 ' + (m.qdrant_hits ?? 0) +
      ' 点 | 最终取 ' + (m.used ?? 0) + ' 帖 | 耗时 ' + (m.took_ms ?? 0) + 'ms';
    let html = '<b>【AI 回答】</b><br>' + escapeHtml(data.answer || '(回答生成失败，仅展示检索结果)').replace(/\\n/g, '<br>');
    html += '<br><br><b>【引用】</b><br>';
    (data.references || []).forEach(r => {
      html += '· <a href="' + r.url + '" target="_blank">《' + escapeHtml(r.title) + '》</a>' +
              ' <span style="color:#999">heat=' + r.heat + '</span><br>';
    });
    html += '<br><span class="hint">内容由 AI 生成，仅供参考（只读实验，不影响站内任何数据）</span>';
    result.innerHTML = html;
  } catch (e) {
    outbox.style.display = 'block';
    document.getElementById('result').innerHTML = '<span class="err">请求失败：' + escapeHtml(String(e)) + '</span>';
  } finally {
    btn.disabled = false; btn.textContent = '开始测试';
  }
}
function escapeHtml(s) {
  return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
}
</script>
</body>
</html>
"""


def call_assist(partition: str, title: str, content: str) -> dict:
    """调用正式版服务（127.0.0.1:19082）。"""
    payload = json.dumps(
        {"partition": partition, "title": title, "content": content}, ensure_ascii=False
    ).encode("utf-8")
    req = urllib.request.Request(
        ASSIST_URL, data=payload, headers={"Content-Type": "application/json"}
    )
    with urllib.request.urlopen(req, timeout=90) as resp:
        return json.loads(resp.read().decode("utf-8"))


class Handler(BaseHTTPRequestHandler):
    server_version = "SSEMarketAssistTest/2.0"

    def log_message(self, fmt, *args):
        print("[web]", self.address_string(), fmt % args, flush=True)

    def do_GET(self):
        path = urlparse(self.path).path
        if path != "/":
            self.send_response(404)
            self.end_headers()
            return
        body = PAGE.replace("%PARTS%", json.dumps(PARTITIONS, ensure_ascii=False))
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body.encode("utf-8"))))
        self.end_headers()
        self.wfile.write(body.encode("utf-8"))

    def do_POST(self):
        path = urlparse(self.path).path
        if path != "/test":
            self.send_response(404)
            self.end_headers()
            return
        length = int(self.headers.get("Content-Length", 0))
        try:
            req = json.loads(self.rfile.read(length).decode("utf-8"))
        except Exception:
            self._json(400, {"ok": False, "error": "请求体不是合法 JSON"})
            return
        if req.get("token") != TOKEN:
            self._json(403, {"ok": False, "error": "测试口令错误"})
            return
        partition = (req.get("partition") or "").strip()
        title = (req.get("title") or "").strip()
        content = (req.get("content") or "").strip()
        if not (partition and title and content):
            self._json(400, {"ok": False, "error": "partition/title/content 均必填"})
            return
        try:
            resp = call_assist(partition, title, content)
        except Exception as e:
            print("[web] assist call error:", repr(e), flush=True)
            self._json(500, {"ok": False, "error": f"正式版服务调用失败：{e}"})
            return
        if not resp.get("ok"):
            self._json(400, {"ok": False, "error": resp.get("error", "服务返回异常")})
            return
        data = resp.get("data") or {}
        self._json(200, {
            "ok": True,
            "answer": data.get("answer"),
            "references": data.get("references") or [],
            "keywords": (data.get("meta") or {}).get("queries") or [],
            "meta": data.get("meta") or {},
        })

    def _json(self, status, payload):
        body = json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)


def main():
    host, port = "0.0.0.0", 19083
    server = ThreadingHTTPServer((host, port), Handler)
    print(f"web test page: http://<server-ip>:{port}/  (token 见 .env TEST_TOKEN)", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
