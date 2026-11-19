param([int]$Port, [switch]$NoBrowser)
$ErrorActionPreference = 'Stop'
try {
    $projectDirectory = Split-Path -Parent $PSScriptRoot
    if (-not $PSBoundParameters.ContainsKey('Port')) {
        $platform = Get-Content -Raw -LiteralPath (Join-Path $projectDirectory 'config/platform.json') -Encoding UTF8 | ConvertFrom-Json
        $Port = if ($platform.port) { [int]$platform.port } else { 8882 }
    }
    $settingsFile = Join-Path $projectDirectory 'config\active-workspace.json'
    if (-not (Test-Path -LiteralPath $settingsFile)) { throw '请按 config/active-workspace.example.json 指定现有完整工作区，不能用空库替代运行资料。' }
    $settings = Get-Content -Raw -LiteralPath $settingsFile -Encoding UTF8 | ConvertFrom-Json
    $workspaceDirectory = if ([IO.Path]::IsPathRooted($settings.workspace)) { $settings.workspace } else { Join-Path $projectDirectory $settings.workspace }
    $workspaceDirectory = [IO.Path]::GetFullPath($workspaceDirectory)
    if (-not (Test-Path -LiteralPath (Join-Path $workspaceDirectory 'database\hydrology.sqlite3'))) { throw '所选工作区缺少数据库，请接入原完整资料。' }
    if (-not (Test-Path -LiteralPath (Join-Path $projectDirectory 'frontend\dist\index.html'))) { throw '前端尚未构建，请在 frontend 目录执行 npm run build。' }
    $pythonExecutable = if ($settings.python) { $settings.python } else { (Get-Command python -ErrorAction Stop).Source }
    $sourceVersion = (Get-Content -Raw -LiteralPath (Join-Path $projectDirectory 'frontend\package.json') -Encoding UTF8 | ConvertFrom-Json).version
    $url = 'http://127.0.0.1:' + $Port
    $health = $null
    try { $health = Invoke-RestMethod -Uri ($url + '/api/health') -TimeoutSec 2 } catch { }
    if ($health) {
        if ($health.projectId -ne 'selinco' -or $health.version -ne $sourceVersion -or [IO.Path]::GetFullPath($health.workspace) -ne $workspaceDirectory) { throw ('端口' + $Port + '已由另一版本或工作区占用。请核查该服务，或指定其他端口。') }
        $identityCode = 'import sys; sys.path.insert(0, sys.argv[1]); from backend.common import model_identity; print(model_identity())'
        $sourceIdentity = & $pythonExecutable -B -c $identityCode $projectDirectory
        if ($LASTEXITCODE -ne 0 -or -not $sourceIdentity) { throw '无法核对当前后端源码摘要，请检查Python环境。' }
        $sourceIdentity = $sourceIdentity.Trim()
        if ($health.backendSourceSHA256 -ne $sourceIdentity) { throw ('端口' + $Port + '运行的不是当前源码版。请在原平台选择“资料更新 → 后台运行 → 退出平台”，再打开此快捷方式。') }
    } else {
        $batchName = 'build\源码服务-' + (Get-Date -Format 'yyyyMMdd-HHmmss') + '-' + [Guid]::NewGuid().ToString('N').Substring(0,8)
        $logDirectory = Join-Path $projectDirectory $batchName
        New-Item -ItemType Directory -Path $logDirectory | Out-Null
        $env:PYTHONUTF8 = '1'
        $arguments = @('-B', ('"' + (Join-Path $projectDirectory 'app_entry.py') + '"'), '--port', $Port, '--workspace', ('"' + $workspaceDirectory + '"'), '--no-browser')
        $startedService = Start-Process -FilePath $pythonExecutable -ArgumentList $arguments -WorkingDirectory $projectDirectory -WindowStyle Hidden -RedirectStandardOutput (Join-Path $logDirectory 'service.out.log') -RedirectStandardError (Join-Path $logDirectory 'service.err.log') -PassThru
        $wait = [Diagnostics.Stopwatch]::StartNew()
        while ($wait.Elapsed.TotalSeconds -lt 90) {
            try { $health = Invoke-RestMethod -Uri ($url + '/api/health') -TimeoutSec 1; break } catch { Start-Sleep -Milliseconds 300 }
            if ($startedService.HasExited) {
                $failureDetail = Get-Content -Encoding UTF8 -LiteralPath (Join-Path $logDirectory 'service.err.log') -Tail 1 -ErrorAction SilentlyContinue
                throw ('源码服务启动失败。' + $failureDetail + ' 日志：' + $logDirectory)
            }
        }
        if (-not $health -or $health.projectId -ne 'selinco' -or $health.version -ne $sourceVersion -or $health.processId -ne $startedService.Id -or [IO.Path]::GetFullPath($health.workspace) -ne $workspaceDirectory) { throw ('服务未以预期版本、工作区和进程就绪。日志：' + $logDirectory) }
    }
    Write-Output ('色林错平台 ' + $health.version + ' · ' + $url)
    if (-not $NoBrowser) { Start-Process $url }
} catch {
    if ($NoBrowser) { throw }
    Add-Type -AssemblyName System.Windows.Forms
    [System.Windows.Forms.MessageBox]::Show($_.Exception.Message, '色林错平台启动失败') | Out-Null
    exit 1
}
