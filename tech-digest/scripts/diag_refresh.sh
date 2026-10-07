#!/bin/bash
# 诊断：.env 是否存在 refresh token + 手动 refresh 看响应
cd /root/market-deploy/agent/tech-digest || exit 1
echo "== .env token 行数: $(grep -c '^MARKET_REFRESH_TOKEN=.' .env)"
TOKEN=$(grep '^MARKET_REFRESH_TOKEN=' .env | head -1 | cut -d= -f2)
echo "== token 长度: ${#TOKEN} 前缀: ${TOKEN:0:12}..."
echo "== refresh 响应:"
curl -sS -m 15 -X POST http://172.18.0.5:8080/api/auth/refresh \
  -H 'Content-Type: application/json' \
  -d "{\"refresh_token\":\"$TOKEN\"}" | head -c 500
echo ""
