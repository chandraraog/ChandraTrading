# MODULAR-02.24 — Licensing Foundation

- Added Ed25519 signed offline license validation.
- Expiry date is cryptographically signed; editing the license invalidates it.
- Added machine binding and feature permissions.
- Added maximum lot limit to the signed license.
- Added basic system-clock rollback detection using local validation state.
- Trading engine refuses to start when license validation fails.
- Added `/api/license/status` for dashboard integration.
- Added developer-only license key generator under `tools/license_keys/`.
- Private key is never required by the application and must never be distributed.
- Existing Timing Candle / Strategic / Magical trading logic is unchanged.
