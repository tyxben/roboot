#!/usr/bin/env python3
"""Test script for mobile access implementation."""

import sys
from pathlib import Path


def test_imports():
    """Test that all required modules can be imported."""
    print("Testing imports...")
    try:
        import qrcode
        print("✓ qrcode library available")
    except ImportError:
        print("✗ qrcode not installed - run: pip install 'qrcode[pil]'")
        return False

    try:
        from PIL import Image
        print("✓ Pillow (PIL) available")
    except ImportError:
        print("✗ Pillow not installed - run: pip install 'qrcode[pil]'")
        return False

    try:
        import network_utils
        print("✓ network_utils module found")
    except ImportError:
        print("✗ network_utils.py not found")
        return False

    return True


def test_network_detection():
    """Test IP detection functionality."""
    print("\nTesting network detection...")
    try:
        from network_utils import get_local_ip_addresses, get_primary_ip

        all_ips = get_local_ip_addresses()
        primary_ip = get_primary_ip()

        if all_ips:
            print(f"✓ Found {len(all_ips)} network interface(s):")
            for ip in all_ips:
                marker = " (primary)" if ip == primary_ip else ""
                print(f"  - {ip}{marker}")
        else:
            print("⚠ No network interfaces detected (not connected to WiFi?)")

        if primary_ip:
            print(f"✓ Primary IP: {primary_ip}")
        else:
            print("⚠ No primary IP detected")

        return True
    except Exception as e:
        print(f"✗ Network detection failed: {e}")
        return False


def test_qr_generation():
    """Test QR code generation."""
    print("\nTesting QR code generation...")
    try:
        from network_utils import generate_qr_code, generate_qr_ascii

        test_url = "http://192.168.1.100:8765"

        # Test PNG generation
        qr_bytes = generate_qr_code(test_url)
        if len(qr_bytes) > 0:
            print(f"✓ PNG QR code generated ({len(qr_bytes)} bytes)")
        else:
            print("✗ PNG QR code generation failed")
            return False

        # Test ASCII generation
        qr_ascii = generate_qr_ascii(test_url)
        if qr_ascii and "██" in qr_ascii:
            print("✓ ASCII QR code generated")
            print("\nPreview (first 5 lines):")
            lines = qr_ascii.split("\n")
            for line in lines[:5]:
                print("  " + line)
            print("  ...")
        else:
            print("✗ ASCII QR code generation failed")
            return False

        return True
    except Exception as e:
        print(f"✗ QR generation failed: {e}")
        return False


def test_files_exist():
    """Test that all required files exist."""
    print("\nChecking files...")
    required_files = [
        "server.py",
        "network_utils.py",
        "static/console.html",
        "static/manifest.json",
        "static/sw.js",
        "static/generate_icons.py",
        "docs/mobile-access.md",
        "MOBILE_QUICKSTART.md",
    ]

    all_exist = True
    for file_path in required_files:
        path = Path(file_path)
        if path.exists():
            print(f"✓ {file_path}")
        else:
            print(f"✗ {file_path} missing")
            all_exist = False

    return all_exist


def test_server_config():
    """Test server.py configuration."""
    print("\nChecking server.py configuration...")
    try:
        with open("server.py") as f:
            content = f.read()

        checks = [
            ("network_utils import", "from network_utils import" in content),
            ("/api/network-info endpoint", "/api/network-info" in content),
            ("/api/qr-code endpoint", "/api/qr-code" in content),
            ("Bind to 0.0.0.0", 'host="0.0.0.0"' in content),
            ("QR code display", "generate_qr_ascii" in content),
        ]

        all_passed = True
        for check_name, passed in checks:
            if passed:
                print(f"✓ {check_name}")
            else:
                print(f"✗ {check_name}")
                all_passed = False

        return all_passed
    except Exception as e:
        print(f"✗ Failed to check server.py: {e}")
        return False


def test_console_html():
    """Test console.html modifications."""
    print("\nChecking console.html...")
    try:
        with open("static/console.html") as f:
            content = f.read()

        checks = [
            ("PWA manifest link", 'rel="manifest"' in content),
            ("Mobile viewport", 'name="viewport"' in content),
            ("Mobile header", 'class="mobile-header"' in content),
            ("Network panel", 'id="panel-network"' in content),
            ("Service worker registration", "serviceWorker.register" in content),
            ("Responsive CSS", "@media (max-width: 768px)" in content),
            ("Sidebar toggle", "toggleSidebar" in content),
        ]

        all_passed = True
        for check_name, passed in checks:
            if passed:
                print(f"✓ {check_name}")
            else:
                print(f"✗ {check_name}")
                all_passed = False

        return all_passed
    except Exception as e:
        print(f"✗ Failed to check console.html: {e}")
        return False


def test_icon_generation():
    """Test icon generator script."""
    print("\nTesting icon generation...")
    try:
        import subprocess

        result = subprocess.run(
            [sys.executable, "static/generate_icons.py"],
            capture_output=True,
            text=True,
            timeout=10,
        )

        if result.returncode == 0:
            print("✓ Icon generation script runs successfully")

            # Check if icons were created
            icon_192 = Path("static/icon-192.png")
            icon_512 = Path("static/icon-512.png")

            if icon_192.exists():
                print(f"✓ icon-192.png created ({icon_192.stat().st_size} bytes)")
            else:
                print("✗ icon-192.png not created")
                return False

            if icon_512.exists():
                print(f"✓ icon-512.png created ({icon_512.stat().st_size} bytes)")
            else:
                print("✗ icon-512.png not created")
                return False

            return True
        else:
            print(f"✗ Icon generation failed: {result.stderr}")
            return False

    except Exception as e:
        print(f"✗ Icon generation test failed: {e}")
        return False


def main():
    """Run all tests."""
    print("=" * 60)
    print("Mobile Access Implementation - Test Suite")
    print("=" * 60)

    tests = [
        ("Import Test", test_imports),
        ("Network Detection", test_network_detection),
        ("QR Code Generation", test_qr_generation),
        ("File Existence", test_files_exist),
        ("Server Configuration", test_server_config),
        ("Console HTML", test_console_html),
        ("Icon Generation", test_icon_generation),
    ]

    results = []
    for test_name, test_func in tests:
        try:
            passed = test_func()
            results.append((test_name, passed))
        except Exception as e:
            print(f"\n✗ {test_name} crashed: {e}")
            results.append((test_name, False))

    # Summary
    print("\n" + "=" * 60)
    print("Test Summary")
    print("=" * 60)

    passed_count = sum(1 for _, passed in results if passed)
    total_count = len(results)

    for test_name, passed in results:
        status = "✓ PASS" if passed else "✗ FAIL"
        print(f"{status}: {test_name}")

    print(f"\nTotal: {passed_count}/{total_count} tests passed")

    if passed_count == total_count:
        print("\n🎉 All tests passed! Mobile access implementation is ready.")
        return 0
    else:
        print(f"\n⚠️  {total_count - passed_count} test(s) failed. Please fix the issues above.")
        return 1


if __name__ == "__main__":
    sys.exit(main())
