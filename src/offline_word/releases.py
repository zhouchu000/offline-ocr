"""Bundled, offline release history. No user documents or network dependencies."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date
import json
from pathlib import Path
import re

from .version import __version__


@dataclass(frozen=True)
class Release:
    version: str
    date: str
    title: str
    changes: tuple[str, ...]


def version_key(value: str) -> tuple[int, ...]:
    if not isinstance(value, str) or not re.fullmatch(r"(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)", value):
        raise ValueError("invalid_release_version")
    return tuple(int(part) for part in value.split("."))


def load_releases(path: Path | None = None, *, current: str = __version__) -> tuple[Release, ...]:
    """Reject missing current notes, duplicate versions and malformed release data."""
    value = json.loads((path or Path(__file__).with_name("releases.json")).read_text(encoding="utf-8"))
    if not isinstance(value, list) or not value:
        raise ValueError("invalid_release_history")
    releases = []
    seen = set()
    for entry in value:
        version = entry["version"]
        version_key(version)
        if version in seen:
            raise ValueError("duplicate_release_version")
        seen.add(version)
        date.fromisoformat(entry["date"])
        if not isinstance(entry["title"], str) or not entry["title"].strip():
            raise ValueError("invalid_release_title")
        changes = entry["changes"]
        if not isinstance(changes, list) or not changes or any(not isinstance(item, str) or not item.strip() for item in changes):
            raise ValueError("invalid_release_changes")
        releases.append(Release(version, entry["date"], entry["title"], tuple(changes)))
    if current not in seen:
        raise ValueError("current_release_notes_missing")
    return tuple(sorted(releases, key=lambda item: version_key(item.version), reverse=True))
