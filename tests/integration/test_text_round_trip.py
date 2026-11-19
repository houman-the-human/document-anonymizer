
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


def create_engine(
    rules,
    vault_id=None,
    token_factory=None,
):
    vault = MaskingVault(
        ruleset=RuleSet(tuple(rules)),
        vault_id=vault_id,
        token_factory=token_factory,
    )

    return MaskingEngine(vault), vault


def test_complete_literal_and_regex_round_trip():

    engine, vault = create_engine([
        MaskingRule(
            "person",
            "literal",
            "Jane Doe",
        ),
        MaskingRule(
            "account",
            "regex",
            r"\bACCT-\d{6}\b",
        ),
    ])

    original = (
        "Jane Doe signed ACCT-123456. "
        "Jane Doe received another copy."
    )

    masked = engine.mask_text(original)

    assert masked.replacements_count == 3
    assert len(masked.unique_tokens) == 2

    assert "Jane Doe" not in masked.masked_text
    assert "ACCT-123456" not in masked.masked_text

    restored = engine.unmask_text(masked.masked_text)

    assert restored.restored_text == original
    assert restored.restored_count == 3
    assert restored.unknown_tokens == ()


def test_unicode_whitespace_and_punctuation_round_trip():

    engine, vault = create_engine([
        MaskingRule(
            "company",
            "literal",
            "東京株式会社",
        ),
        MaskingRule(
            "person",
            "literal",
            "علی رضایی",
        ),
    ])

    original = (
        "شرکت 東京株式会社\n"
        "\tمدیر: علی رضایی!\n"
        "End."
    )

    masked = engine.mask_text(original)

    restored = engine.unmask_text(masked.masked_text)

    assert restored.restored_text == original


def test_same_token_reused_across_separate_calls():

    engine, vault = create_engine([
        MaskingRule(
            "person",
            "literal",
            "Jane Doe",
        )
    ])

    first = engine.mask_text("Jane Doe works here.")

    second = engine.mask_text("Jane Doe signed the contract.")

    first_token = first.unique_tokens[0]
    second_token = second.unique_tokens[0]

    assert first_token == second_token

    assert engine.unmask_text(
        first.masked_text
    ).restored_text == "Jane Doe works here."

    assert engine.unmask_text(
        second.masked_text
    ).restored_text == "Jane Doe signed the contract."


def test_existing_source_token_collision():

    token_a = "A" * 32
    token_b = "B" * 32

    generated_values = iter([
        token_a,
        token_b,
    ])

    engine, vault = create_engine(
        [
            MaskingRule(
                "person",
                "literal",
                "Jane Doe",
            )
        ],
        token_factory=lambda: next(generated_values),
    )

    existing_token = f"[[MASK_{token_a}]]"

    original = f"Existing: {existing_token}; Person: Jane Doe"

    masked = engine.mask_text(original)

    generated_token = masked.unique_tokens[0]

    assert generated_token == f"[[M_{token_b[:13]}]]"

    restored = engine.unmask_text(masked.masked_text)

    assert restored.restored_text == original
    assert restored.unknown_tokens == (existing_token,)


def test_unknown_tokens_are_preserved_and_reported_once():

    engine, vault = create_engine([
        MaskingRule(
            "person",
            "literal",
            "Jane Doe",
        )
    ])

    unknown = "[[MASK_00000000000000000000000000000000]]"

    original = f"{unknown} and {unknown}"

    result = engine.unmask_text(original)

    assert result.restored_text == original
    assert result.restored_count == 0
    assert result.unknown_tokens == (unknown,)


def test_different_vaults_cannot_restore_each_others_tokens():

    engine_a, vault_a = create_engine(
        [
            MaskingRule("person", "literal", "Jane Doe")
        ],
        vault_id="vault-a",
        token_factory=lambda: "A" * 32,
    )

    engine_b, vault_b = create_engine(
        [
            MaskingRule("person", "literal", "Jane Doe")
        ],
        vault_id="vault-b",
        token_factory=lambda: "B" * 32,
    )

    masked = engine_a.mask_text("Jane Doe")

    result = engine_b.unmask_text(masked.masked_text)

    assert result.restored_text == masked.masked_text
    assert result.restored_count == 0
    assert result.unknown_tokens == masked.unique_tokens


def test_no_matching_rules_preserve_original_document():

    engine, vault = create_engine([
        MaskingRule(
            "person",
            "literal",
            "Jane Doe",
        )
    ])

    original = (
        "This document contains no matching values.\n"
        "It should remain exactly the same."
    )

    masked = engine.mask_text(original)

    assert masked.masked_text == original
    assert masked.replacements_count == 0

    restored = engine.unmask_text(masked.masked_text)

    assert restored.restored_text == original


def test_case_insensitive_matching_preserves_exact_original_values():

    engine, vault = create_engine([
        MaskingRule(
            "person",
            "literal",
            "jane doe",
            case_sensitive=False,
        )
    ])

    original = "Jane Doe and JANE DOE"

    masked = engine.mask_text(original)

    # Different matched strings have different mappings,
    # even though the matching rule is case-insensitive.
    assert len(masked.unique_tokens) == 2

    restored = engine.unmask_text(masked.masked_text)

    assert restored.restored_text == original


def test_original_value_can_contain_token_syntax():

    original_token = "[[MASK_" + ("B" * 32) + "]]"

    engine, vault = create_engine(
        [
            MaskingRule(
                "special",
                "literal",
                original_token,
            )
        ],
        token_factory=lambda: "A" * 32,
    )

    masked = engine.mask_text(original_token)

    assert masked.masked_text != original_token

    restored = engine.unmask_text(masked.masked_text)

    assert restored.restored_text == original_token
    assert restored.unknown_tokens == ()


def test_already_masked_document_is_not_masked_again():

    engine, vault = create_engine([
        MaskingRule(
            "person",
            "literal",
            "Jane Doe",
        )
    ])

    masked = engine.mask_text("Jane Doe")

    with pytest.raises(AlreadyMaskedInputError):
        engine.mask_text(masked.masked_text)
