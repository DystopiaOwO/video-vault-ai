$ErrorActionPreference = "Stop"

$project = $PSScriptRoot
$pythonCommand = Get-Command python -ErrorAction SilentlyContinue
if (!$pythonCommand) {
    $pythonCommand = Get-Command py -ErrorAction SilentlyContinue
}
$python = if ($pythonCommand) { $pythonCommand.Source } else { $null }
$port = 8765
$url = "http://127.0.0.1:$port"

if (!$python -or !(Test-Path $python)) {
    Add-Type -AssemblyName System.Windows.Forms
    [System.Windows.Forms.MessageBox]::Show("找不到專案使用的 Python。", "video-vault-ai")
    exit 1
}

Set-Location $project
$env:PYTHONPATH = Join-Path $project "src"

$running = Get-NetTCPConnection -LocalPort $port -ErrorAction SilentlyContinue |
    Where-Object { $_.State -eq "Listen" -and $_.OwningProcess -gt 0 }

if (!$running) {
    $arguments = @("-m", "video_vault", "ui", "--port", "$port")
    if ($pythonCommand.Name -eq "py.exe") {
        $arguments = @("-3") + $arguments
    }
    Start-Process -WindowStyle Hidden -FilePath $python -ArgumentList $arguments -WorkingDirectory $project
    for ($i = 0; $i -lt 20; $i++) {
        Start-Sleep -Milliseconds 500
        $running = Get-NetTCPConnection -LocalPort $port -ErrorAction SilentlyContinue |
            Where-Object { $_.State -eq "Listen" -and $_.OwningProcess -gt 0 }
        if ($running) { break }
    }
}

Start-Process $url
