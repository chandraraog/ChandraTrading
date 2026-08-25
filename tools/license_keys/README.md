# Developer-only license administration

**Never ship the private signing key.** Keep `Chandra-Trading-private-key.pem` in a secure developer-only location.

## 1. Create the signing keypair once

```powershell
python tools/license_keys/license_admin.py keygen
```

After key generation, copy the private key to a secure location outside the project and remove it from the project folder.

The public key must be embedded into the application before a customer build.

## 2. Generate a customer license

Get the customer's machine ID from `/api/license/status` (the status endpoint remains available even when no license is valid).

```powershell
python tools/license_keys/license_admin.py generate `
  --license-id CT-0001 `
  --customer Friend-01 `
  --expires 2026-09-30 `
  --machine MACHINE_ID `
  --features TIMING_CANDLE `
  --max-lot 0.01
```

The signature covers the complete license payload. Editing expiry, machine ID, features or max lot invalidates it.

## Development

Use `start_dev.ps1` for source-level testing. It enables `CHANDRA_DEV_MODE=1` so the trading engine can be exercised without a customer license.

The final binary does not use developer mode.
