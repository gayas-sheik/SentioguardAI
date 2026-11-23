
$ErrorActionPreference = "Stop"

$Root = "C:\Users\lenovo\Documents\Final Year Project\SentioGaurdAI"
$Comfy = Join-Path $Root "comfyui"
$Gateway = Join-Path $Root "gateway"
$Python = Join-Path $Root ".venv\Scripts\python.exe"
$Cloudflared = "C:\Users\lenovo\Documents\cloudflared\cloudflared-windows-amd64.exe"
$Logs = Join-Path $Root "logs"

New-Item -ItemType Directory -Force -Path $Logs | Out-Null

function Test-Port([int]$Port) {
    $Client = [Net.Sockets.TcpClient]::new()
    try {
        $Result = $Client.BeginConnect("127.0.0.1", $Port, $null, $null)
        return ($Result.AsyncWaitHandle.WaitOne(500) -and $Client.Connected)
    }
    catch {
        return $false
    }
    finally {
        $Client.Dispose()
    }
}

function Wait-Port([int]$Port, [int]$Seconds = 300) {
    $Deadline = (Get-Date).AddSeconds($Seconds)
    while ((Get-Date) -lt $Deadline) {
        if (Test-Port $Port) { return $true }
        Start-Sleep -Seconds 2
    }
    return $false
}

try {
    Write-Host "`n=== SENTIOGUARDAI STARTUP ===" -ForegroundColor Cyan

    if (!(Test-Path $Python)) { throw "Python environment not found: $Python" }
    if (!(Test-Path $Cloudflared)) { throw "Cloudflared executable not found: $Cloudflared" }
    if (!(Test-Path (Join-Path $Comfy "main.py"))) { throw "ComfyUI installation not found." }

    # Start ComfyUI
    if (!(Test-Port 8188)) {
        Write-Host "[1/5] Starting AI generation engine..."
        Start-Process -FilePath $python `
            -ArgumentList @("main.py", "--lowvram") `
            -WorkingDirectory $Comfy `
            -RedirectStandardOutput (Join-Path $Logs "comfyui.log") `
            -RedirectStandardError (Join-Path $Logs "comfyui-error.log")
    }
    else {
        Write-Host "[1/5] AI generation engine is already running."
    }

    # Start FastAPI
    if (!(Test-Port 8000)) {
        Write-Host "[2/5] Starting API backend..."
        Start-Process -FilePath $Python `
            -ArgumentList @("-m", "uvicorn", "main:app", "--host", "127.0.0.1", "--port", "8000", "--app-dir", "backend") `
            -WorkingDirectory $Root `
            -RedirectStandardOutput (Join-Path $Logs "backend.log") `
            -RedirectStandardError (Join-Path $Logs "backend-error.log")
    }
    else {
        Write-Host "[2/5] API backend is already running."
    }

    Write-Host "[3/5] Waiting for backend..."
    if (!(Wait-Port 8000)) { throw "Backend did not start. Check logs\backend-error.log." }

    Write-Host "[4/5] Starting Cloudflare Quick Tunnel..."
    $TunnelOut = Join-Path $Logs "tunnel.log"
    $TunnelErr = Join-Path $Logs "tunnel-error.log"
    Remove-Item $TunnelOut, $TunnelErr -Force -ErrorAction SilentlyContinue

    Start-Process -FilePath $Cloudflared `
        -ArgumentList @("tunnel", "--url", "http://127.0.0.1:8000", "--protocol", "http2") `
        -RedirectStandardOutput $TunnelOut `
        -RedirectStandardError $TunnelErr

    $TunnelUrl = $null
    $Deadline = (Get-Date).AddMinutes(2)

    while ((Get-Date) -lt $Deadline -and !$TunnelUrl) {
        foreach ($LogFile in @($TunnelOut, $TunnelErr)) {
            if (Test-Path $LogFile) {
                $Match = Select-String -Path $LogFile -Pattern 'https://[a-zA-Z0-9-]+\.trycloudflare\.com' |
                    Select-Object -First 1
                if ($Match) {
                    $TunnelUrl = $Match.Matches[0].Value
                    break
                }
            }
        }
        Start-Sleep -Seconds 2
    }

    if (!$TunnelUrl) { throw "Tunnel URL was not detected. Check logs\tunnel-error.log." }

    Write-Host "Tunnel URL: $TunnelUrl" -ForegroundColor Green

    # Update Worker with the new tunnel URL
    Write-Host "[5/5] Updating Cloudflare gateway..."
    Push-Location $Gateway
    try {
        & npx.cmd wrangler deploy --var "BACKEND_URL:$TunnelUrl"
        if ($LASTEXITCODE -ne 0) { throw "Cloudflare Worker deployment failed." }
    }
    finally {
        Pop-Location
    }

    Write-Host "`nSENTIOGUARDAI IS READY!" -ForegroundColor Green
    Start-Process "https://sentioguard-ai-web.pages.dev/"

    Write-Host "`nKeep this window open while using the website."
    Read-Host "Press Enter when you want to close this launcher"
}
catch {
    Write-Host "`nSTARTUP FAILED: $_" -ForegroundColor Red
    Write-Host "Check the log files in: $Logs"
    Read-Host "Press Enter to close"
}
