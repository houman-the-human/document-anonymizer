from __future__ import annotations

import re
from collections.abc import Callable, Mapping
from secrets import token_hex
from types import MappingProxyType
from uuid import uuid4

from .exceptions import (
    AlreadyMaskedInputError,
    TokenGenerationError,
)


# New compact token format:
# [[M_0123456789ABC]]
#
# Legacy 32-character tokens remain supported.
TOKEN_PATTERN = re.compile(
    r"(?:\[\[M_[A-F0-9]{13}\]\]|\[\[MASK_[A-F0-9]{32}\]\])"
)

COMPACT_TOKEN_PATTERN = re.compile(
    r"\[\[M_([A-F0-9]{13})\]\]"
)

LEGACY_TOKEN_PATTERN = re.compile(
    r"\[\[MASK_([A-F0-9]{32})\]\]"
)

TOKEN_HEX_PATTERN = re.compile(
    r"^[0-9a-fA-F]{32}$"
)


def _token_collision_key(token: str) -> str | None:
    """
    Return the canonical 13-character identifier.

    Compact tokens use their complete identifier.
    Legacy tokens use the first 13 characters of their
    original 32-character identifier.
    """

    compact_match = COMPACT_TOKEN_PATTERN.fullmatch(token)

    if compact_match:
        return compact_match.group(1)

    legacy_match = LEGACY_TOKEN_PATTERN.fullmatch(token)

    if legacy_match:
        return legacy_match.group(1)[:13]

    return None


class TokenManager:
    """
    Manages reversible token generation within one vault.

    The same original value always receives the same token
    within this manager.

    Different managers maintain independent mappings.
    """

    def __init__(
        self,
        vault_id: str | None = None,
        token_factory: Callable[[], str] | None = None,
        max_attempts: int = 100,
    ):
        if vault_id is not None:
            if not isinstance(vault_id, str) or not vault_id.strip():
                raise ValueError("Vault ID must be a non-empty string")

        if (
            isinstance(max_attempts, bool)
            or not isinstance(max_attempts, int)
            or max_attempts <= 0
        ):
            raise ValueError("max_attempts must be a positive integer")

        if token_factory is not None and not callable(token_factory):
            raise ValueError("token_factory must be callable")

        self.vault_id = vault_id or str(uuid4())

        self._token_factory = token_factory or (
            lambda: token_hex(16)
        )

        self._max_attempts = max_attempts

        # Original value -> generated token
        self._original_to_token: dict[str, str] = {}

        # Generated token -> original value
        self._token_to_original: dict[str, str] = {}

        # Tokens already present in source documents.
        self._reserved_tokens: set[str] = set()

    # -------------------------------------------------
    # Source collision protection
    # -------------------------------------------------


    def reserve_source_text(self, text: str) -> None:
        """
        Register existing token-like strings in source text.

        Generated tokens must not collide with either
        compact or legacy token identifiers.
        """

        if not isinstance(text, str):
            raise TypeError("Source text must be a string")

        existing_tokens = set(
            TOKEN_PATTERN.findall(text)
        )

        existing_keys = {
            key
            for token in existing_tokens
            if (key := _token_collision_key(token)) is not None
        }

        owned_keys = {
            key
            for token in self._token_to_original
            if (key := _token_collision_key(token)) is not None
        }

        if existing_keys.intersection(owned_keys):
            raise AlreadyMaskedInputError(
                "Source contains tokens already owned by this vault"
            )

        self._reserved_tokens.update(existing_tokens)


    # -------------------------------------------------
    # Token generation
    # -------------------------------------------------


    def _generate_unique_token(self) -> str:
        """
        Generate a unique compact token.

        Collision detection works across both compact and
        legacy token formats.
        """

        reserved_keys = {
            key
            for token in self._reserved_tokens
            if (key := _token_collision_key(token)) is not None
        }

        owned_keys = {
            key
            for token in self._token_to_original
            if (key := _token_collision_key(token)) is not None
        }

        for _ in range(self._max_attempts):

            random_value = self._token_factory()

            if (
                not isinstance(random_value, str)
                or not TOKEN_HEX_PATTERN.fullmatch(random_value)
            ):
                raise TokenGenerationError(
                    "Token factory must return exactly "
                    "32 hexadecimal characters"
                )

            identifier = random_value[:13].upper()

            token = f"[[M_{identifier}]]"

            # Reject collisions with either token format.
            if identifier in reserved_keys:
                continue

            if identifier in owned_keys:
                continue

            return token

        raise TokenGenerationError(
            "Could not generate a unique token "
            f"after {self._max_attempts} attempts"
        )


    # -------------------------------------------------
    # Mapping operations
    # -------------------------------------------------

    def get_or_create_token(self, original: str) -> str:
        """
        Return an existing token or generate a new one.

        Source text must have been registered through
        reserve_source_text() before this method is called.
        """

        if not isinstance(original, str):
            raise TypeError("Original value must be a string")

        if original == "":
            raise ValueError("Cannot tokenize an empty string")

        existing = self._original_to_token.get(original)

        if existing is not None:
            return existing

        token = self._generate_unique_token()

        self._original_to_token[original] = token
        self._token_to_original[token] = original

        return token

    def get_original(self, token: str) -> str | None:
        """Retrieve the original value associated with a token."""

        return self._token_to_original.get(token)

    def get_token(self, original: str) -> str | None:
        """Retrieve an existing token for an original value."""

        return self._original_to_token.get(original)

    def contains_token(self, token: str) -> bool:
        """Check whether a token belongs to this vault."""

        return token in self._token_to_original

    # -------------------------------------------------
    # Read-only mapping access
    # -------------------------------------------------

    @property
    def original_to_token(self) -> Mapping[str, str]:

        return MappingProxyType(
            self._original_to_token
        )

    @property
    def token_to_original(self) -> Mapping[str, str]:

        return MappingProxyType(
            self._token_to_original
        )

    @property
    def token_count(self) -> int:

        return len(self._token_to_original)

    
    def export_state(self) -> dict:
        """
        Export token mappings and reserved tokens
        into a serializable representation.
        """

        return {
            "mappings": [
                {
                    "token": token,
                    "original": original,
                }
                for token, original in self._token_to_original.items()
            ],
            "reserved_tokens": sorted(self._reserved_tokens),
        }

    def restore_state(
        self,
        mappings: list[dict],
        reserved_tokens: list[str],
    ) -> None:
        """
        Restore mappings into a fresh TokenManager.

        All data is validated before modifying internal state.
        """

        if (
            self._original_to_token
            or self._token_to_original
            or self._reserved_tokens
        ):
            raise ValueError(
                "TokenManager must be empty before restoring state"
            )

        if not isinstance(mappings, list):
            raise ValueError("Mappings must be a list")

        if not isinstance(reserved_tokens, list):
            raise ValueError("Reserved tokens must be a list")

        original_to_token = {}
        token_to_original = {}

        for item in mappings:

            if not isinstance(item, dict):
                raise ValueError("Invalid mapping entry")

            if set(item) != {"token", "original"}:
                raise ValueError("Invalid mapping structure")

            token = item["token"]
            original = item["original"]

            if (
                not isinstance(token, str)
                or TOKEN_PATTERN.fullmatch(token) is None
            ):
                raise ValueError("Invalid masking token")

            if not isinstance(original, str) or not original:
                raise ValueError("Invalid original value")

            if token in token_to_original:
                raise ValueError("Duplicate token")

            if original in original_to_token:
                raise ValueError("Duplicate original value")

            token_to_original[token] = original
            original_to_token[original] = token

        reserved = set()

        for token in reserved_tokens:

            if (
                not isinstance(token, str)
                or TOKEN_PATTERN.fullmatch(token) is None
            ):
                raise ValueError("Invalid reserved token")

            if token in reserved:
                raise ValueError("Duplicate reserved token")

            reserved.add(token)

        if reserved.intersection(token_to_original):
            raise ValueError(
                "Reserved tokens conflict with existing mappings"
            )

        # Modify internal state only after complete validation.
        self._original_to_token = original_to_token
        self._token_to_original = token_to_original
        self._reserved_tokens = reserved


    def clear(self) -> None:
        """
        Clear all mappings and reserved tokens.

        Intended for explicit vault lifecycle management.
        """

        self._original_to_token.clear()
        self._token_to_original.clear()
        self._reserved_tokens.clear()
