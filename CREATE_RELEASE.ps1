$ErrorActionPreference = "Stop"

Write-Host ""
Write-Host "================================================"
Write-Host " CHANDRA TRADING v1.0 - RELEASE PACKAGE"
Write-Host "================================================"
Write-Host ""

$ProjectRoot = "C:\dashboard\forex\ChandraTrading"
$DistDir     = "$ProjectRoot\dist\ChandraTrading"
$ReleaseDir  = "$ProjectRoot\release"
$ZipFile     = "$ProjectRoot\ChandraTrading-v1.0-Friend-Release.zip"

Set-Location $ProjectRoot

# ------------------------------------------------
# 1. Verify EXE exists
# ------------------------------------------------

$Exe = "$DistDir\ChandraTrading.exe"

if (!(Test-Path $Exe)) {
    throw "ChandraTrading.exe not found. Build the EXE first."
}

Write-Host "[OK] EXE found:"
Write-Host "     $Exe"
Write-Host ""

# ------------------------------------------------
# 2. Remove previous release
# ------------------------------------------------

if (Test-Path $ReleaseDir) {
    Write-Host "[CLEAN] Removing previous release..."
    Remove-Item $ReleaseDir -Recurse -Force
}

New-Item -ItemType Directory -Force $ReleaseDir | Out-Null

# ------------------------------------------------
# 3. Copy complete PyInstaller folder
# ------------------------------------------------

Write-Host "[COPY] Copying EXE and _internal..."

Copy-Item `
    "$DistDir\*" `
    $ReleaseDir `
    -Recurse `
    -Force

# ------------------------------------------------
# 4. Create customer folders
# ------------------------------------------------

Write-Host "[CREATE] license folder..."
New-Item `
    -ItemType Directory `
    -Force `
    -Path "$ReleaseDir\license" |
    Out-Null

Write-Host "[CREATE] data folder..."
New-Item `
    -ItemType Directory `
    -Force `
    -Path "$ReleaseDir\data" |
    Out-Null

# ------------------------------------------------
# 5. Create DUMMY license
#
# IMPORTANT:
# This is intentionally NOT a valid signed license.
# It prevents the real developer/friend license
# from being distributed accidentally.
# ------------------------------------------------

Write-Host "[CREATE] Dummy license.key..."

$DummyLicense = @'
{
  "product": "ChandraTrading",
  "license_id": "DUMMY-NOT-A-VALID-LICENSE",
  "issued_at": "2026-08-28",
  "expires_at": "2026-08-31",
  "customer": "DUMMY",
  "features": [
    "TIMING_CANDLE",
    "STRATEGIC_ENTRY",
    "MAGICAL_ENTRY"
  ],
  "max_lot": 0.01,
  "machine_id": "DUMMY",
  "signature": "DUMMY-SIGNATURE"
}
'@

Set-Content `
    -Path "$ReleaseDir\license\license.key" `
    -Value $DummyLicense `
    -Encoding UTF8

# ------------------------------------------------
# 6. Create empty trade history
# ------------------------------------------------

Write-Host "[CREATE] data\trade_history.json..."

Set-Content `
    -Path "$ReleaseDir\data\trade_history.json" `
    -Value "[]" `
    -Encoding UTF8

# ------------------------------------------------
# 7. Remove developer/private files if present
# ------------------------------------------------

Write-Host "[SECURITY] Checking release package..."

Get-ChildItem `
    $ReleaseDir `
    -Recurse `
    -File `
    -ErrorAction SilentlyContinue |
    Where-Object {
        $_.Name -match "private_key|generate_license|public_key"
    } |
    Remove-Item -Force -ErrorAction SilentlyContinue

# ------------------------------------------------
# 8. Verify final structure
# ------------------------------------------------

Write-Host ""
Write-Host "================================================"
Write-Host " RELEASE CONTENTS"
Write-Host "================================================"

Get-ChildItem $ReleaseDir

Write-Host ""
Write-Host "License:"
Get-Item "$ReleaseDir\license\license.key"

Write-Host ""
Write-Host "Data:"
Get-Item "$ReleaseDir\data\trade_history.json"

# ------------------------------------------------
# 9. Security check
# ------------------------------------------------

$SensitiveFiles = Get-ChildItem `
    $ReleaseDir `
    -Recurse `
    -File `
    -ErrorAction SilentlyContinue |
    Where-Object {
        $_.Name -match "private_key|generate_license"
    }

if ($SensitiveFiles) {
    Write-Host ""
    Write-Host "[ERROR] Sensitive files found!"
    $SensitiveFiles | Select-Object FullName
    throw "Release contains developer signing files."
}

Write-Host ""
Write-Host "[OK] No private signing files found."

# ------------------------------------------------
# 10. Remove previous ZIP
# ------------------------------------------------

if (Test-Path $ZipFile) {
    Remove-Item $ZipFile -Force
}

# ------------------------------------------------
# 11. Create ZIP
# ------------------------------------------------

Write-Host ""
Write-Host "[ZIP] Creating release ZIP..."

Compress-Archive `
    -Path "$ReleaseDir\*" `
    -DestinationPath $ZipFile `
    -Force

# ------------------------------------------------
# 12. Final result
# ------------------------------------------------

Write-Host ""
Write-Host "================================================"
Write-Host " RELEASE BUILD COMPLETE"
Write-Host "================================================"
Write-Host ""

Write-Host "Release folder:"
Write-Host "  $ReleaseDir"

Write-Host ""
Write-Host "ZIP:"
Write-Host "  $ZipFile"

Write-Host ""
Write-Host "IMPORTANT:"
Write-Host "  license.key is intentionally DUMMY."
Write-Host "  Replace it with a real signed license before"
Write-Host "  giving the application to a friend."
Write-Host ""