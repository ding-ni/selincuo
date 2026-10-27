param([string]$Python = 'python', [string]$ISCC = "$env:LOCALAPPDATA\Programs\Inno Setup 6\ISCC.exe")
$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
Set-Location -LiteralPath $projectRoot
$releaseVersion = (& $Python -c "from backend.common import VERSION; print(VERSION)").Trim()
if ($LASTEXITCODE -ne 0 -or -not $releaseVersion) { throw 'Cannot read release version' }
& $Python scripts/prepare_windows_text.py
& $Python -m unittest discover -s tests -v
if ($LASTEXITCODE -ne 0) { throw 'Tests failed' }
node --test tests/workflow.test.mjs tests/dashboard.test.mjs tests/history-workflow.test.mjs tests/business-view.test.mjs
if ($LASTEXITCODE -ne 0) { throw 'Frontend workflow tests failed' }
Push-Location frontend
npm ci --offline
if ($LASTEXITCODE -ne 0) { throw 'Install frontend dependencies failed' }
npm run build
if ($LASTEXITCODE -ne 0) { throw 'Frontend build failed' }
Pop-Location
$buildRun = Get-Date -Format 'yyyyMMdd-HHmmss'
$distributionRoot = Join-Path $projectRoot "build\release-$buildRun"
& $Python -m PyInstaller --distpath $distributionRoot --workpath build/pyinstaller installer/selinco.spec
if ($LASTEXITCODE -ne 0) { throw 'PyInstaller failed' }
& $Python scripts/assemble_release.py --destination (Join-Path $distributionRoot 'SelincoPlatform')
if ($LASTEXITCODE -ne 0) { throw 'Assembly failed' }
$releaseSource = Join-Path $distributionRoot 'SelincoPlatform'
$releaseExe = Join-Path $releaseSource 'SelincoPlatform.exe'
& $releaseExe --self-check
if ($LASTEXITCODE -ne 0) { throw 'Packaged runtime self-check failed' }
& (Join-Path $PSScriptRoot 'verify_delivery.ps1') -Application $releaseExe -VerificationRoot (Join-Path $distributionRoot 'portable-verification') -Python $Python -ExpectedVersion $releaseVersion
if ($LASTEXITCODE -ne 0) { throw 'Packaged workflow verification failed' }
& $ISCC "/DAppSource=$releaseSource" "/DAppVersion=$releaseVersion" '/DVerificationInstall=1' installer/selinco.iss
if ($LASTEXITCODE -ne 0) { throw 'Verification installer build failed' }
$verificationSetup = Join-Path $projectRoot "releases/色林错预警预报平台-v$releaseVersion-Verification-Setup.exe"
& (Join-Path $PSScriptRoot 'verify_installation.ps1') -Setup $verificationSetup -VerificationRoot (Join-Path $distributionRoot 'installed-verification') -Python $Python -ExpectedVersion $releaseVersion
if ($LASTEXITCODE -ne 0) { throw 'Installed workflow verification failed' }
& $ISCC "/DAppSource=$releaseSource" "/DAppVersion=$releaseVersion" installer/selinco.iss
if ($LASTEXITCODE -ne 0) { throw 'Installer failed' }
