#!/bin/bash
# 启动 sync watch 常驻进程（脚本文件方式：内容不进 cmdline，避免 pkill -f 自杀）
cd /home/cloud/sse_market_assist || exit 1
pkill -f 'run_sync[.]py --watch' 2>/dev/null || true
sleep 1
setsid nohup .venv/bin/python scripts/run_sync.py --watch >> logs/sync.log 2>&1 < /dev/null &
sleep 8
echo '--- ps ---'
ps aux | grep -E '[r]un_sync|[m]ain.py'
echo '--- sync.log 尾部 ---'
tail -5 logs/sync.log
