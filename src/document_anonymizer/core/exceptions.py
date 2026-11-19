class MaskingError(Exception):
    """Base exception for the masking package."""


# Rule-related errors

class RuleValidationError(MaskingError, ValueError):
    """Raised when a masking rule is invalid."""


# Matching-related errors

class RegexTimeoutError(MaskingError, TimeoutError):
    """Raised when regex execution exceeds its time limit."""


class ZeroWidthMatchError(MaskingError):
    """Raised when a regex produces an empty match."""


# Token-related errors

class TokenGenerationError(MaskingError):
    """Raised when a unique token cannot be generated."""


class AlreadyMaskedInputError(MaskingError):
    """Raised when input contains conflicting existing tokens."""


# Vault-related errors

class VaultError(MaskingError):
    """Base exception for vault operations."""


class VaultNotFoundError(VaultError):
    """Raised when the requested vault does not exist."""


class VaultMismatchError(VaultError):
    """Raised when a token or operation belongs to another vault."""


class VaultIntegrityError(VaultError):
    """Raised when vault data is corrupted or inconsistent."""

class VaultFormatError(VaultError):
    """Raised when an encrypted vault has an invalid format."""


class VaultAuthenticationError(VaultIntegrityError):
    """
    Raised when vault authentication fails.

    This can indicate an incorrect password or modified data.
    """
