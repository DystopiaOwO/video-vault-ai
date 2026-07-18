$ErrorActionPreference = "Stop"

$project = $PSScriptRoot
$python = "C:\Users\b3b3b\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe"
$port = 8765
$url = "http://127.0.0.1:$port"

if (!(Test-Path $python)) {
    Add-Type -AssemblyName System.Windows.Forms
    [System.Windows.Forms.MessageBox]::Show("找不到專案使用的 Python。", "video-vault-ai")
    exit 1
}

Set-Location $project
$env:PYTHONPATH = Join-Path $project "src"

$running = Get-NetTCPConnection -LocalPort $port -ErrorAction SilentlyContinue |
    Where-Object { $_.State -eq "Listen" -and $_.OwningProcess -gt 0 }

if (!$running) {
    Start-Process -WindowStyle Hidden -FilePath $python -ArgumentList @("-m", "video_vault", "ui", "--port", "$port") -WorkingDirectory $project
    for ($i = 0; $i -lt 20; $i++) {
        Start-Sleep -Milliseconds 500
        $running = Get-NetTCPConnection -LocalPort $port -ErrorAction SilentlyContinue |
            Where-Object { $_.State -eq "Listen" -and $_.OwningProcess -gt 0 }
        if ($running) { break }
    }
}

Start-Process $url
