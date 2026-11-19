
from __future__ import annotations

import hashlib
import os
import tempfile

from pathlib import Path

from document_anonymizer.core.exceptions import (
    VaultFormatError,
    VaultMismatchError,
    VaultNotFoundError,
)

from document_anonymizer.core.vault import MaskingVault

from document_anonymizer.storage.encryption import (
    encrypt_bytes,
    decrypt_bytes,
)

from document_anonymizer.storage.serialization import (
    serialize_vault,
    deserialize_vault,
)


MAX_ENCRYPTED_VAULT_BYTES = 90 * 1024 * 1024


class FileVaultStore:
    """
    Persistent encrypted storage for masking vaults.

    Each vault is stored as an independent encrypted file.

    Features:
        - AES-256-GCM encryption.
        - Password-based encryption.
        - SHA-256-derived filenames.
        - Atomic file writes.
        - Vault identity verification.
        - Bounded file reading.
    """

    def __init__(self, directory: str | Path):

        self.directory = Path(directory).expanduser()

    # -------------------------------------------------
    # Path management
    # -------------------------------------------------

    def _vault_path(self, vault_id: str) -> Path:
        """
        Generate a deterministic filename from a vault ID.

        The original vault ID is never used as a filename,
        preventing path traversal.
        """

        if (
            not isinstance(vault_id, str)
            or not vault_id.strip()
        ):
            raise ValueError(
                "Vault ID must be a non-empty string"
            )

        digest = hashlib.sha256(
            vault_id.encode("utf-8")
        ).hexdigest()

        return self.directory / f"{digest}.vault"

    # -------------------------------------------------
    # Save
    # -------------------------------------------------

    def save(
        self,
        vault: MaskingVault,
        password: str,
    ) -> Path:
        """
        Serialize, encrypt and atomically save a vault.

        An existing vault with the same ID is updated.
        """

        if not isinstance(vault, MaskingVault):
            raise TypeError(
                "Expected a MaskingVault instance"
            )

        # 1. Serialize vault into plaintext bytes.
        serialized = serialize_vault(vault)

        # 2. Encrypt before touching the destination file.
        encrypted = encrypt_bytes(
            serialized,
            password,
        )

        if len(encrypted) > MAX_ENCRYPTED_VAULT_BYTES:
            raise VaultFormatError(
                "Encrypted vault exceeds maximum allowed size"
            )

        # 3. Determine destination.
        destination = self._vault_path(
            vault.vault_id
        )

        self.directory.mkdir(
            parents=True,
            exist_ok=True,
        )

        temporary_path = None

        try:
            # 4. Create temporary file in the same directory.
            # This allows atomic replacement on the same filesystem.
            with tempfile.NamedTemporaryFile(
                mode="wb",
                dir=self.directory,
                prefix=f".{destination.name}.",
                suffix=".tmp",
                delete=False,
            ) as temporary:

                temporary_path = Path(
                    temporary.name
                )

                temporary.write(encrypted)
                temporary.flush()

                os.fsync(
                    temporary.fileno()
                )

            # On POSIX systems, restrict the temporary file
            # permissions before making it the destination.
            # Windows access control should be managed through NTFS ACLs.
            if os.name != "nt":
                os.chmod(
                    temporary_path,
                    0o600,
                )

            # 5. Atomic replacement.
            os.replace(
                temporary_path,
                destination,
            )

            temporary_path = None

            return destination

        finally:
            # Remove temporary files after any failure.
            if temporary_path is not None:
                try:
                    temporary_path.unlink(
                        missing_ok=True
                    )
                except OSError:
                    pass

    # -------------------------------------------------
    # Load
    # -------------------------------------------------

    def load(
        self,
        vault_id: str,
        password: str,
    ) -> MaskingVault:
        """
        Load and decrypt a previously saved vault.

        The ID inside the decrypted vault must match
        the requested vault ID.
        """

        source = self._vault_path(vault_id)

        if source.is_symlink():
            raise VaultFormatError(
                "Symbolic links are not accepted as vault files"
            )

        if not source.exists():
            raise VaultNotFoundError(
                f"Vault '{vault_id}' was not found"
            )

        if not source.is_file():
            raise VaultFormatError(
                "Vault path is not a regular file"
            )

        # Read at most the configured limit plus one byte.
        with source.open("rb") as file:
            encrypted = file.read(
                MAX_ENCRYPTED_VAULT_BYTES + 1
            )

        if len(encrypted) > MAX_ENCRYPTED_VAULT_BYTES:
            raise VaultFormatError(
                "Encrypted vault exceeds maximum allowed size"
            )

        # 1. Decrypt.
        serialized = decrypt_bytes(
            encrypted,
            password,
        )

        # 2. Reconstruct the vault.
        vault = deserialize_vault(
            serialized
        )

        # 3. Verify identity.
        if vault.vault_id != vault_id:
            raise VaultMismatchError(
                "Loaded vault ID does not match requested vault ID"
            )

        return vault

    # -------------------------------------------------
    # Existence
    # -------------------------------------------------

    def exists(self, vault_id: str) -> bool:
        """Check whether a regular vault file exists."""

        path = self._vault_path(vault_id)

        return (
            path.is_file()
            and not path.is_symlink()
        )

    # -------------------------------------------------
    # Delete
    # -------------------------------------------------

    def delete(self, vault_id: str) -> bool:
        """
        Delete a vault file.

        Returns False if the vault does not exist.
        """

        path = self._vault_path(vault_id)

        if not path.exists() and not path.is_symlink():
            return False

        if path.is_dir() and not path.is_symlink():
            raise VaultFormatError(
                "Vault path points to a directory"
            )

        path.unlink()

        return True

    def __repr__(self) -> str:
        return (
            f"FileVaultStore("
            f"directory={str(self.directory)!r}"
            f")"
        )
