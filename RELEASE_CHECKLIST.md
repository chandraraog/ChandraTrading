# Chandra Trading v1 — Friend Release Checklist

## Developer machine

- Keep `Chandra-Trading-private-key.pem` outside the project.
- Never put it in Git, ZIP, release, or friend machine.
- Build the executable with `BUILD_WINDOWS.ps1`.

## Generate friend license

```powershell
python tools/license_keys/license_admin.py generate `
  --license-id CT-0001 `
  --customer Friend-01 `
  --expires 2026-09-30 `
  --machine <MACHINE_ID> `
  --features TIMING_CANDLE `
  --max-lot 0.01 `
  --private-key C:\Secure\Chandra-Trading-private-key.pem `
  --output release\license\license.key
```

## Customer package

```text
ChandraTrading.exe
license\license.key
data\
```

No Python files, `.pyc`, source directories, private key, or developer tools.

## First release feature set

- TIMING_CANDLE enabled
- max lot 0.01
- Strategic/Strategic and Magical can be licensed later
- Manual order can be licensed later
