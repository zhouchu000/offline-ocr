"""Offline SHA-256 verification for a complete portable release folder."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path, PurePosixPath


def _safe_relative_path(name: object) -> PurePosixPath:
    if not isinstance(name, str):
        raise ValueError("invalid_manifest_path")
    relative = PurePosixPath(name)
    if relative.is_absolute() or not relative.parts or ".." in relative.parts or ":" in name or "\\" in name:
        raise ValueError("invalid_manifest_path")
    return relative


def _digest(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _validate_symlink_target(root: Path, path: Path, target: object) -> str:
    if not isinstance(target, str) or not target or "\x00" in target or Path(target).is_absolute():
        raise ValueError("unsafe_symlink_target")
    try:
        resolved = (path.parent / target).resolve(strict=False)
        resolved.relative_to(root)
    except (OSError, RuntimeError, ValueError):
        raise ValueError("unsafe_symlink_target") from None
    return target


def _is_digest(value: object) -> bool:
    return isinstance(value, str) and len(value) == 64 and all(
        character in "0123456789abcdef" for character in value.casefold()
    )


def verify(folder: Path) -> dict[str, int | bool]:
    """Return a path-free integrity result without running external programs."""
    if folder.is_symlink():
        raise ValueError("invalid_package_root")
    expected = json.loads((folder / "checksums.json").read_text(encoding="utf-8"))
    if not isinstance(expected, dict) or not expected:
        raise ValueError("invalid_manifest")
    root = folder.resolve()
    missing = changed = links = 0
    checked_paths: set[str] = set()
    for name, expected_entry in expected.items():
        relative = _safe_relative_path(name)
        path = folder.joinpath(*relative.parts)
        if isinstance(expected_entry, dict):
            if set(expected_entry) != {"type", "target"} or expected_entry.get("type") != "symlink":
                raise ValueError("invalid_manifest_link")
            target = _validate_symlink_target(root, path, expected_entry.get("target"))
            links += 1
            if not path.is_symlink():
                missing += 1
                continue
            try:
                actual_target = os.readlink(path)
            except OSError:
                missing += 1
                continue
            _validate_symlink_target(root, path, actual_target)
            checked_paths.add(relative.as_posix())
            changed += actual_target != target
            continue
        if not _is_digest(expected_entry):
            raise ValueError("invalid_manifest_digest")
        if path.is_symlink() or not path.is_file():
            missing += 1
            continue
        checked_paths.add(relative.as_posix())
        changed += _digest(path) != expected_entry.casefold()
    actual = {
        path.relative_to(folder).as_posix()
        for path in folder.rglob("*")
        if path.is_file() or path.is_symlink()
    }
    extra = len(actual - checked_paths - {"checksums.json"})
    return {
        "pass": not (missing or changed or extra),
        "checked": len(expected),
        "links": links,
        "missing": missing,
        "changed": changed,
        "extra": extra,
    }
