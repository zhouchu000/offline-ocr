import hashlib
import json
import os
import socket
import subprocess
import sys
from pathlib import Path

import pytest

import offline_word.security as security
from offline_word.security import OfflineError, SessionStore, assert_local_path, claim_session_lease, configure_offline_env, validate_models


def test_windows_remote_drive_rejected_before_any_filesystem_query(monkeypatch):
    import ctypes
    import ntpath
    from pathlib import PureWindowsPath
    from types import SimpleNamespace
    queries = []
    monkeypatch.setattr(security, 'Path', PureWindowsPath)
    monkeypatch.setattr(security, 'os', SimpleNamespace(
        name='nt', fsdecode=os.fsdecode, fspath=os.fspath, path=ntpath,
        lstat=lambda path: queries.append(str(path)) or SimpleNamespace(st_mode=0),
    ))
    monkeypatch.setattr(ctypes, 'windll', SimpleNamespace(kernel32=SimpleNamespace(GetDriveTypeW=lambda root: 4)), raising=False)
    with pytest.raises(OfflineError):
        assert_local_path(r'Z:\\folder\\image.png')
    assert queries == []

_REQUIRED_MODELS = (
    "PP-DocLayout_plus-L", "PP-OCRv6_medium_det", "PP-OCRv6_medium_rec",
    "PP-LCNet_x1_0_doc_ori", "PP-LCNet_x1_0_textline_ori", "PP-LCNet_x1_0_table_cls",
    "SLANeXt_wired", "SLANet_plus", "RT-DETR-L_wired_table_cell_det", "RT-DETR-L_wireless_table_cell_det",
)


def _write_manifest(root: Path, models: dict[str, dict[str, str]]) -> None:
    manifest_models: dict[str, dict[str, object]] = {}
    for name, files in models.items():
        directory = root / name
        directory.mkdir(parents=True, exist_ok=True)
        hashes = {}
        for relative, content in files.items():
            file_path = directory / relative
            file_path.parent.mkdir(parents=True, exist_ok=True)
            file_path.write_text(content, encoding="utf-8")
            hashes[relative] = hashlib.sha256(file_path.read_bytes()).hexdigest()
        manifest_models[name] = {"directory": name, "files": hashes}
    (root / "manifest.json").write_text(json.dumps({
        "schema": 1,
        "versions": {"paddleocr": "3.7.0", "paddlex": "3.7.0", "paddlepaddle": "3.3.0"},
        "models": manifest_models,
    }), encoding="utf-8")


def test_validate_models_rejects_tampered_model_tree_before_paddle_import(tmp_path: Path):
    _write_manifest(tmp_path, {name: {"model.pdmodel": "good"} for name in _REQUIRED_MODELS})
    (tmp_path / "PP-OCRv6_medium_det" / "model.pdmodel").write_text("changed", encoding="utf-8")

    with pytest.raises(OfflineError, match="model_hash_mismatch"):
        validate_models(tmp_path)


def test_validate_models_rejects_model_path_traversal(tmp_path: Path):
    _write_manifest(tmp_path, {name: {"model.pdmodel": "good"} for name in _REQUIRED_MODELS})
    manifest = json.loads((tmp_path / "manifest.json").read_text(encoding="utf-8"))
    manifest["models"]["PP-OCRv6_medium_det"]["directory"] = "../outside"
    (tmp_path / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(OfflineError, match="model_manifest_invalid"):
        validate_models(tmp_path)


def test_session_store_owns_only_its_directory_and_close_does_not_remove_peer(tmp_path: Path):
    first = SessionStore(tmp_path)
    second = SessionStore(tmp_path)
    peer_path = second.path

    first.close()

    assert not first.path.exists()
    assert peer_path.exists()
    second.close()


def test_session_store_removes_only_provably_dead_owned_session(tmp_path: Path):
    stale = tmp_path / "session-dead-token"
    stale.mkdir()
    (stale / "leases").mkdir()
    (stale / "owner.lock").write_bytes(b"0")
    (stale / "owner.json").write_text(json.dumps({
        "schema": 1, "token": "dead", "pid": 999_999_999, "create_time": 0.0,
    }), encoding="utf-8")

    session = SessionStore(tmp_path)

    assert not stale.exists()
    session.close()


def test_session_store_preserves_pid_reuse_and_live_child_lease_markers(tmp_path: Path):
    stale = tmp_path / "session-reused-token"
    stale.mkdir()
    leases = stale / "leases"
    leases.mkdir()
    (stale / "owner.lock").write_bytes(b"0")
    (stale / "owner.json").write_text(json.dumps({
        "schema": 1, "token": "reused", "pid": os.getpid(), "create_time": 0.0,
    }), encoding="utf-8")
    (leases / "child.lock").write_bytes(b"0")
    import psutil
    (leases / "child.json").write_text(json.dumps({
        "schema": 1, "session_token": "reused", "lease_token": "child",
        "pid": os.getpid(), "create_time": psutil.Process().create_time(),
    }), encoding="utf-8")

    session = SessionStore(tmp_path)

    assert stale.exists()
    session.close()


def test_session_store_preserves_a_held_worker_lease_until_the_child_exits(tmp_path: Path):
    session = SessionStore(tmp_path)
    stale = session.path
    lease = claim_session_lease(stale)
    session.close()
    # Model a GUI crash after it released its lock while the OCR child remains.
    owner = json.loads((stale / "owner.json").read_text(encoding="utf-8"))
    owner.update({"pid": 999_999_999, "create_time": 0.0})
    (stale / "owner.json").write_text(json.dumps(owner), encoding="utf-8")

    recovery = SessionStore(tmp_path)
    assert stale.exists()
    lease.close()
    recovery.close()

    final = SessionStore(tmp_path)
    assert not stale.exists()
    final.close()


def test_configure_offline_env_uses_actual_paddlex_cache_variable(tmp_path: Path):
    configure_offline_env(tmp_path / "models", tmp_path / "session")

    assert os.environ["PADDLE_PDX_CACHE_HOME"].endswith("paddlex-cache")
    assert os.environ["OMP_NUM_THREADS"] == str(min(4, os.cpu_count() or 1))
    assert os.environ["MKL_NUM_THREADS"] == str(min(4, os.cpu_count() or 1))


def test_configure_offline_env_for_windows_bounds_native_cpu_threads(tmp_path: Path, monkeypatch):
    monkeypatch.delenv("OFFLINE_WORD_CPU_THREADS", raising=False)
    monkeypatch.setattr(security.os, "cpu_count", lambda: 32)
    configure_offline_env(tmp_path / "models", tmp_path / "session", platform="nt")

    assert os.environ["OMP_NUM_THREADS"] == "4"
    assert os.environ["MKL_NUM_THREADS"] == "4"
    assert os.environ["OPENBLAS_NUM_THREADS"] == "4"


@pytest.mark.parametrize(("value", "cores", "expected"), [("1", 8, 1), ("2", 8, 2), ("4", 2, 2), ("64", 32, 4), ("bad", 8, 4), ("0", 1, 1)])
def test_cpu_thread_limit_respects_older_pcs_and_bounded_override(monkeypatch, value, cores, expected):
    monkeypatch.setenv("OFFLINE_WORD_CPU_THREADS", value)
    monkeypatch.setattr(security.os, "cpu_count", lambda: cores)
    assert security.cpu_thread_limit(platform="nt") == expected


def test_assert_local_path_normalizes_relative_paths_and_rejects_symlink_ancestors(tmp_path: Path):
    assert assert_local_path("relative-local-file").is_absolute()
    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "link"
    link.symlink_to(real, target_is_directory=True)

    with pytest.raises(OfflineError, match="remote_path"):
        assert_local_path(link / "input.png")


def test_assert_local_path_rejects_windows_drive_relative_form_before_io():
    with pytest.raises(OfflineError, match="remote_path"):
        assert_local_path("Z:relative.png")


def test_assert_local_path_fails_closed_when_windows_drive_query_is_unavailable(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(security.os, "name", "nt")
    import ctypes
    from types import SimpleNamespace
    def unavailable(_path):
        raise OSError("drive API unavailable")
    monkeypatch.setattr(ctypes, "windll", SimpleNamespace(kernel32=SimpleNamespace(GetDriveTypeW=unavailable)), raising=False)

    with pytest.raises(OfflineError, match="remote_path"):
        security.assert_local_path(tmp_path)


def test_deny_network_blocks_dns_and_inet_udp_in_fresh_process():
    source_root = Path(__file__).parents[1] / "src"
    code = """
import socket
from offline_word.security import OfflineError, deny_network
deny_network()
for action in (
    lambda: socket.getaddrinfo('example.com', 443),
    lambda: socket.gethostbyname('example.com'),
    lambda: socket.socket(socket.AF_INET, socket.SOCK_DGRAM),
    lambda: socket.socket(socket.AF_INET6, socket.SOCK_DGRAM),
    lambda: socket.SocketType(socket.AF_INET, socket.SOCK_DGRAM),
):
    try:
        action()
    except OfflineError as error:
        assert error.code == 'network_disabled'
    else:
        raise AssertionError('network operation was not blocked')
print('blocked')
"""
    env = os.environ | {"PYTHONPATH": str(source_root)}
    completed = subprocess.run([sys.executable, "-c", code], env=env, capture_output=True, text=True, check=False)

    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.strip() == "blocked"
