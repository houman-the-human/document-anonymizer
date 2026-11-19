import pytest

from document_anonymizer.core.engine import MaskingEngine
from document_anonymizer.core.exceptions import UnsafeNormalizedMatchError
from document_anonymizer.core.normalization import normalize_text
from document_anonymizer.core.rules import MaskingRule, RuleSet
from document_anonymizer.core.vault import MaskingVault


def engine_for(pattern, *, mode="persian", ignore_zwnj=False, kind="literal"):
    rules = RuleSet((MaskingRule("sensitive", kind, pattern),))
    vault = MaskingVault(
        rules,
        normalization=mode,
        ignore_zwnj=ignore_zwnj,
    )
    return MaskingEngine(vault), vault


def test_none_mode_preserves_exact_matching():
    engine, _ = engine_for("علی", mode="none")
    assert engine.mask_text("علي").replacements_count == 0
    assert engine.mask_text("علی").replacements_count == 1


def test_unicode_mode_matches_nfc_against_nfd_and_keeps_original_value():
    engine, vault = engine_for("é", mode="unicode")
    source = "Name: e\u0301!"
    result = engine.mask_text(source)

    assert result.replacements_count == 1
    replacement = result.replacements[0]
    assert replacement.original == "e\u0301"
    assert source[replacement.start:replacement.end] == "e\u0301"
    assert vault.get_original(replacement.token) == "e\u0301"


def test_unicode_mode_does_not_apply_persian_character_equivalence():
    engine, _ = engine_for("علي", mode="unicode")
    assert engine.mask_text("علی").replacements_count == 0


def test_persian_mode_normalizes_arabic_yeh_and_kaf():
    engine, vault = engine_for("علی کاظم")
    source = "مراجعه‌کننده: علي كاظم"
    result = engine.mask_text(source)

    assert result.replacements_count == 1
    assert result.replacements[0].original == "علي كاظم"
    assert vault.get_original(result.replacements[0].token) == "علي كاظم"


def test_persian_mode_matches_arabic_variant_pattern_to_persian_source():
    engine, _ = engine_for("علي كاظم")
    result = engine.mask_text("مشتری: علی کاظم")
    assert result.replacements_count == 1
    assert result.replacements[0].original == "علی کاظم"


def test_zwnj_is_not_optional_by_default():
    engine, _ = engine_for("میروم")
    assert engine.mask_text("می‌روم").replacements_count == 0


def test_zwnj_can_be_ignored_and_original_half_space_is_preserved():
    engine, vault = engine_for("میروم", ignore_zwnj=True)
    source = "فردا می‌روم خانه"
    result = engine.mask_text(source)

    assert result.replacements_count == 1
    replacement = result.replacements[0]
    assert replacement.original == "می‌روم"
    assert source[replacement.start:replacement.end] == "می‌روم"
    assert vault.get_original(replacement.token) == "می‌روم"


def test_zwnj_only_pattern_is_rejected_after_ignoring_zwnj():
    with pytest.raises(ValueError, match="becomes empty"):
        engine_for("\u200c", ignore_zwnj=True)


def test_persian_and_arabic_numerals_are_not_converted():
    engine, _ = engine_for("۱۲۳۴۵")
    result = engine.mask_text("شماره ١٢٣٤٥ و 12345 و ۱۲۳۴۵")

    assert result.replacements_count == 1
    assert result.replacements[0].original == "۱۲۳۴۵"
    assert "١٢٣٤٥" in result.masked_text
    assert "12345" in result.masked_text


def test_mixed_persian_and_english_text_maps_original_offsets():
    engine, _ = engine_for("علي")
    source = "Customer: Alex | نام: علی | ID: A-123"
    result = engine.mask_text(source)
    replacement = result.replacements[0]

    assert source[replacement.start:replacement.end] == "علی"
    assert result.masked_text.startswith("Customer: Alex | نام: ")
    assert result.masked_text.endswith(" | ID: A-123")


def test_repeated_identical_source_values_reuse_token():
    engine, vault = engine_for("علی")
    source = "علي، سپس علي"
    result = engine.mask_text(source)

    assert result.replacements_count == 2
    assert result.replacements[0].token == result.replacements[1].token
    assert [r.original for r in result.replacements] == ["علي", "علي"]
    assert vault.token_count == 1


def test_equivalent_but_differently_spelled_values_keep_distinct_mappings():
    engine, vault = engine_for("علی")
    result = engine.mask_text("علي، سپس علی")

    assert result.replacements_count == 2
    assert result.replacements[0].token != result.replacements[1].token
    assert [r.original for r in result.replacements] == ["علي", "علی"]
    assert vault.token_count == 2


def test_regex_rules_are_normalized_too():
    engine, _ = engine_for(r"علي\s+رضايي", kind="regex")
    result = engine.mask_text("مشتری: علی رضایی")
    assert result.replacements_count == 1
    assert result.replacements[0].original == "علی رضایی"


def test_partial_match_inside_expanded_grapheme_is_rejected():
    # U+0344 NFC-normalizes to two combining marks. A regex matching only
    # one of those normalized marks cannot be mapped to a safe source span.
    engine, _ = engine_for("\u0308", mode="unicode")
    with pytest.raises(UnsafeNormalizedMatchError):
        engine.mask_text("\u0344")


def test_normalized_text_source_spans_cover_composed_cluster():
    view = normalize_text("A e\u0301 B", "unicode")
    index = view.text.index("é")
    assert view.source_span(index, index + 1) == (2, 4)


def test_persian_mode_does_not_collapse_arabic_alef_maksura():
    engine, _ = engine_for("على")
    result = engine.mask_text("علي")
    assert result.replacements_count == 0


def test_invalid_normalization_configuration_is_rejected():
    with pytest.raises(ValueError, match="Unsupported normalization"):
        engine_for("علی", mode="unknown")
    with pytest.raises(ValueError, match="only be enabled"):
        engine_for("علی", mode="unicode", ignore_zwnj=True)
