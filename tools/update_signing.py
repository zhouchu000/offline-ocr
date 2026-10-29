"""Windows-user DPAPI storage for the offline Ed25519 release key."""
import argparse
import ctypes
from ctypes import wintypes
from pathlib import Path

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, NoEncryption, PrivateFormat, PublicFormat


def _dpapi(data, *, decrypt=False):
    class Blob(ctypes.Structure):
        _fields_ = [('cbData', wintypes.DWORD), ('pbData', ctypes.POINTER(ctypes.c_ubyte))]
    array = (ctypes.c_ubyte*len(data)).from_buffer_copy(data)
    incoming = Blob(len(data), array)
    outgoing = Blob()
    api = ctypes.WinDLL('crypt32', use_last_error=True)
    function = api.CryptUnprotectData if decrypt else api.CryptProtectData
    function.argtypes = [ctypes.POINTER(Blob), ctypes.c_void_p, ctypes.c_void_p,
                         ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(Blob)]
    function.restype = wintypes.BOOL
    if not function(ctypes.byref(incoming), None, None, None, None, 1, ctypes.byref(outgoing)):
        raise RuntimeError('release_key_protection_failed')
    try:
        return ctypes.string_at(outgoing.pbData, outgoing.cbData)
    finally:
        api_free = ctypes.WinDLL('kernel32').LocalFree
        api_free.argtypes = [ctypes.c_void_p]
        api_free.restype = ctypes.c_void_p
        api_free(outgoing.pbData)


def load_key(path):
    return Ed25519PrivateKey.from_private_bytes(_dpapi(path.read_bytes(), decrypt=True))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--private', type=Path, required=True)
    parser.add_argument('--public-module', type=Path, required=True)
    args = parser.parse_args()
    if args.private.exists():
        key = load_key(args.private)
    else:
        key = Ed25519PrivateKey.generate()
        args.private.parent.mkdir(parents=True, exist_ok=True)
        raw = key.private_bytes(Encoding.Raw, PrivateFormat.Raw, NoEncryption())
        with args.private.open('xb') as stream:
            stream.write(_dpapi(raw))
        args.private.chmod(0o600)
    public = key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw).hex()
    current = args.public_module.read_text(encoding='utf-8')
    if "UPDATE_PUBLIC_KEY_HEX = ''" in current:
        args.public_module.write_text(current.replace("UPDATE_PUBLIC_KEY_HEX = ''", f"UPDATE_PUBLIC_KEY_HEX = '{public}'"), encoding='utf-8', newline='\n')
    elif public not in current:
        raise RuntimeError('release_key_mismatch')
    print('Release signing key protected for the current Windows user; public trust anchor ready.')


if __name__ == '__main__':
    main()
