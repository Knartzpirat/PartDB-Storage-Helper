<#
.SYNOPSIS
  Baut den Windows-Installer des Druck-Helfers: installer\dist\Labeltool-Helfer-Setup.exe

.DESCRIPTION
  1. PyInstaller (onedir) -> installer\build\bundle\Labeltool-Helfer\
       LabeltoolHelfer.exe (ohne Konsole) + LabeltoolHelferSetup.exe (Assistent) + _internal\
  2. Inno Setup (ISCC) -> installer\dist\Labeltool-Helfer-Setup.exe

  Lokal: .\installer\build.ps1            (Python aus .venv, PyInstaller wird bei Bedarf installiert,
                                           Inno Setup bei Bedarf per winget)
  CI:    .\installer\build.ps1 -Python python -Version 1.2.3

.PARAMETER Version
  Versionsnummer für Setup und Programme. Standard: Git-Tag v1.2.3 -> 1.2.3, sonst 0.0.0-<commit>.
#>
[CmdletBinding()]
param(
    [string]$Version = "",
    [string]$Python = "",
    [string]$Iscc = "",
    [switch]$OhneWinget
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

$Installer = $PSScriptRoot
$Projekt = Split-Path $Installer -Parent
$Build = Join-Path $Installer "build"
$Bundle = Join-Path $Build "bundle\Labeltool-Helfer"
$Dist = Join-Path $Installer "dist"

function Schritt([string]$Text) { Write-Host "`n==> $Text" -ForegroundColor Cyan }

# ---------------------------------------------------------------- Version
if (-not $Version) {
    $tag = (& git -C $Projekt describe --tags --exact-match 2>$null)
    if ($LASTEXITCODE -eq 0 -and $tag -match '^v?(\d+\.\d+\.\d+.*)$') {
        $Version = $Matches[1]
    } else {
        $sha = (& git -C $Projekt rev-parse --short HEAD 2>$null)
        $Version = if ($LASTEXITCODE -eq 0 -and $sha) { "0.0.0-$sha" } else { "0.0.0-dev" }
    }
}
$Version = $Version.Trim() -replace '^v', ''
if ($Version -notmatch '^\d+\.\d+\.\d+') { throw "Version '$Version' muss mit x.y.z beginnen (z. B. 1.2.3 oder 0.0.0-abc1234)." }
$VersionNum = ($Version -split '-')[0]
Write-Host "Version: $Version (Dateiversion $VersionNum)"

# ---------------------------------------------------------------- Python + PyInstaller
if (-not $Python) {
    $Python = Join-Path $Projekt ".venv\Scripts\python.exe"
    if (-not (Test-Path $Python)) { throw "Python nicht gefunden: $Python - erst die venv anlegen (start.bat) oder -Python angeben." }
}
Schritt "PyInstaller prüfen"
& $Python -m PyInstaller --version 2>$null | Out-Null
if ($LASTEXITCODE -ne 0) {
    & $Python -m pip install --disable-pip-version-check pyinstaller
    if ($LASTEXITCODE -ne 0) { throw "pip install pyinstaller fehlgeschlagen." }
}
& $Python -c "import yaml, websockets, win32com.client, win32print, tkinter"
if ($LASTEXITCODE -ne 0) { throw "Python-Pakete fehlen (pyyaml, websockets, pywin32, tkinter) - pip install -r requirements.txt" }

# ---------------------------------------------------------------- Inno Setup
function Finde-Iscc {
    $kandidaten = @()
    $cmd = Get-Command iscc.exe -ErrorAction SilentlyContinue
    if ($cmd) { $kandidaten += $cmd.Source }
    foreach ($basis in @(${env:ProgramFiles(x86)}, $env:ProgramFiles, (Join-Path $env:LOCALAPPDATA "Programs"))) {
        if ($basis) { $kandidaten += (Join-Path $basis "Inno Setup 6\ISCC.exe") }
    }
    foreach ($k in $kandidaten) { if ($k -and (Test-Path $k)) { return $k } }
    return $null
}
if (-not $Iscc) { $Iscc = Finde-Iscc }
if (-not $Iscc) {
    if ($OhneWinget -or -not (Get-Command winget -ErrorAction SilentlyContinue)) {
        throw "Inno Setup 6 (ISCC.exe) nicht gefunden - https://jrsoftware.org/isdl.php oder: winget install --id JRSoftware.InnoSetup -e"
    }
    Schritt "Inno Setup per winget installieren"
    & winget install --id JRSoftware.InnoSetup -e --silent --accept-package-agreements --accept-source-agreements
    $Iscc = Finde-Iscc
    if (-not $Iscc) { throw "Inno Setup wurde installiert, ISCC.exe aber nicht gefunden." }
}
Write-Host "ISCC: $Iscc"

# ---------------------------------------------------------------- Versionsinfo der EXE-Dateien
New-Item -ItemType Directory -Force $Build | Out-Null
$teile = @($VersionNum.Split('.') | ForEach-Object { [int]$_ }) + @(0)
$tupel = ($teile[0..3] -join ', ')
foreach ($e in @(@{ Datei = "version_helfer.txt"; Name = "LabeltoolHelfer.exe"; Text = "Labeltool Druck-Helfer" },
                 @{ Datei = "version_setup.txt"; Name = "LabeltoolHelferSetup.exe"; Text = "Labeltool Druck-Helfer - Einrichtung" })) {
    $inhalt = @"
VSVersionInfo(
  ffi=FixedFileInfo(filevers=($tupel), prodvers=($tupel), mask=0x3f, flags=0x0, OS=0x40004,
                    fileType=0x1, subtype=0x0, date=(0, 0)),
  kids=[
    StringFileInfo([StringTable('040704B0', [
      StringStruct('CompanyName', 'Labeltool'),
      StringStruct('FileDescription', '$($e.Text)'),
      StringStruct('FileVersion', '$Version'),
      StringStruct('InternalName', '$($e.Name)'),
      StringStruct('OriginalFilename', '$($e.Name)'),
      StringStruct('ProductName', 'Labeltool Druck-Helfer'),
      StringStruct('ProductVersion', '$Version')])]),
    VarFileInfo([VarStruct('Translation', [1031, 1200])])
  ]
)
"@
    [IO.File]::WriteAllText((Join-Path $Build $e.Datei), $inhalt, [Text.UTF8Encoding]::new($false))
}

# ---------------------------------------------------------------- PyInstaller
Schritt "PyInstaller: Programme bündeln"
Push-Location $Projekt
try {
    & $Python -m PyInstaller --noconfirm --clean --log-level WARN `
        --distpath (Join-Path $Build "bundle") --workpath (Join-Path $Build "work") `
        (Join-Path $Installer "helfer.spec")
    if ($LASTEXITCODE -ne 0) { throw "PyInstaller fehlgeschlagen." }
} finally { Pop-Location }
foreach ($exe in @("LabeltoolHelfer.exe", "LabeltoolHelferSetup.exe")) {
    if (-not (Test-Path (Join-Path $Bundle $exe))) { throw "$exe fehlt im Bundle $Bundle" }
}

# ---------------------------------------------------------------- Inno Setup
Schritt "Inno Setup: Setup-Datei bauen"
New-Item -ItemType Directory -Force $Dist | Out-Null
& $Iscc /Q "/DVersion=$Version" "/DBundle=$Bundle" "/O$Dist" (Join-Path $Installer "labeltool-helfer.iss")
if ($LASTEXITCODE -ne 0) { throw "ISCC fehlgeschlagen (Code $LASTEXITCODE)." }

$setup = Get-Item (Join-Path $Dist "Labeltool-Helfer-Setup.exe")
Schritt ("Fertig: {0} ({1:N1} MB, Version {2})" -f $setup.FullName, ($setup.Length / 1MB), $Version)
