import pytest

from document_anonymizer.core.exceptions import (
    MaskingError,
    RuleValidationError,
    RegexTimeoutError,
    ZeroWidthMatchError,
    VaultError,
    VaultNotFoundError,
    VaultMismatchError,
)


def test_rule_validation_error():

    with pytest.raises(MaskingError):
        raise RuleValidationError("Invalid rule")


def test_regex_timeout_error():

    with pytest.raises(MaskingError):
        raise RegexTimeoutError("Timeout")


def test_zero_width_error():

    with pytest.raises(MaskingError):
        raise ZeroWidthMatchError("Empty match")


def test_vault_exception_hierarchy():

    with pytest.raises(VaultError):
        raise VaultNotFoundError("Vault missing")


def test_vault_mismatch():

    with pytest.raises(VaultError):
        raise VaultMismatchError("Wrong vault")


def test_masking_error_is_exception():

    assert issubclass(MaskingError, Exception)
