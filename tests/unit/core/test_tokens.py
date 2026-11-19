import pytest

from document_anonymizer.core.tokens import (
    TokenManager,
    TOKEN_PATTERN,
)

from document_anonymizer.core.exceptions import (
    AlreadyMaskedInputError,
    TokenGenerationError,
)


def test_token_format():

    manager = TokenManager()

    manager.reserve_source_text("John Smith")

    token = manager.get_or_create_token("John Smith")

    assert TOKEN_PATTERN.fullmatch(token)
    assert TOKEN_PATTERN.fullmatch(token)
    assert token.startswith("[[M_")
    assert len(token) == 19


def test_legacy_token_format_remains_supported():

    legacy_token = f"[[MASK_{'A' * 32}]]"

    assert TOKEN_PATTERN.fullmatch(legacy_token)


def test_same_original_gets_same_token():

    manager = TokenManager()

    manager.reserve_source_text(
        "John Smith met John Smith"
    )

    first = manager.get_or_create_token("John Smith")
    second = manager.get_or_create_token("John Smith")

    assert first == second
    assert manager.token_count == 1


def test_different_originals_get_different_tokens():

    manager = TokenManager()

    manager.reserve_source_text("John Smith and Sarah")

    first = manager.get_or_create_token("John Smith")
    second = manager.get_or_create_token("Sarah")

    assert first != second
    assert manager.token_count == 2


def test_token_maps_to_original():

    manager = TokenManager()

    manager.reserve_source_text("John Smith")

    token = manager.get_or_create_token("John Smith")

    assert manager.get_original(token) == "John Smith"


def test_original_maps_to_token():

    manager = TokenManager()

    manager.reserve_source_text("John Smith")

    token = manager.get_or_create_token("John Smith")

    assert manager.get_token("John Smith") == token


def test_unknown_token_returns_none():

    manager = TokenManager()

    assert manager.get_original(
        "[[MASK_00000000000000000000000000000000]]"
    ) is None


def test_vaults_have_independent_mappings():

    manager_a = TokenManager(
        vault_id="vault-a",
        token_factory=lambda: "A" * 32,
    )

    manager_b = TokenManager(
        vault_id="vault-b",
        token_factory=lambda: "B" * 32,
    )

    manager_a.reserve_source_text("John Smith")
    manager_b.reserve_source_text("John Smith")

    token_a = manager_a.get_or_create_token("John Smith")
    token_b = manager_b.get_or_create_token("John Smith")

    assert token_a != token_b

    assert manager_a.get_original(token_b) is None
    assert manager_b.get_original(token_a) is None


def test_source_collision_is_avoided():

    generated_values = iter([
        "A" * 32,
        "B" * 32,
    ])

    manager = TokenManager(
        token_factory=lambda: next(generated_values)
    )

    existing_token = f"[[M_{'A' * 13}]]"

    manager.reserve_source_text(
        f"Existing value: {existing_token}"
    )

    token = manager.get_or_create_token("John Smith")

    assert token == f"[[M_{'B' * 13}]]"


def test_existing_vault_token_in_source_is_rejected():

    manager = TokenManager(
        token_factory=lambda: "A" * 32
    )

    manager.reserve_source_text("John Smith")

    token = manager.get_or_create_token("John Smith")

    with pytest.raises(AlreadyMaskedInputError):

        manager.reserve_source_text(
            f"Already masked: {token}"
        )


def test_token_generation_collision_is_retried():

    manager = TokenManager(
        token_factory=lambda: "A" * 32,
        max_attempts=2,
    )

    manager.reserve_source_text("John Smith and Sarah")

    manager.get_or_create_token("John Smith")

    with pytest.raises(TokenGenerationError):

        manager.get_or_create_token("Sarah")


def test_invalid_token_factory_value():

    manager = TokenManager(
        token_factory=lambda: "INVALID"
    )

    manager.reserve_source_text("John Smith")

    with pytest.raises(TokenGenerationError):

        manager.get_or_create_token("John Smith")


def test_empty_original_is_rejected():

    manager = TokenManager()

    manager.reserve_source_text("")

    with pytest.raises(ValueError):

        manager.get_or_create_token("")


def test_mapping_is_read_only():

    manager = TokenManager()

    manager.reserve_source_text("John Smith")

    token = manager.get_or_create_token("John Smith")

    with pytest.raises(TypeError):

        manager.original_to_token["Sarah"] = token


def test_token_count():

    manager = TokenManager()

    manager.reserve_source_text("John Smith and Sarah")

    manager.get_or_create_token("John Smith")
    manager.get_or_create_token("Sarah")

    assert manager.token_count == 2


def test_clear_removes_mappings():

    manager = TokenManager()

    manager.reserve_source_text("John Smith")

    manager.get_or_create_token("John Smith")

    manager.clear()

    assert manager.token_count == 0
    assert manager.original_to_token == {}
    assert manager.token_to_original == {}


def test_legacy_source_token_reserves_compact_identifier():

    generated_values = iter([
        "A" * 32,
        "B" * 32,
    ])

    manager = TokenManager(
        token_factory=lambda: next(generated_values)
    )

    legacy_token = f"[[MASK_{'A' * 32}]]"

    manager.reserve_source_text(
        f"Existing: {legacy_token}"
    )

    token = manager.get_or_create_token("Jane Doe")

    assert token == f"[[M_{'B' * 13}]]"


def test_legacy_vault_mapping_reserves_compact_identifier():

    generated_values = iter([
        "A" * 32,
        "B" * 32,
    ])

    manager = TokenManager(
        token_factory=lambda: next(generated_values)
    )

    legacy_token = f"[[MASK_{'A' * 32}]]"

    manager.restore_state(
        mappings=[
            {
                "token": legacy_token,
                "original": "Existing Person",
            }
        ],
        reserved_tokens=[],
    )

    manager.reserve_source_text("Jane Doe")

    token = manager.get_or_create_token("Jane Doe")

    assert token == f"[[M_{'B' * 13}]]"

    # The old mapping must still work.
    assert manager.get_original(legacy_token) == "Existing Person"