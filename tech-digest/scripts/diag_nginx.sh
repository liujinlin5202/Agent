#!/bin/bash
# 只读诊断：nginx_proxy 443 配置（api.<MARKET_DOMAIN> 的 Authorization 分流 upstream）
set -u
echo "== nginx.conf include 行 =="
docker exec sse_market_server-nginx_proxy-1 sh -c "grep -n 'include' /etc/nginx/nginx.conf"
echo
echo "== conf.d & http.d 全列表 =="
docker exec sse_market_server-nginx_proxy-1 sh -c "ls -la /etc/nginx/conf.d/ /etc/nginx/http.d/ 2>/dev/null"
echo
echo "== 所有 server/proxy_pass（排除注释） =="
docker exec sse_market_server-nginx_proxy-1 sh -c \
  "grep -rn -E 'listen|server_name|proxy_pass|map ' /etc/nginx/ 2>/dev/null | grep -v -E '#|default.conf' | head -40"
