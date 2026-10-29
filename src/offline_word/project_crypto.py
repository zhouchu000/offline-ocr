"""Bounded, seekable AES-GCM records for an encrypted project archive.

Each save uses a fresh 128-bit scrypt salt and 64-bit nonce prefix. The final
32 nonce bits are a monotonically increasing record index. Header, index,
record length and final-record flag are authenticated. A mandatory empty final
record detects truncation; neither filenames nor archive metadata are public.
No plaintext staging file is needed by zipfile's streaming writer/reader.
"""
from __future__ import annotations

import io
import os
import struct

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.scrypt import Scrypt

from .security import OfflineError

MAGIC = b'OWPROJ1\0'
CHUNK = 1024 * 1024
MAX_BYTES = 32 * 1024 ** 3
HEADER_SIZE = len(MAGIC) + 24
_U32 = struct.Struct('>I')


def _cipher(password: str, salt: bytes) -> AESGCM:
    if not isinstance(password, str) or not 10 <= len(password) <= 1024:
        raise OfflineError('project_password_length')
    key = Scrypt(salt=salt, length=32, n=2**17, r=8, p=1).derive(password.encode('utf-8'))
    return AESGCM(key)


class EncryptedWriter(io.RawIOBase):
    def __init__(self, stream, password):
        super().__init__()
        self.stream = stream
        self.header = MAGIC + os.urandom(24)
        self.cipher = _cipher(password, self.header[len(MAGIC):len(MAGIC)+16])
        self.prefix = self.header[-8:]
        self.buffer = bytearray()
        self.position = self.index = 0
        self.finished = False
        stream.write(self.header)

    def writable(self):
        return True

    def tell(self):
        return self.position

    def write(self, data):
        if self.finished:
            raise ValueError('project_stream_finished')
        if self.position + len(data) > MAX_BYTES:
            raise OfflineError('project_too_large')
        view = memoryview(data)
        length = len(view)
        while view:
            count = min(CHUNK - len(self.buffer), len(view))
            self.buffer.extend(view[:count])
            view = view[count:]
            if len(self.buffer) == CHUNK:
                self._record(bytes(self.buffer))
                self.buffer.clear()
        self.position += length
        return length

    def _record(self, data):
        index, length = _U32.pack(self.index), _U32.pack(len(data))
        aad = self.header + index + length + (b'D' if data else b'F')
        ciphertext = self.cipher.encrypt(self.prefix + index, data, aad)
        self.stream.write(length)
        self.stream.write(ciphertext)
        self.index += 1

    def finish(self):
        if self.finished:
            return
        if self.buffer:
            self._record(bytes(self.buffer))
            self.buffer.clear()
        self._record(b'')
        self.finished = True
        self.stream.flush()


class EncryptedReader(io.RawIOBase):
    def __init__(self, stream, password):
        super().__init__()
        self.stream = stream
        stream.seek(0, io.SEEK_END)
        file_size = stream.tell()
        if not HEADER_SIZE + 20 <= file_size <= MAX_BYTES + (MAX_BYTES // CHUNK + 2)*20 + HEADER_SIZE:
            raise OfflineError('invalid_project_file')
        stream.seek(0)
        self.header = stream.read(HEADER_SIZE)
        if self.header[:len(MAGIC)] != MAGIC:
            raise OfflineError('unsupported_project_version')
        self.cipher = _cipher(password, self.header[len(MAGIC):len(MAGIC)+16])
        self.prefix = self.header[-8:]
        self.records = []
        self.position = self.length = 0
        self._cache_index, self._cache = -1, b''
        short = False
        while True:
            encoded = stream.read(4)
            if len(encoded) != 4:
                raise OfflineError('project_damaged')
            length = _U32.unpack(encoded)[0]
            offset = stream.tell()
            if length > CHUNK or offset + length + 16 > file_size:
                raise OfflineError('project_damaged')
            if length == 0:
                self._decrypt(len(self.records), length, stream.read(16))
                if stream.tell() != file_size:
                    raise OfflineError('project_damaged')
                break
            if short or self.length + length > MAX_BYTES:
                raise OfflineError('project_damaged')
            short = length != CHUNK
            self.records.append((offset, length))
            self.length += length
            stream.seek(length + 16, io.SEEK_CUR)

    def _decrypt(self, index, length, data):
        counter = _U32.pack(index)
        aad = self.header + counter + _U32.pack(length) + (b'D' if length else b'F')
        try:
            return self.cipher.decrypt(self.prefix + counter, data, aad)
        except InvalidTag:
            raise OfflineError('project_password_or_integrity') from None

    def _chunk(self, index):
        if self._cache_index != index:
            offset, length = self.records[index]
            self.stream.seek(offset)
            self._cache = self._decrypt(index, length, self.stream.read(length + 16))
            self._cache_index = index
        return self._cache

    def verify_all(self):
        for index in range(len(self.records)):
            self._chunk(index)
        self._cache_index, self._cache = -1, b''

    def readable(self):
        return True

    def seekable(self):
        return True

    def tell(self):
        return self.position

    def seek(self, offset, whence=io.SEEK_SET):
        position = offset + (self.position if whence == io.SEEK_CUR else self.length if whence == io.SEEK_END else 0)
        if whence not in {io.SEEK_SET, io.SEEK_CUR, io.SEEK_END} or position < 0:
            raise OSError('invalid_project_seek')
        self.position = position
        return position

    def read(self, size=-1):
        remaining = max(0, self.length - self.position)
        size = remaining if size is None or size < 0 else min(size, remaining)
        output = bytearray()
        while size:
            index, offset = divmod(self.position, CHUNK)
            chunk = self._chunk(index)
            count = min(size, len(chunk) - offset)
            output.extend(chunk[offset:offset+count])
            self.position += count
            size -= count
        return bytes(output)
