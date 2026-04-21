"""Tests for tools.generate_cert.

We exercise the programmatic ``generate_cert()`` entry (the CLI is a thin
wrapper) and the module-level CLI via ``_main([...])`` with an isolated
output directory.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from cryptography import x509
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec

from tools import generate_cert as gc


def _load_cert(path: Path) -> x509.Certificate:
    return x509.load_pem_x509_certificate(path.read_bytes())


def _load_key(path: Path):
    return serialization.load_pem_private_key(path.read_bytes(), password=None)


def test_generate_cert_writes_valid_pem_files(tmp_path):
    cert_path, key_path, fp_hex = gc.generate_cert(
        tmp_path, hostname="testhost", extra_ips=["192.168.1.50"]
    )

    assert cert_path.exists()
    assert key_path.exists()
    assert cert_path == tmp_path / "cert.pem"
    assert key_path == tmp_path / "key.pem"

    # Parseable PEM cert.
    cert = _load_cert(cert_path)
    # CN matches.
    cns = [
        attr.value
        for attr in cert.subject
        if attr.oid.dotted_string == "2.5.4.3"  # CN
    ]
    assert cns == ["testhost"]

    # SANs contain localhost, 127.0.0.1, and the extra IP.
    san = cert.extensions.get_extension_for_class(
        x509.SubjectAlternativeName
    ).value
    dns_names = san.get_values_for_type(x509.DNSName)
    ip_values = [str(ip) for ip in san.get_values_for_type(x509.IPAddress)]
    assert "localhost" in dns_names
    assert "127.0.0.1" in ip_values
    assert "192.168.1.50" in ip_values

    # Private key is ECDSA P-256.
    key = _load_key(key_path)
    assert isinstance(key, ec.EllipticCurvePrivateKey)
    assert key.curve.name == "secp256r1"

    # Fingerprint string is well-formed (32 bytes * "XX:" - 1 = 95 chars).
    assert len(fp_hex) == 95
    assert fp_hex.count(":") == 31
    assert all(c in "0123456789ABCDEF:" for c in fp_hex)


def test_generate_cert_refuses_existing_without_force(tmp_path):
    # First call creates the files.
    gc.generate_cert(tmp_path, extra_ips=[])

    # Second call without --force must refuse.
    with pytest.raises(FileExistsError):
        gc.generate_cert(tmp_path, extra_ips=[])


def test_generate_cert_force_overwrites(tmp_path):
    cert1, key1, fp1 = gc.generate_cert(tmp_path, extra_ips=[])
    cert1_bytes = cert1.read_bytes()

    # force=True overwrites; a fresh keypair means a different fingerprint.
    cert2, key2, fp2 = gc.generate_cert(tmp_path, force=True, extra_ips=[])
    assert cert2 == cert1
    assert key2 == key1
    assert cert2.read_bytes() != cert1_bytes
    assert fp2 != fp1


def test_main_cli_writes_files(tmp_path, capsys):
    rc = gc._main(["--out-dir", str(tmp_path), "--hostname", "clihost"])
    assert rc == 0
    captured = capsys.readouterr().out
    assert "cert.pem" in captured
    assert "key.pem" in captured
    assert "SHA-256 fingerprint" in captured

    assert (tmp_path / "cert.pem").exists()
    assert (tmp_path / "key.pem").exists()

    cert = _load_cert(tmp_path / "cert.pem")
    cns = [
        attr.value
        for attr in cert.subject
        if attr.oid.dotted_string == "2.5.4.3"
    ]
    assert cns == ["clihost"]


def test_main_cli_refuses_without_force(tmp_path, capsys):
    rc1 = gc._main(["--out-dir", str(tmp_path), "--hostname", "a"])
    assert rc1 == 0
    capsys.readouterr()  # drain

    rc2 = gc._main(["--out-dir", str(tmp_path), "--hostname", "b"])
    assert rc2 == 1
    err = capsys.readouterr().err
    assert "refusing to overwrite" in err


def test_main_cli_force_overwrites(tmp_path):
    assert gc._main(["--out-dir", str(tmp_path), "--hostname", "a"]) == 0
    cert_bytes_before = (tmp_path / "cert.pem").read_bytes()

    assert (
        gc._main(
            ["--out-dir", str(tmp_path), "--hostname", "b", "--force"]
        )
        == 0
    )
    cert_bytes_after = (tmp_path / "cert.pem").read_bytes()
    assert cert_bytes_after != cert_bytes_before

    cert = _load_cert(tmp_path / "cert.pem")
    cns = [
        attr.value
        for attr in cert.subject
        if attr.oid.dotted_string == "2.5.4.3"
    ]
    assert cns == ["b"]
