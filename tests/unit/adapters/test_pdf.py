import pytest
import pymupdf

from document_anonymizer.adapters.pdf import (
    PDFCharacter,
    PDFEncryptedError,
    PDFExtractionError,
    PDFTextDocument,
    extract_pdf_text,
)

from document_anonymizer.adapters.pdf import (
    PDFCharacter,
    PDFPageText,
    PDFTextDocument,
    PDFCoordinateMappingError,
    build_pdf_masking_plan,
)

from document_anonymizer.adapters.pdf import (
    PDFRedactionError,
    PDFSourceChangedError,
    redact_pdf,
)

from document_anonymizer.core.engine import MaskingEngine

from document_anonymizer.core.rules import (
    MaskingRule,
    RuleSet,
)

from document_anonymizer.core.vault import MaskingVault


import hashlib
import os

from dataclasses import replace
from pathlib import Path


from document_anonymizer.adapters.pdf import (
    PDFTokenInsertionError,
    PDFTokenFontError,
    PDFRestorationError,
    insert_pdf_tokens,
    restore_pdf,
)


def create_pdf(path, page_contents):

    document = pymupdf.open()

    for content in page_contents:

        page = document.new_page(
            width=300,
            height=200,
        )

        if content:
            page.insert_text(
                (72, 72),
                content,
                fontsize=12,
            )

    document.save(path)
    document.close()

    return path


def test_extract_simple_pdf_text(tmp_path):

    path = create_pdf(
        tmp_path / "sample.pdf",
        ["Jane Doe"],
    )

    result = extract_pdf_text(path)

    assert isinstance(result, PDFTextDocument)
    assert result.page_count == 1
    assert result.pages[0].text == "Jane Doe"


def test_extract_character_coordinates(tmp_path):

    path = create_pdf(
        tmp_path / "coordinates.pdf",
        ["Jane Doe"],
    )

    result = extract_pdf_text(path)

    page = result.pages[0]

    assert len(page.characters) == 8

    for character in page.characters:

        assert isinstance(character, PDFCharacter)

        assert (
            page.text[character.start:character.end]
            == character.text
        )

        x0, y0, x1, y1 = character.bbox

        assert x1 > x0
        assert y1 > y0


def test_character_positions_are_contiguous(tmp_path):

    path = create_pdf(
        tmp_path / "positions.pdf",
        ["Jane Doe"],
    )

    page = extract_pdf_text(path).pages[0]

    for previous, current in zip(
        page.characters,
        page.characters[1:],
    ):

        assert previous.end == current.start


def test_font_information_is_extracted(tmp_path):

    path = create_pdf(
        tmp_path / "fonts.pdf",
        ["Jane Doe"],
    )

    page = extract_pdf_text(path).pages[0]

    first_character = page.characters[0]

    assert first_character.font_name
    assert first_character.font_size == pytest.approx(12)


def test_multiple_pages_are_extracted_independently(tmp_path):

    path = create_pdf(
        tmp_path / "multiple.pdf",
        [
            "Jane Doe",
            "John Smith",
        ],
    )

    result = extract_pdf_text(path)

    assert result.page_count == 2

    assert result.pages[0].page_number == 1
    assert result.pages[1].page_number == 2

    assert result.pages[0].text == "Jane Doe"
    assert result.pages[1].text == "John Smith"


def test_page_boundaries_are_preserved(tmp_path):

    path = create_pdf(
        tmp_path / "boundaries.pdf",
        [
            "Jane",
            "Doe",
        ],
    )

    result = extract_pdf_text(path)

    assert result.pages[0].text == "Jane"
    assert result.pages[1].text == "Doe"

    assert result.pages[0].text + result.pages[1].text == "JaneDoe"


def test_pages_without_text_are_reported(tmp_path):

    path = create_pdf(
        tmp_path / "empty-page.pdf",
        [
            "Jane Doe",
            "",
        ],
    )

    result = extract_pdf_text(path)

    assert result.page_count == 2

    assert result.pages_without_text == (2,)

    assert result.has_extractable_text


def test_entirely_empty_pdf_is_reported(tmp_path):

    path = create_pdf(
        tmp_path / "empty.pdf",
        [
            "",
        ],
    )

    result = extract_pdf_text(path)

    assert not result.has_extractable_text

    assert result.pages_without_text == (1,)

    assert result.total_character_count == 0


def test_total_character_count(tmp_path):

    path = create_pdf(
        tmp_path / "count.pdf",
        [
            "Jane",
            "Smith",
        ],
    )

    result = extract_pdf_text(path)

    assert result.total_character_count == 9


def test_page_dimensions_are_extracted(tmp_path):

    path = create_pdf(
        tmp_path / "dimensions.pdf",
        ["Jane Doe"],
    )

    page = extract_pdf_text(path).pages[0]

    assert page.width == pytest.approx(300)
    assert page.height == pytest.approx(200)
    assert page.rotation == 0


def test_source_path_is_absolute(tmp_path):

    path = create_pdf(
        tmp_path / "source.pdf",
        ["Jane Doe"],
    )

    result = extract_pdf_text(path)

    assert result.source_path.is_absolute()


def test_missing_pdf_raises_error(tmp_path):

    with pytest.raises(FileNotFoundError):

        extract_pdf_text(
            tmp_path / "missing.pdf"
        )


def test_invalid_pdf_raises_error(tmp_path):

    path = tmp_path / "invalid.pdf"

    path.write_bytes(
        b"This is not a valid PDF"
    )

    with pytest.raises(PDFExtractionError):

        extract_pdf_text(path)


def test_non_pdf_file_raises_error(tmp_path):

    path = tmp_path / "sample.txt"

    path.write_text(
        "Jane Doe",
        encoding="utf-8",
    )

    with pytest.raises(PDFExtractionError):

        extract_pdf_text(path)


def test_invalid_source_type():

    with pytest.raises(TypeError):

        extract_pdf_text(123)


def test_invalid_sort_type(tmp_path):

    path = create_pdf(
        tmp_path / "sort.pdf",
        ["Jane Doe"],
    )

    with pytest.raises(TypeError):

        extract_pdf_text(
            path,
            sort="yes",
        )


def test_password_protected_pdf_requires_password(tmp_path):

    path = tmp_path / "protected.pdf"

    document = pymupdf.open()

    page = document.new_page()

    page.insert_text(
        (72, 72),
        "Jane Doe",
    )

    document.save(
        path,
        encryption=pymupdf.PDF_ENCRYPT_AES_256,
        owner_pw="owner-password",
        user_pw="user-password",
    )

    document.close()

    with pytest.raises(PDFEncryptedError):

        extract_pdf_text(path)


def test_password_protected_pdf_accepts_correct_password(tmp_path):

    path = tmp_path / "protected.pdf"

    document = pymupdf.open()

    page = document.new_page()

    page.insert_text(
        (72, 72),
        "Jane Doe",
    )

    document.save(
        path,
        encryption=pymupdf.PDF_ENCRYPT_AES_256,
        owner_pw="owner-password",
        user_pw="user-password",
    )

    document.close()

    result = extract_pdf_text(
        path,
        password="user-password",
    )

    assert result.pages[0].text == "Jane Doe"


def test_wrong_pdf_password_is_rejected(tmp_path):

    path = tmp_path / "protected.pdf"

    document = pymupdf.open()

    page = document.new_page()

    page.insert_text(
        (72, 72),
        "Jane Doe",
    )

    document.save(
        path,
        encryption=pymupdf.PDF_ENCRYPT_AES_256,
        owner_pw="owner-password",
        user_pw="user-password",
    )

    document.close()

    with pytest.raises(PDFEncryptedError):

        extract_pdf_text(
            path,
            password="wrong-password",
        )


def test_multiline_character_offsets_include_newlines(tmp_path):

    path = tmp_path / "multiline.pdf"

    document = pymupdf.open()
    page = document.new_page(
        width=300,
        height=200,
    )

    page.insert_text(
        (72, 72),
        "Jane",
    )

    page.insert_text(
        (72, 100),
        "Doe",
    )

    document.save(path)
    document.close()

    result = extract_pdf_text(path)

    extracted_page = result.pages[0]

    assert extracted_page.text == "Jane\nDoe"

    for character in extracted_page.characters:

        assert (
            extracted_page.text[
                character.start:character.end
            ]
            == character.text
        )

    doe_start = extracted_page.text.index("Doe")

    doe_character = next(
        character
        for character in extracted_page.characters
        if character.text == "D"
    )

    assert doe_character.start == doe_start


def create_masking_engine(*rules):

    vault = MaskingVault(
        ruleset=RuleSet(tuple(rules))
    )

    return MaskingEngine(vault), vault


def create_multiline_pdf(path, pages):

    document = pymupdf.open()

    for lines in pages:

        page = document.new_page(
            width=400,
            height=300,
        )

        for index, line in enumerate(lines):

            page.insert_text(
                (72, 72 + index * 25),
                line,
                fontsize=12,
            )

    document.save(path)
    document.close()

    return path


def test_pdf_literal_match_maps_to_character_boxes(tmp_path):

    path = create_pdf(
        tmp_path / "literal.pdf",
        ["Customer: Jane Doe"],
    )

    document = extract_pdf_text(path)

    engine, vault = create_masking_engine(
        MaskingRule(
            "person",
            "literal",
            "Jane Doe",
        )
    )

    plan = build_pdf_masking_plan(
        document,
        engine,
    )

    assert plan.total_replacements == 1

    replacement = plan.pages[0].replacements[0]

    assert replacement.original == "Jane Doe"
    assert replacement.rule_id == "person"
    assert len(replacement.character_boxes) == 8

    assert replacement.token in plan.pages[0].masked_text

    assert "Jane Doe" not in plan.pages[0].masked_text


def test_pdf_regex_match_maps_to_character_boxes(tmp_path):

    path = create_pdf(
        tmp_path / "regex.pdf",
        ["Phone: 0912345678"],
    )

    document = extract_pdf_text(path)

    engine, vault = create_masking_engine(
        MaskingRule(
            "phone",
            "regex",
            r"\b\d{10}\b",
        )
    )

    plan = build_pdf_masking_plan(
        document,
        engine,
    )

    replacement = plan.pages[0].replacements[0]

    assert replacement.original == "0912345678"
    assert replacement.rule_id == "phone"
    assert len(replacement.character_boxes) == 10


def test_repeated_values_reuse_token_but_have_separate_coordinates(tmp_path):

    path = create_pdf(
        tmp_path / "repeated.pdf",
        ["Jane Doe and Jane Doe"],
    )

    document = extract_pdf_text(path)

    engine, vault = create_masking_engine(
        MaskingRule(
            "person",
            "literal",
            "Jane Doe",
        )
    )

    plan = build_pdf_masking_plan(
        document,
        engine,
    )

    replacements = plan.pages[0].replacements

    assert len(replacements) == 2

    assert replacements[0].token == replacements[1].token

    assert replacements[0].start != replacements[1].start

    assert replacements[0].character_boxes != (
        replacements[1].character_boxes
    )


def test_multiple_pages_are_mapped_independently(tmp_path):

    path = create_pdf(
        tmp_path / "multiple-pages.pdf",
        [
            "Jane Doe",
            "Jane Doe",
        ],
    )

    document = extract_pdf_text(path)

    engine, vault = create_masking_engine(
        MaskingRule(
            "person",
            "literal",
            "Jane Doe",
        )
    )

    plan = build_pdf_masking_plan(
        document,
        engine,
    )

    assert plan.total_replacements == 2

    first = plan.pages[0].replacements[0]
    second = plan.pages[1].replacements[0]

    assert first.page_number == 1
    assert second.page_number == 2

    assert first.token == second.token


def test_spaces_are_included_in_character_mapping(tmp_path):

    path = create_pdf(
        tmp_path / "spaces.pdf",
        ["Jane Doe"],
    )

    document = extract_pdf_text(path)

    engine, vault = create_masking_engine(
        MaskingRule(
            "person",
            "literal",
            "Jane Doe",
        )
    )

    plan = build_pdf_masking_plan(
        document,
        engine,
    )

    replacement = plan.pages[0].replacements[0]

    assert len(replacement.character_boxes) == len("Jane Doe")


def test_match_spanning_multiple_lines_maps_both_lines(tmp_path):

    path = create_multiline_pdf(
        tmp_path / "multiline-match.pdf",
        [
            [
                "Jane Doe",
                "Phone: 0912345678",
            ]
        ],
    )

    document = extract_pdf_text(path)

    assert document.pages[0].text == (
        "Jane Doe\nPhone: 0912345678"
    )

    engine, vault = create_masking_engine(
        MaskingRule(
            "combined",
            "literal",
            "Jane Doe\nPhone: 0912345678",
        )
    )

    plan = build_pdf_masking_plan(
        document,
        engine,
    )

    replacement = plan.pages[0].replacements[0]

    assert replacement.original == (
        "Jane Doe\nPhone: 0912345678"
    )

    # Newline has no glyph box.
    assert len(replacement.character_boxes) == (
        len(replacement.original) - 1
    )


def test_no_matches_preserve_extracted_text(tmp_path):

    path = create_pdf(
        tmp_path / "no-match.pdf",
        ["Nothing to mask"],
    )

    document = extract_pdf_text(path)

    engine, vault = create_masking_engine(
        MaskingRule(
            "person",
            "literal",
            "Jane Doe",
        )
    )

    plan = build_pdf_masking_plan(
        document,
        engine,
    )

    assert plan.total_replacements == 0

    assert (
        plan.pages[0].masked_text
        == plan.pages[0].original_text
    )


def test_partial_multicodepoint_glyph_match_is_rejected(tmp_path):

    page = PDFPageText(
        page_number=1,
        text="AB",
        characters=(
            PDFCharacter(
                text="AB",
                start=0,
                end=2,
                bbox=(10.0, 10.0, 30.0, 20.0),
                origin=(10.0, 20.0),
                font_name="TestFont",
                font_size=12.0,
                font_flags=0,
                color=0,
            ),
        ),
        width=300,
        height=200,
        rotation=0,
    )

    document = PDFTextDocument(
        source_path=tmp_path / "synthetic.pdf",
        pages=(page,),
    )

    engine, vault = create_masking_engine(
        MaskingRule(
            "partial",
            "literal",
            "A",
        )
    )

    with pytest.raises(PDFCoordinateMappingError):

        build_pdf_masking_plan(
            document,
            engine,
        )

    # Preflight should fail before creating mappings.
    assert vault.token_count == 0


def test_missing_character_geometry_is_rejected(tmp_path):

    page = PDFPageText(
        page_number=1,
        text="Jane Doe",
        characters=(),
        width=300,
        height=200,
        rotation=0,
    )

    document = PDFTextDocument(
        source_path=tmp_path / "synthetic.pdf",
        pages=(page,),
    )

    engine, vault = create_masking_engine(
        MaskingRule(
            "person",
            "literal",
            "Jane Doe",
        )
    )

    with pytest.raises(PDFCoordinateMappingError):

        build_pdf_masking_plan(
            document,
            engine,
        )

    assert vault.token_count == 0


def create_pdf_masking_plan(path, pattern="Jane Doe"):

    document = extract_pdf_text(path)

    engine, vault = create_masking_engine(
        MaskingRule(
            "sensitive",
            "literal",
            pattern,
        )
    )

    plan = build_pdf_masking_plan(
        document,
        engine,
    )

    return plan, vault


def test_redact_pdf_removes_original_text(tmp_path):

    source = create_pdf(
        tmp_path / "source.pdf",
        ["Customer: Jane Doe"],
    )

    plan, vault = create_pdf_masking_plan(source)

    output = tmp_path / "redacted.pdf"

    result = redact_pdf(plan, output)

    assert result.exists()

    masked = extract_pdf_text(result)

    assert "Jane Doe" not in masked.pages[0].text
    assert "Customer:" in masked.pages[0].text


def test_redact_pdf_preserves_unmatched_text(tmp_path):

    source = create_pdf(
        tmp_path / "source.pdf",
        ["Customer: Jane Doe | Account: 12345"],
    )

    plan, vault = create_pdf_masking_plan(source)

    output = tmp_path / "redacted.pdf"

    redact_pdf(plan, output)

    masked = extract_pdf_text(output)

    text = masked.pages[0].text

    assert "Jane Doe" not in text
    assert "Account:" in text
    assert "12345" in text


def test_redact_pdf_handles_repeated_occurrences(tmp_path):

    source = create_pdf(
        tmp_path / "source.pdf",
        ["Jane Doe and Jane Doe"],
    )

    plan, vault = create_pdf_masking_plan(source)

    assert plan.total_replacements == 2

    output = tmp_path / "redacted.pdf"

    redact_pdf(plan, output)

    masked = extract_pdf_text(output)

    assert "Jane Doe" not in masked.pages[0].text
    assert "and" in masked.pages[0].text


def test_redact_pdf_does_not_modify_source(tmp_path):

    source = create_pdf(
        tmp_path / "source.pdf",
        ["Customer: Jane Doe"],
    )

    plan, vault = create_pdf_masking_plan(source)

    output = tmp_path / "redacted.pdf"

    redact_pdf(plan, output)

    original = extract_pdf_text(source)

    assert "Jane Doe" in original.pages[0].text


def test_redact_pdf_preserves_page_geometry(tmp_path):

    source = create_pdf(
        tmp_path / "source.pdf",
        ["Customer: Jane Doe"],
    )

    original = extract_pdf_text(source)

    plan, vault = create_pdf_masking_plan(source)

    output = tmp_path / "redacted.pdf"

    redact_pdf(plan, output)

    masked = extract_pdf_text(output)

    assert masked.page_count == original.page_count

    assert masked.pages[0].width == pytest.approx(
        original.pages[0].width
    )

    assert masked.pages[0].height == pytest.approx(
        original.pages[0].height
    )


def test_redact_pdf_rejects_modified_source(tmp_path):

    source = create_pdf(
        tmp_path / "source.pdf",
        ["Customer: Jane Doe"],
    )

    plan, vault = create_pdf_masking_plan(source)

    # Modify the source after creating the plan.
    document = pymupdf.open(source)

    document[0].insert_text(
        (72, 120),
        "Additional content",
    )

    modified = tmp_path / "modified.pdf"

    document.save(modified)
    document.close()

    modified.replace(source)

    with pytest.raises(PDFSourceChangedError):

        redact_pdf(
            plan,
            tmp_path / "redacted.pdf",
        )


def test_redact_pdf_rejects_same_source_and_output(tmp_path):

    source = create_pdf(
        tmp_path / "source.pdf",
        ["Customer: Jane Doe"],
    )

    plan, vault = create_pdf_masking_plan(source)

    with pytest.raises(ValueError):

        redact_pdf(
            plan,
            source,
        )


def test_redact_pdf_without_matches_preserves_text(tmp_path):

    source = create_pdf(
        tmp_path / "source.pdf",
        ["Customer: Jane Doe"],
    )

    plan, vault = create_pdf_masking_plan(
        source,
        pattern="Not Present",
    )

    assert plan.total_replacements == 0

    output = tmp_path / "redacted.pdf"

    redact_pdf(plan, output)

    masked = extract_pdf_text(output)

    assert masked.pages[0].text == "Customer: Jane Doe"


def test_redact_pdf_handles_multiline_matches(tmp_path):

    source = create_multiline_pdf(
        tmp_path / "multiline.pdf",
        [
            [
                "Jane Doe",
                "Phone: 0912345678",
                "Address: New York",
            ]
        ],
    )

    document = extract_pdf_text(source)

    engine, vault = create_masking_engine(
        MaskingRule(
            "sensitive",
            "literal",
            "Jane Doe\nPhone: 0912345678",
        )
    )

    plan = build_pdf_masking_plan(
        document,
        engine,
    )

    output = tmp_path / "redacted.pdf"

    redact_pdf(plan, output)

    masked = extract_pdf_text(output)

    text = masked.pages[0].text

    assert "Jane Doe" not in text
    assert "0912345678" not in text
    assert "Address: New York" in text


def replace_first_plan_token(plan, token):

    page_plan = plan.pages[0]

    replacements = list(
        page_plan.replacements
    )

    replacements[0] = replace(
        replacements[0],
        token=token,
    )

    updated_page = replace(
        page_plan,
        replacements=tuple(replacements),
    )

    pages = list(plan.pages)
    pages[0] = updated_page

    return replace(
        plan,
        pages=tuple(pages),
    )



def test_insert_pdf_tokens_inserts_token(tmp_path):

    source = create_pdf(
        tmp_path / "source.pdf",
        ["Customer: Jane Doe"],
    )

    plan, vault = create_pdf_masking_plan(source)

    redacted = tmp_path / "redacted.pdf"
    output = tmp_path / "masked.pdf"

    redact_pdf(plan, redacted)

    insert_pdf_tokens(
        redacted,
        plan,
        output,
    )

    page = extract_pdf_text(output).pages[0]

    token = plan.pages[0].replacements[0].token

    assert token in page.text
    assert "Jane Doe" not in page.text
    assert "Customer:" in page.text


def test_insert_pdf_tokens_adjusts_font_size(tmp_path):

    source = create_pdf(
        tmp_path / "source.pdf",
        ["Jane Doe"],
    )

    plan, vault = create_pdf_masking_plan(source)

    long_token = "[[MASK_ABCDEFGH]]"

    plan = replace_first_plan_token(
        plan,
        long_token,
    )

    original_font_size = (
        plan.pages[0]
        .replacements[0]
        .characters[0]
        .font_size
    )

    redacted = tmp_path / "redacted.pdf"
    output = tmp_path / "masked.pdf"

    redact_pdf(plan, redacted)

    insert_pdf_tokens(
        redacted,
        plan,
        output,
    )

    page = extract_pdf_text(output).pages[0]

    token_start = page.text.index(long_token)

    token_characters = [
        character
        for character in page.characters
        if (
            character.start >= token_start
            and character.end <= token_start + len(long_token)
        )
    ]

    assert token_characters

    inserted_font_size = max(
        character.font_size
        for character in token_characters
    )

    assert inserted_font_size < original_font_size


def test_insert_pdf_tokens_handles_repeated_values(tmp_path):

    source = create_pdf(
        tmp_path / "source.pdf",
        ["Jane Doe and Jane Doe"],
    )

    plan, vault = create_pdf_masking_plan(source)

    redacted = tmp_path / "redacted.pdf"
    output = tmp_path / "masked.pdf"

    redact_pdf(plan, redacted)

    insert_pdf_tokens(
        redacted,
        plan,
        output,
    )

    page = extract_pdf_text(output).pages[0]

    token = plan.pages[0].replacements[0].token

    assert len(plan.pages[0].replacements) == 2

    assert (
        plan.pages[0].replacements[0].token
        == plan.pages[0].replacements[1].token
    )

    assert page.text.count(token) == 2
    assert "Jane Doe" not in page.text


def test_insert_pdf_tokens_handles_multiline_match(tmp_path):

    source = create_multiline_pdf(
        tmp_path / "multiline.pdf",
        [
            [
                "Jane Doe",
                "Phone: 0912345678",
                "Address: New York",
            ]
        ],
    )

    document = extract_pdf_text(source)

    engine, vault = create_masking_engine(
        MaskingRule(
            "sensitive",
            "literal",
            "Jane Doe\nPhone: 0912345678",
        )
    )

    plan = build_pdf_masking_plan(
        document,
        engine,
    )

    redacted = tmp_path / "redacted.pdf"
    output = tmp_path / "masked.pdf"

    redact_pdf(plan, redacted)

    insert_pdf_tokens(
        redacted,
        plan,
        output,
    )

    page = extract_pdf_text(output).pages[0]

    token = plan.pages[0].replacements[0].token

    assert page.text.count(token) == 1

    assert "Jane Doe" not in page.text
    assert "0912345678" not in page.text

    assert "Address: New York" in page.text


def test_unicode_token_requires_custom_font(tmp_path):

    source = create_pdf(
        tmp_path / "source.pdf",
        ["Jane Doe"],
    )

    plan, vault = create_pdf_masking_plan(source)

    plan = replace_first_plan_token(
        plan,
        "[[MASK_中]]",
    )

    redacted = tmp_path / "redacted.pdf"
    output = tmp_path / "masked.pdf"

    redact_pdf(plan, redacted)

    with pytest.raises(PDFTokenFontError):

        insert_pdf_tokens(
            redacted,
            plan,
            output,
        )


def test_unicode_token_works_with_custom_font(tmp_path):

    font_path = (
        Path(os.environ.get("WINDIR", r"C:\Windows"))
        / "Fonts"
        / "arial.ttf"
    )

    if not font_path.is_file():
        pytest.skip("Windows Arial font is unavailable")

    font = pymupdf.Font(
        fontfile=str(font_path)
    )

    if not font.has_glyph(ord("Ω")):
        pytest.skip("Test font does not support Omega")

    source = create_pdf(
        tmp_path / "source.pdf",
        ["Jane Doe"],
    )

    plan, vault = create_pdf_masking_plan(source)

    token = "[[MASK_Ω]]"

    plan = replace_first_plan_token(
        plan,
        token,
    )

    redacted = tmp_path / "redacted.pdf"
    output = tmp_path / "masked.pdf"

    redact_pdf(plan, redacted)

    insert_pdf_tokens(
        redacted,
        plan,
        output,
        font_path=font_path,
    )

    page = extract_pdf_text(output).pages[0]

    assert token in page.text


def test_missing_custom_font_is_rejected(tmp_path):

    source = create_pdf(
        tmp_path / "source.pdf",
        ["Jane Doe"],
    )

    plan, vault = create_pdf_masking_plan(source)

    redacted = tmp_path / "redacted.pdf"
    output = tmp_path / "masked.pdf"

    redact_pdf(plan, redacted)

    with pytest.raises(PDFTokenFontError):

        insert_pdf_tokens(
            redacted,
            plan,
            output,
            font_path=tmp_path / "missing.ttf",
        )


def test_token_insertion_does_not_modify_inputs(tmp_path):

    source = create_pdf(
        tmp_path / "source.pdf",
        ["Customer: Jane Doe"],
    )

    plan, vault = create_pdf_masking_plan(source)

    redacted = tmp_path / "redacted.pdf"
    output = tmp_path / "masked.pdf"

    redact_pdf(plan, redacted)

    source_hash = hashlib.sha256(
        source.read_bytes()
    ).hexdigest()

    redacted_hash = hashlib.sha256(
        redacted.read_bytes()
    ).hexdigest()

    insert_pdf_tokens(
        redacted,
        plan,
        output,
    )

    assert hashlib.sha256(
        source.read_bytes()
    ).hexdigest() == source_hash

    assert hashlib.sha256(
        redacted.read_bytes()
    ).hexdigest() == redacted_hash


def test_token_insertion_rejects_unredacted_pdf(tmp_path):

    source = create_pdf(
        tmp_path / "source.pdf",
        ["Customer: Jane Doe"],
    )

    plan, vault = create_pdf_masking_plan(source)

    unredacted_copy = tmp_path / "unredacted-copy.pdf"

    unredacted_copy.write_bytes(
        source.read_bytes()
    )

    with pytest.raises(PDFTokenInsertionError):

        insert_pdf_tokens(
            unredacted_copy,
            plan,
            tmp_path / "masked.pdf",
        )


def test_token_insertion_without_matches(tmp_path):

    source = create_pdf(
        tmp_path / "source.pdf",
        ["Customer: Jane Doe"],
    )

    plan, vault = create_pdf_masking_plan(
        source,
        pattern="Not Present",
    )

    redacted = tmp_path / "redacted.pdf"
    output = tmp_path / "masked.pdf"

    redact_pdf(plan, redacted)

    insert_pdf_tokens(
        redacted,
        plan,
        output,
    )

    original_text = extract_pdf_text(source).pages[0].text
    output_text = extract_pdf_text(output).pages[0].text

    assert output_text == original_text


def test_token_insertion_rejects_modified_source(tmp_path):

    source = create_pdf(
        tmp_path / "source.pdf",
        ["Customer: Jane Doe"],
    )

    plan, vault = create_pdf_masking_plan(source)

    redacted = tmp_path / "redacted.pdf"

    redact_pdf(plan, redacted)

    source.write_bytes(
        source.read_bytes() + b"modified"
    )

    with pytest.raises(PDFSourceChangedError):

        insert_pdf_tokens(
            redacted,
            plan,
            tmp_path / "masked.pdf",
        )


def test_token_insertion_rejects_same_output_path(tmp_path):

    source = create_pdf(
        tmp_path / "source.pdf",
        ["Customer: Jane Doe"],
    )

    plan, vault = create_pdf_masking_plan(source)

    redacted = tmp_path / "redacted.pdf"

    redact_pdf(plan, redacted)

    with pytest.raises(ValueError):

        insert_pdf_tokens(
            redacted,
            plan,
            redacted,
        )


def test_restore_pdf_round_trip_restores_original_values(tmp_path):
    source = create_pdf(
        tmp_path / "original.pdf",
        [
            "Name: Jane Doe",
            "Email: jane.doe@example.test",
            "Phone: +1-202-555-0147",
            "Again: Jane Doe",
        ],
    )
    redacted = tmp_path / "redacted.pdf"
    masked = tmp_path / "masked.pdf"
    restored = tmp_path / "restored.pdf"

    original_bytes = source.read_bytes()

    values = (
        "Jane Doe",
        "jane.doe@example.test",
        "+1-202-555-0147",
    )
    rules = RuleSet(
        tuple(
            MaskingRule(
                rule_id=f"restore-{index}",
                kind="literal",
                pattern=value,
            )
            for index, value in enumerate(values)
        )
    )
    engine = MaskingEngine(MaskingVault(rules))

    extracted = extract_pdf_text(source)
    plan = build_pdf_masking_plan(extracted, engine)

    redact_pdf(plan, redacted)
    insert_pdf_tokens(redacted, plan, masked)

    masked_bytes = masked.read_bytes()
    masked_text = "\n".join(
        page.text for page in extract_pdf_text(masked).pages
    )

    for value in values:
        assert value not in masked_text

    restore_pdf(masked, plan, restored)

    restored_text = "\n".join(
        page.text for page in extract_pdf_text(restored).pages
    )

    for value in values:
        assert value in restored_text

    for token in engine.vault.token_to_original:
        assert token not in restored_text

    assert source.read_bytes() == original_bytes
    assert masked.read_bytes() == masked_bytes
    assert restored.is_file()


def test_restore_pdf_rejects_masked_file_without_expected_tokens(tmp_path):
    source = create_pdf(
        tmp_path / "original.pdf",
        ["Jane Doe"],
    )
    masked = create_pdf(
        tmp_path / "not-masked.pdf",
        ["No masking token here"],
    )
    output = tmp_path / "restored.pdf"

    rules = RuleSet((
        MaskingRule(
            rule_id="name",
            kind="literal",
            pattern="Jane Doe",
        ),
    ))
    engine = MaskingEngine(MaskingVault(rules))
    plan = build_pdf_masking_plan(extract_pdf_text(source), engine)

    with pytest.raises(PDFRestorationError):
        restore_pdf(masked, plan, output)

    assert not output.exists()


def test_restore_pdf_rejects_existing_output(tmp_path):
    source = create_pdf(
        tmp_path / "original.pdf",
        ["Jane Doe"],
    )
    redacted = tmp_path / "redacted.pdf"
    masked = tmp_path / "masked.pdf"
    output = tmp_path / "restored.pdf"

    rules = RuleSet((
        MaskingRule(
            rule_id="name",
            kind="literal",
            pattern="Jane Doe",
        ),
    ))
    engine = MaskingEngine(MaskingVault(rules))
    plan = build_pdf_masking_plan(extract_pdf_text(source), engine)
    redact_pdf(plan, redacted)
    insert_pdf_tokens(redacted, plan, masked)
    output.write_bytes(b"Do not overwrite")

    with pytest.raises(FileExistsError):
        restore_pdf(masked, plan, output)

    assert output.read_bytes() == b"Do not overwrite"



def test_restore_pdf_round_trip_restores_multiline_match(tmp_path):
    source = create_multiline_pdf(
        tmp_path / "multiline-original.pdf",
        [[
            "Jane Doe",
            "Phone: 0912345678",
            "Address: New York",
        ]],
    )
    redacted = tmp_path / "multiline-redacted.pdf"
    masked = tmp_path / "multiline-masked.pdf"
    restored = tmp_path / "multiline-restored.pdf"

    engine, _vault = create_masking_engine(
        MaskingRule(
            "sensitive-multiline",
            "literal",
            "Jane Doe\nPhone: 0912345678",
        )
    )
    plan = build_pdf_masking_plan(extract_pdf_text(source), engine)

    redact_pdf(plan, redacted)
    insert_pdf_tokens(redacted, plan, masked)
    restore_pdf(masked, plan, restored)

    masked_text = extract_pdf_text(masked).pages[0].text
    restored_text = extract_pdf_text(restored).pages[0].text

    assert "Jane Doe" not in masked_text
    assert "0912345678" not in masked_text
    assert "Jane Doe\nPhone: 0912345678" in restored_text
    assert "Address: New York" in restored_text



def test_pdf_persian_normalization_maps_arabic_presentation_forms_and_restores(tmp_path):
    from pathlib import Path

    font_candidates = (
        Path("/usr/share/fonts/truetype/noto/NotoNaskhArabic-Regular.ttf"),
        Path("/usr/share/fonts/truetype/noto/NotoSansArabic-Regular.ttf"),
        Path("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"),
        Path("C:/Windows/Fonts/arial.ttf"),
        Path("C:/Windows/Fonts/tahoma.ttf"),
    )
    font_path = next((item for item in font_candidates if item.is_file()), None)
    if font_path is None:
        pytest.skip("No Arabic-capable font is installed for the PDF RTL test")

    source = tmp_path / "persian-rtl.pdf"
    redacted = tmp_path / "persian-rtl-redacted.pdf"
    masked = tmp_path / "persian-rtl-masked.pdf"
    restored = tmp_path / "persian-rtl-restored.pdf"

    document = pymupdf.open()
    page = document.new_page(width=520, height=220)
    html = (
        '<div style="font-family: Noto; font-size: 12pt;">KEEP BEFORE</div>'
        '<div dir="rtl" style="font-family: Noto; font-size: 22pt;">علی رضایی</div>'
        '<div style="font-family: Noto; font-size: 12pt;">KEEP AFTER</div>'
    )
    css = (
        f'@font-face {{font-family: Noto; src: url("{font_path.as_posix()}");}}'
    )
    page.insert_htmlbox(
        pymupdf.Rect(20, 15, 500, 200),
        html,
        css=css,
    )
    document.save(source)
    document.close()

    extracted = extract_pdf_text(source)
    source_text = extracted.pages[0].text
    assert "KEEP\u00a0BEFORE" in source_text
    assert "KEEP\u00a0AFTER" in source_text

    rules = RuleSet((
        MaskingRule("persian-name", "literal", "علي رضايي"),
    ))
    engine = MaskingEngine(
        MaskingVault(rules, normalization="persian")
    )
    plan = build_pdf_masking_plan(extracted, engine)

    assert plan.total_replacements == 1
    replacement = plan.pages[0].replacements[0]
    assert len(replacement.character_boxes) == len(replacement.original)
    assert "KEEP BEFORE" not in replacement.original
    assert "KEEP AFTER" not in replacement.original

    redact_pdf(plan, redacted)
    insert_pdf_tokens(redacted, plan, masked)
    masked_text = "\n".join(
        page.text for page in extract_pdf_text(masked).pages
    )

    assert replacement.original not in masked_text
    assert engine.vault.get_token(replacement.original) in masked_text
    assert "KEEP\u00a0BEFORE" in masked_text
    assert "KEEP\u00a0AFTER" in masked_text

    restore_pdf(masked, plan, restored)
    restored_text = "\n".join(
        page.text for page in extract_pdf_text(restored).pages
    )
    assert replacement.original in restored_text
    assert "KEEP\u00a0BEFORE" in restored_text
    assert "KEEP\u00a0AFTER" in restored_text
