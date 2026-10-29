"""Shared application / exclusive updater lock, released by the OS on exit."""
from contextlib import contextmanager
import os

from .security import OfflineError, assert_local_path


@contextmanager
def installation_lock(root, *, shared=False):
    root = assert_local_path(root, 'invalid_update_root')
    path = assert_local_path(root.parent / ('.'+root.name+'.update.lock'), 'invalid_update_lock')
    stream = path.open('a+b')
    try:
        if os.name == 'nt':
            import ctypes
            from ctypes import wintypes
            import msvcrt
            class Overlapped(ctypes.Structure):
                _fields_ = [('Internal', ctypes.c_size_t), ('InternalHigh', ctypes.c_size_t),
                            ('Offset', wintypes.DWORD), ('OffsetHigh', wintypes.DWORD), ('hEvent', wintypes.HANDLE)]
            api = ctypes.WinDLL('kernel32', use_last_error=True)
            api.LockFileEx.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.DWORD,
                                      wintypes.DWORD, wintypes.DWORD, ctypes.POINTER(Overlapped)]
            api.LockFileEx.restype = wintypes.BOOL
            api.UnlockFileEx.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.DWORD,
                                        wintypes.DWORD, ctypes.POINTER(Overlapped)]
            api.UnlockFileEx.restype = wintypes.BOOL
            state = Overlapped()
            handle = msvcrt.get_osfhandle(stream.fileno())
            if not api.LockFileEx(handle, 1 if shared else 3, 0, 1, 0, ctypes.byref(state)):
                raise OfflineError('application_or_update_running')
            try:
                yield
            finally:
                api.UnlockFileEx(handle, 0, 1, 0, ctypes.byref(state))
        else:
            import fcntl
            try:
                fcntl.flock(stream, (fcntl.LOCK_SH if shared else fcntl.LOCK_EX) | fcntl.LOCK_NB)
            except OSError:
                raise OfflineError('application_or_update_running') from None
            try:
                yield
            finally:
                fcntl.flock(stream, fcntl.LOCK_UN)
    finally:
        stream.close()
