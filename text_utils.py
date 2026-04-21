"""Shared text utilities — kept dependency-free so any adapter can import."""

from __future__ import annotations

import re


def extract_spoken_text(text: str) -> str:
    """Extract lines marked with `> ` (blockquote) — the model's chosen spoken words.

    The system prompt tells the model to prefix spoken lines with `> `. If the
    model forgets, fall back to the first few sentences (up to 300 chars),
    skipping code blocks, headings, and lists.
    """
    if not text:
        return ""

    spoken_lines = []
    for line in text.split("\n"):
        if line.startswith("> "):
            spoken_lines.append(line[2:].strip())

    if spoken_lines:
        result = " ".join(spoken_lines)
        result = re.sub(r"\*\*(.+?)\*\*", r"\1", result)
        result = re.sub(r"`([^`]+)`", r"\1", result)
        return result.strip()

    clean = re.sub(r"```[\s\S]*?```", "", text)
    clean = re.sub(r"\*\*(.+?)\*\*", r"\1", clean)
    lines = [
        l.strip()
        for l in clean.split("\n")
        if l.strip() and not l.strip().startswith(("-", "*", "|", "#", "1.", "2.", "3."))
    ]
    if not lines:
        return ""

    spoken_text = ""
    for line in lines:
        if len(spoken_text) + len(line) > 300:
            break
        spoken_text += line + " "
        if line.endswith(("。", "！", "？", ".", "!", "?")):
            continue

    spoken_text = spoken_text.strip()
    if len(spoken_text) > 300:
        spoken_text = spoken_text[:300]
        m = re.search(r".*[。！？.!?]", spoken_text)
        if m:
            spoken_text = m.group()

    return spoken_text
