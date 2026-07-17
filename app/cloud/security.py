from __future__ import annotations

import hashlib
import secrets

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError
from cryptography.fernet import Fernet, InvalidToken


class SecretDecryptionError(ValueError):
    """Raised when protected data cannot be authenticated and decrypted."""


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
        encoded_key = key.encode("ascii") if isinstance(key, str) else key
        self._fernet = Fernet(encoded_key)

    def encrypt(self, value: str) -> str:
        return self._fernet.encrypt(value.encode("utf-8")).decode("ascii")

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

    @staticmethod
    def generate_csrf_token() -> str:
        return secrets.token_urlsafe(32)


def hash_secret(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()
