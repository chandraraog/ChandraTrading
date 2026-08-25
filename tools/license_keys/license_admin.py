"""Developer-only license administration.

Usage:
  python tools/license_keys/license_admin.py keygen
  python tools/license_keys/license_admin.py generate --customer Friend-01 --expires 2026-09-30 --machine MACHINE_ID --features TIMING_CANDLE --max-lot 0.01

Keep the generated private_key.pem OFF the customer/distribution package.
"""
from __future__ import annotations
import argparse, base64, json
from datetime import date
from pathlib import Path
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

ROOT = Path(__file__).resolve().parents[2]
KEYDIR = ROOT / "tools" / "license_keys"
PRIVATE = KEYDIR / "private_key.pem"
PUBLIC = ROOT / "license" / "public_key.pem"


def canonical(obj):
    return json.dumps({k:v for k,v in obj.items() if k != "signature"}, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()


def keygen():
    KEYDIR.mkdir(parents=True, exist_ok=True)
    PUBLIC.parent.mkdir(parents=True, exist_ok=True)
    key = Ed25519PrivateKey.generate()
    PRIVATE.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
    pub = key.public_key()
    PUBLIC.write_bytes(pub.public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo))
    print(f"Private key: {PRIVATE}")
    print(f"Public key : {PUBLIC}")
    print("IMPORTANT: never distribute private_key.pem")


def generate(args):
    private_path = Path(args.private_key) if args.private_key else PRIVATE
    if not private_path.exists():
        raise SystemExit(f"Private key missing: {private_path}")
    key = serialization.load_pem_private_key(private_path.read_bytes(), password=None)
    obj = {
        "product": "Chandra Trading",
        "license_id": args.license_id,
        "customer": args.customer,
        "issued_at": date.today().isoformat(),
        "expires_at": args.expires,
        "machine_id": args.machine,
        "features": [x.strip().upper() for x in args.features.split(",") if x.strip()],
        "max_lot": float(args.max_lot),
        "signature": "",
    }
    obj["signature"] = base64.b64encode(key.sign(canonical(obj))).decode("ascii")
    out = Path(args.output)
    if not out.is_absolute():
        out = ROOT / out
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(obj, indent=2), encoding="utf-8")
    print(f"License written: {out}")


p = argparse.ArgumentParser()
sub = p.add_subparsers(dest="cmd", required=True)
k = sub.add_parser("keygen"); k.set_defaults(func=lambda a: keygen())
g = sub.add_parser("generate")
g.add_argument("--license-id", required=True)
g.add_argument("--customer", required=True)
g.add_argument("--expires", required=True, help="YYYY-MM-DD")
g.add_argument("--machine", default="*", help="Machine ID or * for developer/internal use")
g.add_argument("--features", default="TIMING_CANDLE")
g.add_argument("--max-lot", default="0.01", type=float)
g.add_argument("--output", default="license/license.key")
g.add_argument("--private-key", default=None, help="Path to developer private_key.pem kept outside the project")
g.set_defaults(func=generate)
a = p.parse_args(); a.func(a)
