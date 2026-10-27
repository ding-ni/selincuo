param(
    [Parameter(Mandatory=$true)][string]$Setup,
    [Parameter(Mandatory=$true)][string]$VerificationRoot,
    [string]$Python = 'python',
    [string]$ExpectedVersion = '0.7.0',
    [int]$Port = 8882
)
$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
$verificationPath = [IO.Path]::GetFullPath($VerificationRoot)
$allowedRoot = [IO.Path]::GetFullPath((Join-Path $projectRoot 'build')) + [IO.Path]::DirectorySeparatorChar
if (-not $verificationPath.StartsWith($allowedRoot,[StringComparison]::OrdinalIgnoreCase)) { throw 'Installation verification must use a new build directory' }
if (Test-Path -LiteralPath $verificationPath) { throw 'Verification target already exists' }
if ((Split-Path -Leaf $Setup) -notlike '*-Verification-Setup.exe') { throw 'Only the isolated Verification installer may be installed automatically' }
New-Item -ItemType Directory -Path $verificationPath | Out-Null
$installed = Join-Path $verificationPath 'application'
$installLog = Join-Path $verificationPath 'installation.log'
$installer = Start-Process -FilePath ([IO.Path]::GetFullPath($Setup)) -ArgumentList @('/VERYSILENT','/SUPPRESSMSGBOXES','/NORESTART','/NOICONS',('/DIR="'+$installed+'"'),('/LOG="'+$installLog+'"')) -WindowStyle Hidden -PassThru -Wait
if ($installer.ExitCode -ne 0) { throw "Verification installation failed with code $($installer.ExitCode)" }
if ((Test-Path -LiteralPath (Join-Path $installed 'config/active-workspace.json')) -or (Test-Path -LiteralPath (Join-Path $installed 'workspace/database/hydrology.sqlite3'))) { throw 'Installer copied an existing user workspace' }
if ((Test-Path -LiteralPath (Join-Path $installed 'config/model-candidates')) -or (Test-Path -LiteralPath (Join-Path $installed 'scripts'))) { throw 'Installer contains development research resources' }
& (Join-Path $PSScriptRoot 'verify_delivery.ps1') -Application (Join-Path $installed 'SelincoPlatform.exe') -VerificationRoot (Join-Path $verificationPath 'workflow') -Python $Python -ExpectedVersion $ExpectedVersion -Port $Port
if ($LASTEXITCODE -ne 0) { throw 'Installed workflow verification failed' }
