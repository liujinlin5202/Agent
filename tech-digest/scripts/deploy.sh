#!/usr/bin/env bash
# tech-digest 一键部署（幂等，可重复执行）。服务器: /root/market-deploy/agent/tech-digest
set -euo pipefail

APP=/root/market-deploy/agent/tech-digest
cd "$APP"

echo "[1/4] venv + 依赖"
if [ ! -x .venv/bin/python ]; then
  python3 -m venv .venv
fi
.venv/bin/pip install --quiet --disable-pip-version-check requests beautifulsoup4

echo "[2/4] 目录与 .env"
mkdir -p data output log
if [ ! -f .env ]; then
  cp .env.example .env
  chmod 600 .env
  echo "  !! 已生成 .env 模板，请编辑填入:
     - SSE_MARKET_API_KEY     (market-deploy 网关 key)
     - MARKET_REFRESH_TOKEN   (集市登录 refresh_token, M2 用)
     - MARKET_USER_TELEPHONE  (集市账号手机号, M2 用)"
fi
chmod 600 .env 2>/dev/null || true

echo "[3/4] systemd 单元"
for f in deploy/tech-digest-*.service deploy/tech-digest-*.timer; do
  cp "$f" /etc/systemd/system/
done
systemctl daemon-reload
systemctl enable --now tech-digest-daily.timer tech-digest-weekly.timer tech-digest-star.timer
# 1 楼补楼 timer 已随功能下线（2026-09-14 星榜改折叠块进正文，评论区不再发）：
systemctl disable --now tech-digest-comment.timer 2>/dev/null || true
rm -f /etc/systemd/system/tech-digest-comment.service /etc/systemd/system/tech-digest-comment.timer

echo "[4/4] 确认"
systemctl list-timers --no-pager | grep tech-digest || true
echo "done. 手动触发: systemctl start tech-digest-daily"
