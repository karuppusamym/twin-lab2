"""Envelope encryption of local artifact files (AES-256-GCM, `cryptography`).

With `TWINLAB2_ARTIFACT_KEY` set (32 random bytes, base64 or hex, from a Secret), every file the
local artifact store writes is sealed: a fresh data key per file encrypts it in 1 MiB chunks, and
the data key is wrapped by the key-encryption key (the Secret). Reading needs the key that
wrapped it: the current one, or one of `TWINLAB2_ARTIFACT_KEYS_PREVIOUS` (comma-separated) while
keys are rotated. `python -m twinlab2.admin rewrap-local` re-wraps every file's data key with the
current key (and seals files written before encryption was on), so a previous key can then go.

    header  b"TL2E\\x01" | key id (8) | wrap nonce (12) | wrapped data key (48) | chunk nonce base (8)
    chunk   length (4, big-endian) | AES-GCM(data key, base + counter(4), chunk, aad = counter | last)

The last chunk is marked in its associated data, so a truncated file does not decrypt. Re-wrapping
rewrites the header only; the chunks do not change. Files without the header (written before
encryption was on) are read as they are.

S3 is encrypted by the bucket (server-side encryption): see artifacts.S3Store (`TWINLAB2_S3_SSE`).
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import io
import os
from pathlib import Path
import struct

MAGIC = b"TL2E\x01"
HEADER = len(MAGIC) + 8 + 12 + 48 + 8
CHUNK = 1024 * 1024


class SealError(Exception):
    """A key that is malformed, or a file wrapped by a key this portal does not have."""


def parse_key(text: str) -> bytes:
    text = (text or "").strip()
    for decode in (lambda t: base64.b64decode(t, validate=True), bytes.fromhex):
        try:
            key = decode(text)
        except (binascii.Error, ValueError):
            continue
        if len(key) == 32:
            return key
    raise SealError("TWINLAB2_ARTIFACT_KEY must be 32 bytes, base64 or hex (openssl rand -base64 32)")


def key_id(key: bytes) -> bytes:
    return hashlib.sha256(b"twinlab2-artifact-key" + key).digest()[:8]


class Keyring:
    """The current key-encryption key and the previous ones still accepted for reading."""

    def __init__(self, current: bytes, previous=()):
        self.current = current
        self.keys = {key_id(current): current}
        for key in previous:
            self.keys.setdefault(key_id(key), key)

    @property
    def current_id(self) -> str:
        return key_id(self.current).hex()

    @classmethod
    def from_env(cls, environ=None):
        env = os.environ if environ is None else environ
        current = (env.get("TWINLAB2_ARTIFACT_KEY") or "").strip()
        if not current:
            return None
        previous = [parse_key(k) for k in (env.get("TWINLAB2_ARTIFACT_KEYS_PREVIOUS") or "").split(",") if k.strip()]
        return cls(parse_key(current), previous)


def _aesgcm(key):
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    return AESGCM(key)


def _wrap(keyring: Keyring, dek: bytes) -> bytes:
    kid = key_id(keyring.current)
    nonce = os.urandom(12)
    return kid + nonce + _aesgcm(keyring.current).encrypt(nonce, dek, MAGIC + kid)


def _unwrap(keyring: Keyring, header: bytes) -> tuple[bytes, bytes]:
    """(data key, chunk nonce base) of a header."""
    if len(header) != HEADER or not header.startswith(MAGIC):
        raise SealError("not a sealed artifact file")
    at = len(MAGIC)
    kid, nonce, wrapped, base = header[at:at + 8], header[at + 8:at + 20], header[at + 20:at + 68], header[at + 68:]
    key = keyring.keys.get(kid)
    if key is None:
        raise SealError(f"sealed with key {kid.hex()}, which is not TWINLAB2_ARTIFACT_KEY or a previous key")
    return _aesgcm(key).decrypt(nonce, wrapped, MAGIC + kid), base


def _aad(counter, last):
    return struct.pack(">IB", counter, 1 if last else 0)


def is_sealed(path) -> bool:
    with open(path, "rb") as handle:
        return handle.read(len(MAGIC)) == MAGIC


def seal_file(src, dst, keyring: Keyring) -> None:
    """Write `src`'s bytes sealed to `dst` (fsynced)."""
    dek, base = os.urandom(32), os.urandom(8)
    cipher = _aesgcm(dek)
    with open(src, "rb") as inp, open(dst, "wb") as out:
        out.write(MAGIC + _wrap(keyring, dek) + base)
        counter = 0
        chunk = inp.read(CHUNK)
        while True:
            following = inp.read(CHUNK) if chunk else b""
            last = not following
            sealed = cipher.encrypt(base + struct.pack(">I", counter), chunk, _aad(counter, last))
            out.write(struct.pack(">I", len(sealed)) + sealed)
            if last:
                break
            chunk, counter = following, counter + 1
        out.flush()
        os.fsync(out.fileno())


class SealedReader(io.RawIOBase):
    """A binary stream of a sealed file's plaintext."""

    def __init__(self, path, keyring: Keyring):
        super().__init__()
        self.handle = open(path, "rb")
        try:
            dek, self.base = _unwrap(keyring, self.handle.read(HEADER))
        except Exception:
            self.handle.close()
            raise
        self.cipher = _aesgcm(dek)
        self.counter, self.buffer, self.done = 0, b"", False

    def readable(self):
        return True

    def _next(self):
        size = self.handle.read(4)
        if len(size) != 4:
            raise SealError("sealed artifact file is truncated")
        sealed = self.handle.read(struct.unpack(">I", size)[0])
        nonce = self.base + struct.pack(">I", self.counter)
        last = self.handle.peek(1)[:1] == b"" if hasattr(self.handle, "peek") else False
        try:
            plain = self.cipher.decrypt(nonce, sealed, _aad(self.counter, last))
        except Exception as error:  # cryptography's InvalidTag
            raise SealError("sealed artifact file does not decrypt (changed or truncated)") from error
        self.counter += 1
        self.done = last
        return plain

    def read(self, n=-1):
        while not self.done and (n is None or n < 0 or len(self.buffer) < n):
            self.buffer += self._next()
        if n is None or n < 0:
            out, self.buffer = self.buffer, b""
        else:
            out, self.buffer = self.buffer[:n], self.buffer[n:]
        return out

    def readinto(self, target):
        data = self.read(len(target))
        target[:len(data)] = data
        return len(data)

    def close(self):
        try:
            self.handle.close()
        finally:
            super().close()


def open_file(path, keyring: Keyring | None):
    """A binary stream of a local artifact file: decrypted when sealed, as it is otherwise."""
    if is_sealed(path):
        if keyring is None:
            raise SealError("this artifact file is sealed: set TWINLAB2_ARTIFACT_KEY to read it")
        return SealedReader(path, keyring)
    return open(path, "rb")


def rewrap(path, keyring: Keyring) -> str:
    """Seal a plaintext file, or re-wrap a sealed file's data key with the current key.
    Returns what was done: "sealed", "rewrapped" or "current"."""
    path = Path(path)
    if not is_sealed(path):
        tmp = path.with_name(path.name + ".sealing")
        seal_file(path, tmp, keyring)
        os.replace(tmp, path)
        return "sealed"
    with open(path, "r+b") as handle:
        header = handle.read(HEADER)
        if header[len(MAGIC):len(MAGIC) + 8] == key_id(keyring.current):
            return "current"
        dek, base = _unwrap(keyring, header)
        handle.seek(0)
        handle.write(MAGIC + _wrap(keyring, dek) + base)
        handle.flush()
        os.fsync(handle.fileno())
    return "rewrapped"
