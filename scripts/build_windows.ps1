param([string]$Python = 'python', [string]$ISCC = "$env:LOCALAPPDATA\Programs\Inno Setup 6\ISCC.exe")
$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
Set-Location -LiteralPath $projectRoot
& $Python -m unittest discover -s tests -v
if ($LASTEXITCODE -ne 0) { throw 'Tests failed' }
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
& $ISCC "/DAppSource=$(Join-Path $distributionRoot 'SelincoPlatform')" installer/selinco.iss
if ($LASTEXITCODE -ne 0) { throw 'Installer failed' }
