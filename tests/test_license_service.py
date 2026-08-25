import base64, json
from datetime import date, timedelta
from pathlib import Path
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives import serialization

from backend.app.licensing import license_service as ls


def make_license(path, private, machine="*", expires=None, features=None):
    expires = expires or (date.today() + timedelta(days=10)).isoformat()
    obj = {
        "product":"Chandra Trading", "license_id":"TEST-1", "customer":"Test",
        "issued_at":date.today().isoformat(), "expires_at":expires,
        "machine_id":machine, "features":features or ["TIMING_CANDLE"], "max_lot":0.01,
        "signature":""
    }
    payload=ls._canonical_payload(obj)
    obj["signature"]=base64.b64encode(private.sign(payload)).decode()
    path.write_text(json.dumps(obj),encoding="utf-8")
    return obj


def test_valid_signed_license(tmp_path, monkeypatch):
    private=Ed25519PrivateKey.generate(); public=private.public_key()
    pem=public.public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)
    pub=tmp_path/'public.pem'; pub.write_bytes(pem)
    lic=tmp_path/'license.key'; state=tmp_path/'state.json'
    monkeypatch.setattr(ls,'PUBLIC_KEY_FILE',pub); monkeypatch.setattr(ls,'EMBEDDED_PUBLIC_KEY_B64',''); monkeypatch.setattr(ls,'STATE_FILE',state)
    make_license(
        lic,
        private,
        machine=ls.machine_id(),
    )
    info=ls.LicenseService(lic).require('TIMING_CANDLE',0.01)
    assert info['valid'] is True


def test_tampered_expiry_rejected(tmp_path, monkeypatch):
    private=Ed25519PrivateKey.generate(); public=private.public_key()
    pem=public.public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)
    pub=tmp_path/'public.pem'; pub.write_bytes(pem)
    lic=tmp_path/'license.key'; state=tmp_path/'state.json'
    monkeypatch.setattr(ls,'PUBLIC_KEY_FILE',pub); monkeypatch.setattr(ls,'EMBEDDED_PUBLIC_KEY_B64',''); monkeypatch.setattr(ls,'STATE_FILE',state)
    make_license(lic,private)
    obj=json.loads(lic.read_text()); obj['expires_at']='2099-12-31'; lic.write_text(json.dumps(obj))
    assert ls.LicenseService(lic).status()['valid'] is False
