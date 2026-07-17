from __future__ import annotations

import hashlib

import pytest
from cryptography.fernet import Fernet

from app.cloud.security import (
    KeyCipher,
    PasswordService,
    SecretDecryptionError,
    TokenService,
    hash_secret,
)


def test_password_service_uses_argon2id_and_verifies_without_raising():
    service = PasswordService()
    password = "correct horse battery staple"

    password_hash = service.hash(password)

    assert password_hash.startswith("$argon2id$")
    assert service.verify(password_hash, password) is True
    assert service.verify(password_hash, "wrong password") is False
    assert password not in repr(service)


def test_token_service_generates_independent_random_session_and_csrf_tokens():
    service = TokenService()

    session_tokens = {service.generate_session_token() for _ in range(4)}
    csrf_tokens = {service.generate_csrf_token() for _ in range(4)}

    assert len(session_tokens) == 4
    assert len(csrf_tokens) == 4
    assert session_tokens.isdisjoint(csrf_tokens)
    assert all(len(token) >= 43 for token in session_tokens | csrf_tokens)


def test_hash_secret_is_the_lowercase_sha256_hex_digest():
    value = "opaque-token-value"

    assert hash_secret(value) == hashlib.sha256(value.encode("utf-8")).hexdigest()


def test_key_cipher_round_trips_utf8_values_without_exposing_them_in_repr():
    key = Fernet.generate_key()
    cipher = KeyCipher(key)
    plaintext = "tmp-key-with-sensitive-value"

    encrypted = cipher.encrypt(plaintext)

    assert encrypted != plaintext
    assert cipher.decrypt(encrypted) == plaintext
    assert plaintext not in repr(cipher)
    assert key.decode("ascii") not in repr(cipher)


def test_key_cipher_wrong_key_failure_redacts_all_sensitive_values():
    plaintext = "tmp-key-that-must-never-leak"
    first_key = Fernet.generate_key()
    encrypted = KeyCipher(first_key).encrypt(plaintext)
    second_key = Fernet.generate_key()

    with pytest.raises(SecretDecryptionError) as error:
        KeyCipher(second_key).decrypt(encrypted)

    rendered = f"{error.value!r} {error.value}"
    assert plaintext not in rendered
    assert encrypted not in rendered
    assert first_key.decode("ascii") not in rendered
    assert second_key.decode("ascii") not in rendered
