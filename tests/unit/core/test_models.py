import pytest

from document_anonymizer.core.models import (
    MatchCandidate,
    MaskResult,
    RestoreResult,
)


def test_match_candidate_creation():

    candidate = MatchCandidate(
        start=0,
        end=10,
        value="John Smith",
        rule_id="person_name",
        priority=100,
        rule_index=0,
    )

    assert candidate.length == 10
    assert candidate.start == 0
    assert candidate.end == 10


def test_match_candidate_invalid_positions():

    with pytest.raises(ValueError):
        MatchCandidate(
            start=5,
            end=5,
            value="John",
            rule_id="name",
            priority=100,
            rule_index=0,
        )


def test_match_candidate_length_mismatch():

    with pytest.raises(ValueError):
        MatchCandidate(
            start=0,
            end=10,
            value="John",
            rule_id="name",
            priority=100,
            rule_index=0,
        )


def test_mask_result_creation():

    result = MaskResult(
        vault_id="vault-001",
        masked_text="Hello [[MASK_ABC]]",
        replacements_count=1,
        unique_tokens=("[[MASK_ABC]]",),
        replacements_by_rule={"name": 1},
    )

    assert result.replacements_count == 1
    assert result.unique_tokens == ("[[MASK_ABC]]",)
    assert result.replacements_by_rule["name"] == 1


def test_mask_result_mapping_is_immutable():

    result = MaskResult(
        vault_id="vault-001",
        masked_text="Hello",
        replacements_count=0,
    )

    with pytest.raises(TypeError):
        result.replacements_by_rule["name"] = 1


def test_mask_result_negative_count():

    with pytest.raises(ValueError):
        MaskResult(
            vault_id="vault-001",
            masked_text="Hello",
            replacements_count=-1,
        )


def test_restore_result_creation():

    result = RestoreResult(
        vault_id="vault-001",
        restored_text="Hello John",
        restored_count=1,
        unknown_tokens=("[[MASK_UNKNOWN]]",),
    )

    assert result.restored_count == 1
    assert len(result.unknown_tokens) == 1
