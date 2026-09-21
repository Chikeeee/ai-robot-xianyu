<#
  停止 AI Robot 本地服务。
  默认只停应用（端口 8000）；加 -IncludeOllama 连 Ollama（端口 11434）一起停。

  用法：powershell -ExecutionPolicy Bypass -File .\stop-local.ps1 [-IncludeOllama]
#>
param([switch]$IncludeOllama)

function Stop-Port([int]$port, [string]$label) {
    $conn = Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue
    if ($conn) {
        $ids = @($conn | Select-Object -ExpandProperty OwningProcess -Unique)
        foreach ($id in $ids) { Stop-Process -Id $id -Force -ErrorAction SilentlyContinue }
        Write-Host "已停止 $label（端口 $port，PID: $($ids -join ', ')）" -ForegroundColor Green
    } else {
        Write-Host "$label（端口 $port）未在运行" -ForegroundColor Yellow
    }
}

Stop-Port 8000 'AI Robot 应用'
if ($IncludeOllama) { Stop-Port 11434 'Ollama' }
