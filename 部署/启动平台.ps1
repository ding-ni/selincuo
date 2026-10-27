param([int]$Port = 8765)
$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
if (Get-NetTCPConnection -State Listen -LocalPort $Port -ErrorAction SilentlyContinue) {
    throw "Port $Port is occupied. Specify another port with -Port."
}
$outputFolder = Join-Path $projectRoot ('成果/服务日志/启动-' + [guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Path $outputFolder | Out-Null
$pythonPath = (Get-Command python).Source
$process = Start-Process -FilePath $pythonPath -ArgumentList @('-m', 'uvicorn', '后端.应用:app', '--host', '127.0.0.1', '--port', "$Port") -WorkingDirectory $projectRoot -WindowStyle Hidden -RedirectStandardOutput (Join-Path $outputFolder '输出.log') -RedirectStandardError (Join-Path $outputFolder '错误.log') -PassThru
Write-Output "PID: $($process.Id)"
Write-Output "URL: http://127.0.0.1:$Port"
Write-Output "Logs: $outputFolder"
