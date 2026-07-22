$ErrorActionPreference = "Stop"

$project = $PSScriptRoot
$pythonCommand = Get-Command py.exe -All -ErrorAction SilentlyContinue |
    Where-Object { $_.Source -notlike "*\WindowsApps\*" } |
    Select-Object -First 1
if (!$pythonCommand) {
    $pythonCommand = Get-Command python.exe -All -ErrorAction SilentlyContinue |
        Where-Object { $_.Source -notlike "*\WindowsApps\*" } |
        Select-Object -First 1
}
$python = if ($pythonCommand) { $pythonCommand.Source } else { $null }
$port = 8765
$url = "http://127.0.0.1:$port"
$logDir = Join-Path $project "logs"
$stdoutLog = Join-Path $logDir "video-vault-ui.stdout.log"
$stderrLog = Join-Path $logDir "video-vault-ui.stderr.log"

function Test-UiReady {
    try {
        $response = Invoke-WebRequest -UseBasicParsing -Uri $url -TimeoutSec 2
        return $response.StatusCode -ge 200 -and $response.StatusCode -lt 500
    } catch {
        return $false
    }
}

if (!$python -or !(Test-Path $python)) {
    Add-Type -AssemblyName System.Windows.Forms
    [System.Windows.Forms.MessageBox]::Show("找不到專案使用的 Python。", "video-vault-ai")
    exit 1
}

Set-Location $project
$env:PYTHONPATH = Join-Path $project "src"

$ready = Test-UiReady
if (!$ready) {
    New-Item -ItemType Directory -Force -Path $logDir | Out-Null
    $arguments = @("-m", "video_vault", "ui", "--port", "$port")
    if ($pythonCommand.Name -eq "py.exe") {
        $arguments = @("-3") + $arguments
    }
    Start-Process -WindowStyle Hidden -FilePath $python -ArgumentList $arguments `
        -WorkingDirectory $project -RedirectStandardOutput $stdoutLog -RedirectStandardError $stderrLog | Out-Null
    for ($i = 0; $i -lt 30; $i++) {
        Start-Sleep -Milliseconds 500
        if (Test-UiReady) {
            $ready = $true
            break
        }
    }
}

if ($ready) {
    Start-Process $url
    exit 0
}

Add-Type -AssemblyName System.Windows.Forms
[System.Windows.Forms.MessageBox]::Show(
    "WebUI 啟動失敗，請查看：$stderrLog",
    "video-vault-ai"
)
exit 1
