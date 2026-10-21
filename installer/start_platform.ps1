param([switch]$NoBrowser)
$ErrorActionPreference = 'Stop'
$applicationRoot = Split-Path -Parent $PSScriptRoot
$workspacePath = Join-Path $applicationRoot 'workspace'
$activeConfiguration = Join-Path $applicationRoot 'config\active-workspace.json'
if (Test-Path -LiteralPath $activeConfiguration) {
    $activeWorkspace = (Get-Content -LiteralPath $activeConfiguration -Raw -Encoding UTF8 | ConvertFrom-Json).workspace
    if ($activeWorkspace) { $workspacePath = [IO.Path]::GetFullPath((Join-Path $applicationRoot $activeWorkspace)) }
}
$platformUrl = 'http://127.0.0.1:8879'
function Test-SelincoService {
    try {
        $health = Invoke-RestMethod -Uri "$platformUrl/api/health" -TimeoutSec 2
        return ($health.projectId -eq 'selinco' -and $health.workspace -eq $workspacePath)
    } catch { return $false }
}
try {
    if (-not (Test-SelincoService)) {
        $logFolder = Join-Path $workspacePath 'logs'
        New-Item -ItemType Directory -Path $logFolder -Force | Out-Null
        $stdoutPath = Join-Path $logFolder 'service.stdout.log'
        $stderrPath = Join-Path $logFolder 'service.stderr.log'
        $process = Start-Process -FilePath (Join-Path $applicationRoot 'SelincoPlatform.exe') -ArgumentList '--no-browser' -WorkingDirectory $applicationRoot -WindowStyle Hidden -RedirectStandardOutput $stdoutPath -RedirectStandardError $stderrPath -PassThru
        $serviceReady = $false
        for ($attempt = 0; $attempt -lt 100; $attempt++) {
            if (Test-SelincoService) { $serviceReady = $true; break }
            if ($process.HasExited) { break }
            Start-Sleep -Milliseconds 300
        }
        if (-not $serviceReady) { throw "平台未能启动。请检查8879端口，并查看 $stderrPath" }
    }
    if (-not $NoBrowser) { Start-Process $platformUrl }
} catch {
    Add-Type -AssemblyName System.Windows.Forms
    [System.Windows.Forms.MessageBox]::Show($_.Exception.Message, '色林错平台启动失败') | Out-Null
    exit 1
}
