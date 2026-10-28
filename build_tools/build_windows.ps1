# Build standalone Windows bundles on a Windows machine.
# PyInstaller cannot cross-compile; run this in PowerShell on Windows.
# Usage: powershell -ExecutionPolicy Bypass -File .\build_tools\build_windows.ps1 [-Target all|web|desktop]
param(
    [ValidateSet("all", "web", "desktop")]
    [string]$Target = "all"
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
Set-Location $Root

$Venv = Join-Path $Root ".build\venv-win"
$Dist = Join-Path $Root "dist"
$Build = Join-Path $Root "build"
$Stamp = Join-Path $Venv ".anomizer-build-deps"

if (-not (Test-Path (Join-Path $Venv "Scripts\python.exe"))) {
    Write-Host "==> Создаю сборочное окружение: $Venv"
    python -m venv $Venv
}

$VenvPython = Join-Path $Venv "Scripts\python.exe"
if ((-not (Test-Path $Stamp)) -or
    ((Get-Item "requirements-build.txt").LastWriteTime -gt (Get-Item $Stamp).LastWriteTime)) {
    Write-Host "==> Устанавливаю сборочные зависимости"
    & $VenvPython -m pip install --upgrade pip
    if ($LASTEXITCODE -ne 0) { throw "pip install завершился с ошибкой" }
    & $VenvPython -m pip install -r "requirements-build.txt"
    if ($LASTEXITCODE -ne 0) { throw "pip install завершился с ошибкой" }
    New-Item -ItemType File -Path $Stamp -Force | Out-Null
}

New-Item -ItemType Directory -Force -Path $Dist, $Build | Out-Null
$Version = (Select-String -Path "anomizer\__init__.py" -Pattern '^__version__ = "(.*)"').Matches[0].Groups[1].Value
if (-not $Version) { throw "Не удалось определить версию" }

function Copy-Docs([string]$Folder) {
    Copy-Item "THIRD_PARTY_NOTICES.md" $Folder
    Copy-Item "README.md" $Folder
    Copy-Item "build_tools\README-WEB.txt" $Folder
    New-Item -ItemType Directory -Force -Path (Join-Path $Folder "models") | Out-Null
    Copy-Item "models\README.md" (Join-Path $Folder "models")
}

function Build-Web {
    Write-Host "==> Собираю веб-версию"
    & $VenvPython -m PyInstaller --noconfirm --clean `
        --distpath $Dist --workpath (Join-Path $Build "web") `
        "build_tools\anomizer_web.spec"
    if ($LASTEXITCODE -ne 0) { throw "Сборка веб-версии не удалась" }
    Copy-Docs (Join-Path $Dist "Anomizer-Web")
    Copy-Item "build_tools\Anomizer-Web.bat" (Join-Path $Dist "Anomizer-Web")
    $Archive = Join-Path $Dist "Anomizer-Web-$Version-windows-x86_64.zip"
    Remove-Item $Archive -Force -ErrorAction SilentlyContinue
    Compress-Archive -Path (Join-Path $Dist "Anomizer-Web") -DestinationPath $Archive
}

function Build-Desktop {
    Write-Host "==> Собираю desktop-версию"
    & $VenvPython -m PyInstaller --noconfirm --clean `
        --distpath $Dist --workpath (Join-Path $Build "desktop") `
        "build_tools\anomizer_desktop.spec"
    if ($LASTEXITCODE -ne 0) { throw "Сборка desktop-версии не удалась" }
    Copy-Docs (Join-Path $Dist "Anomizer")
    $Archive = Join-Path $Dist "Anomizer-$Version-windows-x86_64.zip"
    Remove-Item $Archive -Force -ErrorAction SilentlyContinue
    Compress-Archive -Path (Join-Path $Dist "Anomizer") -DestinationPath $Archive
}

if ($Target -in @("all", "web")) { Build-Web }
if ($Target -in @("all", "desktop")) { Build-Desktop }

Get-ChildItem $Dist -File |
    Where-Object { $_.Name -like "*.zip" -or $_.Name -like "*.tar.gz" } |
    Sort-Object Name |
    ForEach-Object {
        $hash = (Get-FileHash $_.FullName -Algorithm SHA256).Hash.ToLower()
        "$hash  $($_.Name)"
    } | Set-Content (Join-Path $Dist "SHA256SUMS") -Encoding ASCII

Write-Host ""
Write-Host "Готово. Архивы и контрольные суммы:"
Get-ChildItem $Dist -File |
    Where-Object { $_.Name -like "*.zip" -or $_.Name -like "*.tar.gz" } |
    Sort-Object Name | Format-Table Name, Length
Get-Content (Join-Path $Dist "SHA256SUMS")
Write-Host ""
Write-Host "Проверка веб-сборки без окна браузера:"
Write-Host "  .\dist\Anomizer-Web\anomizer-web.exe --no-browser --port 8765"
