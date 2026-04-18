"""Tests for identity.py — daemon long-term ed25519 key persistence."""

from __future__ import annotations

import base64
import hashlib

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

import identity


# conftest.py's autouse fixture already redirects identity paths into a
# pytest-managed tmp dir, so these tests operate on an isolated key each
# time and never touch the user's real .identity/.


def test_first_call_creates_key():
    priv, pub, fp = identity.load_or_create()
    assert isinstance(priv, Ed25519PrivateKey)
    assert len(pub) == 32
    assert len(fp) == 26
    assert fp.islower()
    # Key file should exist with 0600 perms
    assert identity.KEY_PATH.exists()
    assert identity.KEY_PATH.stat().st_mode & 0o777 == 0o600


def test_subsequent_calls_reuse_key():
    priv1, pub1, fp1 = identity.load_or_create()
    priv2, pub2, fp2 = identity.load_or_create()
    # Same pub + fingerprint → same private key under the hood
    assert pub1 == pub2
    assert fp1 == fp2


def test_fingerprint_is_base32_of_sha256():
    _, pub, fp = identity.load_or_create()
    expected = base64.b32encode(hashlib.sha256(pub).digest()[:16]).decode("ascii").rstrip("=").lower()
    assert fp == expected


def test_fingerprint_unique_per_key():
    # Two independent keys → different fingerprints (probabilistic, but 128-bit
    # collision space makes this effectively certain).
    k1 = Ed25519PrivateKey.generate()
    k2 = Ed25519PrivateKey.generate()
    p1 = k1.public_key().public_bytes(encoding=serialization.Encoding.Raw, format=serialization.PublicFormat.Raw)
    p2 = k2.public_key().public_bytes(encoding=serialization.Encoding.Raw, format=serialization.PublicFormat.Raw)
    assert identity._fingerprint(p1) != identity._fingerprint(p2)


def test_signature_verifiable_with_returned_pubkey():
    """The private key we return must produce signatures verifiable with the
    public bytes we return — sanity check that we're not handing back a
    mismatched pair."""
    priv, pub_bytes, _ = identity.load_or_create()
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

    msg = b"hello roboot"
    sig = priv.sign(msg)
    pub_key = Ed25519PublicKey.from_public_bytes(pub_bytes)
    # verify() raises on mismatch — so no assertion needed beyond "doesn't throw"
    pub_key.verify(sig, msg)
