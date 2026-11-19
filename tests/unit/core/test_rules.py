import pytest

from document_anonymizer.core.rules import (
    MaskingRule,
    RuleSet,
    compile_rule,
)

from document_anonymizer.core.exceptions import (
    RuleValidationError,
    ZeroWidthMatchError,
)


def test_create_literal_rule():

    rule = MaskingRule(
        rule_id="person",
        kind="literal",
        pattern="John Smith",
    )

    assert rule.kind == "literal"
    assert rule.pattern == "John Smith"
    assert rule.enabled is True


def test_literal_special_characters():

    rule = MaskingRule(
        rule_id="email",
        kind="literal",
        pattern="john.smith@example.com",
    )

    compiled = compile_rule(rule)

    assert compiled.search("john.smith@example.com")
    assert not compiled.search("johnXsmith@example.com")


def test_literal_case_insensitive():

    rule = MaskingRule(
        rule_id="name",
        kind="literal",
        pattern="John Smith",
        case_sensitive=False,
    )

    compiled = compile_rule(rule)

    assert compiled.search("JOHN SMITH")
    assert compiled.search("john smith")


def test_create_regex_rule():

    rule = MaskingRule(
        rule_id="phone",
        kind="regex",
        pattern=r"\b\d{10}\b",
    )

    compiled = compile_rule(rule)

    assert compiled.search("Phone: 0912345678")


def test_regex_case_insensitive():

    rule = MaskingRule(
        rule_id="email",
        kind="regex",
        pattern=r"john",
        case_sensitive=False,
    )

    compiled = compile_rule(rule)

    assert compiled.search("JOHN")


def test_regex_multiline_flag():

    rule = MaskingRule(
        rule_id="heading",
        kind="regex",
        pattern=r"^Name:",
        flags=("MULTILINE",),
    )

    compiled = compile_rule(rule)

    assert compiled.search("Hello\nName: John")


def test_empty_pattern_rejected():

    with pytest.raises(RuleValidationError):
        MaskingRule(
            rule_id="empty",
            kind="literal",
            pattern="",
        )


def test_invalid_rule_type_rejected():

    with pytest.raises(RuleValidationError):
        MaskingRule(
            rule_id="invalid",
            kind="unknown",
            pattern="John",
        )


def test_invalid_regex_rejected():

    with pytest.raises(RuleValidationError):
        MaskingRule(
            rule_id="invalid",
            kind="regex",
            pattern="(",
        )


def test_regex_matching_empty_string_rejected():

    with pytest.raises(ZeroWidthMatchError):
        MaskingRule(
            rule_id="empty_match",
            kind="regex",
            pattern=r"a*",
        )


def test_lookahead_pattern_is_allowed():

    rule = MaskingRule(
        rule_id="lookahead",
        kind="regex",
        pattern=r"(?=John)",
    )

    compiled = compile_rule(rule)

    match = compiled.search("John Smith")

    assert match is not None
    assert match.start() == match.end()


def test_unknown_regex_flag_rejected():

    with pytest.raises(RuleValidationError):
        MaskingRule(
            rule_id="invalid_flag",
            kind="regex",
            pattern="John",
            flags=("UNKNOWN",),
        )


def test_literal_rule_does_not_accept_regex_flags():

    with pytest.raises(RuleValidationError):
        MaskingRule(
            rule_id="literal",
            kind="literal",
            pattern="John",
            flags=("MULTILINE",),
        )


def test_duplicate_regex_flags_rejected():

    with pytest.raises(RuleValidationError):
        MaskingRule(
            rule_id="duplicate",
            kind="regex",
            pattern="John",
            flags=("MULTILINE", "MULTILINE"),
        )


def test_boolean_priority_rejected():

    with pytest.raises(RuleValidationError):
        MaskingRule(
            rule_id="invalid_priority",
            kind="literal",
            pattern="John",
            priority=True,
        )


def test_ruleset_preserves_declaration_order():

    first = MaskingRule(
        rule_id="first",
        kind="literal",
        pattern="John",
    )

    second = MaskingRule(
        rule_id="second",
        kind="literal",
        pattern="Smith",
    )

    ruleset = RuleSet((first, second))

    assert ruleset.rules[0].rule_id == "first"
    assert ruleset.rules[1].rule_id == "second"


def test_ruleset_filters_disabled_rules():

    enabled = MaskingRule(
        rule_id="enabled",
        kind="literal",
        pattern="John",
    )

    disabled = MaskingRule(
        rule_id="disabled",
        kind="literal",
        pattern="Smith",
        enabled=False,
    )

    ruleset = RuleSet((enabled, disabled))

    assert len(ruleset.enabled_rules) == 1
    assert ruleset.enabled_rules[0].rule_id == "enabled"


def test_duplicate_rule_ids_rejected():

    first = MaskingRule(
        rule_id="person",
        kind="literal",
        pattern="John",
    )

    second = MaskingRule(
        rule_id="person",
        kind="literal",
        pattern="Smith",
    )

    with pytest.raises(RuleValidationError):
        RuleSet((first, second))


def test_invalid_ruleset_member_rejected():

    with pytest.raises(RuleValidationError):
        RuleSet(("not-a-rule",))
