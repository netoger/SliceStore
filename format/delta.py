# format/delta.py
"""Дельта-кодирование между версиями файла."""
import bsdiff4


def create_delta(old_data: bytes, new_data: bytes) -> bytes:
    return bsdiff4.diff(old_data, new_data)


def apply_delta(old_data: bytes, patch: bytes) -> bytes:
    return bsdiff4.patch(old_data, patch)


def delta_stats(old_data: bytes, patch: bytes) -> dict:
    return {
        "original_size": len(old_data),
        "patch_size": len(patch),
        "ratio_percent": round(len(patch) / len(old_data) * 100, 2),
    }
