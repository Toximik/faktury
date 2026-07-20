"""
uploads_util.py – Shared constants & helpers for user-uploaded files.

Single source of truth for the upload directory, size caps, allowed MIME
types, and the magic-byte content sniff. Lives in a neutral module so both
the HTTP upload handlers (app/main.py) and the backup import/export logic
(app/backup.py) can reuse it without a circular import.

The declared Content-Type / file extension is always attacker-controlled, so
``content_matches_type`` verifies the file's leading "magic" bytes before any
file is trusted or written to disk.
"""

from __future__ import annotations

from pathlib import Path

# ── Paths ─────────────────────────────────────────────────────────────────────

APP_DIR = Path(__file__).resolve().parent
UPLOADS_DIR = APP_DIR / "uploads"

# ── Size caps ─────────────────────────────────────────────────────────────────

MAX_IMAGE_BYTES = 5 * 1024 * 1024     # 5 MB – logo / stamp
MAX_SCAN_BYTES = 10 * 1024 * 1024     # 10 MB – expense receipt scans

# ── Allowed types ───────────────────────────────────────────────────────────

ALLOWED_IMAGE_TYPES = {"image/jpeg", "image/png", "image/webp", "image/gif"}
ALLOWED_SCAN_TYPES = {"image/jpeg", "image/png", "application/pdf"}

# content-type → canonical file extension
EXT_MAP = {
    "image/jpeg": "jpg",
    "image/png": "png",
    "image/webp": "webp",
    "image/gif": "gif",
    "application/pdf": "pdf",
}

# file extension → content-type (for sniffing archive files by name on import)
_CONTENT_TYPE_BY_EXT = {
    "jpg": "image/jpeg",
    "jpeg": "image/jpeg",
    "png": "image/png",
    "webp": "image/webp",
    "gif": "image/gif",
    "pdf": "application/pdf",
}

# ── Magic-byte signatures ─────────────────────────────────────────────────────

MAGIC_PREFIXES = {
    "image/jpeg":      (b"\xff\xd8\xff",),
    "image/png":       (b"\x89PNG\r\n\x1a\n",),
    "image/gif":       (b"GIF87a", b"GIF89a"),
    "application/pdf": (b"%PDF-",),
    # image/webp is handled specially: "RIFF"<4 bytes>"WEBP"
}


def content_matches_type(data: bytes, content_type: str) -> bool:
    """Return True if the file's magic bytes match its declared MIME type."""
    if content_type == "image/webp":
        return len(data) >= 12 and data[0:4] == b"RIFF" and data[8:12] == b"WEBP"
    return any(data.startswith(sig) for sig in MAGIC_PREFIXES.get(content_type, ()))


def content_type_for_ext(ext: str) -> str | None:
    """Map a file extension (with or without a leading dot) to a content type."""
    return _CONTENT_TYPE_BY_EXT.get(ext.lower().lstrip("."))
