# Chandra Trading Licensing — v1 foundation

The application now validates an Ed25519-signed offline license before starting the trading engine.

## Customer package

Ship only the application/binary, `license/license.key`, and the public key embedded in the final binary. **Never ship `tools/license_keys/private_key.pem`.**

## License fields

- product
- license_id
- customer
- issued_at
- expires_at
- machine_id
- features
- max_lot
- signature

Changing expiry date, machine ID, features, or lot limit invalidates the signature.

## Developer workflow

1. `python tools/license_keys/license_admin.py keygen`
2. Generate a license with an expiry date, machine ID and features.
3. Keep `private_key.pem` outside all customer builds/backups that are shared.
4. The final Windows executable should embed the public key rather than relying on a customer-editable PEM file.

Current source build reads `license/public_key.pem` so we can test licensing before the executable packaging step.
