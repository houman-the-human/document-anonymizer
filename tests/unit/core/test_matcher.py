import pytest

from types import SimpleNamespace

from document_anonymizer.core.matcher import Matcher
from document_anonymizer.core.rules import (
    MaskingRule,
    RuleSet,
)
from document_anonymizer.core.exceptions import (
    RegexTimeoutError,
    ZeroWidthMatchError,
)


def create_matcher(*rules):
    return Matcher(RuleSet(tuple(rules)))


def test_literal_matching():

    matcher = create_matcher(
        MaskingRule(
            "name",
            "literal",
            "John Smith",
        )
    )

    matches = matcher.find_matches(
        "Hello John Smith"
    )

    assert len(matches) == 1
    assert matches[0].value == "John Smith"


def test_regex_matching():

    matcher = create_matcher(
        MaskingRule(
            "phone",
            "regex",
            r"\b\d{10}\b",
        )
    )

    matches = matcher.find_matches(
        "Phone: 0912345678"
    )

    assert len(matches) == 1
    assert matches[0].value == "0912345678"


def test_literal_and_regex_together():

    matcher = create_matcher(
        MaskingRule(
            "name",
            "literal",
            "John Smith",
        ),
        MaskingRule(
            "phone",
            "regex",
            r"\b\d{10}\b",
        ),
    )

    matches = matcher.find_matches(
        "John Smith: 0912345678"
    )

    assert len(matches) == 2


def test_longest_match_wins():

    matcher = create_matcher(
        MaskingRule(
            "short",
            "literal",
            "John",
        ),
        MaskingRule(
            "long",
            "literal",
            "John Smith",
        ),
    )

    matches = matcher.find_matches("John Smith")

    assert len(matches) == 1
    assert matches[0].value == "John Smith"
    assert matches[0].rule_id == "long"


def test_higher_priority_wins_over_longer_match():

    matcher = create_matcher(
        MaskingRule(
            "long",
            "literal",
            "John Smith",
            priority=100,
        ),
        MaskingRule(
            "short",
            "literal",
            "Smith",
            priority=200,
        ),
    )

    matches = matcher.find_matches("John Smith")

    assert len(matches) == 1
    assert matches[0].value == "Smith"
    assert matches[0].rule_id == "short"


def test_earlier_position_wins():

    matcher = create_matcher(
        MaskingRule(
            "first",
            "literal",
            "abc",
        ),
        MaskingRule(
            "second",
            "literal",
            "bcd",
        ),
    )

    matches = matcher.find_matches("abcde")

    assert len(matches) == 1
    assert matches[0].value == "abc"


def test_earlier_rule_declaration_wins():

    matcher = create_matcher(
        MaskingRule(
            "first",
            "literal",
            "John",
        ),
        MaskingRule(
            "second",
            "regex",
            r"J.hn",
        ),
    )

    matches = matcher.find_matches("John")

    assert len(matches) == 1
    assert matches[0].rule_id == "first"


def test_multiple_non_overlapping_matches():

    matcher = create_matcher(
        MaskingRule("john", "literal", "John"),
        MaskingRule("sarah", "literal", "Sarah"),
    )

    matches = matcher.find_matches(
        "John works with Sarah"
    )

    assert len(matches) == 2

    assert [
        match.value for match in matches
    ] == ["John", "Sarah"]


def test_overlapping_matches_from_same_regex():

    matcher = create_matcher(
        MaskingRule(
            "ana",
            "regex",
            "ana",
        )
    )

    candidates = matcher.find_candidates("banana")

    assert len(candidates) == 2

    assert [
        candidate.start for candidate in candidates
    ] == [1, 3]

    matches = matcher.resolve_overlaps(candidates)

    assert len(matches) == 1
    assert matches[0].value == "ana"
    assert matches[0].start == 1


def test_disabled_rule_is_ignored():

    matcher = create_matcher(
        MaskingRule(
            "disabled",
            "literal",
            "John",
            enabled=False,
        )
    )

    assert matcher.find_matches("John") == ()


def test_case_insensitive_matching_preserves_original():

    matcher = create_matcher(
        MaskingRule(
            "name",
            "literal",
            "john",
            case_sensitive=False,
        )
    )

    matches = matcher.find_matches("JOHN")

    assert len(matches) == 1
    assert matches[0].value == "JOHN"


def test_literal_special_characters():

    matcher = create_matcher(
        MaskingRule(
            "email",
            "literal",
            "john.smith@example.com",
        )
    )

    matches = matcher.find_matches(
        "Contact john.smith@example.com"
    )

    assert len(matches) == 1
    assert matches[0].value == "john.smith@example.com"


def test_regex_flags_are_applied():

    matcher = create_matcher(
        MaskingRule(
            "heading",
            "regex",
            r"^Name:",
            flags=("MULTILINE",),
        )
    )

    matches = matcher.find_matches(
        "Hello\nName: John"
    )

    assert len(matches) == 1
    assert matches[0].value == "Name:"


def test_matches_are_returned_in_source_order():

    matcher = create_matcher(
        MaskingRule("sarah", "literal", "Sarah"),
        MaskingRule("john", "literal", "John"),
    )

    matches = matcher.find_matches(
        "John and Sarah"
    )

    assert [
        match.value for match in matches
    ] == ["John", "Sarah"]


def test_candidate_positions_are_correct():

    matcher = create_matcher(
        MaskingRule(
            "name",
            "literal",
            "John",
        )
    )

    matches = matcher.find_matches(
        "Hello John Smith"
    )

    assert matches[0].start == 6
    assert matches[0].end == 10


def test_empty_input_returns_no_matches():

    matcher = create_matcher(
        MaskingRule(
            "name",
            "literal",
            "John",
        )
    )

    assert matcher.find_matches("") == ()


def test_no_rules_returns_no_matches():

    matcher = Matcher(RuleSet(()))

    assert matcher.find_matches("John Smith") == ()


def test_zero_width_match_on_actual_text_is_rejected():

    matcher = create_matcher(
        MaskingRule(
            "lookahead",
            "regex",
            r"(?=John)",
        )
    )

    with pytest.raises(ZeroWidthMatchError):
        matcher.find_matches("John Smith")


def test_non_string_input_is_rejected():

    matcher = create_matcher(
        MaskingRule(
            "name",
            "literal",
            "John",
        )
    )

    with pytest.raises(TypeError):
        matcher.find_matches(123)


def test_invalid_timeout_is_rejected():

    with pytest.raises(ValueError):
        Matcher(
            RuleSet(()),
            regex_timeout_seconds=0,
        )

def test_nan_timeout_is_rejected():

    with pytest.raises(ValueError):
        Matcher(
            RuleSet(()),
            regex_timeout_seconds=float("nan"),
        )


def test_resolve_empty_candidates():

    matcher = Matcher(RuleSet(()))

    assert matcher.resolve_overlaps(()) == ()


def test_regex_timeout_is_translated():

    matcher = create_matcher(
        MaskingRule("slow", "regex", "test")
    )

    class TimeoutPattern:

        def finditer(self, *args, **kwargs):
            raise TimeoutError("Simulated timeout")

    matcher._compiled_rules = (
        SimpleNamespace(
            rule_id="slow",
            pattern=TimeoutPattern(),
        ),
    )

    with pytest.raises(RegexTimeoutError):
        matcher.find_candidates("test")
