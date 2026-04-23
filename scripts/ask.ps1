# Quick Callisto query shortcut
# Usage: powershell -File scripts\ask.ps1 "your question here"
param([string]$q)
$body = @{query=$q; priority=0} | ConvertTo-Json
$r = Invoke-RestMethod -Uri "http://localhost:8420/task" -Method Post -Body $body -ContentType "application/json"
Write-Host "Task $($r.task_id) submitted. Check with:"
Write-Host "  curl http://localhost:8420/task/$($r.task_id)"
