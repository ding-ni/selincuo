param([int]$Port = 8882, [switch]$NoBrowser)
$ErrorActionPreference = 'Stop'
$projectDirectory = Split-Path -Parent $PSScriptRoot
$settingsFile = Join-Path $projectDirectory 'config\active-workspace.json'
if (-not (Test-Path -LiteralPath $settingsFile)) { throw '请按 config/active-workspace.example.json 指定现有完整工作区，不能用空库替代运行资料。' }
$settings = Get-Content -Raw -LiteralPath $settingsFile -Encoding UTF8 | ConvertFrom-Json
$workspaceDirectory = if ([IO.Path]::IsPathRooted($settings.workspace)) { $settings.workspace } else { Join-Path $projectDirectory $settings.workspace }
$workspaceDirectory = [IO.Path]::GetFullPath($workspaceDirectory)
if (-not (Test-Path -LiteralPath (Join-Path $workspaceDirectory 'database\hydrology.sqlite3'))) { throw '所选工作区缺少数据库，请接入原完整资料。' }
if (-not (Test-Path -LiteralPath (Join-Path $projectDirectory 'frontend\dist\index.html'))) { throw '前端尚未构建，请在 frontend 目录执行 npm run build。' }
$pythonExecutable = if ($settings.python) { $settings.python } else { (Get-Command python -ErrorAction Stop).Source }
$sourceVersion = (Get-Content -Raw -LiteralPath (Join-Path $projectDirectory 'frontend\package.json') -Encoding UTF8 | ConvertFrom-Json).version
$identityCode = 'import sys; sys.path.insert(0, sys.argv[1]); from backend.common import model_identity; print(model_identity())'
$sourceIdentity = & $pythonExecutable -B -c $identityCode $projectDirectory
if ($LASTEXITCODE -ne 0 -or -not $sourceIdentity) { throw '无法核对当前后端源码摘要，请检查Python环境。' }
$sourceIdentity = $sourceIdentity.Trim()
$url = 'http://127.0.0.1:' + $Port
$health = $null
try { $health = Invoke-RestMethod -Uri ($url + '/api/health') -TimeoutSec 2 } catch { }
if ($health) {
    if ($health.projectId -ne 'selinco' -or $health.version -ne $sourceVersion -or [IO.Path]::GetFullPath($health.workspace) -ne $workspaceDirectory) { throw ('端口' + $Port + '已由另一版本或工作区占用。请核查该服务，或指定其他端口。') }
    if ($health.backendSourceSHA256 -ne $sourceIdentity) { throw ('端口' + $Port + '运行的源码已更新，请先结束该服务，再重新启动平台。') }
} else {
    $batchName = 'build\源码服务-' + (Get-Date -Format 'yyyyMMdd-HHmmss') + '-' + [Guid]::NewGuid().ToString('N').Substring(0,8)
    $logDirectory = Join-Path $projectDirectory $batchName
    New-Item -ItemType Directory -Path $logDirectory | Out-Null
    $env:PYTHONUTF8 = '1'
    $arguments = @('-B', ('"' + (Join-Path $projectDirectory 'app_entry.py') + '"'), '--port', $Port, '--workspace', ('"' + $workspaceDirectory + '"'), '--no-browser')
    $startedService = Start-Process -FilePath $pythonExecutable -ArgumentList $arguments -WorkingDirectory $projectDirectory -WindowStyle Hidden -RedirectStandardOutput (Join-Path $logDirectory 'service.out.log') -RedirectStandardError (Join-Path $logDirectory 'service.err.log') -PassThru
    for ($attempt = 0; $attempt -lt 60; $attempt++) {
        try { $health = Invoke-RestMethod -Uri ($url + '/api/health') -TimeoutSec 1; break } catch { Start-Sleep -Milliseconds 300 }
        if ($startedService.HasExited) { throw ('源码服务启动失败，请查看 ' + $logDirectory + '\service.err.log') }
    }
    if (-not $health -or $health.projectId -ne 'selinco' -or $health.version -ne $sourceVersion -or $health.backendSourceSHA256 -ne $sourceIdentity -or [IO.Path]::GetFullPath($health.workspace) -ne $workspaceDirectory) { throw '服务未以预期版本、工作区和源码就绪，请查看启动日志。' }
}
Write-Output ('色林错平台 ' + $health.version + ' · ' + $url)
if (-not $NoBrowser) { Start-Process $url }
