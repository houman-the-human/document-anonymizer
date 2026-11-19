from __future__ import annotations

from collections.abc import Callable, Mapping
from copy import deepcopy
import base64
import binascii
import json
from types import MappingProxyType
from uuid import uuid4

from .exceptions import (
    VaultMismatchError,
)
from .rules import RuleSet
from .normalization import validate_normalization
from .tokens import TokenManager


class MaskingVault:
    """
    Represents one independent masking context.

    Each vault owns:
        - A unique identifier.
        - Its masking rules.
        - Its token manager.
        - Its original-to-token mappings.
        - Its token-to-original mappings.

    The vault is independent of storage implementation.
    Currently, all mappings are maintained in memory.
    """

    def __init__(
        self,
        ruleset: RuleSet,
        vault_id: str | None = None,
        *,
        token_factory: Callable[[], str] | None = None,
        max_token_attempts: int = 100,
        normalization: str = "none",
        ignore_zwnj: bool = False,
    ):

        if not isinstance(ruleset, RuleSet):
            raise TypeError(
                "ruleset must be a RuleSet instance"
            )

        if vault_id is None:
            vault_id = str(uuid4())

        if not isinstance(vault_id, str) or not vault_id.strip():
            raise ValueError(
                "Vault ID must be a non-empty string"
            )

        self._vault_id = vault_id
        self._ruleset = ruleset
        self._normalization, self._ignore_zwnj = validate_normalization(
            normalization,
            ignore_zwnj,
        )

        # Each vault gets its own TokenManager.
        self._token_manager = TokenManager(
            vault_id=vault_id,
            token_factory=token_factory,
            max_attempts=max_token_attempts,
        )
        self._metadata_snapshot: dict | None = None

    # -------------------------------------------------
    # Identity and configuration
    # -------------------------------------------------

    @property
    def vault_id(self) -> str:
        return self._vault_id

    @property
    def ruleset(self) -> RuleSet:
        return self._ruleset

    @property
    def normalization(self) -> str:
        """Normalization policy used when matching source text."""
        return self._normalization

    @property
    def ignore_zwnj(self) -> bool:
        """Whether Persian matching treats ZWNJ as optional."""
        return self._ignore_zwnj

    def validate_vault_id(self, vault_id: str) -> None:
        """
        Ensure an operation belongs to this vault.
        """

        if vault_id != self._vault_id:
            raise VaultMismatchError(
                f"Vault mismatch: expected '{self._vault_id}', "
                f"received '{vault_id}'"
            )

    # -------------------------------------------------
    # Source document handling
    # -------------------------------------------------

    def reserve_source_text(self, text: str) -> None:
        """
        Register existing token-like strings before masking.

        This prevents generated tokens from colliding
        with content already present in the document.
        """

        self._token_manager.reserve_source_text(text)

    # -------------------------------------------------
    # Token operations
    # -------------------------------------------------

    def get_or_create_token(self, original: str) -> str:
        """
        Return an existing token or create a new one.

        Repeated original values always reuse their token
        within this vault.
        """

        return self._token_manager.get_or_create_token(
            original
        )

    def get_original(self, token: str) -> str | None:
        """Retrieve the original value for a known token."""

        return self._token_manager.get_original(token)

    def get_token(self, original: str) -> str | None:
        """Retrieve an existing token for an original value."""

        return self._token_manager.get_token(original)

    def contains_token(self, token: str) -> bool:
        """Check whether a token belongs to this vault."""

        return self._token_manager.contains_token(token)

    # -------------------------------------------------
    # Mapping access
    # -------------------------------------------------

    @property
    def original_to_token(self) -> Mapping[str, str]:
        """Read-only original-to-token mapping."""

        return MappingProxyType(
            dict(self._token_manager.original_to_token)
        )

    @property
    def token_to_original(self) -> Mapping[str, str]:
        """Read-only token-to-original mapping."""

        return MappingProxyType(
            dict(self._token_manager.token_to_original)
        )

    @property
    def token_count(self) -> int:
        """Number of unique original values stored."""

        return self._token_manager.token_count


    
    def export_token_state(self) -> dict:
        """Export token mappings and reserved tokens."""

        return self._token_manager.export_state()

    def restore_token_state(
        self,
        mappings: list[dict],
        reserved_tokens: list[str],
    ) -> None:
        """
        Restore token state into this vault.

        Intended for trusted storage deserialization.
        The vault must not already contain mappings.
        """

        self._token_manager.restore_state(
            mappings=mappings,
            reserved_tokens=reserved_tokens,
        )


    # -------------------------------------------------
    # Reversible document metadata
    # -------------------------------------------------

    @property
    def metadata_snapshot(self) -> dict | None:
        """Return a defensive copy of the original document metadata snapshot."""
        return deepcopy(self._metadata_snapshot)

    def set_metadata_snapshot(self, snapshot: dict) -> None:
        """Attach one JSON-serializable metadata snapshot to this vault."""
        if not isinstance(snapshot, dict):
            raise TypeError("metadata snapshot must be a dictionary")
        if self._metadata_snapshot is not None:
            raise ValueError("A metadata snapshot is already attached to this vault")
        if snapshot.get("schema_version") != 1:
            raise ValueError("Unsupported metadata snapshot schema")
        metadata_format = snapshot.get("format")
        if metadata_format in {"docx", "xlsx"}:
            if set(snapshot) != {"schema_version", "format", "parts", "source_sha256", "masked_sha256"}:
                raise ValueError("Invalid OOXML metadata snapshot structure")
            parts = snapshot["parts"]
            expected_parts = {"docProps/core.xml", "docProps/app.xml", "docProps/thumbnail.jpeg"}
            if not isinstance(parts, dict) or set(parts) != expected_parts:
                raise ValueError("Invalid OOXML metadata parts")
            for encoded_part in parts.values():
                if encoded_part is not None:
                    if not isinstance(encoded_part, str):
                        raise ValueError("Metadata parts must be base64 strings")
                    try:
                        base64.b64decode(encoded_part, validate=True)
                    except (ValueError, binascii.Error) as exc:
                        raise ValueError("Invalid base64 metadata part") from exc
        elif metadata_format == "pdf":
            expected_fields = {
                "title", "author", "subject", "keywords", "creator",
                "producer", "creationDate", "modDate", "trapped",
            }
            if set(snapshot) != {"schema_version", "format", "properties", "xmp", "source_sha256", "masked_sha256"}:
                raise ValueError("Invalid PDF metadata snapshot structure")
            properties = snapshot["properties"]
            if not isinstance(properties, dict) or set(properties) != expected_fields:
                raise ValueError("Invalid PDF metadata properties")
            if any(value is not None and not isinstance(value, str) for value in properties.values()):
                raise ValueError("PDF metadata values must be strings or null")
            if snapshot["xmp"] is not None and not isinstance(snapshot["xmp"], str):
                raise ValueError("PDF XMP metadata must be a string or null")
        else:
            raise ValueError("Unsupported metadata snapshot format")

        for fingerprint_name in ("source_sha256", "masked_sha256"):
            fingerprint = snapshot.get(fingerprint_name)
            if (
                not isinstance(fingerprint, str)
                or len(fingerprint) != 64
                or any(character not in "0123456789abcdef" for character in fingerprint)
            ):
                raise ValueError(f"Invalid metadata snapshot {fingerprint_name}")

        try:
            encoded = json.dumps(
                snapshot,
                ensure_ascii=False,
                allow_nan=False,
                separators=(",", ":"),
            ).encode("utf-8")
        except (TypeError, ValueError) as exc:
            raise ValueError("metadata snapshot must contain JSON-safe values") from exc
        if len(encoded) > 16 * 1024 * 1024:
            raise ValueError("metadata snapshot exceeds the 16 MiB limit")
        self._metadata_snapshot = deepcopy(snapshot)

    def restore_metadata_snapshot(self, snapshot: dict | None) -> None:
        """Restore metadata snapshot during trusted vault deserialization."""
        if snapshot is None:
            self._metadata_snapshot = None
            return
        self.set_metadata_snapshot(snapshot)

    # -------------------------------------------------
    # Lifecycle
    # -------------------------------------------------

    def clear_mappings(self) -> None:
        """
        Clear all mappings and reserved source tokens.

        Rules and vault identity remain unchanged.
        """

        self._token_manager.clear()

    def __repr__(self) -> str:
        return (
            f"MaskingVault("
            f"vault_id={self._vault_id!r}, "
            f"rules={len(self._ruleset.rules)}, "
            f"normalization={self._normalization!r}, "
            f"tokens={self.token_count}"
            f")"
        )
