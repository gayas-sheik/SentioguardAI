
$ErrorActionPreference = "Stop"

# ==========================================
# SENTIOGUARDAI CONFIGURATION
# ==========================================

$Root = "C:\Users\lenovo\Documents\Final Year Project\SentioGaurdAI"
$Comfy = Join-Path $Root "comfyui"
$Gateway = Join-Path $Root "gateway"
$Python = Join-Path $Root ".venv\Scripts\python.exe"
$Cloudflared = "C:\Users\lenovo\Documents\cloudflared\cloudflared-windows-amd64.exe"
$Logs = Join-Path $Root "logs"
$WranglerConfig = Join-Path $Gateway "wrangler.toml"

$script:StartedProcesses = @()
$StartupFailed = $false

New-Item -ItemType Directory -Force -Path $Logs | Out-Null

# ==========================================
# PROCESS MANAGEMENT
# ==========================================

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
        if (Test-Port $Port) {
            return $true
        }

        Start-Sleep -Seconds 2
    }

    return $false
}

function Start-TrackedProcess {
    param(
        [string]$Name,
        [string]$FilePath,
        [string[]]$Arguments,
        [string]$WorkingDirectory,
        [string]$StdOut,
        [string]$StdErr
    )

    $Process = Start-Process `
        -FilePath $FilePath `
        -ArgumentList $Arguments `
        -WorkingDirectory $WorkingDirectory `
        -RedirectStandardOutput $StdOut `
        -RedirectStandardError $StdErr `
        -PassThru

    $script:StartedProcesses += [PSCustomObject]@{
        Name = $Name
        Id = $Process.Id
    }

    return $Process
}

function Stop-StartedProcesses {
    Write-Host "`nStopping SentioGaurdAI services..." -ForegroundColor Yellow

    foreach ($Process in @($script:StartedProcesses | Select-Object -Reverse)) {
        try {
            $Existing = Get-Process -Id $Process.Id -ErrorAction SilentlyContinue

            if ($Existing) {
                Write-Host "Stopping $($Process.Name)..."
                & taskkill.exe /PID $Process.Id /T /F 2>$null | Out-Null
            }
        }
        catch {
            Write-Warning "Could not stop $($Process.Name)."
        }
    }

    $script:StartedProcesses = @()

    Write-Host "Launcher-owned services stopped." -ForegroundColor Green
}

# ==========================================
# MAIN STARTUP
# ==========================================

try {
    Write-Host "`n==========================================" -ForegroundColor Cyan
    Write-Host "       SENTIOGUARDAI STARTUP" -ForegroundColor Cyan
    Write-Host "==========================================" -ForegroundColor Cyan

    # Validate installation
    if (!(Test-Path $Python)) {
        throw "Python environment not found: $Python"
    }

    if (!(Test-Path $Cloudflared)) {
        throw "Cloudflared executable not found: $Cloudflared"
    }

    if (!(Test-Path (Join-Path $Comfy "main.py"))) {
        throw "ComfyUI installation not found."
    }

    if (!(Test-Path $WranglerConfig)) {
        throw "Wrangler configuration not found: $WranglerConfig"
    }

    # ======================================
    # 1. START COMFYUI
    # ======================================

    Write-Host "`n[1/5] Starting AI generation engine..." -ForegroundColor Cyan

    if (!(Test-Port 8188)) {
        Start-TrackedProcess `
            -Name "ComfyUI" `
            -FilePath $Python `
            -Arguments @("main.py", "--lowvram") `
            -WorkingDirectory $Comfy `
            -StdOut (Join-Path $Logs "comfyui.log") `
            -StdErr (Join-Path $Logs "comfyui-error.log") | Out-Null

        if (!(Wait-Port 8188 300)) {
            throw "ComfyUI did not start. Check logs\comfyui-error.log."
        }
    }
    else {
        Write-Host "ComfyUI is already running." -ForegroundColor Yellow
    }

    Write-Host "ComfyUI is ready." -ForegroundColor Green

    # ======================================
    # 2. START FASTAPI
    # ======================================

    Write-Host "`n[2/5] Starting API backend..." -ForegroundColor Cyan

    if (!(Test-Port 8000)) {
        Start-TrackedProcess `
            -Name "FastAPI" `
            -FilePath $Python `
            -Arguments @(
                "-m", "uvicorn",
                "main:app",
                "--host", "127.0.0.1",
                "--port", "8000",
                "--app-dir", "backend"
            ) `
            -WorkingDirectory $Root `
            -StdOut (Join-Path $Logs "backend.log") `
            -StdErr (Join-Path $Logs "backend-error.log") | Out-Null
    }
    else {
        Write-Host "FastAPI is already running." -ForegroundColor Yellow
    }

    if (!(Wait-Port 8000 120)) {
        throw "Backend did not start. Check logs\backend-error.log."
    }

    Write-Host "Backend is ready." -ForegroundColor Green

    # ======================================
    # 3. START CLOUDFLARE QUICK TUNNEL
    # ======================================

    Write-Host "`n[3/5] Starting Cloudflare Quick Tunnel..." -ForegroundColor Cyan

    $TunnelOut = Join-Path $Logs "tunnel.log"
    $TunnelErr = Join-Path $Logs "tunnel-error.log"

    Remove-Item $TunnelOut, $TunnelErr -Force -ErrorAction SilentlyContinue

    $TunnelProcess = Start-TrackedProcess `
        -Name "Cloudflare Tunnel" `
        -FilePath $Cloudflared `
        -Arguments @(
            "tunnel",
            "--url", "http://127.0.0.1:8000",
            "--protocol", "http2"
        ) `
        -WorkingDirectory $Root `
        -StdOut $TunnelOut `
        -StdErr $TunnelErr

    $TunnelUrl = $null
    $Deadline = (Get-Date).AddMinutes(2)

    while ((Get-Date) -lt $Deadline -and !$TunnelUrl) {

        $TunnelProcess.Refresh()

        if ($TunnelProcess.HasExited) {
            throw "Cloudflare Tunnel exited unexpectedly. Check logs\tunnel-error.log."
        }

        foreach ($LogFile in @($TunnelOut, $TunnelErr)) {
            if (Test-Path $LogFile) {
                $Match = Select-String `
                    -Path $LogFile `
                    -Pattern 'https://[a-zA-Z0-9-]+\.trycloudflare\.com' |
                    Select-Object -First 1

                if ($Match) {
                    $TunnelUrl = $Match.Matches[0].Value
                    break
                }
            }
        }

        Start-Sleep -Seconds 2
    }

    if (!$TunnelUrl) {
        throw "Tunnel URL was not detected. Check logs\tunnel-error.log."
    }

    Write-Host "Tunnel connected: $TunnelUrl" -ForegroundColor Green

    # ======================================
    # 4. DEPLOY CLOUDFLARE WORKER
    # ======================================

    Write-Host "`n[4/5] Updating Cloudflare gateway..." -ForegroundColor Cyan

    Push-Location $Gateway

    try {
        & npx.cmd wrangler deploy `
            --config $WranglerConfig `
            --var "BACKEND_URL:$TunnelUrl"

        if ($LASTEXITCODE -ne 0) {
            throw "Cloudflare Worker deployment failed."
        }
    }
    finally {
        Pop-Location
    }

    Write-Host "Cloudflare gateway updated successfully." -ForegroundColor Green

    # ======================================
    # 5. OPEN WEBSITE
    # ======================================

    Write-Host "`n[5/5] Opening SentioGaurdAI..." -ForegroundColor Cyan

    Start-Process "https://sentioguard-ai-web.pages.dev/"

    Write-Host "`n==========================================" -ForegroundColor Green
    Write-Host "       SENTIOGUARDAI IS READY!" -ForegroundColor Green
    Write-Host "==========================================" -ForegroundColor Green

    Write-Host "`nServices running:"
    Write-Host "ComfyUI       : http://127.0.0.1:8188"
    Write-Host "FastAPI       : http://127.0.0.1:8000"
    Write-Host "Tunnel        : $TunnelUrl"
    Write-Host "Frontend      : https://sentioguard-ai-web.pages.dev/"

    Write-Host "`nKeep this window open while using the application."
    Write-Host "Press ENTER when you want to stop SentioGaurdAI." -ForegroundColor Yellow

    Read-Host

}
catch {
    $StartupFailed = $true

    Write-Host "`n==========================================" -ForegroundColor Red
    Write-Host "       SENTIOGUARDAI STARTUP FAILED" -ForegroundColor Red
    Write-Host "==========================================" -ForegroundColor Red

    Write-Host "`nError: $_" -ForegroundColor Red
    Write-Host "Check logs in: $Logs"
}
finally {
    Stop-StartedProcesses
}

if ($StartupFailed) {
    Read-Host "`nPress ENTER to close this window"
}
