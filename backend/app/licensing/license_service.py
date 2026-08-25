"""Signed offline license validation for Chandra Trading.

The distributed application contains ONLY the public key.
The private key is kept separately by the developer and must
never be shipped to customers.

License protection:
- Ed25519 signature verification
- Hardware-bound machine ID
- Expiry validation
- Offline clock rollback detection
- Local validation state
- Feature authorization
- Maximum lot authorization

Windows machine binding:
- Uses Windows MachineGuid
- Does NOT depend on MAC address
- Does NOT depend on Wi-Fi/Ethernet adapter
- Does NOT use uuid.getnode()
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import platform
from datetime import date
from pathlib import Path
from typing import Any

from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PublicKey,
)

from backend.app.runtime_paths import app_root


# ----------------------------------------------------------------------
# Application paths
# ----------------------------------------------------------------------

PROJECT_ROOT = app_root()

LICENSE_DIR = PROJECT_ROOT / "license"
LICENSE_FILE = LICENSE_DIR / "license.key"
PUBLIC_KEY_FILE = LICENSE_DIR / "public_key.pem"
STATE_FILE = PROJECT_ROOT / "data" / "license_state.json"


# ----------------------------------------------------------------------
# Developer public key
#
# This public key corresponds to the private signing key stored separately
# under C:\ChandraTrading-Keys.
#
# NEVER include the private key in the distributed application.
# ----------------------------------------------------------------------

EMBEDDED_PUBLIC_KEY_B64 = (
    "kMIKoydMHBc84eTnagDUqgI2tw79p2hIunYE9ykw0jM="
)


# ----------------------------------------------------------------------
# Canonical signed payload
# ----------------------------------------------------------------------

def _canonical_payload(
    license_obj: dict[str, Any],
) -> bytes:
    """Create deterministic bytes for Ed25519 verification."""

    payload = {
        key: value
        for key, value in license_obj.items()
        if key != "signature"
    }

    return json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode("utf-8")


# ----------------------------------------------------------------------
# Hardware fingerprint
# ----------------------------------------------------------------------

def machine_id() -> str:
    """Return a stable machine fingerprint for offline licensing.

    Windows:
        Uses the Windows MachineGuid as the machine identity.

        Network adapters, MAC addresses, Wi-Fi, Ethernet, VPN adapters,
        and uuid.getnode() are intentionally NOT used.

        This prevents normal network changes from changing the license
        Machine ID.

    Linux/other:
        Uses /etc/machine-id when available.

    The underlying machine identifier is hashed before being displayed
    or stored in a license.
    """

    if os.name == "nt":

        try:
            import winreg

            with winreg.OpenKey(
                winreg.HKEY_LOCAL_MACHINE,
                r"SOFTWARE\Microsoft\Cryptography",
            ) as key:

                guid, _ = winreg.QueryValueEx(
                    key,
                    "MachineGuid",
                )

        except Exception as exc:

            raise RuntimeError(
                "Unable to read Windows MachineGuid"
            ) from exc

        guid = str(guid).strip().upper()

        if not guid:
            raise RuntimeError(
                "Windows MachineGuid is empty"
            )

        # Include platform identifier so the fingerprint format
        # remains explicitly Windows-specific.
        raw = (
            "CHANDRA|WINDOWS|"
            + guid
        ).encode("utf-8")

    else:

        try:

            machine_file = Path(
                "/etc/machine-id"
            )

            if not machine_file.exists():
                raise RuntimeError(
                    "Machine identity is unavailable"
                )

            system_machine_id = (
                machine_file
                .read_text(
                    encoding="utf-8"
                )
                .strip()
                .upper()
            )

            if not system_machine_id:
                raise RuntimeError(
                    "Machine identity is empty"
                )

            raw = (
                "CHANDRA|"
                + platform.system().upper()
                + "|"
                + system_machine_id
            ).encode("utf-8")

        except RuntimeError:
            raise

        except Exception as exc:
            raise RuntimeError(
                "Unable to determine machine identity"
            ) from exc

    return (
        hashlib.sha256(raw)
        .hexdigest()[:32]
        .upper()
    )


# ----------------------------------------------------------------------
# License service
# ----------------------------------------------------------------------

class LicenseService:

    def __init__(
        self,
        license_file: Path = LICENSE_FILE,
    ):
        self.license_file = Path(
            license_file
        )

    # ------------------------------------------------------------------
    # Public verification key
    # ------------------------------------------------------------------

    def _public_key(
        self,
    ) -> Ed25519PublicKey:

        if EMBEDDED_PUBLIC_KEY_B64:

            try:
                raw = base64.b64decode(
                    EMBEDDED_PUBLIC_KEY_B64,
                    validate=True,
                )

                return (
                    Ed25519PublicKey
                    .from_public_bytes(raw)
                )

            except Exception as exc:
                raise ValueError(
                    "Embedded license public key is invalid"
                ) from exc

        # Developer-build fallback.
        if PUBLIC_KEY_FILE.exists():

            from cryptography.hazmat.primitives.serialization import (
                load_pem_public_key,
            )

            key = load_pem_public_key(
                PUBLIC_KEY_FILE.read_bytes()
            )

            if not isinstance(
                key,
                Ed25519PublicKey,
            ):
                raise ValueError(
                    "Unsupported public key type"
                )

            return key

        raise ValueError(
            "License public key is not configured"
        )

    # ------------------------------------------------------------------
    # Read license
    # ------------------------------------------------------------------

    def read(
        self,
    ) -> dict[str, Any]:

        if not self.license_file.exists():

            raise FileNotFoundError(
                "License file not found: "
                f"{self.license_file}"
            )

        try:

            obj = json.loads(
                self.license_file.read_text(
                    encoding="utf-8"
                )
            )

        except json.JSONDecodeError as exc:

            raise ValueError(
                "License file is not valid JSON"
            ) from exc

        if not isinstance(
            obj,
            dict,
        ):
            raise ValueError(
                "License file must contain a JSON object"
            )

        return obj

    # ------------------------------------------------------------------
    # Validate license
    # ------------------------------------------------------------------

    def validate(
        self,
    ) -> dict[str, Any]:

        obj = self.read()

        required = [
            "product",
            "license_id",
            "issued_at",
            "expires_at",
            "customer",
            "machine_id",
            "features",
            "max_lot",
            "signature",
        ]

        missing = [
            key
            for key in required
            if key not in obj
        ]

        if missing:

            raise ValueError(
                "License missing fields: "
                + ", ".join(missing)
            )


        # --------------------------------------------------------------
        # Signature verification
        #
        # This MUST happen before trusting machine ID, expiry,
        # features or maximum lot.
        # --------------------------------------------------------------

        try:

            signature = base64.b64decode(
                obj["signature"],
                validate=True,
            )

        except Exception as exc:

            raise ValueError(
                "License signature is invalid"
            ) from exc

        try:

            self._public_key().verify(
                signature,
                _canonical_payload(obj),
            )

        except Exception as exc:

            raise ValueError(
                "License signature verification failed"
            ) from exc

        # --------------------------------------------------------------
        # Hardware validation
        # --------------------------------------------------------------

        current_machine = (
            machine_id()
        )

        licensed_machine = (
            str(obj["machine_id"])
            .strip()
            .upper()
        )

        if not licensed_machine:

            raise ValueError(
                "License machine ID is empty"
            )

        # Wildcard machine licenses are intentionally not supported.
        if licensed_machine == "*":

            raise ValueError(
                "Wildcard machine licenses are not allowed"
            )

        if (
            licensed_machine
            != current_machine
        ):

            raise ValueError(
                "License machine mismatch"
            )

        # --------------------------------------------------------------
        # Date validation
        # --------------------------------------------------------------

        try:

            issued = date.fromisoformat(
                str(obj["issued_at"])
            )

            expiry = date.fromisoformat(
                str(obj["expires_at"])
            )

        except ValueError as exc:

            raise ValueError(
                "License contains an invalid date"
            ) from exc

        if expiry < issued:

            raise ValueError(
                "License expiry is before issue date"
            )

        today = date.today()

        if today > expiry:

            raise ValueError(
                "License expired on "
                f"{expiry.isoformat()}"
            )

        # --------------------------------------------------------------
        # Feature structure
        # --------------------------------------------------------------

        if not isinstance(
            obj["features"],
            list,
        ):

            raise ValueError(
                "License features are invalid"
            )

        # --------------------------------------------------------------
        # Maximum lot
        # --------------------------------------------------------------

        try:

            max_lot = float(
                obj["max_lot"]
            )

        except (
            TypeError,
            ValueError,
        ) as exc:

            raise ValueError(
                "License maximum lot is invalid"
            ) from exc

        if max_lot < 0:

            raise ValueError(
                "License maximum lot cannot be negative"
            )

        # --------------------------------------------------------------
        # Offline clock rollback protection
        # --------------------------------------------------------------

        self._check_clock_rollback()

        # --------------------------------------------------------------
        # Save successful validation state
        # --------------------------------------------------------------

        self._save_state(
            today
        )

        return {
            "valid": True,
            "license_id": obj[
                "license_id"
            ],
            "customer": obj[
                "customer"
            ],
            "expires_at": obj[
                "expires_at"
            ],
            "features": obj[
                "features"
            ],
            "max_lot": max_lot,
            "machine_id":
                current_machine,
        }

    # ------------------------------------------------------------------
    # License status
    # ------------------------------------------------------------------

    def status(
        self,
    ) -> dict[str, Any]:

        try:

            return self.validate()

        except Exception as exc:

            return {
                "valid": False,
                "error": str(exc),
                "machine_id":
                    machine_id(),
            }

    # ------------------------------------------------------------------
    # Require valid license
    # ------------------------------------------------------------------

    def require(
        self,
        feature: str | None = None,
        lot: float | None = None,
    ) -> dict[str, Any]:

        # --------------------------------------------------------------
        # Developer mode
        #
        # This is for local source-code development only.
        # The Friend EXE does not enable this environment variable.
        # --------------------------------------------------------------

        if os.getenv(
            "CHANDRA_DEV_MODE"
        ) == "1":

            return {
                "valid": True,
                "developer_mode":
                    True,
                "license_id":
                    "DEV-MODE",
                "customer":
                    "Developer",
                "expires_at":
                    None,
                "features":
                    ["*"],
                "max_lot":
                    999.0,
                "machine_id":
                    machine_id(),
            }

        try:

            info = self.validate()

            # ----------------------------------------------------------
            # Feature authorization
            # ----------------------------------------------------------

            if feature:

                allowed_features = {
                    str(item).upper()
                    for item
                    in info["features"]
                }

                if (
                    "*"
                    not in allowed_features
                    and
                    feature.upper()
                    not in allowed_features
                ):

                    raise PermissionError(
                        "License does not allow "
                        f"feature: {feature}"
                    )

            # ----------------------------------------------------------
            # Maximum lot authorization
            # ----------------------------------------------------------

            if lot is not None:

                if (
                    float(lot)
                    >
                    info["max_lot"]
                    + 1e-12
                ):

                    raise PermissionError(
                        "License maximum lot is "
                        f"{info['max_lot']}"
                    )

            return info

        except Exception as exc:

            # ----------------------------------------------------------
            # Friend-facing message.
            #
            # launcher.py catches the exception so the packaged EXE
            # exits without displaying a Python traceback.
            # ----------------------------------------------------------

            print()
            print("=" * 58)
            print(
                "              CHANDRA TRADING"
            )
            print("=" * 58)
            print()

            print(
                "LICENSE VALIDATION FAILED"
            )

            print()

            print(
                f"Reason    : {exc}"
            )

            print()

            print(
                "Machine ID:"
            )

            try:
                print(
                    machine_id()
                )
            except Exception:
                print(
                    "UNAVAILABLE"
                )

            print()

            print(
                "Please send this Machine ID "
                "to the administrator"
            )

            print(
                "to obtain or renew your license."
            )

            print()

            print("=" * 58)
            print()

            raise

    # ------------------------------------------------------------------
    # Clock rollback protection
    # ------------------------------------------------------------------

    def _check_clock_rollback(
        self,
    ) -> None:

        if not STATE_FILE.exists():

            return

        try:

            old = json.loads(
                STATE_FILE.read_text(
                    encoding="utf-8"
                )
            )

            last = date.fromisoformat(
                old[
                    "last_validation_date"
                ]
            )

            if date.today() < last:

                raise ValueError(
                    "System date appears to "
                    "have moved backwards"
                )

        except ValueError:

            raise

        except Exception:

            # Corrupt state should not permanently block
            # the application.
            return

    # ------------------------------------------------------------------
    # Save successful validation date
    # ------------------------------------------------------------------

    def _save_state(
        self,
        today: date,
    ) -> None:

        STATE_FILE.parent.mkdir(
            parents=True,
            exist_ok=True,
        )

        tmp = (
            STATE_FILE
            .with_suffix(".tmp")
        )

        tmp.write_text(
            json.dumps(
                {
                    "last_validation_date":
                        today.isoformat()
                },
                indent=2,
            ),
            encoding="utf-8",
        )

        tmp.replace(
            STATE_FILE
        )