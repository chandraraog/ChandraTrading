"""Developer-only Ed25519 license generator for Chandra Trading v1.0.

IMPORTANT:
- Keep private_key.pem outside the trading application.
- Never ship private_key.pem to customers.
- This script is a developer-side tool only.
"""

from __future__ import annotations

import argparse
import base64
import json
import math
import re
from datetime import date
from pathlib import Path
from typing import Any

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import load_pem_private_key


def canonical_payload(obj: dict[str, Any]) -> bytes:
    payload = {k: v for k, v in obj.items() if k != "signature"}
    return json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode("utf-8")


def load_private_key(path: Path) -> Ed25519PrivateKey:
    key = load_pem_private_key(path.read_bytes(), password=None)
    if not isinstance(key, Ed25519PrivateKey):
        raise ValueError("private_key.pem is not an Ed25519 private key")
    return key


def generate_license(
    private_key_path: Path,
    output_path: Path,
    license_id: str,
    customer: str,
    issued_at: str,
    expires_at: str,
    machine_id: str,
    features: list[str],
    max_lot: float,
) -> None:
    # Validate dates before signing.
    issued = date.fromisoformat(issued_at)
    expiry = date.fromisoformat(expires_at)

    if expiry < issued:
        raise ValueError("expires_at cannot be before issued_at")

    machine_id = machine_id.strip().upper()
    if not re.fullmatch(r"[0-9A-F]{32}", machine_id):
        raise ValueError("machine_id must be exactly 32 hexadecimal characters")

    if not math.isfinite(max_lot) or max_lot < 0:
        raise ValueError("max_lot must be finite and non-negative")

    if not features:
        raise ValueError("At least one feature is required")

    obj: dict[str, Any] = {
        "product": "ChandraTrading",
        "license_id": license_id,
        "issued_at": issued.isoformat(),
        "expires_at": expiry.isoformat(),
        "customer": customer,
        "machine_id": machine_id,
        "features": features,
        "max_lot": max_lot,
    }

    private_key = load_private_key(private_key_path)

    signature = private_key.sign(canonical_payload(obj))
    obj["signature"] = base64.b64encode(signature).decode("ascii")

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        json.dumps(obj, indent=2) + "\n",
        encoding="utf-8",
    )

    print("License created successfully.")
    print(f"License ID : {license_id}")
    print(f"Machine ID : {machine_id}")
    print(f"Customer   : {customer}")
    print(f"Expires    : {expires_at}")
    print(f"Features   : {', '.join(features)}")
    print(f"Max lot    : {max_lot}")
    print(f"Output     : {output_path}")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Generate a signed Chandra Trading license"
    )

    parser.add_argument("--private-key", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--license-id", required=True)
    parser.add_argument("--customer", required=True)
    parser.add_argument("--machine-id", required=True)
    parser.add_argument("--issued-at", required=True)
    parser.add_argument("--expires-at", required=True)
    parser.add_argument(
        "--features",
        nargs="+",
        required=True,
        choices=[
            "TIMING_CANDLE",
            "STRATEGIC_ENTRY",
            "MAGICAL_ENTRY",
            "POB",
            "*",
        ],
    )
    parser.add_argument("--max-lot", type=float, required=True)

    args = parser.parse_args()

    generate_license(
        private_key_path=Path(args.private_key),
        output_path=Path(args.output),
        license_id=args.license_id,
        customer=args.customer,
        issued_at=args.issued_at,
        expires_at=args.expires_at,
        machine_id=args.machine_id,
        features=args.features,
        max_lot=args.max_lot,
    )


if __name__ == "__main__":
    main()
