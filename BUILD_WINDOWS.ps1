$ErrorActionPreference = 'Stop'

Write-Host ""
Write-Host "==============================================="
Write-Host "      CHANDRA TRADING - WINDOWS BUILD"
Write-Host "==============================================="
Write-Host ""

# ------------------------------------------------------------
# Install dependencies
# ------------------------------------------------------------

python -m pip install -r requirements.txt
python -m pip install pyinstaller

# ------------------------------------------------------------
# Clean previous build output
# ------------------------------------------------------------

if (Test-Path dist) {
    Remove-Item dist -Recurse -Force
}

if (Test-Path build) {
    Remove-Item build -Recurse -Force
}

if (Test-Path release) {
    Remove-Item release -Recurse -Force
}

# ------------------------------------------------------------
# Build executable
#
# launcher.py performs license validation BEFORE
# starting the FastAPI application.
#
# IMPORTANT:
# The developer private key is NOT included.
# ------------------------------------------------------------

pyinstaller --noconfirm --clean --onefile --name ChandraTrading `
  --add-data 'frontend;frontend' `
  launcher.py

# ------------------------------------------------------------
# Prepare customer release structure
# ------------------------------------------------------------

New-Item `
  -ItemType Directory `
  -Force `
  -Path release/license |
  Out-Null

New-Item `
  -ItemType Directory `
  -Force `
  -Path release/data |
  Out-Null

# ------------------------------------------------------------
# Copy executable
# ------------------------------------------------------------

Copy-Item `
  dist/ChandraTrading.exe `
  release/ChandraTrading.exe `
  -Force

# ------------------------------------------------------------
# Final build information
# ------------------------------------------------------------

Write-Host ""
Write-Host "==============================================="
Write-Host "             BUILD COMPLETE"
Write-Host "==============================================="
Write-Host ""

Write-Host "Executable:"
Write-Host "  release/ChandraTrading.exe"

Write-Host ""
Write-Host "Customer license location:"
Write-Host "  release/license/license.key"

Write-Host ""
Write-Host "IMPORTANT:"
Write-Host "  DO NOT ship the developer private signing key."
Write-Host "  DO NOT ship C:\ChandraTrading-Keys."
Write-Host ""

Write-Host "Friend workflow:"
Write-Host "  1. Run ChandraTrading.exe"
Write-Host "  2. Copy the displayed Machine ID"
Write-Host "  3. Send Machine ID to administrator"
Write-Host "  4. Receive license.key"
Write-Host "  5. Place license.key in release/license/"
Write-Host "  6. Run ChandraTrading.exe again"
Write-Host ""