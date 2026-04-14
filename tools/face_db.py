"""Face database — store and match face encodings."""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import NamedTuple

import numpy as np

DB_DIR = Path(__file__).parent.parent / ".faces"
DB_FILE = DB_DIR / "faces.json"
PHOTO_DIR = DB_DIR / "photos"

# Distance threshold for a match (lower = stricter)
# 0.6 is the standard recommendation from face_recognition library
# Lower values (0.4-0.5) are more strict but may fail in varying lighting
MATCH_THRESHOLD = 0.6


class FaceMatch(NamedTuple):
    name: str
    distance: float
    confidence: float  # 0-1, higher = more confident


def _ensure_dirs():
    DB_DIR.mkdir(exist_ok=True)
    PHOTO_DIR.mkdir(exist_ok=True)


def _load_db() -> dict:
    """Load face database. Format: {name: {encodings: [[...]], enrolled_at: ..., photo_count: N}}"""
    if DB_FILE.exists():
        return json.loads(DB_FILE.read_text())
    return {}


def _save_db(db: dict):
    _ensure_dirs()
    DB_FILE.write_text(json.dumps(db, ensure_ascii=False, indent=2))


def enroll(name: str, encoding: np.ndarray, photo_bytes: bytes | None = None) -> int:
    """Add a face encoding for a person. Returns total encodings for this person."""
    db = _load_db()

    if name not in db:
        db[name] = {"encodings": [], "enrolled_at": time.time(), "photo_count": 0}

    db[name]["encodings"].append(encoding.tolist())
    db[name]["photo_count"] = len(db[name]["encodings"])

    # Save a reference photo
    if photo_bytes:
        _ensure_dirs()
        idx = db[name]["photo_count"]
        (PHOTO_DIR / f"{name}_{idx}.jpg").write_bytes(photo_bytes)

    _save_db(db)
    return db[name]["photo_count"]


def recognize(encoding: np.ndarray, debug: bool = False) -> FaceMatch | None:
    """Find the closest match in the database. Returns None if no match within threshold."""
    db = _load_db()
    if not db:
        return None

    best_name = None
    best_distance = float("inf")

    for name, data in db.items():
        for stored in data["encodings"]:
            stored_enc = np.array(stored)
            dist = np.linalg.norm(encoding - stored_enc)
            if dist < best_distance:
                best_distance = dist
                best_name = name

    # Debug logging
    if debug:
        print(f"[face_db] Best match: {best_name}, distance: {best_distance:.3f}, threshold: {MATCH_THRESHOLD}")

    if best_distance > MATCH_THRESHOLD or best_name is None:
        return None

    confidence = max(0.0, min(1.0, 1.0 - best_distance / MATCH_THRESHOLD))
    return FaceMatch(name=best_name, distance=round(best_distance, 3), confidence=round(confidence, 2))


def list_known() -> list[str]:
    """Return names of all enrolled people."""
    return list(_load_db().keys())


def forget(name: str) -> bool:
    """Remove a person from the database."""
    db = _load_db()
    if name not in db:
        return False
    del db[name]
    _save_db(db)
    # Clean up photos
    for p in PHOTO_DIR.glob(f"{name}_*"):
        p.unlink(missing_ok=True)
    return True
