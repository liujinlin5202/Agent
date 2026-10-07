# tech-digest manual panel - open in local browser via SSH tunnel
# Usage:  powershell -ExecutionPolicy Bypass -File manual-panel.ps1
$ErrorActionPreference = "Stop"

$ssh = Get-Command ssh -ErrorAction SilentlyContinue
if (-not $ssh) { Write-Host "ERROR: ssh command not found" -ForegroundColor Red; exit 1 }

Write-Host "Opening tunnel: ssh -N -L 19086:127.0.0.1:19086 market-server ..." -ForegroundColor Cyan
Write-Host "Press Ctrl+C or close this window to disconnect."

Start-Process ssh -ArgumentList "-N","-L","19086:127.0.0.1:19086","market-server" -WindowStyle Minimized
Start-Sleep -Seconds 2
Start-Process "http://127.0.0.1:19086"
Write-Host "Panel opened at http://127.0.0.1:19086 (tunnel is encrypted, server binds 127.0.0.1 only)" -ForegroundColor Green

Write-Host ""
Write-Host "To stop the panel later, run on server:"
Write-Host "  bash /root/market-deploy/agent/tech-digest/scripts/manual-panel.sh stop"
Write-Host ""
Read-Host "Press Enter to exit (tunnel will be closed)"
