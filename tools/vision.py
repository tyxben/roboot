"""Camera, screenshot, and face recognition tools."""

from __future__ import annotations

import asyncio
import subprocess
import tempfile
import time
from pathlib import Path

import arcana

from tools import face_db

CAPTURES_DIR = Path(__file__).parent.parent / "static" / "captures"


def _ensure_captures_dir():
    CAPTURES_DIR.mkdir(parents=True, exist_ok=True)


def _save_capture(image_bytes: bytes, prefix: str = "cap") -> str:
    """Save image to static/captures/ and return the URL path."""
    _ensure_captures_dir()
    filename = f"{prefix}_{int(time.time() * 1000)}.jpg"
    (CAPTURES_DIR / filename).write_bytes(image_bytes)
    return f"/static/captures/{filename}"


def _capture_camera() -> bytes | None:
    """Capture a frame from the Mac camera. Returns JPEG bytes or None."""
    try:
        import cv2
        import numpy as np

        cap = cv2.VideoCapture(0)
        if not cap.isOpened():
            return None

        # Let camera warm up for better exposure (more frames = better quality)
        for _ in range(15):
            cap.read()

        ret, frame = cap.read()
        cap.release()
        if not ret:
            return None

        # Enhance image quality: adjust brightness and contrast
        # Convert to LAB color space and apply CLAHE to L channel
        lab = cv2.cvtColor(frame, cv2.COLOR_BGR2LAB)
        l, a, b = cv2.split(lab)
        clahe = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8,8))
        l = clahe.apply(l)
        enhanced = cv2.merge([l, a, b])
        frame = cv2.cvtColor(enhanced, cv2.COLOR_LAB2BGR)

        # Higher JPEG quality for better clarity
        _, buf = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 95])
        return buf.tobytes()
    except ImportError:
        return None


def _detect_and_encode(image_bytes: bytes) -> list[tuple[tuple, "np.ndarray"]]:
    """Detect faces and return list of (location, encoding) tuples."""
    import cv2
    import face_recognition
    import numpy as np

    nparr = np.frombuffer(image_bytes, np.uint8)
    img = cv2.imdecode(nparr, cv2.IMREAD_COLOR)
    rgb = cv2.cvtColor(img, cv2.COLOR_BGR2RGB)

    locations = face_recognition.face_locations(rgb, model="hog")
    encodings = face_recognition.face_encodings(rgb, locations)
    return list(zip(locations, encodings))


def _annotate_image(image_bytes: bytes, faces: list[dict]) -> bytes:
    """Draw boxes and names on the image. Returns annotated JPEG bytes."""
    import cv2
    import numpy as np

    nparr = np.frombuffer(image_bytes, np.uint8)
    img = cv2.imdecode(nparr, cv2.IMREAD_COLOR)

    for face in faces:
        top, right, bottom, left = face["location"]
        name = face.get("name", "?")
        conf = face.get("confidence")

        color = (0, 200, 0) if name != "?" else (0, 140, 255)
        cv2.rectangle(img, (left, top), (right, bottom), color, 2)

        label = name
        if conf is not None:
            label += f" ({int(conf * 100)}%)"
        cv2.putText(img, label, (left, top - 10),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.7, color, 2)

    _, buf = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, 90])
    return buf.tobytes()


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
    when_to_use="当用户说'看看'、'你看到什么'、'看一下'，或需要了解用户面前的实物时。也会自动识别画面中的人脸。",
    what_to_expect="返回摄像头拍到的画面描述和人脸识别结果，以及图片链接",
    failure_meaning="摄像头不可用，建议用 screenshot 代替",
)
async def look() -> str:
    """用摄像头拍照，自动识别画面中的人脸。"""
    image_bytes = await asyncio.to_thread(_capture_camera)
    if image_bytes is None:
        return "摄像头不可用。可能需要安装 opencv-python 或授权摄像头权限。"

    # Try face detection
    face_info = []
    annotated = image_bytes
    try:
        faces = await asyncio.to_thread(_detect_and_encode, image_bytes)
        for loc, enc in faces:
            match = face_db.recognize(enc, debug=True)  # Enable debug logging
            face_info.append({
                "location": loc,
                "name": match.name if match else "未知",
                "confidence": match.confidence if match else None,
                "distance": match.distance if match else None,
            })
        if face_info:
            annotated = await asyncio.to_thread(_annotate_image, image_bytes, face_info)
    except Exception as e:
        print(f"[vision] Face detection error: {e}")

    # Save image and get URL
    url = _save_capture(annotated, "look")

    # Build text-only result
    lines = [f"![摄像头画面]({url})"]
    if face_info:
        recognized = [f for f in face_info if f["name"] != "未知"]
        unknown = [f for f in face_info if f["name"] == "未知"]
        lines.append(f"检测到 {len(face_info)} 张人脸。")
        if recognized:
            for f in recognized:
                lines.append(f"- 认出了 **{f['name']}**（置信度 {int(f['confidence']*100)}%）")
        if unknown:
            lines.append(f"- {len(unknown)} 张未识别的脸（可以用 enroll_face 注册）")
    else:
        lines.append("没有检测到人脸。")

    return "\n".join(lines)


@arcana.tool(
    when_to_use="当用户说'记住我'、'记住我的脸'、'注册人脸'、'我是xxx'，或让你认识一个新面孔时",
    what_to_expect="拍照并把用户的脸存入数据库，下次就能认出来",
    failure_meaning="拍照失败或没检测到人脸",
)
async def enroll_face(name: str) -> str:
    """拍照并注册人脸。name 是这个人的名字。会拍多张提高准确度。"""
    results = []
    total_enrolled = 0
    last_image = None

    # Take 3 shots for better accuracy
    for i in range(3):
        image_bytes = await asyncio.to_thread(_capture_camera)
        if image_bytes is None:
            return "摄像头不可用，请检查摄像头权限。"

        faces = await asyncio.to_thread(_detect_and_encode, image_bytes)
        if not faces:
            results.append(f"第{i+1}张：未检测到人脸")
            continue

        # Use the largest face (closest to camera)
        largest = max(faces, key=lambda f: (f[0][2] - f[0][0]) * (f[0][1] - f[0][3]))
        _, enc = largest
        count = face_db.enroll(name, enc, image_bytes if i == 0 else None)
        total_enrolled = count
        last_image = image_bytes
        results.append(f"第{i+1}张：已录入")

        if i < 2:
            await asyncio.sleep(0.5)

    if total_enrolled == 0:
        return "注册失败：3次拍照都没检测到人脸。请确保光线充足、正面对着摄像头。"

    # Save the last captured image
    url = _save_capture(last_image, "enroll")

    return (
        f"![注册照片]({url})\n"
        f"已注册「{name}」的人脸，共 {total_enrolled} 个编码。\n"
        f"{'、'.join(results)}\n"
        f"下次看到 {name} 就能认出来了！"
    )


@arcana.tool(
    when_to_use="当用户问'你认识谁'、'注册了哪些人'、'人脸列表'时",
    what_to_expect="返回已注册的人脸名单",
)
async def list_faces() -> str:
    """列出所有已注册的人脸。"""
    names = face_db.list_known()
    if not names:
        return "还没有注册任何人脸。用 enroll_face 来注册。"
    return f"已注册 {len(names)} 人: {', '.join(names)}"


@arcana.tool(
    when_to_use="当用户说'忘掉某人'、'删除人脸'时",
    what_to_expect="从数据库中删除指定人的人脸数据",
)
async def forget_face(name: str) -> str:
    """从数据库中删除一个人的人脸数据。"""
    if face_db.forget(name):
        return f"已删除「{name}」的人脸数据。"
    return f"没有找到「{name}」的记录。"


@arcana.tool(
    when_to_use="当用户说'看看屏幕'、'截个屏'，或需要了解桌面/应用状态时",
    what_to_expect="返回截屏图片链接",
    failure_meaning="截屏失败",
)
async def screenshot() -> str:
    """截取当前屏幕并返回图片链接。"""
    image_bytes = await asyncio.to_thread(_capture_screenshot)
    if image_bytes is None:
        return "截屏失败"

    _ensure_captures_dir()
    filename = f"screen_{int(time.time() * 1000)}.png"
    (CAPTURES_DIR / filename).write_bytes(image_bytes)
    url = f"/static/captures/{filename}"

    return f"![屏幕截图]({url})\n这是当前屏幕截图。"
