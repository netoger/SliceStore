# format/versioning.py
"""Версионирование формата и G-code файлов."""
import hashlib
import os
import bsdiff4
from pathlib import Path

from .constants import FORMAT_MAJOR, FORMAT_MINOR

DELTA_THRESHOLD = 0.05
LARGE_FILE_THRESHOLD = 200 * 1024 * 1024


def format_version_string(major: int = FORMAT_MAJOR, minor: int = FORMAT_MINOR) -> str:
    return f"{major}.{minor}"


def is_compatible(major: int, minor: int) -> bool:
    return major == FORMAT_MAJOR and minor <= FORMAT_MINOR


def gcode_delta_or_full(
    old_path: str, new_path: str, force_snapshot: bool = False
) -> dict:
    old_size = os.path.getsize(old_path)
    new_size = os.path.getsize(new_path)

    if force_snapshot or old_size > LARGE_FILE_THRESHOLD:
        checksum = _file_sha256(new_path)
        return {
            "type": "full",
            "reason": "snapshot" if force_snapshot else "file_too_large_for_delta",
            "path": new_path,
            "checksum": checksum,
            "size": new_size,
        }

    with open(old_path, "rb") as f:
        old_data = f.read()
    with open(new_path, "rb") as f:
        new_data = f.read()

    patch = bsdiff4.diff(old_data, new_data)
    ratio = len(patch) / max(len(new_data), 1)

    if ratio < DELTA_THRESHOLD:
        return {
            "type": "delta",
            "patch": patch,
            "patch_size": len(patch),
            "ratio": ratio,
            "base_checksum": _file_sha256(old_path),
            "target_checksum": _file_sha256(new_path),
        }

    return {
        "type": "full",
        "reason": "delta_too_large",
        "path": new_path,
        "checksum": _file_sha256(new_path),
        "size": new_size,
    }


def apply_gcode_patch(base_path: str, patch: bytes, output_path: str) -> None:
    with open(base_path, "rb") as f:
        base_data = f.read()
    result = bsdiff4.patch(base_data, patch)
    with open(output_path, "wb") as f:
        f.write(result)


def _file_sha256(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(8 * 1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()
