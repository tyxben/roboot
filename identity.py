"""Daemon long-term identity for authenticated relay handshake.

One ed25519 keypair per Roboot install, persisted at
`.identity/daemon.ed25519.key` (raw 32-byte private scalar). First run
generates + writes; every subsequent start loads the same key. Losing
the file invalidates all pairing URLs previously distributed, because
their URL fragment carries a fingerprint of the *old* public key.

Fingerprint format: lowercase base32 of SHA-256(pub_raw_32B), first 26
characters (≈130 bits). Short enough to embed in a QR URL fragment,
long enough that a preimage collision is out of reach. The browser
compares the fingerprint it read from the URL fragment against the
one it computes from the daemon's id_pubkey delivered during the
e2ee_handshake reply.
"""

from __future__ import annotations

import base64
import hashlib
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

IDENTITY_DIR = Path(__file__).parent / ".identity"
KEY_PATH = IDENTITY_DIR / "daemon.ed25519.key"


def _fingerprint(pub_bytes: bytes) -> str:
    """26-char lowercase-base32 SHA-256 truncation of the raw 32B pubkey."""
    digest = hashlib.sha256(pub_bytes).digest()
    # base32 of 16B = 26 chars with no padding (we strip '=' anyway).
    return base64.b32encode(digest[:16]).decode("ascii").rstrip("=").lower()


def load_or_create() -> tuple[Ed25519PrivateKey, bytes, str]:
    """Return (private_key, public_bytes, fingerprint).

    Creates `.identity/daemon.ed25519.key` on first call with 0600 perms.
    Later calls read the same key, so the daemon identity survives
    restarts (and so old pairing URLs stay valid).
    """
    IDENTITY_DIR.mkdir(parents=True, exist_ok=True)

    if KEY_PATH.exists():
        priv = Ed25519PrivateKey.from_private_bytes(KEY_PATH.read_bytes())
    else:
        priv = Ed25519PrivateKey.generate()
        raw = priv.private_bytes(
            encoding=serialization.Encoding.Raw,
            format=serialization.PrivateFormat.Raw,
            encryption_algorithm=serialization.NoEncryption(),
        )
        KEY_PATH.write_bytes(raw)
        KEY_PATH.chmod(0o600)

    pub_bytes = priv.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    return priv, pub_bytes, _fingerprint(pub_bytes)
