<#
  AI Robot 本地一键启动（本机部署版：DeepSeek 对话 + 本地 Ollama 向量）
  与上游 start.ps1 的区别：本脚本会先确保 Ollama 在线，并把代理环境变量带进 Ollama 进程
  （本机直连 Cloudflare R2 会 TLS 超时，必须走 127.0.0.1:7897；详见 LOCAL-DEPLOY-WINDOWS.md 第 5 节）。

  用法：powershell -ExecutionPolicy Bypass -File .\start-local.ps1 [-NoBrowser] [-SkipDeps]
#>
param(
    [switch]$NoBrowser,
    [switch]$SkipDeps
)

$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
$appPort = 8000
$ollamaPort = 11434
$py = Join-Path $root '.venv\Scripts\python.exe'
$ollamaExe = Join-Path $env:LOCALAPPDATA 'Programs\Ollama\ollama.exe'
$embedModel = 'nomic-embed-text'

Write-Host '=========================================' -ForegroundColor Cyan
Write-Host '  AI Robot 本地启动（DeepSeek + Ollama）' -ForegroundColor Cyan
Write-Host '=========================================' -ForegroundColor Cyan

# 0) 代理环境：Ollama 拉模型与 api.deepseek.com 直连都依赖这个设置
$env:HTTP_PROXY = 'http://127.0.0.1:7897'
$env:HTTPS_PROXY = 'http://127.0.0.1:7897'
$env:NO_PROXY = 'localhost,127.0.0.1,::1,api.deepseek.com'

function Test-Listening([int]$port) {
    $c = Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue
    return [bool]$c
}

# 1) 虚拟环境与依赖
if (-not (Test-Path $py)) {
    Write-Host '[1/4] 未找到 .venv，正在创建 ...' -ForegroundColor Yellow
    python -m venv (Join-Path $root '.venv')
    if (-not (Test-Path $py)) { throw '创建 .venv 失败，请先安装 Python 3.10+ 并确认 python 在 PATH 上' }
} else {
    Write-Host '[1/4] 虚拟环境就绪' -ForegroundColor Green
}
if (-not $SkipDeps) {
    Write-Host '[1/4] 检查核心依赖（幂等，用阿里云镜像）...' -ForegroundColor Yellow
    & $py -m pip install -q -r (Join-Path $root 'requirements.txt') `
        -i https://mirrors.aliyun.com/pypi/simple/ --trusted-host mirrors.aliyun.com --timeout 60
    if ($LASTEXITCODE -ne 0) { throw '依赖安装失败，请检查网络（清华镜像在本机会 403，用阿里云）' }
}

# 2) Ollama 服务
if (Test-Listening $ollamaPort) {
    Write-Host '[2/4] Ollama 已在运行' -ForegroundColor Green
} else {
    if (-not (Test-Path $ollamaExe)) { throw "未找到 Ollama：$ollamaExe" }
    Write-Host '[2/4] 启动 Ollama（带代理环境）...' -ForegroundColor Yellow
    Start-Process -FilePath $ollamaExe -ArgumentList 'serve' -WindowStyle Hidden
    $up = $false
    for ($i = 0; $i -lt 30; $i++) {
        Start-Sleep -Seconds 1
        try { Invoke-RestMethod "http://127.0.0.1:$ollamaPort/api/version" -TimeoutSec 2 | Out-Null; $up = $true; break } catch {}
    }
    if (-not $up) { throw "Ollama 启动失败，请看 $env:LOCALAPPDATA\Ollama\server.log" }
    Write-Host '[2/4] Ollama 已启动' -ForegroundColor Green
}

# 2b) 向量模型就绪检查
$tags = Invoke-RestMethod "http://127.0.0.1:$ollamaPort/api/tags" -TimeoutSec 10
$names = @()
foreach ($m in @($tags.models)) { $names += ($m.name -replace ':latest$', '') }
if ($names -notcontains $embedModel) {
    Write-Host "[2/4] 缺少向量模型 $embedModel，开始拉取（走代理，可能较慢）..." -ForegroundColor Yellow
    & $ollamaExe pull $embedModel
}
Write-Host "[2/4] 向量模型就绪：$embedModel" -ForegroundColor Green

# 3) 应用服务
if (Test-Listening $appPort) {
    Write-Host '[3/4] 端口 8000 已有服务，跳过启动' -ForegroundColor Yellow
} else {
    $logDir = Join-Path $root '.logs'
    New-Item -ItemType Directory -Path $logDir -Force | Out-Null
    Write-Host '[3/4] 启动 uvicorn（日志 .logs\uvicorn.*.log）...' -ForegroundColor Yellow
    Start-Process -FilePath $py `
        -ArgumentList '-m', 'uvicorn', 'app.main:app', '--host', '127.0.0.1', '--port', "$appPort" `
        -WorkingDirectory $root -WindowStyle Hidden `
        -RedirectStandardOutput (Join-Path $logDir 'uvicorn.out.log') `
        -RedirectStandardError (Join-Path $logDir 'uvicorn.err.log') | Out-Null
    $up = $false
    for ($i = 0; $i -lt 40; $i++) {
        Start-Sleep -Seconds 1
        try { Invoke-RestMethod "http://127.0.0.1:$appPort/health" -TimeoutSec 2 | Out-Null; $up = $true; break } catch {}
    }
    if (-not $up) { throw "服务启动超时，请看 $logDir\uvicorn.err.log" }
}

# 4) 健康检查结果
$health = Invoke-RestMethod "http://127.0.0.1:$appPort/health" -TimeoutSec 5
$stats = Invoke-RestMethod "http://127.0.0.1:$appPort/api/v1/stats" -TimeoutSec 10
Write-Host '[4/4] 服务已就绪' -ForegroundColor Green
Write-Host "      LLM=$($health.llm_model) / Embedding=$($health.embedding_model)" -ForegroundColor Green
Write-Host "      知识库分块=$($stats.total_chunks) / BM25=$($stats.bm25_ready) / 重排=$($stats.rerank_enabled) / CrewAI=$($stats.crew_available)" -ForegroundColor Green
Write-Host "      控制台: http://127.0.0.1:$appPort/dashboard" -ForegroundColor Cyan
Write-Host '      停止:   .\stop-local.ps1' -ForegroundColor DarkGray
if (-not $NoBrowser) { Start-Process "http://127.0.0.1:$appPort/dashboard" }
