<#
    Fotodit build script: virtual environment, dependencies, tests, exe and self-test.

    Usage:
      powershell -ExecutionPolicy Bypass -File packaging\build.ps1              # folder distribution (recommended)
      powershell -ExecutionPolicy Bypass -File packaging\build.ps1 -OneFile     # single-file exe
      powershell -ExecutionPolicy Bypass -File packaging\build.ps1 -Release     # both, then the release zips (dist\release)
      powershell -ExecutionPolicy Bypass -File packaging\build.ps1 -SkipTests
      ... -Sign                                                       # also sign Fotodit.exe (see README, "Signing")

    -Sign needs signtool.exe (Windows SDK) and a code-signing certificate in the current user's
    certificate store: set FOTODIT_SIGN_THUMBPRINT to its thumbprint (FOTODIT_SIGN_TIMESTAMP: another
    RFC 3161 timestamp server).
#>
param([switch]$OneFile, [switch]$SkipTests, [switch]$Release, [switch]$Sign)

$ErrorActionPreference = "Stop"
$root = Split-Path $PSScriptRoot -Parent  # the project folder
Set-Location $root

if (-not (Test-Path ".venv\Scripts\python.exe")) {
    Write-Host "Creating the virtual environment..."
    py -3.14 -m venv .venv
}
$py = Join-Path $root ".venv\Scripts\python.exe"

& $py -m pip install --quiet --upgrade pip
& $py -m pip install --quiet -r requirements-dev.txt
& $py packaging\make_icon.py

if (-not $SkipTests) {
    & $py -m pytest tests -q -p no:cacheprovider
    if ($LASTEXITCODE -ne 0) { throw "Tests failed." }
}

function Find-SignTool {
    $tool = Get-Command signtool.exe -ErrorAction SilentlyContinue
    if ($tool) { return $tool.Source }
    $kits = Join-Path ${env:ProgramFiles(x86)} "Windows Kits\10\bin"
    $found = Get-ChildItem -Path $kits -Filter signtool.exe -Recurse -ErrorAction SilentlyContinue |
        Where-Object { $_.FullName -like "*\x64\*" } | Sort-Object FullName -Descending | Select-Object -First 1
    if ($found) { return $found.FullName }
    throw "signtool.exe was not found: install the Windows SDK (its 'Signing Tools' part)."
}

function Invoke-Sign([string]$path) {
    if (-not $env:FOTODIT_SIGN_THUMBPRINT) {
        throw "Set FOTODIT_SIGN_THUMBPRINT to the code-signing certificate's thumbprint (Cert:\CurrentUser\My)."
    }
    $timestamp = $env:FOTODIT_SIGN_TIMESTAMP
    if (-not $timestamp) { $timestamp = "http://timestamp.digicert.com" }
    & (Find-SignTool) sign /sha1 $env:FOTODIT_SIGN_THUMBPRINT /fd SHA256 /tr $timestamp /td SHA256 /d "Fotodit" $path | Out-Host
    if ($LASTEXITCODE -ne 0) { throw "Signing failed: $path" }
}

function Test-Exe([string]$exe) {
    $report = Join-Path $env:TEMP "fotodit-self-test.txt"
    $proc = Start-Process -FilePath $exe -ArgumentList @("--self-test", "`"$report`"") -Wait -PassThru
    Get-Content $report -Encoding UTF8 | Out-Host
    if ($proc.ExitCode -ne 0) { throw "The packaged app's self-test failed: $exe" }
}

function Build-Exe([bool]$single) {
    if ($single) {
        $env:FOTODIT_ONEFILE = "1"
        $dist = "dist\onefile"; $work = "build\onefile"; $exe = "$dist\Fotodit.exe"
    } else {
        $env:FOTODIT_ONEFILE = $null
        $dist = "dist"; $work = "build"; $exe = "$dist\Fotodit\Fotodit.exe"
    }
    & $py -m PyInstaller --noconfirm --clean --distpath $dist --workpath $work packaging\fotodit.spec | Out-Host
    if ($LASTEXITCODE -ne 0) { throw "PyInstaller failed." }
    $env:FOTODIT_ONEFILE = $null
    if ($Sign) { Invoke-Sign $exe }
    Test-Exe $exe
    return $exe
}

if ($Release) {
    $built = @((Build-Exe $false), (Build-Exe $true))
    & $py packaging\package_release.py
    if ($LASTEXITCODE -ne 0) { throw "Packing the release failed." }
} else {
    $built = @(Build-Exe $OneFile.IsPresent)
}
foreach ($exe in $built) { Write-Host "Ready: $exe" -ForegroundColor Green }
if ($Release) { Write-Host "Release files: dist\release" -ForegroundColor Green }
