import pytest

from document_anonymizer.core.engine import MaskingEngine
from document_anonymizer.core.vault import MaskingVault
from document_anonymizer.core.rules import (
    MaskingRule,
    RuleSet,
)
from document_anonymizer.core.exceptions import (
    AlreadyMaskedInputError,
)


def create_engine(*rules, vault_id=None, token_factory=None):

    ruleset = RuleSet(tuple(rules))

    vault = MaskingVault(
        ruleset=ruleset,
        vault_id=vault_id,
        token_factory=token_factory,
    )

    return MaskingEngine(vault), vault


def test_literal_masking():

    engine, vault = create_engine(
        MaskingRule(
            "person",
            "literal",
            "John Smith",
        )
    )

    result = engine.mask_text(
        "Hello John Smith"
    )

    assert result.replacements_count == 1
    assert "John Smith" not in result.masked_text
    assert result.masked_text.startswith("Hello [[M_")


def test_regex_masking():

    engine, vault = create_engine(
        MaskingRule(
            "phone",
            "regex",
            r"\b\d{10}\b",
        )
    )

    result = engine.mask_text(
        "Phone: 0912345678"
    )

    assert result.replacements_count == 1
    assert "0912345678" not in result.masked_text


def test_literal_and_regex_together():

    engine, vault = create_engine(
        MaskingRule(
            "person",
            "literal",
            "John Smith",
        ),
        MaskingRule(
            "phone",
            "regex",
            r"\b\d{10}\b",
        ),
    )

    result = engine.mask_text(
        "John Smith: 0912345678"
    )

    assert result.replacements_count == 2
    assert len(result.unique_tokens) == 2


def test_repeated_original_reuses_same_token():

    engine, vault = create_engine(
        MaskingRule(
            "person",
            "literal",
            "John Smith",
        )
    )

    result = engine.mask_text(
        "John Smith met John Smith"
    )

    assert result.replacements_count == 2
    assert len(result.unique_tokens) == 1

    first, second = result.masked_text.split(" met ")

    assert first == second


def test_same_original_reuses_token_across_calls():

    engine, vault = create_engine(
        MaskingRule(
            "person",
            "literal",
            "John Smith",
        )
    )

    first = engine.mask_text("John Smith")
    second = engine.mask_text("John Smith")

    assert first.masked_text == second.masked_text


def test_different_values_get_different_tokens():

    engine, vault = create_engine(
        MaskingRule("john", "literal", "John"),
        MaskingRule("sarah", "literal", "Sarah"),
    )

    result = engine.mask_text("John and Sarah")

    assert len(result.unique_tokens) == 2
    assert result.unique_tokens[0] != result.unique_tokens[1]


def test_replacements_by_rule():

    engine, vault = create_engine(
        MaskingRule("person", "literal", "John"),
        MaskingRule("phone", "regex", r"\b\d{10}\b"),
    )

    result = engine.mask_text(
        "John called 0912345678"
    )

    assert result.replacements_by_rule["person"] == 1
    assert result.replacements_by_rule["phone"] == 1


def test_no_matching_rules():

    engine, vault = create_engine(
        MaskingRule("person", "literal", "John")
    )

    result = engine.mask_text("Hello Sarah")

    assert result.masked_text == "Hello Sarah"
    assert result.replacements_count == 0
    assert result.unique_tokens == ()
    assert vault.token_count == 0


def test_empty_text():

    engine, vault = create_engine(
        MaskingRule("person", "literal", "John")
    )

    result = engine.mask_text("")

    assert result.masked_text == ""
    assert result.replacements_count == 0


def test_masking_result_contains_vault_id():

    engine, vault = create_engine(
        MaskingRule("person", "literal", "John"),
        vault_id="vault-001",
    )

    result = engine.mask_text("John")

    assert result.vault_id == "vault-001"


def test_unmasking_restores_original_text():

    engine, vault = create_engine(
        MaskingRule("person", "literal", "John Smith"),
    )

    original = "Hello John Smith"

    masked = engine.mask_text(original)
    restored = engine.unmask_text(masked.masked_text)

    assert restored.restored_text == original
    assert restored.restored_count == 1


def test_unmasking_multiple_occurrences():

    engine, vault = create_engine(
        MaskingRule("person", "literal", "John"),
    )

    original = "John met John and John"

    masked = engine.mask_text(original)

    restored = engine.unmask_text(masked.masked_text)

    assert restored.restored_text == original
    assert restored.restored_count == 3


def test_unknown_tokens_are_preserved():

    engine, vault = create_engine(
        MaskingRule("person", "literal", "John"),
    )

    unknown = "[[MASK_00000000000000000000000000000000]]"

    result = engine.unmask_text(
        f"Hello {unknown}"
    )

    assert unknown in result.restored_text
    assert result.restored_count == 0
    assert result.unknown_tokens == (unknown,)


def test_known_and_unknown_tokens_together():

    engine, vault = create_engine(
        MaskingRule("person", "literal", "John"),
    )

    masked = engine.mask_text("John")

    unknown = "[[MASK_00000000000000000000000000000000]]"

    result = engine.unmask_text(
        f"{masked.masked_text} and {unknown}"
    )

    assert result.restored_text == f"John and {unknown}"
    assert result.restored_count == 1
    assert result.unknown_tokens == (unknown,)


def test_different_vault_cannot_restore_token():

    engine_a, vault_a = create_engine(
        MaskingRule("person", "literal", "John"),
        vault_id="vault-a",
        token_factory=lambda: "A" * 32,
    )

    engine_b, vault_b = create_engine(
        MaskingRule("person", "literal", "John"),
        vault_id="vault-b",
        token_factory=lambda: "B" * 32,
    )

    masked = engine_a.mask_text("John")

    restored = engine_b.unmask_text(
        masked.masked_text
    )

    assert restored.restored_text == masked.masked_text
    assert restored.restored_count == 0
    assert restored.unknown_tokens == masked.unique_tokens


def test_original_token_like_text_is_restored_once():

    original_token = f"[[MASK_{'B' * 32}]]"

    engine, vault = create_engine(
        MaskingRule(
            "special",
            "literal",
            original_token,
        ),
        token_factory=lambda: "A" * 32,
    )

    masked = engine.mask_text(original_token)

    restored = engine.unmask_text(
        masked.masked_text
    )

    assert restored.restored_text == original_token
    assert restored.restored_count == 1
    assert restored.unknown_tokens == ()


def test_already_masked_input_is_rejected():

    engine, vault = create_engine(
        MaskingRule("person", "literal", "John")
    )

    masked = engine.mask_text("John")

    with pytest.raises(AlreadyMaskedInputError):
        engine.mask_text(masked.masked_text)


def test_case_insensitive_masking_preserves_original():

    engine, vault = create_engine(
        MaskingRule(
            "person",
            "literal",
            "john",
            case_sensitive=False,
        )
    )

    masked = engine.mask_text("JOHN")

    restored = engine.unmask_text(masked.masked_text)

    assert restored.restored_text == "JOHN"


def test_masking_rejects_non_string_input():

    engine, vault = create_engine(
        MaskingRule("person", "literal", "John")
    )

    with pytest.raises(TypeError):
        engine.mask_text(123)


def test_restoration_rejects_non_string_input():

    engine, vault = create_engine(
        MaskingRule("person", "literal", "John")
    )

    with pytest.raises(TypeError):
        engine.unmask_text(123)


def test_overlap_resolution_is_respected():

    engine, vault = create_engine(
        MaskingRule(
            "full_name",
            "literal",
            "John Smith",
            priority=100,
        ),
        MaskingRule(
            "surname",
            "literal",
            "Smith",
            priority=200,
        ),
    )

    result = engine.mask_text("John Smith")

    assert result.replacements_count == 1
    assert result.replacements_by_rule == {"surname": 1}
    assert "John " in result.masked_text