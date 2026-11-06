"""Ed25519 signatures for the evidence trail.

The hash chain already shows when a record was edited, reordered or removed from the middle. A signature adds
who wrote it: each record's hash is signed with ScopePay's private key, so a merchant, PayPal or an arbiter can
check with the public key alone that ScopePay produced the record, and nobody holding a copy of the trail can
rebuild a "valid" chain after editing it.

Key: EVIDENCE_SIGNING_KEY (base64 of the 32-byte raw private key) if set, otherwise a PEM file created on first
use next to the database (git-ignored). Without the `cryptography` package records are written unsigned and
verification reports them as unsigned rather than failing.
Pattern credit: luongs3/agent-spend-gate (MIT), which signs hash-chained gate receipts with Ed25519.
"""
import base64
import hashlib
import os
from pathlib import Path

try:
    from cryptography.exceptions import InvalidSignature
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey
    AVAILABLE = True
except ImportError:  # pragma: no cover - exercised only where cryptography is missing
    AVAILABLE = False

ALG = "Ed25519"
_CACHE = {}


def _raw_public(pub):
    return pub.public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)


def key_id_of(public_raw):
    return hashlib.sha256(public_raw).hexdigest()[:16]


class Signer:
    def __init__(self, key_file):
        self.key_file = Path(key_file)
        self._key = None

    def _load(self):
        if self._key is not None:
            return self._key
        env = os.getenv("EVIDENCE_SIGNING_KEY", "").strip()
        if env:
            self._key = Ed25519PrivateKey.from_private_bytes(base64.b64decode(env))
        elif self.key_file.exists():
            self._key = serialization.load_pem_private_key(self.key_file.read_bytes(), password=None)
        else:
            self._key = Ed25519PrivateKey.generate()
            pem = self._key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                          serialization.NoEncryption())
            self.key_file.write_bytes(pem)
            try:
                os.chmod(self.key_file, 0o600)
            except OSError:
                pass
        return self._key

    def public(self):
        raw = _raw_public(self._load().public_key())
        return {"alg": ALG, "public_key_b64": base64.b64encode(raw).decode(), "key_id": key_id_of(raw)}

    def sign(self, record_hash):
        """Sign the record's hex hash. Returns the fields to add to the record."""
        key = self._load()
        raw = _raw_public(key.public_key())
        return {"sig": base64.b64encode(key.sign(record_hash.encode())).decode(), "sig_alg": ALG, "key_id": key_id_of(raw)}


def signer_for(key_file):
    """One Signer per key file (tests use a fresh database, and so a fresh key, per test)."""
    if not AVAILABLE:
        return None
    key_file = str(Path(key_file).resolve())
    if key_file not in _CACHE:
        _CACHE[key_file] = Signer(key_file)
    return _CACHE[key_file]


def verify(record_hash, sig_b64, public_key_b64):
    if not AVAILABLE:
        return False
    try:
        Ed25519PublicKey.from_public_bytes(base64.b64decode(public_key_b64)).verify(base64.b64decode(sig_b64), record_hash.encode())
        return True
    except (InvalidSignature, ValueError, TypeError):
        return False
