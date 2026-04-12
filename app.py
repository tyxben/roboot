#!/usr/bin/env python3
"""Roboot Desktop — native macOS popup window.

Usage:
    python app.py
"""

from __future__ import annotations

import threading
import time

import uvicorn


def start_server():
    """Start FastAPI server in background thread."""
    from server import app

    config = uvicorn.Config(app, host="127.0.0.1", port=8765, log_level="warning")
    server = uvicorn.Server(config)
    server.run()


def main():
    try:
        import webview
    except ImportError:
        print("需要安装 pywebview:")
        print("  pip install pywebview")
        print("")
        print("或者直接用浏览器模式:")
        print("  python server.py")
        return

    # Start server in background
    thread = threading.Thread(target=start_server, daemon=True)
    thread.start()
    time.sleep(1)  # Wait for server to start

    # Create native window
    webview.create_window(
        "Roboot",
        "http://127.0.0.1:8765",
        width=420,
        height=640,
        resizable=True,
        on_top=False,
        confirm_close=False,
    )
    webview.start()


if __name__ == "__main__":
    main()
