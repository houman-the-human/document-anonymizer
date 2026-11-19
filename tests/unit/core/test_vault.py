import pytest

from document_anonymizer.core.vault import MaskingVault
from document_anonymizer.core.rules import (
    MaskingRule,
    RuleSet,
)
from document_anonymizer.core.exceptions import (
    AlreadyMaskedInputError,
    VaultMismatchError,
)


def create_ruleset():
    return RuleSet((
        MaskingRule(
            rule_id="person",
            kind="literal",
            pattern="John Smith",
        ),
        MaskingRule(
            rule_id="email",
            kind="regex",
            pattern=r"[\w.-]+@[\w.-]+\.\w+",
        ),
    ))


def test_vault_creation():

    vault = MaskingVault(create_ruleset())

    assert vault.vault_id
    assert isinstance(vault.vault_id, str)


def test_vault_has_unique_id():

    vault_a = MaskingVault(create_ruleset())
    vault_b = MaskingVault(create_ruleset())

    assert vault_a.vault_id != vault_b.vault_id


def test_custom_vault_id():

    vault = MaskingVault(
        create_ruleset(),
        vault_id="my-vault-001",
    )

    assert vault.vault_id == "my-vault-001"


def test_empty_vault_id_rejected():

    with pytest.raises(ValueError):
        MaskingVault(
            create_ruleset(),
            vault_id="",
        )


def test_vault_requires_ruleset():

    with pytest.raises(TypeError):
        MaskingVault("invalid")


def test_vault_owns_ruleset():

    ruleset = create_ruleset()

    vault = MaskingVault(ruleset)

    assert vault.ruleset is ruleset
    assert len(vault.ruleset.rules) == 2


def test_vault_reserves_source_text():

    vault = MaskingVault(
        create_ruleset(),
        token_factory=lambda: "A" * 32,
    )

    vault.reserve_source_text("John Smith")

    token = vault.get_or_create_token("John Smith")

    assert token == f"[[M_{'A' * 13}]]"


def test_vault_token_round_trip():

    vault = MaskingVault(
        create_ruleset(),
        token_factory=lambda: "A" * 32,
    )

    vault.reserve_source_text("John Smith")

    token = vault.get_or_create_token("John Smith")

    assert vault.get_original(token) == "John Smith"
    assert vault.get_token("John Smith") == token


def test_same_original_reuses_token():

    vault = MaskingVault(create_ruleset())

    vault.reserve_source_text("John Smith John Smith")

    first = vault.get_or_create_token("John Smith")
    second = vault.get_or_create_token("John Smith")

    assert first == second
    assert vault.token_count == 1


def test_different_originals_get_different_tokens():

    vault = MaskingVault(create_ruleset())

    vault.reserve_source_text("John Smith Sarah")

    first = vault.get_or_create_token("John Smith")
    second = vault.get_or_create_token("Sarah")

    assert first != second
    assert vault.token_count == 2


def test_vaults_are_independent():

    vault_a = MaskingVault(
        create_ruleset(),
        token_factory=lambda: "A" * 32,
    )

    vault_b = MaskingVault(
        create_ruleset(),
        token_factory=lambda: "B" * 32,
    )

    vault_a.reserve_source_text("John Smith")
    vault_b.reserve_source_text("John Smith")

    token_a = vault_a.get_or_create_token("John Smith")
    token_b = vault_b.get_or_create_token("John Smith")

    assert token_a != token_b

    assert vault_a.get_original(token_b) is None
    assert vault_b.get_original(token_a) is None


def test_unknown_token_returns_none():

    vault = MaskingVault(create_ruleset())

    assert vault.get_original(
        "[[MASK_00000000000000000000000000000000]]"
    ) is None


def test_contains_token():

    vault = MaskingVault(create_ruleset())

    vault.reserve_source_text("John Smith")

    token = vault.get_or_create_token("John Smith")

    assert vault.contains_token(token)
    assert not vault.contains_token(
        "[[MASK_00000000000000000000000000000000]]"
    )


def test_mappings_are_read_only():

    vault = MaskingVault(create_ruleset())

    vault.reserve_source_text("John Smith")

    token = vault.get_or_create_token("John Smith")

    with pytest.raises(TypeError):
        vault.original_to_token["Sarah"] = token

    with pytest.raises(TypeError):
        vault.token_to_original[token] = "Sarah"


def test_vault_id_validation():

    vault = MaskingVault(
        create_ruleset(),
        vault_id="vault-a",
    )

    vault.validate_vault_id("vault-a")


def test_vault_mismatch():

    vault = MaskingVault(
        create_ruleset(),
        vault_id="vault-a",
    )

    with pytest.raises(VaultMismatchError):
        vault.validate_vault_id("vault-b")


def test_source_collision_is_rejected():

    vault = MaskingVault(
        create_ruleset(),
        token_factory=lambda: "A" * 32,
    )

    vault.reserve_source_text("John Smith")

    token = vault.get_or_create_token("John Smith")

    with pytest.raises(AlreadyMaskedInputError):
        vault.reserve_source_text(token)


def test_clear_mappings():

    vault = MaskingVault(create_ruleset())

    vault.reserve_source_text("John Smith")

    vault.get_or_create_token("John Smith")

    vault.clear_mappings()

    assert vault.token_count == 0
    assert vault.original_to_token == {}
    assert vault.token_to_original == {}


def test_clear_preserves_vault_identity_and_rules():

    ruleset = create_ruleset()

    vault = MaskingVault(ruleset)

    vault_id = vault.vault_id

    vault.clear_mappings()

    assert vault.vault_id == vault_id
    assert vault.ruleset is ruleset


def test_vault_repr():

    vault = MaskingVault(
        create_ruleset(),
        vault_id="vault-test",
    )

    assert "vault-test" in repr(vault)
    assert "tokens=0" in repr(vault)