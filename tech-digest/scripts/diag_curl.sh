#!/bin/bash
# 诊断：curl 带 Bearer key 完整复现 requests 的请求，看是否复现 400
set -u
cd /root/market-deploy/agent/tech-digest
KEY=$(grep '^SSE_MARKET_API_KEY=' .env | cut -d= -f2)
echo "== curl + Bearer + full payload =="
curl -s -D- -X POST https://api.<MARKET_DOMAIN>/v1/chat/completions \
  -H "Authorization: Bearer $KEY" -H 'Content-Type: application/json' \
  -d '{"model":"qwen3.8-27b-awq","messages":[{"role":"user","content":"hi"}],"max_tokens":5,"reasoning_effort":"none"}' \
  --max-time 30 | head -20
echo
echo "== curl + Bearer + empty payload =="
curl -s -o /dev/null -w '%{http_code}\n' -X POST https://api.<MARKET_DOMAIN>/v1/chat/completions \
  -H "Authorization: Bearer $KEY" -H 'Content-Type: application/json' \
  -d '{}' --max-time 10
echo "== curl with python UA =="
curl -s -o /dev/null -w '%{http_code}\n' -X POST https://api.<MARKET_DOMAIN>/v1/chat/completions \
  -A 'python-requests/2.32.3' -H 'Content-Type: application/json' \
  -d '{}' --max-time 10
