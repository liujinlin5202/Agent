#!/bin/bash
# 重启 assist 服务：脚本文件方式（避免 pkill -f 误杀执行 shell）
set -e
cd /home/cloud/sse_market_assist
pkill -f '[m]ain.py' || true
sleep 1
setsid nohup .venv/bin/python main.py >> logs/assist.log 2>&1 < /dev/null &
sleep 6
echo '--- log ---'
tail -8 logs/assist.log
echo '--- listen ---'
(netstat -tln 2>/dev/null || ss -tln 2>/dev/null) | grep -E ':(8080|19084)' || echo 'not listening'
