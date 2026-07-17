from __future__ import annotations

import hashlib
import hmac
import secrets

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError
from cryptography.fernet import Fernet, InvalidToken


class SecretDecryptionError(ValueError):
    """Raised when protected data cannot be authenticated and decrypted."""


def _encode_protected_value(value: str, encoding: str) -> bytes:
    try:
        return value.encode(encoding)
    except UnicodeError:
        pass
    raise ValueError("unable to encode protected value")


class PasswordService:
    def __init__(self) -> None:
        self._hasher = PasswordHasher()

    def hash(self, password: str) -> str:
        return self._hasher.hash(password)

    def verify(self, password_hash: str, password: str) -> bool:
        try:
            return self._hasher.verify(password_hash, password)
        except (VerificationError, InvalidHashError):
            return False

    def __repr__(self) -> str:
        return "PasswordService()"


class KeyCipher:
    def __init__(self, key: str | bytes) -> None:
        encoded_key = _encode_protected_value(key, "ascii") if isinstance(key, str) else key
        self._fernet = Fernet(encoded_key)

    def encrypt(self, value: str) -> str:
        return self._fernet.encrypt(_encode_protected_value(value, "utf-8")).decode(
            "ascii"
        )

    def decrypt(self, encrypted_value: str) -> str:
        try:
            plaintext = self._fernet.decrypt(encrypted_value.encode("ascii"))
            return plaintext.decode("utf-8")
        except (InvalidToken, UnicodeError, ValueError):
            raise SecretDecryptionError("unable to decrypt protected value") from None

    def __repr__(self) -> str:
        return "KeyCipher(<redacted>)"


class TokenService:
    @staticmethod
    def generate_session_token() -> str:
        return secrets.token_urlsafe(32)


def derive_csrf_token(session_secret: str, session_token: str) -> str:
    return hmac.new(
        _encode_protected_value(session_secret, "utf-8"),
        b"cloud-csrf-token:v1:\x00"
        + _encode_protected_value(session_token, "utf-8"),
        hashlib.sha256,
    ).hexdigest()


def hash_secret(value: str) -> str:
    return hashlib.sha256(_encode_protected_value(value, "utf-8")).hexdigest()
