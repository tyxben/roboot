"""Camera and screenshot tools."""

from __future__ import annotations

import asyncio
import base64
import subprocess
import tempfile
from pathlib import Path

import arcana


def _capture_camera() -> bytes | None:
    """Capture a frame from the Mac camera. Returns JPEG bytes or None."""
    try:
        import cv2

        cap = cv2.VideoCapture(0)
        if not cap.isOpened():
            return None
        ret, frame = cap.read()
        cap.release()
        if not ret:
            return None
        _, buf = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 85])
        return buf.tobytes()
    except ImportError:
        return None


def _capture_screenshot() -> bytes | None:
    """Take a screenshot on macOS. Returns PNG bytes or None."""
    try:
        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as f:
            path = f.name
        subprocess.run(
            ["screencapture", "-x", "-C", path],
            timeout=5,
            check=True,
        )
        data = Path(path).read_bytes()
        Path(path).unlink(missing_ok=True)
        return data
    except Exception:
        return None


@arcana.tool(
    when_to_use="当用户说'看看'、'你看到什么'、'看一下'，或需要了解用户面前的实物时",
    what_to_expect="返回摄像头拍到的图片，你可以描述看到的内容",
    failure_meaning="摄像头不可用，建议用 screenshot 代替",
)
async def look() -> str | list:
    """用摄像头拍照并返回图片。"""
    image_bytes = await asyncio.to_thread(_capture_camera)
    if image_bytes is None:
        return "摄像头不可用。可能需要安装 opencv-python: pip install opencv-python"

    b64 = base64.b64encode(image_bytes).decode()
    return [
        {
            "type": "image_url",
            "image_url": {"url": f"data:image/jpeg;base64,{b64}"},
        },
        {"type": "text", "text": "这是摄像头刚拍到的画面。"},
    ]


@arcana.tool(
    when_to_use="当用户说'看看屏幕'、'截个屏'，或需要了解桌面/应用状态时",
    what_to_expect="返回当前屏幕截图，你可以描述屏幕上的内容",
    failure_meaning="截屏失败",
)
async def screenshot() -> str | list:
    """截取当前屏幕并返回图片。"""
    image_bytes = await asyncio.to_thread(_capture_screenshot)
    if image_bytes is None:
        return "截屏失败"

    b64 = base64.b64encode(image_bytes).decode()
    return [
        {
            "type": "image_url",
            "image_url": {"url": f"data:image/png;base64,{b64}"},
        },
        {"type": "text", "text": "这是当前屏幕截图。"},
    ]
