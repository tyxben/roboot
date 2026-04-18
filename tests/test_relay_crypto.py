"""Tests for relay_client's E2EE crypto path.

Only the pure crypto functions are exercised — no WebSocket, no Arcana
runtime, no event loop beyond what pytest-asyncio provides. We simulate
the browser side by running ECDH + HKDF with the same parameters the
WebCrypto implementation uses (see pair-page.ts).
"""

from __future__ import annotations

import base64
import json
import os

import pytest
from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

import relay_client
from relay_client import (
    RelayClient,
    _b64d,
    _b64e,
    _derive_session_key,
    _pubkey_bytes,
)


# ---------------------------------------------------------------------------
# b64 helpers
# ---------------------------------------------------------------------------


def test_b64_round_trip():
    data = os.urandom(64)
    assert _b64d(_b64e(data)) == data


def test_b64_encodes_to_ascii_str():
    out = _b64e(b"hello")
    assert isinstance(out, str)
    assert out == base64.b64encode(b"hello").decode("ascii")


# ---------------------------------------------------------------------------
# ECDH + HKDF
# ---------------------------------------------------------------------------


def test_pubkey_bytes_is_65_byte_uncompressed_point():
    key = ec.generate_private_key(ec.SECP256R1())
    raw = _pubkey_bytes(key)
    # SEC1 uncompressed P-256 point: 1 header byte (0x04) + 32 X + 32 Y
    assert len(raw) == 65
    assert raw[0] == 0x04


def test_both_sides_derive_same_session_key():
    """Simulates browser + daemon: both sides run HKDF on ECDH(shared),
    using the same `info` string. They must land on identical 32-byte keys.
    """
    client_priv = ec.generate_private_key(ec.SECP256R1())
    daemon_priv = ec.generate_private_key(ec.SECP256R1())

    client_pub_bytes = _pubkey_bytes(client_priv)
    daemon_pub_bytes = _pubkey_bytes(daemon_priv)

    daemon_key = _derive_session_key(daemon_priv, client_pub_bytes)
    client_key = _derive_session_key(client_priv, daemon_pub_bytes)

    assert daemon_key == client_key
    assert len(daemon_key) == 32


def test_different_handshakes_yield_different_keys():
    a_priv = ec.generate_private_key(ec.SECP256R1())
    b_priv = ec.generate_private_key(ec.SECP256R1())
    c_priv = ec.generate_private_key(ec.SECP256R1())

    k1 = _derive_session_key(a_priv, _pubkey_bytes(b_priv))
    k2 = _derive_session_key(a_priv, _pubkey_bytes(c_priv))
    assert k1 != k2


# ---------------------------------------------------------------------------
# _encrypt / _decrypt on a RelayClient instance
# ---------------------------------------------------------------------------


def _make_client() -> RelayClient:
    """Construct a RelayClient with inert stubs — no WS, no runtime."""

    class StubRuntime:
        def create_chat_session(self, system_prompt):
            raise AssertionError("not needed for crypto tests")

    return RelayClient(
        relay_url="wss://relay.example.com",
        runtime=StubRuntime(),
        build_personality=lambda: "you are a test",
        get_name=lambda: "roboot-test",
    )


def test_encrypt_envelope_shape():
    client = _make_client()
    client._ciphers["cid"] = AESGCM(os.urandom(32))

    env = client._encrypt("cid", {"type": "chat", "content": "hi"})

    assert env["type"] == "encrypted"
    assert env["client_id"] == "cid"
    # iv: 12 bytes -> base64 of 12 bytes is 16 chars
    iv_raw = _b64d(env["iv"])
    assert len(iv_raw) == 12
    # ct is non-empty base64
    ct_raw = _b64d(env["ct"])
    assert len(ct_raw) > 0


def test_encrypt_decrypt_round_trip():
    client = _make_client()
    client._ciphers["cid"] = AESGCM(os.urandom(32))

    payload = {
        "type": "chat",
        "content": "hello 你好",
        "nested": {"a": [1, 2, 3]},
    }
    env = client._encrypt("cid", payload)
    plaintext = client._decrypt("cid", env)

    assert json.loads(plaintext) == payload


def test_fresh_iv_per_encrypt():
    """Two encrypts of the same payload must produce different ciphertexts
    because a fresh random IV is drawn each time."""
    client = _make_client()
    client._ciphers["cid"] = AESGCM(os.urandom(32))

    env1 = client._encrypt("cid", {"x": 1})
    env2 = client._encrypt("cid", {"x": 1})
    assert env1["iv"] != env2["iv"]
    assert env1["ct"] != env2["ct"]


def test_tampered_ciphertext_raises_invalid_tag():
    client = _make_client()
    client._ciphers["cid"] = AESGCM(os.urandom(32))

    env = client._encrypt("cid", {"hello": "world"})
    ct_bytes = bytearray(_b64d(env["ct"]))
    ct_bytes[0] ^= 0x01  # flip a bit
    env["ct"] = _b64e(bytes(ct_bytes))

    with pytest.raises(InvalidTag):
        client._decrypt("cid", env)


def test_tampered_iv_raises_invalid_tag():
    client = _make_client()
    client._ciphers["cid"] = AESGCM(os.urandom(32))

    env = client._encrypt("cid", {"hello": "world"})
    iv_bytes = bytearray(_b64d(env["iv"]))
    iv_bytes[0] ^= 0x01
    env["iv"] = _b64e(bytes(iv_bytes))

    with pytest.raises(InvalidTag):
        client._decrypt("cid", env)


def test_wrong_client_cipher_fails():
    """Encrypted with one key, decrypting under a different client_id's key
    must fail (authentication tag mismatch)."""
    client = _make_client()
    client._ciphers["a"] = AESGCM(os.urandom(32))
    client._ciphers["b"] = AESGCM(os.urandom(32))

    env = client._encrypt("a", {"msg": "secret"})
    # Pretend the envelope was for b by swapping which cipher we decrypt with.
    env_for_b = dict(env)
    env_for_b["client_id"] = "b"
    with pytest.raises(InvalidTag):
        client._decrypt("b", env_for_b)


def test_decrypt_missing_cipher_raises_keyerror():
    """If the daemon has no cipher for a client_id, _decrypt raises. The
    real dispatch path in _handle_raw checks `client_id in self._ciphers`
    first and replies with a handshake_required error, so this raw
    KeyError is the expected low-level failure mode.
    """
    client = _make_client()
    env = {"type": "encrypted", "client_id": "unknown", "iv": _b64e(b"0" * 12), "ct": _b64e(b"0" * 16)}
    with pytest.raises(KeyError):
        client._decrypt("unknown", env)


# ---------------------------------------------------------------------------
# End-to-end browser <-> daemon simulation
# ---------------------------------------------------------------------------


def test_full_handshake_and_bidirectional_round_trip():
    """Simulates the complete on-the-wire flow:
      1. Browser generates ephemeral keypair, sends pubkey.
      2. Daemon (RelayClient) derives shared key, stores AESGCM under client_id.
      3. Browser derives the same key independently.
      4. Daemon->browser: daemon encrypts, browser decrypts.
      5. Browser->daemon: browser encrypts, daemon decrypts.
    """
    client = _make_client()

    browser_priv = ec.generate_private_key(ec.SECP256R1())
    browser_pub_bytes = _pubkey_bytes(browser_priv)

    # Daemon side of the handshake (replicating _on_handshake logic).
    daemon_priv = ec.generate_private_key(ec.SECP256R1())
    daemon_key = _derive_session_key(daemon_priv, browser_pub_bytes)
    client._ciphers["cid"] = AESGCM(daemon_key)

    # Browser independently derives the same key.
    daemon_pub_bytes = _pubkey_bytes(daemon_priv)
    browser_key = _derive_session_key(browser_priv, daemon_pub_bytes)
    assert daemon_key == browser_key
    browser_cipher = AESGCM(browser_key)

    # Daemon -> browser
    env = client._encrypt("cid", {"type": "delta", "text": "hi"})
    iv = _b64d(env["iv"])
    ct = _b64d(env["ct"])
    plaintext = browser_cipher.decrypt(iv, ct, None)
    assert json.loads(plaintext.decode("utf-8")) == {"type": "delta", "text": "hi"}

    # Browser -> daemon
    iv2 = os.urandom(12)
    payload = json.dumps({"type": "chat", "content": "ping"}).encode("utf-8")
    ct2 = browser_cipher.encrypt(iv2, payload, None)
    envelope = {
        "type": "encrypted",
        "client_id": "cid",
        "iv": _b64e(iv2),
        "ct": _b64e(ct2),
    }
    recovered = client._decrypt("cid", envelope)
    assert json.loads(recovered) == {"type": "chat", "content": "ping"}


def test_hkdf_info_string_is_stable():
    """The info string is part of the wire protocol (pair-page.ts uses the
    same bytes). If this test breaks, the browser can no longer derive a
    matching key — bump the version suffix on both sides in lockstep."""
    import inspect

    src = inspect.getsource(relay_client._derive_session_key)
    assert b"roboot-relay-e2ee-v1".decode() in src
