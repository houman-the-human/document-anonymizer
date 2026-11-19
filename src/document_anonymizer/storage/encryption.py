from __future__ import annotations

import base64
import binascii
import json
import secrets

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.scrypt import Scrypt

from document_anonymizer.core.exceptions import (
    VaultAuthenticationError,
    VaultFormatError,
)


# -------------------------------------------------
# Encryption configuration
# -------------------------------------------------

FORMAT_NAME = "document-anonymizer-vault"
FORMAT_VERSION = 1

KDF_NAME = "scrypt"

SCRYPT_N = 2**14
SCRYPT_R = 8
SCRYPT_P = 1

KEY_LENGTH = 32

SALT_LENGTH = 16
NONCE_LENGTH = 12

CIPHER_NAME = "AES-256-GCM"


# -------------------------------------------------
# General helpers
# -------------------------------------------------

def _validate_password(password: str) -> bytes:

    if not isinstance(password, str):
        raise TypeError("Password must be a string")

    if not password:
        raise ValueError("Password cannot be empty")

    return password.encode("utf-8")


def _encode_b64(value: bytes) -> str:

    return base64.urlsafe_b64encode(
        value
    ).decode("ascii")


def _decode_b64(
    value: str,
    expected_length: int | None = None,
) -> bytes:

    if not isinstance(value, str):
        raise VaultFormatError(
            "Invalid Base64 field"
        )

    try:
        decoded = base64.b64decode(
            value.encode("ascii"),
            altchars=b"-_",
            validate=True,
        )

    except (ValueError, UnicodeEncodeError, binascii.Error) as exc:
        raise VaultFormatError(
            "Invalid Base64 encoding"
        ) from exc

    if (
        expected_length is not None
        and len(decoded) != expected_length
    ):
        raise VaultFormatError(
            "Invalid binary field length"
        )

    return decoded


def _reject_duplicate_keys(pairs):

    result = {}

    for key, value in pairs:

        if key in result:
            raise ValueError(
                f"Duplicate JSON key: {key}"
            )

        result[key] = value

    return result


def _canonical_header(envelope: dict) -> bytes:
    """
    Generate deterministic authenticated metadata.

    Header fields are authenticated through AES-GCM AAD.
    """

    header = {
        "format": envelope["format"],
        "version": envelope["version"],
        "kdf": envelope["kdf"],
        "cipher": envelope["cipher"],
    }

    return json.dumps(
        header,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


# -------------------------------------------------
# Key derivation
# -------------------------------------------------

def _derive_key(
    password: bytes,
    salt: bytes,
) -> bytes:

    kdf = Scrypt(
        salt=salt,
        length=KEY_LENGTH,
        n=SCRYPT_N,
        r=SCRYPT_R,
        p=SCRYPT_P,
    )

    return kdf.derive(password)


# -------------------------------------------------
# Encryption
# -------------------------------------------------

def encrypt_bytes(
    plaintext: bytes,
    password: str,
) -> bytes:
    """
    Encrypt bytes using Scrypt and AES-256-GCM.

    Returns a versioned JSON envelope containing:
        - Format information
        - KDF parameters
        - Random salt
        - Random nonce
        - Authenticated ciphertext

    The plaintext is never written to disk by this module.
    """

    if not isinstance(plaintext, bytes):
        raise TypeError(
            "Plaintext must be bytes"
        )

    password_bytes = _validate_password(password)

    # Generate independent random values for each encryption.
    salt = secrets.token_bytes(SALT_LENGTH)
    nonce = secrets.token_bytes(NONCE_LENGTH)

    # Derive a 256-bit encryption key.
    key = _derive_key(
        password_bytes,
        salt,
    )

    envelope = {
        "format": FORMAT_NAME,
        "version": FORMAT_VERSION,

        "kdf": {
            "name": KDF_NAME,
            "n": SCRYPT_N,
            "r": SCRYPT_R,
            "p": SCRYPT_P,
            "salt": _encode_b64(salt),
        },

        "cipher": {
            "name": CIPHER_NAME,
            "nonce": _encode_b64(nonce),
        },
    }

    # Authenticate metadata as well as ciphertext.
    aad = _canonical_header(envelope)

    ciphertext = AESGCM(key).encrypt(
        nonce,
        plaintext,
        aad,
    )

    envelope["ciphertext"] = _encode_b64(
        ciphertext
    )

    return json.dumps(
        envelope,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


# -------------------------------------------------
# Envelope validation
# -------------------------------------------------

def _parse_envelope(encrypted_data: bytes) -> dict:

    if not isinstance(encrypted_data, bytes):
        raise TypeError(
            "Encrypted data must be bytes"
        )

    try:
        envelope = json.loads(
            encrypted_data.decode("utf-8"),
            object_pairs_hook=_reject_duplicate_keys,
        )

    except (UnicodeDecodeError, ValueError) as exc:
        raise VaultFormatError(
            "Invalid encrypted vault document"
        ) from exc

    if not isinstance(envelope, dict):
        raise VaultFormatError(
            "Vault envelope must be a JSON object"
        )

    if set(envelope) != {
        "format",
        "version",
        "kdf",
        "cipher",
        "ciphertext",
    }:
        raise VaultFormatError(
            "Unexpected vault envelope structure"
        )

    if envelope["format"] != FORMAT_NAME:
        raise VaultFormatError(
            "Unsupported vault format"
        )

    if (
        type(envelope["version"]) is not int
        or envelope["version"] != FORMAT_VERSION
    ):
        raise VaultFormatError(
            "Unsupported vault version"
        )

    kdf = envelope["kdf"]

    if not isinstance(kdf, dict):
        raise VaultFormatError(
            "Invalid KDF configuration"
        )

    if set(kdf) != {
        "name",
        "n",
        "r",
        "p",
        "salt",
    }:
        raise VaultFormatError(
            "Invalid KDF configuration"
        )

    if (
        kdf["name"] != KDF_NAME
        or type(kdf["n"]) is not int
        or type(kdf["r"]) is not int
        or type(kdf["p"]) is not int
        or kdf["n"] != SCRYPT_N
        or kdf["r"] != SCRYPT_R
        or kdf["p"] != SCRYPT_P
    ):
        raise VaultFormatError(
            "Unsupported KDF parameters"
        )

    cipher = envelope["cipher"]

    if not isinstance(cipher, dict):
        raise VaultFormatError(
            "Invalid cipher configuration"
        )

    if set(cipher) != {
        "name",
        "nonce",
    }:
        raise VaultFormatError(
            "Invalid cipher configuration"
        )

    if cipher["name"] != CIPHER_NAME:
        raise VaultFormatError(
            "Unsupported encryption algorithm"
        )

    # Validate binary fields before cryptographic operations.
    _decode_b64(
        kdf["salt"],
        expected_length=SALT_LENGTH,
    )

    _decode_b64(
        cipher["nonce"],
        expected_length=NONCE_LENGTH,
    )

    ciphertext = _decode_b64(
        envelope["ciphertext"]
    )

    # AES-GCM requires at least a 16-byte authentication tag.
    if len(ciphertext) < 16:
        raise VaultFormatError(
            "Ciphertext is too short"
        )

    return envelope


# -------------------------------------------------
# Decryption
# -------------------------------------------------

def decrypt_bytes(
    encrypted_data: bytes,
    password: str,
) -> bytes:
    """
    Decrypt and authenticate an encrypted vault payload.

    Raises:
        VaultFormatError:
            Invalid or unsupported envelope.

        VaultAuthenticationError:
            Wrong password or modified authenticated data.
    """

    password_bytes = _validate_password(password)

    envelope = _parse_envelope(
        encrypted_data
    )

    salt = _decode_b64(
        envelope["kdf"]["salt"],
        expected_length=SALT_LENGTH,
    )

    nonce = _decode_b64(
        envelope["cipher"]["nonce"],
        expected_length=NONCE_LENGTH,
    )

    ciphertext = _decode_b64(
        envelope["ciphertext"]
    )

    key = _derive_key(
        password_bytes,
        salt,
    )

    aad = _canonical_header(envelope)

    try:
        return AESGCM(key).decrypt(
            nonce,
            ciphertext,
            aad,
        )

    except InvalidTag as exc:
        raise VaultAuthenticationError(
            "Vault authentication failed. "
            "The password may be incorrect or "
            "the encrypted data may have been modified."
        ) from exc