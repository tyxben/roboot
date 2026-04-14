"""Network utilities for local IP detection and QR code generation."""

from __future__ import annotations

import socket
from io import BytesIO
from typing import List

import qrcode


def get_local_ip_addresses() -> List[str]:
    """Get all local network IP addresses, excluding loopback and docker interfaces.

    Returns:
        List of IP addresses (e.g., ['192.168.1.100'])
    """
    addresses = []

    # Method 1: Connect to external IP to find default route
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.settimeout(0.1)
        # Connect to Google DNS (doesn't actually send data)
        s.connect(("8.8.8.8", 80))
        local_ip = s.getsockname()[0]
        s.close()
        if local_ip and local_ip not in ("127.0.0.1", "0.0.0.0"):
            addresses.append(local_ip)
    except Exception:
        pass

    # Method 2: Get hostname and resolve all addresses
    try:
        hostname = socket.gethostname()
        host_ips = socket.gethostbyname_ex(hostname)[2]
        for ip in host_ips:
            if ip.startswith("127.") or ip.startswith("0."):
                continue
            # Skip docker/veth interfaces (typically 172.17.x.x)
            if ip.startswith("172.17."):
                continue
            if ip not in addresses:
                addresses.append(ip)
    except Exception:
        pass

    return addresses


def get_primary_ip() -> str | None:
    """Get the primary local network IP address.

    Returns:
        Primary IP address or None if not found
    """
    addresses = get_local_ip_addresses()

    # Prefer 192.168.x.x (most common home networks)
    for addr in addresses:
        if addr.startswith("192.168."):
            return addr

    # Then 10.x.x.x (corporate networks)
    for addr in addresses:
        if addr.startswith("10."):
            return addr

    # Then any other
    return addresses[0] if addresses else None


def generate_qr_code(data: str, size: int = 10) -> bytes:
    """Generate QR code as PNG bytes.

    Args:
        data: The data to encode (e.g., URL)
        size: Box size for QR code (default 10)

    Returns:
        PNG image bytes
    """
    qr = qrcode.QRCode(
        version=1,
        error_correction=qrcode.constants.ERROR_CORRECT_L,
        box_size=size,
        border=4,
    )
    qr.add_data(data)
    qr.make(fit=True)

    img = qr.make_image(fill_color="black", back_color="white")

    # Convert to bytes
    buffer = BytesIO()
    img.save(buffer, format="PNG")
    return buffer.getvalue()


def generate_qr_ascii(data: str) -> str:
    """Generate QR code as ASCII art for terminal display.

    Args:
        data: The data to encode (e.g., URL)

    Returns:
        ASCII art string
    """
    qr = qrcode.QRCode(
        version=1,
        error_correction=qrcode.constants.ERROR_CORRECT_L,
        box_size=1,
        border=2,
    )
    qr.add_data(data)
    qr.make(fit=True)

    # Generate ASCII using block characters
    matrix = qr.get_matrix()
    ascii_art = []

    # Use half-block characters for better density
    for row in matrix:
        line = ""
        for cell in row:
            line += "██" if cell else "  "
        ascii_art.append(line)

    return "\n".join(ascii_art)


if __name__ == "__main__":
    # Test IP detection
    print("Local IP addresses:")
    for ip in get_local_ip_addresses():
        print(f"  - {ip}")

    print(f"\nPrimary IP: {get_primary_ip()}")

    # Test QR code generation
    test_url = "http://192.168.1.100:8765"
    print(f"\nQR Code for {test_url}:")
    print(generate_qr_ascii(test_url))
