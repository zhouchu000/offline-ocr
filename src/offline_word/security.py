"""Privacy and local-model integrity boundaries for offline processing."""

from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import shutil
import socket
import stat
import tempfile
from pathlib import Path, PurePosixPath
from typing import Any


REQUIRED_MODEL_NAMES = (
    "PP-DocLayout_plus-L",
    "PP-OCRv6_medium_det",
    "PP-OCRv6_medium_rec",
    "PP-LCNet_x1_0_doc_ori",
    "PP-LCNet_x1_0_textline_ori",
    "PP-LCNet_x1_0_table_cls",
    "SLANeXt_wired",
    "SLANet_plus",
    "RT-DETR-L_wired_table_cell_det",
    "RT-DETR-L_wireless_table_cell_det",
)
_LOCKED_VERSIONS = {"paddleocr": "3.7.0", "paddlex": "3.7.0", "paddlepaddle": "3.3.0"}


class OfflineError(RuntimeError):
    """An error whose string is deliberately safe to put in the user interface."""

    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


def assert_local_path(path: Path | str, code: str = "remote_path") -> Path:
    """Normalise a path and reject remote or link-mediated filesystem access."""
    raw = os.fsdecode(os.fspath(path))
    if re.match(r"^[A-Za-z][A-Za-z0-9+.-]*://", raw) or raw.replace("/", "\\").startswith("\\\\"):
        raise OfflineError(code)
    # ``Z:relative`` uses a per-drive working directory and can otherwise evade
    # a mapped-drive check once it is made absolute.
    if re.match(r"^[A-Za-z]:[^\\/]", raw):
        raise OfflineError(code)
    candidate = Path(os.path.abspath(os.path.expanduser(raw)))
    if not candidate.is_absolute():
        raise OfflineError(code)
    # Reject mapped network drives before even querying file metadata.
    if os.name == "nt":
        try:
            import ctypes

            # DRIVE_REMOTE (4) also catches an ordinary mapped drive such as Z:\\.
            drive_type = ctypes.windll.kernel32.GetDriveTypeW(str(candidate.anchor))
            if drive_type in {0, 1, 4}:
                raise OfflineError(code)
        except OfflineError:
            raise
        except Exception:
            # A failed drive query is not proof that the path is local.
            raise OfflineError(code) from None
    current = Path(candidate.anchor)
    try:
        parts = candidate.parts[1:]
    except IndexError:
        raise OfflineError(code) from None
    for part in parts:
        current /= part
        try:
            information = os.lstat(current)
        except FileNotFoundError:
            # No later component can exist once this component is absent.
            break
        except OSError:
            raise OfflineError(code) from None
        attributes = getattr(information, "st_file_attributes", 0)
        if stat.S_ISLNK(information.st_mode) or attributes & 0x0400:  # FILE_ATTRIBUTE_REPARSE_POINT
            raise OfflineError(code)
    return candidate


class _HeldLock:
    """A small cross-platform advisory file lock held for a session lifetime."""

    def __init__(self, path: Path):
        self.path = path
        self._stream: Any | None = None

    def acquire(self, *, blocking: bool) -> bool:
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        stream: Any | None = None
        try:
            stream = self.path.open("a+b")
            if stream.tell() == 0:
                stream.write(b"0")
                stream.flush()
            if os.name == "nt":
                import msvcrt

                stream.seek(0)
                mode = msvcrt.LK_LOCK if blocking else msvcrt.LK_NBLCK
                msvcrt.locking(stream.fileno(), mode, 1)
            else:
                import fcntl

                flags = fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB)
                fcntl.flock(stream.fileno(), flags)
        except (OSError, ImportError):
            try:
                if stream is not None:
                    stream.close()
            except (AttributeError, OSError):
                pass
            return False
        self._stream = stream
        return True

    def close(self) -> None:
        stream = self._stream
        self._stream = None
        if stream is None:
            return
        try:
            if os.name == "nt":
                import msvcrt

                stream.seek(0)
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
        except (OSError, ImportError):
            pass
        finally:
            stream.close()


def _read_object(path: Path) -> dict[str, object] | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def _identity_is_live(identity: dict[str, object]) -> bool | None:
    """Return ``None`` for malformed/unverifiable identities, never guess live."""
    pid = identity.get("pid")
    started = identity.get("create_time")
    if not isinstance(pid, int) or pid <= 0 or not isinstance(started, (int, float)):
        return None
    try:
        import psutil
    except ImportError:
        return None
    try:
        process = psutil.Process(pid)
        # A PID by itself is insufficient: a later process may have reused it.
        return abs(process.create_time() - float(started)) < 0.01 and process.is_running()
    except psutil.NoSuchProcess:
        return False
    except (psutil.AccessDenied, psutil.ZombieProcess):
        # Permission and platform surprises are not evidence that a peer is dead.
        return None
    except (OSError, ValueError):
        return None


def _valid_owner(directory: Path) -> dict[str, object] | None:
    owner = _read_object(directory / "owner.json")
    if owner is None or owner.get("schema") != 1:
        return None
    token = owner.get("token")
    if not isinstance(token, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", token):
        return None
    if not directory.name.startswith(f"session-{token}-"):
        return None
    return owner if _identity_is_live(owner) is not None else None


def _has_live_lease(directory: Path, owner_token: str) -> bool | None:
    """True means a worker owns the session; None means metadata is unsafe to delete."""
    leases = directory / "leases"
    if not leases.is_dir() or leases.is_symlink():
        return None
    try:
        entries = list(leases.iterdir())
    except OSError:
        return None
    json_names = {entry.stem for entry in entries if entry.suffix == ".json"}
    lock_names = {entry.stem for entry in entries if entry.suffix == ".lock"}
    if json_names != lock_names:
        return None
    for token in json_names:
        marker = leases / f"{token}.json"
        lock_path = leases / f"{token}.lock"
        if marker.is_symlink() or lock_path.is_symlink():
            return None
        identity = _read_object(marker)
        if (
            identity is None
            or identity.get("schema") != 1
            or identity.get("session_token") != owner_token
            or identity.get("lease_token") != token
        ):
            return None
        live = _identity_is_live(identity)
        if live is None:
            return None
        if live:
            return True
        lock = _HeldLock(lock_path)
        if not lock.acquire(blocking=False):
            return True
        lock.close()
    return False


def _safe_remove_owned_session(directory: Path) -> bool:
    """Delete only ordinary entries below a verified session directory."""
    if directory.is_symlink() or not directory.is_dir():
        return False
    try:
        for entry in directory.rglob("*"):
            if entry.is_symlink():
                return False
        shutil.rmtree(directory)
    except OSError:
        return False
    return True


def _cleanup_stale_sessions(parent: Path) -> None:
    """Remove only sessions with a dead verified owner and no live worker lease."""
    try:
        candidates = list(parent.iterdir())
    except OSError:
        return
    for directory in candidates:
        if not directory.name.startswith("session-") or directory.is_symlink() or not directory.is_dir():
            continue
        owner = _valid_owner(directory)
        lock_path = directory / "owner.lock"
        if owner is None or lock_path.is_symlink() or not lock_path.is_file():
            continue
        owner_lock = _HeldLock(lock_path)
        if not owner_lock.acquire(blocking=False):
            continue
        try:
            # Re-read after acquiring the lock so a concurrent owner cannot be raced.
            owner = _valid_owner(directory)
            if owner is None or _identity_is_live(owner):
                continue
            token = owner.get("token")
            if not isinstance(token, str):
                continue
            lease_live = _has_live_lease(directory, token)
            if lease_live is not False:
                continue
        finally:
            owner_lock.close()
        _safe_remove_owned_session(directory)


class SessionLease:
    """A worker-held marker that survives loss of the GUI parent process."""

    def __init__(self, directory: Path, owner_token: str):
        self.directory = directory
        self._lease_token = secrets.token_hex(12)
        self._lock = _HeldLock(directory / "leases" / f"{self._lease_token}.lock")
        if not self._lock.acquire(blocking=True):
            raise OfflineError("session_unavailable")
        try:
            import psutil

            created = psutil.Process().create_time()
            marker = {
                "schema": 1,
                "session_token": owner_token,
                "lease_token": self._lease_token,
                "pid": os.getpid(),
                "create_time": created,
            }
            (directory / "leases" / f"{self._lease_token}.json").write_text(
                json.dumps(marker, separators=(",", ":")), encoding="utf-8"
            )
        except Exception:
            self._lock.close()
            raise OfflineError("session_unavailable") from None
        self._closed = False

    def close(self) -> None:
        if self._closed:
            return
        try:
            (self.directory / "leases" / f"{self._lease_token}.json").unlink(missing_ok=True)
        except OSError:
            pass
        self._lock.close()
        try:
            (self.directory / "leases" / f"{self._lease_token}.lock").unlink(missing_ok=True)
        except OSError:
            pass
        self._closed = True


def claim_session_lease(session_dir: Path | str) -> SessionLease:
    directory = assert_local_path(session_dir, "remote_path")
    owner = _valid_owner(directory)
    token = owner.get("token") if owner else None
    if not isinstance(token, str):
        raise OfflineError("session_unavailable")
    return SessionLease(directory, token)


class SessionStore:
    """An application-owned private temporary directory with conservative cleanup."""

    def __init__(self, base: Path | None = None):
        # macOS commonly exposes the system temporary root through /var or /tmp
        # symlinks.  Resolve this one trusted OS-provided root before enforcing
        # the no-link rule for application-controlled paths.
        default_parent = Path(os.path.realpath(tempfile.gettempdir())) / "offline-word"
        parent = assert_local_path(base if base is not None else default_parent)
        parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        try:
            parent.chmod(0o700)
        except OSError:
            pass
        _cleanup_stale_sessions(parent)
        self._token = secrets.token_hex(12)
        self.path = Path(tempfile.mkdtemp(prefix=f"session-{self._token}-", dir=parent))
        try:
            self.path.chmod(0o700)
        except OSError:
            pass
        (self.path / "leases").mkdir(mode=0o700)
        self._owner_lock = _HeldLock(self.path / "owner.lock")
        if not self._owner_lock.acquire(blocking=True):
            raise OfflineError("session_unavailable")
        try:
            import psutil

            owner = {
                "schema": 1,
                "token": self._token,
                "pid": os.getpid(),
                "create_time": psutil.Process().create_time(),
            }
            (self.path / "owner.json").write_text(json.dumps(owner, separators=(",", ":")), encoding="utf-8")
        except Exception:
            self._owner_lock.close()
            _safe_remove_owned_session(self.path)
            raise OfflineError("session_unavailable") from None
        self._closed = False

    def close(self) -> None:
        if self._closed:
            return
        # A running OCR child keeps a lease if its GUI parent has already exited.
        lease_live = _has_live_lease(self.path, self._token)
        self._owner_lock.close()
        if lease_live is False and self.path.name.startswith(f"session-{self._token}-"):
            _safe_remove_owned_session(self.path)
        self._closed = True

    def __enter__(self) -> "SessionStore":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()


def _safe_relative(value: object) -> PurePosixPath | None:
    if not isinstance(value, str) or not value or "\\" in value:
        return None
    path = PurePosixPath(value)
    if path.is_absolute() or ".." in path.parts or path == PurePosixPath("."):
        return None
    return path


def _file_hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def validate_models(root: Path) -> dict:
    """Validate the hash-locked local model tree before importing Paddle modules."""
    root = assert_local_path(root, "remote_model_path")
    manifest_path = root / "manifest.json"
    if root.is_symlink() or manifest_path.is_symlink() or not manifest_path.is_file():
        raise OfflineError("model_manifest_missing")
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        raise OfflineError("model_manifest_invalid") from None
    if not isinstance(manifest, dict) or manifest.get("schema") != 1 or manifest.get("versions") != _LOCKED_VERSIONS:
        raise OfflineError("model_manifest_invalid")
    models = manifest.get("models")
    if not isinstance(models, dict) or not set(REQUIRED_MODEL_NAMES).issubset(models):
        raise OfflineError("model_missing")

    for name, entry in models.items():
        if not isinstance(name, str) or not isinstance(entry, dict) or entry.get("directory") != name:
            raise OfflineError("model_manifest_invalid")
        model_rel = _safe_relative(name)
        files = entry.get("files")
        if model_rel is None or not isinstance(files, dict) or not files:
            raise OfflineError("model_manifest_invalid")
        model_dir = root / Path(*model_rel.parts)
        if model_dir.is_symlink() or not model_dir.is_dir():
            raise OfflineError("model_missing")
        actual_files: set[str] = set()
        try:
            descendants = list(model_dir.rglob("*"))
        except OSError:
            raise OfflineError("model_missing") from None
        for candidate in descendants:
            if candidate.is_symlink():
                raise OfflineError("model_manifest_invalid")
            if candidate.is_file():
                actual_files.add(candidate.relative_to(model_dir).as_posix())
        declared_files: set[str] = set()
        for relative, expected_hash in files.items():
            rel = _safe_relative(relative)
            if rel is None or not isinstance(expected_hash, str) or len(expected_hash) != 64:
                raise OfflineError("model_manifest_invalid")
            try:
                int(expected_hash, 16)
            except ValueError:
                raise OfflineError("model_manifest_invalid") from None
            relative_name = rel.as_posix()
            declared_files.add(relative_name)
            model_file = model_dir / Path(*rel.parts)
            if model_file.is_symlink() or not model_file.is_file():
                raise OfflineError("model_missing")
            if _file_hash(model_file) != expected_hash:
                raise OfflineError("model_hash_mismatch")
        if actual_files != declared_files:
            raise OfflineError("model_manifest_invalid")
    return manifest


def cpu_thread_limit(*, platform: str | None = None) -> int:
    """Bound CPU inference for older four-core office PCs, including Windows.

    Do not scale to the developer machine's logical processor count. A local
    override permits thermal/low-memory deployments to use fewer threads.
    """
    override = os.environ.get("OFFLINE_WORD_CPU_THREADS", "")
    if override in {"1", "2", "3", "4"}:
        return min(int(override), max(1, os.cpu_count() or 1))
    return min(4, max(1, os.cpu_count() or 1))


def configure_offline_env(model_root: Path, session_dir: Path, *, platform: str | None = None) -> None:
    """Set only local cache/config directories before Paddle/PaddleX is imported."""
    model_root = assert_local_path(model_root, "remote_model_path")
    session_dir = assert_local_path(session_dir, "remote_path")
    cache_dir = session_dir / "paddlex-cache"
    cache_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    threads = str(cpu_thread_limit(platform=platform))
    os.environ.update({
        "PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK": "1",
        "PADDLE_PDX_MODEL_SOURCE": "local",
        # PaddleX 3.7 reads this spelling from its cache module.
        "PADDLE_PDX_CACHE_HOME": str(cache_dir),
        "PADDLEX_HOME": str(cache_dir),
        "PADDLEOCR_HOME": str(cache_dir),
        "XDG_CACHE_HOME": str(cache_dir),
        "HF_HUB_OFFLINE": "1",
        "TRANSFORMERS_OFFLINE": "1",
        "NO_PROXY": "*",
        "CUDA_VISIBLE_DEVICES": "",
        # Must be present before the spawned interpreter imports NumPy/Paddle.
        "OMP_NUM_THREADS": threads,
        "MKL_NUM_THREADS": threads,
        "OPENBLAS_NUM_THREADS": threads,
        "VECLIB_MAXIMUM_THREADS": threads,
        "NUMEXPR_NUM_THREADS": threads,
        "OFFLINE_WORD_MODEL_ROOT": str(model_root),
    })


_NETWORK_DENIED = False


def deny_network() -> None:
    """Block DNS and Internet sockets after local model verification.

    Unix-domain sockets remain available for multiprocessing and other local IPC.
    """
    global _NETWORK_DENIED
    if _NETWORK_DENIED:
        return

    def blocked(*_: object, **__: object) -> None:
        raise OfflineError("network_disabled")

    original_socket = socket.socket

    class DeniedInternetSocket(original_socket):
        # A subclass keeps ``socket.socket`` a type.  ``ssl`` subclasses it at
        # import time, which a function wrapper would break before OCR begins.
        def __new__(
            cls,
            family: int = socket.AF_INET,
            type: int = socket.SOCK_STREAM,
            proto: int = 0,
            fileno: int | None = None,
        ) -> socket.socket:
            if family in (socket.AF_INET, socket.AF_INET6):
                blocked()
            if fileno is None:
                # CPython's native socket allocator accepts these arguments;
                # typeshed exposes only object.__new__ for this extension type.
                return super().__new__(cls, family, type, proto)  # pyright: ignore[reportCallIssue]
            return super().__new__(cls, family, type, proto, fileno=fileno)  # pyright: ignore[reportCallIssue]

    socket.create_connection = blocked  # type: ignore[assignment]
    socket.getaddrinfo = blocked  # type: ignore[assignment]
    socket.gethostbyname = blocked  # type: ignore[assignment]
    socket.gethostbyname_ex = blocked  # type: ignore[assignment]
    socket.gethostbyaddr = blocked  # type: ignore[assignment]
    socket.getnameinfo = blocked  # type: ignore[assignment]
    socket.socket = DeniedInternetSocket  # type: ignore[assignment]
    socket.SocketType = DeniedInternetSocket  # type: ignore[assignment]
    _NETWORK_DENIED = True
