import base64
import json

import pytest

from document_anonymizer.core.exceptions import (
    VaultAuthenticationError,
    VaultFormatError,
)

from document_anonymizer.storage.encryption import (
    encrypt_bytes,
    decrypt_bytes,
)


PASSWORD = "Strong-Test-Password-2026"


def test_encryption_round_trip():

    original = b"John Smith, 0912345678"

    encrypted = encrypt_bytes(
        original,
        PASSWORD,
    )

    decrypted = decrypt_bytes(
        encrypted,
        PASSWORD,
    )

    assert decrypted == original


def test_unicode_payload_round_trip():

    original = "سلام، علی رضایی، 東京株式会社".encode("utf-8")

    encrypted = encrypt_bytes(
        original,
        PASSWORD,
    )

    decrypted = decrypt_bytes(
        encrypted,
        PASSWORD,
    )

    assert decrypted == original


def test_each_encryption_uses_unique_salt_and_nonce():

    first = json.loads(
        encrypt_bytes(b"secret", PASSWORD)
    )

    second = json.loads(
        encrypt_bytes(b"secret", PASSWORD)
    )

    assert first["kdf"]["salt"] != second["kdf"]["salt"]

    assert first["cipher"]["nonce"] != second["cipher"]["nonce"]


def test_wrong_password_is_rejected():

    encrypted = encrypt_bytes(
        b"secret data",
        PASSWORD,
    )

    with pytest.raises(VaultAuthenticationError):

        decrypt_bytes(
            encrypted,
            "Wrong-Password",
        )


def test_modified_ciphertext_is_rejected():

    encrypted = json.loads(
        encrypt_bytes(b"secret data", PASSWORD)
    )

    ciphertext = bytearray(
        base64.urlsafe_b64decode(
            encrypted["ciphertext"]
        )
    )

    ciphertext[0] ^= 1

    encrypted["ciphertext"] = (
        base64.urlsafe_b64encode(ciphertext)
        .decode("ascii")
    )

    modified = json.dumps(encrypted).encode()

    with pytest.raises(VaultAuthenticationError):

        decrypt_bytes(
            modified,
            PASSWORD,
        )


def test_modified_salt_is_rejected():

    encrypted = json.loads(
        encrypt_bytes(b"secret data", PASSWORD)
    )

    salt = bytearray(
        base64.urlsafe_b64decode(
            encrypted["kdf"]["salt"]
        )
    )

    salt[0] ^= 1

    encrypted["kdf"]["salt"] = (
        base64.urlsafe_b64encode(salt)
        .decode("ascii")
    )

    modified = json.dumps(encrypted).encode()

    with pytest.raises(VaultAuthenticationError):

        decrypt_bytes(
            modified,
            PASSWORD,
        )


def test_unsupported_kdf_parameters_are_rejected():

    encrypted = json.loads(
        encrypt_bytes(b"secret", PASSWORD)
    )

    encrypted["kdf"]["n"] = 2**30

    modified = json.dumps(encrypted).encode()

    with pytest.raises(VaultFormatError):

        decrypt_bytes(
            modified,
            PASSWORD,
        )


def test_unsupported_format_is_rejected():

    encrypted = json.loads(
        encrypt_bytes(b"secret", PASSWORD)
    )

    encrypted["format"] = "unknown-format"

    modified = json.dumps(encrypted).encode()

    with pytest.raises(VaultFormatError):

        decrypt_bytes(
            modified,
            PASSWORD,
        )


def test_unsupported_version_is_rejected():

    encrypted = json.loads(
        encrypt_bytes(b"secret", PASSWORD)
    )

    encrypted["version"] = 999

    modified = json.dumps(encrypted).encode()

    with pytest.raises(VaultFormatError):

        decrypt_bytes(
            modified,
            PASSWORD,
        )


def test_malformed_json_is_rejected():

    with pytest.raises(VaultFormatError):

        decrypt_bytes(
            b"this is not json",
            PASSWORD,
        )


def test_invalid_base64_is_rejected():

    encrypted = json.loads(
        encrypt_bytes(b"secret", PASSWORD)
    )

    encrypted["kdf"]["salt"] = "INVALID!!!"

    modified = json.dumps(encrypted).encode()

    with pytest.raises(VaultFormatError):

        decrypt_bytes(
            modified,
            PASSWORD,
        )


def test_plaintext_is_not_visible_in_envelope():

    original = b"VERY-SECRET-ORIGINAL-DATA-123456"

    encrypted = encrypt_bytes(
        original,
        PASSWORD,
    )

    assert original not in encrypted


def test_empty_password_is_rejected():

    with pytest.raises(ValueError):

        encrypt_bytes(
            b"secret",
            "",
        )


def test_empty_password_during_decryption_is_rejected():

    encrypted = encrypt_bytes(
        b"secret",
        PASSWORD,
    )

    with pytest.raises(ValueError):

        decrypt_bytes(
            encrypted,
            "",
        )


def test_non_string_password_is_rejected():

    with pytest.raises(TypeError):

        encrypt_bytes(
            b"secret",
            123,
        )


def test_non_bytes_payload_is_rejected():

    with pytest.raises(TypeError):

        encrypt_bytes(
            "secret",
            PASSWORD,
        )


def test_invalid_envelope_type_is_rejected():

    with pytest.raises(TypeError):

        decrypt_bytes(
            "not-bytes",
            PASSWORD,
        )