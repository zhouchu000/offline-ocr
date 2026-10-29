import hashlib
import json
import os

import pytest

from tools.verify_package import verify


def test_package_verifier_reports_missing_changed_and_extra_files(tmp_path):
    expected = {'a.txt': hashlib.sha256(b'original').hexdigest(), 'b.txt': hashlib.sha256(b'keep').hexdigest()}
    (tmp_path / 'checksums.json').write_text(json.dumps(expected))
    (tmp_path / 'a.txt').write_bytes(b'original')
    (tmp_path / 'b.txt').write_bytes(b'keep')
    assert verify(tmp_path)['pass'] is True
    (tmp_path / 'a.txt').write_bytes(b'changed')
    (tmp_path / 'b.txt').unlink()
    (tmp_path / 'c.txt').write_bytes(b'extra')
    assert verify(tmp_path) == {'pass': False, 'checked': 2, 'links': 0, 'missing': 1, 'changed': 1, 'extra': 1}


def test_package_verifier_covers_symlink_targets_deletion_and_extra_links(tmp_path):
    """Would fail if macOS framework links could change without invalidating checksums."""
    def package(folder):
        folder.mkdir()
        (folder / "payload.txt").write_text("payload", encoding="utf-8")
        (folder / "alternate.txt").write_text("alternate", encoding="utf-8")
        os.symlink("payload.txt", folder / "current")
        (folder / "checksums.json").write_text(json.dumps({
            "payload.txt": hashlib.sha256(b"payload").hexdigest(),
            "alternate.txt": hashlib.sha256(b"alternate").hexdigest(),
            "current": {"type": "symlink", "target": "payload.txt"},
        }), encoding="utf-8")
        return folder

    retargeted = package(tmp_path / "retargeted")
    assert verify(retargeted)["links"] == 1
    (retargeted / "current").unlink()
    os.symlink("alternate.txt", retargeted / "current")
    assert verify(retargeted)["pass"] is False

    removed = package(tmp_path / "removed")
    (removed / "current").unlink()
    assert verify(removed)["pass"] is False

    extra = package(tmp_path / "extra")
    os.symlink("payload.txt", extra / "unexpected")
    assert verify(extra)["pass"] is False


@pytest.mark.parametrize(
    ("links", "manifest"),
    [
        (
            {"self": "self"},
            {"self": {"type": "symlink", "target": "self"}},
        ),
        (
            {"left": "right", "right": "left"},
            {
                "left": {"type": "symlink", "target": "right"},
                "right": {"type": "symlink", "target": "left"},
            },
        ),
    ],
)
def test_package_verifier_normalizes_symlink_resolution_loops_to_safe_error(tmp_path, links, manifest):
    """Would fail if cyclic framework links escaped as resolver tracebacks."""
    for name, target in links.items():
        os.symlink(target, tmp_path / name)
    (tmp_path / "checksums.json").write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(ValueError) as error:
        verify(tmp_path)

    assert str(error.value) == "unsafe_symlink_target"
    assert str(tmp_path) not in str(error.value)
    assert "Traceback" not in str(error.value)


@pytest.mark.parametrize("target", ["/private/outside", "../outside"])
def test_package_verifier_rejects_unsafe_symlink_targets(tmp_path, target):
    """Would fail if a package manifest could accept a link outside its release folder."""
    os.symlink(target, tmp_path / "unsafe")
    (tmp_path / "checksums.json").write_text(json.dumps({
        "unsafe": {"type": "symlink", "target": target},
    }), encoding="utf-8")

    with pytest.raises(ValueError, match="unsafe_symlink_target"):
        verify(tmp_path)
