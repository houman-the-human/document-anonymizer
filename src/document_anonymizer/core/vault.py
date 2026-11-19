from __future__ import annotations

from collections.abc import Callable, Mapping
from types import MappingProxyType
from uuid import uuid4

from .exceptions import (
    VaultMismatchError,
)
from .rules import RuleSet
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

        # Each vault gets its own TokenManager.
        self._token_manager = TokenManager(
            vault_id=vault_id,
            token_factory=token_factory,
            max_attempts=max_token_attempts,
        )

    # -------------------------------------------------
    # Identity and configuration
    # -------------------------------------------------

    @property
    def vault_id(self) -> str:
        return self._vault_id

    @property
    def ruleset(self) -> RuleSet:
        return self._ruleset

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
            f"tokens={self.token_count}"
            f")"
        )
