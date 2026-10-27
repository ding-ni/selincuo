param(
    [Parameter(Mandatory=$true)][string]$Application,
    [Parameter(Mandatory=$true)][string]$VerificationRoot,
    [string]$Python = 'python',
    [int]$Port = 8882,
    [string]$ExpectedVersion = '0.7.0'
)
$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
$verificationPath = [IO.Path]::GetFullPath($VerificationRoot)
$allowedRoot = [IO.Path]::GetFullPath((Join-Path $projectRoot 'build')) + [IO.Path]::DirectorySeparatorChar
if (-not $verificationPath.StartsWith($allowedRoot, [StringComparison]::OrdinalIgnoreCase)) { throw 'Verification must use a new project build directory' }
if (Test-Path -LiteralPath $verificationPath) { throw 'Verification directory already exists; use a new directory' }
$testUrl = "http://127.0.0.1:$Port"
try {
    $occupied = Invoke-WebRequest -Uri "$testUrl/api/health" -TimeoutSec 1
    throw 'Verification port is already in use; choose a free port'
} catch {
    if ($_.Exception.Message -eq 'Verification port is already in use; choose a free port') { throw }
}
New-Item -ItemType Directory -Path $verificationPath | Out-Null
$workspace = Join-Path $verificationPath 'workspace'
$executable = [IO.Path]::GetFullPath($Application)
$process = Start-Process -FilePath $executable -ArgumentList @('--no-browser','--port',"$Port",'--workspace',('"' + $workspace + '"')) -WorkingDirectory (Split-Path -Parent $executable) -WindowStyle Hidden -RedirectStandardOutput (Join-Path $verificationPath 'service.stdout.log') -RedirectStandardError (Join-Path $verificationPath 'service.stderr.log') -PassThru
$ownedService = $false
try {
    $health = $null
    for ($attempt=0; $attempt -lt 120; $attempt++) {
        if ($process.HasExited) { throw "Packaged service exited: $(Get-Content (Join-Path $verificationPath 'service.stderr.log') -Raw)" }
        try { $health = Invoke-RestMethod -Uri "$testUrl/api/health" -TimeoutSec 1 } catch { $health = $null }
        if ($health) {
            if ($health.workspace -ne $workspace -or $health.projectId -ne 'selinco') { throw 'Unexpected service at verification port' }
            $ownedService = $true
            break
        }
        Start-Sleep -Milliseconds 500
    }
    if (-not $ownedService) { throw 'Packaged service startup timed out' }
    & $Python (Join-Path $PSScriptRoot 'verify_packaged.py') --url $testUrl --workspace $workspace --expected-version $ExpectedVersion --output (Join-Path $verificationPath 'verification.json')
    if ($LASTEXITCODE -ne 0) { throw 'Packaged standard scheme verification failed' }
} finally {
    if ($ownedService) {
        try { Invoke-RestMethod -Uri "$testUrl/api/model/shutdown" -Method Post -ContentType 'application/json' -Body '{}' | Out-Null } catch { Write-Warning $_.Exception.Message }
    }
}
