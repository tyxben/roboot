"""Self-signed TLS certificate generator.

Usage::

    python -m tools.generate_cert          # refuses to overwrite certs/
    python -m tools.generate_cert --force  # overwrite existing files
    python -m tools.generate_cert --out-dir /tmp/certs

Writes ``cert.pem`` + ``key.pem`` into the output directory (default:
``<repo>/certs``). The key is ECDSA P-256, the cert is valid for 365 days,
CN is ``socket.gethostname()``, and SANs include every non-loopback local
IPv4 address (via ``network_utils.get_local_ip_addresses``) plus
``localhost`` and ``127.0.0.1``.

Prints the cert path and its SHA-256 fingerprint on success so the user
can verify it on their phone before trusting the self-signed cert.

All crypto uses ``cryptography`` (already a project dep). No OpenSSL
shell-out. Pure stdlib + ``cryptography``.
"""

from __future__ import annotations

import argparse
import ipaddress
import socket
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable, List, Optional

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_OUT_DIR = REPO_ROOT / "certs"
VALIDITY_DAYS = 365


def _san_entries(extra_ips: Iterable[str]) -> List[x509.GeneralName]:
    """Build the SAN list: localhost + 127.0.0.1 + every local IPv4."""
    seen_ips: set = set()
    sans: List[x509.GeneralName] = [
        x509.DNSName("localhost"),
        x509.IPAddress(ipaddress.ip_address("127.0.0.1")),
    ]
    seen_ips.add("127.0.0.1")

    for ip_str in extra_ips:
        if ip_str in seen_ips:
            continue
        try:
            ip_obj = ipaddress.ip_address(ip_str)
        except ValueError:
            continue
        sans.append(x509.IPAddress(ip_obj))
        seen_ips.add(ip_str)

    return sans


def _collect_local_ips() -> List[str]:
    """Call into network_utils if available; fall back to empty list."""
    try:
        from network_utils import get_local_ip_addresses

        return list(get_local_ip_addresses())
    except Exception:
        return []


def generate_cert(
    out_dir: Path,
    *,
    force: bool = False,
    hostname: Optional[str] = None,
    extra_ips: Optional[Iterable[str]] = None,
    validity_days: int = VALIDITY_DAYS,
) -> tuple[Path, Path, str]:
    """Generate a self-signed cert into ``out_dir``.

    Returns ``(cert_path, key_path, sha256_fingerprint_hex)``.

    Raises ``FileExistsError`` when ``force`` is false and either file
    already exists.
    """
    out_dir = Path(out_dir)
    cert_path = out_dir / "cert.pem"
    key_path = out_dir / "key.pem"

    if not force and (cert_path.exists() or key_path.exists()):
        raise FileExistsError(
            f"refusing to overwrite existing cert files in {out_dir} "
            "(pass --force to overwrite)"
        )

    out_dir.mkdir(parents=True, exist_ok=True)

    cn = hostname or socket.gethostname() or "localhost"
    ips = list(extra_ips) if extra_ips is not None else _collect_local_ips()

    # ECDSA P-256 — modern, small, fast, widely supported.
    private_key = ec.generate_private_key(ec.SECP256R1())

    subject = issuer = x509.Name(
        [x509.NameAttribute(NameOID.COMMON_NAME, cn)]
    )

    now = datetime.now(timezone.utc)
    builder = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(issuer)
        .public_key(private_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=5))
        .not_valid_after(now + timedelta(days=validity_days))
        .add_extension(
            x509.SubjectAlternativeName(_san_entries(ips)),
            critical=False,
        )
        .add_extension(
            x509.BasicConstraints(ca=False, path_length=None),
            critical=True,
        )
    )

    cert = builder.sign(private_key=private_key, algorithm=hashes.SHA256())

    cert_pem = cert.public_bytes(serialization.Encoding.PEM)
    key_pem = private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )

    cert_path.write_bytes(cert_pem)
    key_path.write_bytes(key_pem)

    # 0600 on the private key so random processes can't read it.
    try:
        key_path.chmod(0o600)
    except OSError:
        pass

    fp_bytes = cert.fingerprint(hashes.SHA256())
    fp_hex = ":".join(f"{b:02X}" for b in fp_bytes)

    return cert_path, key_path, fp_hex


def _main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m tools.generate_cert",
        description=(
            "Generate a self-signed TLS cert (ECDSA P-256, 365d) for the "
            "Roboot web console."
        ),
    )
    parser.add_argument(
        "--out-dir",
        type=Path,
        default=DEFAULT_OUT_DIR,
        help=f"Output directory (default: {DEFAULT_OUT_DIR})",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Overwrite existing cert.pem / key.pem",
    )
    parser.add_argument(
        "--hostname",
        default=None,
        help="Common Name to embed (default: socket.gethostname())",
    )
    args = parser.parse_args(argv)

    try:
        cert_path, key_path, fp_hex = generate_cert(
            args.out_dir, force=args.force, hostname=args.hostname
        )
    except FileExistsError as e:
        print(f"[generate_cert] {e}", file=sys.stderr)
        return 1

    print(f"[generate_cert] wrote {cert_path}")
    print(f"[generate_cert] wrote {key_path}")
    print(f"[generate_cert] SHA-256 fingerprint: {fp_hex}")
    print(
        "[generate_cert] Verify this fingerprint on your phone before "
        "trusting the certificate."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
