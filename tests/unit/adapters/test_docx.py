import pytest
from docx import Document

from document_anonymizer.adapters.docx import (
    DOCXTextDocument,
    DOCXExtractionError,
    extract_docx_text,
)

from document_anonymizer.adapters.docx import (
    DOCXMaskPlan,
    DOCXMappedReplacement,
    DOCXRunSlice,
    build_docx_masking_plan,
    apply_docx_masking_plan,
    apply_docx_unmasking_plan,
)

from document_anonymizer.adapters.docx import (
    DOCXUnmaskPlan,
    DOCXMappedRestoration,
    build_docx_unmasking_plan,
    DOCXAdapterError,
)

from document_anonymizer.core.models import (
    MaskResult,
    MaskReplacement,
    RestoreResult,
)

from document_anonymizer.core.tokens import TOKEN_PATTERN
from document_anonymizer.core.engine import MaskingEngine
from document_anonymizer.core.rules import MaskingRule, RuleSet
from document_anonymizer.core.vault import MaskingVault


class FakeVault:
    def __init__(self):
        self.vault_id = "test-vault"
        self.reserved_texts = []

    def reserve_source_text(self, text):
        self.reserved_texts.append(text)


class FakeEngine:
    """Small test double for the existing masking engine."""

    def __init__(self, targets):
        self.vault = FakeVault()
        self.targets = tuple(targets)
        self.tokens = {}
        self.mask_calls = 0

    def mask_text(self, text):
        self.mask_calls += 1
        self.vault.reserve_source_text(text)

        matches = []

        for target in self.targets:
            cursor = 0

            while True:
                start = text.find(target, cursor)

                if start < 0:
                    break

                matches.append((start, start + len(target), target))
                cursor = start + len(target)

        matches.sort(key=lambda item: item[0])

        output = []
        replacements = []
        cursor = 0
        rule_counts = {}

        for start, end, original in matches:

            if start < cursor:
                continue

            token = self.tokens.get(original)

            if token is None:
                token = f"[[M_{len(self.tokens) + 1:013X}]]"
                self.tokens[original] = token

            output.append(text[cursor:start])
            output.append(token)

            replacements.append(
                MaskReplacement(
                    start=start,
                    end=end,
                    original=original,
                    token=token,
                    rule_id="test-rule",
                )
            )

            rule_counts["test-rule"] = (
                rule_counts.get("test-rule", 0) + 1
            )

            cursor = end

        output.append(text[cursor:])

        return MaskResult(
            vault_id=self.vault.vault_id,
            masked_text="".join(output),
            replacements_count=len(replacements),
            unique_tokens=tuple(
                dict.fromkeys(r.token for r in replacements)
            ),
            replacements_by_rule=rule_counts,
            replacements=tuple(replacements),
        )


class FakeUnmaskEngine:
    """Test double for the existing restoration engine."""

    def __init__(
        self,
        token_mapping,
        vault_id="test-vault",
        result_vault_id=None,
    ):
        self.vault = FakeVault()
        self.vault.vault_id = vault_id

        self.vault.token_to_original = dict(
            token_mapping
        )

        self.result_vault_id = (
            result_vault_id or vault_id
        )

    def unmask_text(self, text):

        mapping = self.vault.token_to_original

        encountered = tuple(
            dict.fromkeys(
                TOKEN_PATTERN.findall(text)
            )
        )

        unknown_tokens = tuple(
            token
            for token in encountered
            if token not in mapping
        )

        restored_count = 0

        def replace_token(match):
            nonlocal restored_count

            token = match.group(0)

            original = mapping.get(token)

            if original is None:
                return token

            restored_count += 1

            return original

        restored_text = TOKEN_PATTERN.sub(
            replace_token,
            text,
        )

        return RestoreResult(
            vault_id=self.result_vault_id,
            restored_text=restored_text,
            restored_count=restored_count,
            unknown_tokens=unknown_tokens,
        )


def test_docx_plan_without_matches(tmp_path):

    path = tmp_path / "no-matches.docx"

    document = Document()
    document.add_paragraph("Nothing sensitive here")
    document.save(path)

    extracted = extract_docx_text(path)
    engine = FakeEngine(["Jane Doe"])

    plan = build_docx_masking_plan(extracted, engine)

    assert isinstance(plan, DOCXMaskPlan)
    assert plan.replacements_count == 0
    assert plan.original_text == plan.masked_text


def test_docx_plan_maps_single_run_match(tmp_path):

    path = tmp_path / "single-run.docx"

    document = Document()
    document.add_paragraph("Hello Jane Doe!")
    document.save(path)

    extracted = extract_docx_text(path)
    engine = FakeEngine(["Jane Doe"])

    plan = build_docx_masking_plan(extracted, engine)

    paragraph = next(
        p for p in plan.paragraphs
        if p.original_text == "Hello Jane Doe!"
    )

    replacement = paragraph.replacements[0]

    assert replacement.original == "Jane Doe"
    assert len(replacement.run_slices) == 1

    run_slice = replacement.run_slices[0]

    assert run_slice.text == "Jane Doe"
    assert run_slice.run_start == 6
    assert run_slice.run_end == 14

    assert paragraph.masked_text == (
        f"Hello {replacement.token}!"
    )


def test_docx_plan_maps_match_across_runs(tmp_path):

    path = tmp_path / "cross-run.docx"

    document = Document()
    paragraph = document.add_paragraph()

    paragraph.add_run("Hello ")
    paragraph.add_run("Jane").bold = True
    paragraph.add_run(" Doe").italic = True

    document.save(path)

    extracted = extract_docx_text(path)
    engine = FakeEngine(["Jane Doe"])

    plan = build_docx_masking_plan(extracted, engine)

    target = next(
        p for p in plan.paragraphs
        if p.original_text == "Hello Jane Doe"
    )

    replacement = target.replacements[0]

    assert replacement.original == "Jane Doe"
    assert len(replacement.run_slices) == 2

    assert [
        item.text for item in replacement.run_slices
    ] == ["Jane", " Doe"]

    assert [
        item.run_index for item in replacement.run_slices
    ] == [1, 2]

    assert target.masked_text == (
        f"Hello {replacement.token}"
    )


def test_docx_plan_maps_multiple_matches(tmp_path):

    path = tmp_path / "multiple.docx"

    document = Document()
    document.add_paragraph(
        "Jane Doe works with John Smith."
    )
    document.save(path)

    extracted = extract_docx_text(path)

    engine = FakeEngine([
        "Jane Doe",
        "John Smith",
    ])

    plan = build_docx_masking_plan(extracted, engine)

    paragraph = next(
        p for p in plan.paragraphs
        if p.original_text.startswith("Jane Doe")
    )

    assert len(paragraph.replacements) == 2
    assert plan.replacements_count == 2

    assert paragraph.replacements[0].original == "Jane Doe"
    assert paragraph.replacements[1].original == "John Smith"


def test_docx_plan_reuses_token_for_repeated_value(tmp_path):

    path = tmp_path / "repeated.docx"

    document = Document()
    document.add_paragraph(
        "Jane Doe met Jane Doe."
    )
    document.save(path)

    extracted = extract_docx_text(path)
    engine = FakeEngine(["Jane Doe"])

    plan = build_docx_masking_plan(extracted, engine)

    paragraph = next(
        p for p in plan.paragraphs
        if p.original_text.startswith("Jane Doe")
    )

    assert len(paragraph.replacements) == 2

    assert (
        paragraph.replacements[0].token
        == paragraph.replacements[1].token
    )


def test_docx_plan_maps_body_table_header_and_footer(tmp_path):

    path = tmp_path / "all-stories.docx"

    document = Document()

    document.add_paragraph("Jane Doe in body")

    table = document.add_table(rows=1, cols=1)
    table.cell(0, 0).text = "Jane Doe in table"

    document.sections[0].header.paragraphs[0].text = (
        "Jane Doe in header"
    )

    document.sections[0].footer.paragraphs[0].text = (
        "Jane Doe in footer"
    )

    document.save(path)

    extracted = extract_docx_text(path)
    engine = FakeEngine(["Jane Doe"])

    plan = build_docx_masking_plan(extracted, engine)

    stories = {
        paragraph.story
        for paragraph in plan.paragraphs
        if paragraph.replacements
    }

    assert "body" in stories
    assert "header" in stories
    assert "footer" in stories

    assert any(
        p.location and "table" in p.location
        for p in plan.paragraphs
        if p.replacements
    )


def test_docx_plan_reserves_entire_document_before_masking(tmp_path):

    path = tmp_path / "reservation.docx"

    document = Document()
    document.add_paragraph("First paragraph")
    document.add_paragraph(
        "Later paragraph [[M_AAAAAAAAAAAAA]]"
    )
    document.save(path)

    extracted = extract_docx_text(path)
    engine = FakeEngine(["First"])

    plan = build_docx_masking_plan(extracted, engine)

    complete_text = "\n".join(
        paragraph.text
        for paragraph in extracted.paragraphs
    )

    assert engine.vault.reserved_texts[0] == complete_text


def test_docx_plan_does_not_modify_source(tmp_path):

    path = tmp_path / "unchanged.docx"

    document = Document()
    document.add_paragraph("Jane Doe")
    document.save(path)

    original_bytes = path.read_bytes()

    extracted = extract_docx_text(path)
    engine = FakeEngine(["Jane Doe"])

    build_docx_masking_plan(extracted, engine)

    assert path.read_bytes() == original_bytes


def test_extract_simple_docx_text(tmp_path):
    path = tmp_path / "simple.docx"
    document = Document()
    document.add_paragraph("Jane Doe")
    document.save(path)

    result = extract_docx_text(path)

    assert isinstance(result, DOCXTextDocument)
    assert any(p.text == "Jane Doe" for p in result.paragraphs)
    assert result.has_text


def test_extract_run_offsets(tmp_path):
    path = tmp_path / "runs.docx"
    document = Document()
    paragraph = document.add_paragraph()

    paragraph.add_run("Hello ")
    bold_run = paragraph.add_run("Jane")
    bold_run.bold = True
    paragraph.add_run(" Doe")

    document.save(path)

    result = extract_docx_text(path)
    extracted = next(
        p for p in result.paragraphs
        if p.text == "Hello Jane Doe"
    )

    assert len(extracted.runs) == 3
    assert [(r.start, r.end) for r in extracted.runs] == [
        (0, 6), (6, 10), (10, 14)
    ]

    assert "".join(r.text for r in extracted.runs) == extracted.text


def test_extract_table_text(tmp_path):
    path = tmp_path / "table.docx"
    document = Document()

    table = document.add_table(rows=2, cols=2)
    table.cell(0, 0).text = "Jane Doe"
    table.cell(0, 1).text = "Customer"
    table.cell(1, 0).text = "John Smith"
    table.cell(1, 1).text = "Manager"

    document.save(path)

    result = extract_docx_text(path)
    texts = [p.text for p in result.paragraphs]

    assert "Jane Doe" in texts
    assert "Customer" in texts
    assert "John Smith" in texts
    assert "Manager" in texts


def test_extract_nested_table_text(tmp_path):
    path = tmp_path / "nested.docx"
    document = Document()

    outer = document.add_table(rows=1, cols=1)
    inner = outer.cell(0, 0).add_table(rows=1, cols=1)
    inner.cell(0, 0).text = "Nested value"

    document.save(path)

    result = extract_docx_text(path)

    assert any(
        p.text == "Nested value"
        for p in result.paragraphs
    )


def test_extract_header_and_footer_text(tmp_path):
    path = tmp_path / "header-footer.docx"
    document = Document()

    document.sections[0].header.paragraphs[0].text = (
        "Company confidential"
    )

    document.sections[0].footer.paragraphs[0].text = (
        "Page footer"
    )

    document.add_paragraph("Body text")
    document.save(path)

    result = extract_docx_text(path)

    assert any(
        p.story == "header"
        and p.text == "Company confidential"
        for p in result.paragraphs
    )

    assert any(
        p.story == "footer"
        and p.text == "Page footer"
        for p in result.paragraphs
    )

    assert any(
        p.story == "body"
        and p.text == "Body text"
        for p in result.paragraphs
    )


def test_extract_empty_docx(tmp_path):
    path = tmp_path / "empty.docx"
    Document().save(path)

    result = extract_docx_text(path)

    assert result.has_text is False
    assert result.total_character_count == 0


def test_source_path_and_fingerprint(tmp_path):
    path = tmp_path / "fingerprint.docx"

    document = Document()
    document.add_paragraph("Fingerprint")
    document.save(path)

    result = extract_docx_text(path)

    assert result.source_path.is_absolute()
    assert len(result.source_sha256) == 64


def test_total_character_count(tmp_path):
    path = tmp_path / "counts.docx"

    document = Document()
    document.add_paragraph("First")
    document.add_paragraph("Second")
    document.save(path)

    result = extract_docx_text(path)

    assert result.total_character_count == 11


def test_missing_docx_raises_error(tmp_path):
    with pytest.raises(FileNotFoundError):
        extract_docx_text(tmp_path / "missing.docx")


def test_non_docx_file_is_rejected(tmp_path):
    path = tmp_path / "sample.txt"
    path.write_text("Not a Word file")

    with pytest.raises(ValueError):
        extract_docx_text(path)


def test_invalid_docx_raises_extraction_error(tmp_path):
    path = tmp_path / "invalid.docx"
    path.write_text("not a zip package")

    with pytest.raises(DOCXExtractionError):
        extract_docx_text(path)


def test_invalid_source_type_is_rejected():
    with pytest.raises(TypeError):
        extract_docx_text(123)



def test_apply_docx_masking_single_run(tmp_path):

    source = tmp_path / "source.docx"
    output = tmp_path / "masked.docx"

    document = Document()
    document.add_paragraph("Hello Jane Doe!")
    document.save(source)

    extracted = extract_docx_text(source)
    engine = FakeEngine(["Jane Doe"])

    plan = build_docx_masking_plan(extracted, engine)

    result = apply_docx_masking_plan(plan, output)

    assert result == output.resolve()
    assert result.exists()

    masked = extract_docx_text(result)

    assert "Jane Doe" not in masked.text
    assert "Hello " in masked.text
    assert "!" in masked.text


def test_apply_docx_masking_across_runs_preserves_formatting(tmp_path):

    source = tmp_path / "source.docx"
    output = tmp_path / "masked.docx"

    document = Document()
    paragraph = document.add_paragraph()

    paragraph.add_run("Hello ")

    bold_run = paragraph.add_run("Jane")
    bold_run.bold = True

    italic_run = paragraph.add_run(" Doe!")
    italic_run.italic = True

    document.save(source)

    extracted = extract_docx_text(source)
    engine = FakeEngine(["Jane Doe"])

    plan = build_docx_masking_plan(extracted, engine)

    token = plan.paragraphs[0].replacements[0].token

    apply_docx_masking_plan(plan, output)

    masked_document = Document(output)
    masked_paragraph = next(
        p for p in masked_document.paragraphs
        if p.text.startswith("Hello ")
    )

    assert masked_paragraph.text == f"Hello {token}!"

    # Unaffected text retains its formatting.
    assert masked_paragraph.runs[0].text == "Hello "

    assert masked_paragraph.runs[1].text == token
    assert masked_paragraph.runs[1].bold is True

    assert masked_paragraph.runs[2].text == "!"
    assert masked_paragraph.runs[2].italic is True


def test_apply_docx_masking_multiple_replacements(tmp_path):

    source = tmp_path / "multiple.docx"
    output = tmp_path / "multiple-masked.docx"

    document = Document()
    document.add_paragraph(
        "Jane Doe works with John Smith."
    )
    document.save(source)

    extracted = extract_docx_text(source)

    engine = FakeEngine([
        "Jane Doe",
        "John Smith",
    ])

    plan = build_docx_masking_plan(extracted, engine)

    assert plan.replacements_count == 2

    apply_docx_masking_plan(plan, output)

    masked = extract_docx_text(output)

    assert "Jane Doe" not in masked.text
    assert "John Smith" not in masked.text

    assert len([
        p for p in plan.paragraphs
        if p.replacements
    ]) == 1


def test_apply_docx_masking_body_table_header_footer(tmp_path):

    source = tmp_path / "stories.docx"
    output = tmp_path / "stories-masked.docx"

    document = Document()

    document.add_paragraph("Jane Doe in body")

    table = document.add_table(rows=1, cols=1)
    table.cell(0, 0).text = "Jane Doe in table"

    document.sections[0].header.paragraphs[0].text = (
        "Jane Doe in header"
    )

    document.sections[0].footer.paragraphs[0].text = (
        "Jane Doe in footer"
    )

    document.save(source)

    extracted = extract_docx_text(source)
    engine = FakeEngine(["Jane Doe"])

    plan = build_docx_masking_plan(extracted, engine)

    apply_docx_masking_plan(plan, output)

    masked = extract_docx_text(output)

    assert "Jane Doe" not in masked.text

    assert any(
        p.story == "body" and "[[M_" in p.text
        for p in masked.paragraphs
    )

    assert any(
        p.story == "header" and "[[M_" in p.text
        for p in masked.paragraphs
    )

    assert any(
        p.story == "footer" and "[[M_" in p.text
        for p in masked.paragraphs
    )


def test_apply_docx_masking_does_not_modify_source(tmp_path):

    source = tmp_path / "original.docx"
    output = tmp_path / "masked.docx"

    document = Document()
    document.add_paragraph("Jane Doe")
    document.save(source)

    original_bytes = source.read_bytes()

    extracted = extract_docx_text(source)
    engine = FakeEngine(["Jane Doe"])

    plan = build_docx_masking_plan(extracted, engine)

    apply_docx_masking_plan(plan, output)

    assert source.read_bytes() == original_bytes
    assert output.exists()


def test_apply_docx_masking_rejects_changed_source(tmp_path):

    source = tmp_path / "source.docx"
    output = tmp_path / "masked.docx"

    document = Document()
    document.add_paragraph("Jane Doe")
    document.save(source)

    extracted = extract_docx_text(source)
    engine = FakeEngine(["Jane Doe"])

    plan = build_docx_masking_plan(extracted, engine)

    # Modify source after generating the plan.
    document = Document(source)
    document.add_paragraph("New content")
    document.save(source)

    from document_anonymizer.adapters.docx import DOCXAdapterError

    with pytest.raises(DOCXAdapterError):
        apply_docx_masking_plan(plan, output)

    assert not output.exists()


def test_apply_docx_masking_rejects_same_source_and_output(tmp_path):

    source = tmp_path / "source.docx"

    document = Document()
    document.add_paragraph("Jane Doe")
    document.save(source)

    extracted = extract_docx_text(source)
    engine = FakeEngine(["Jane Doe"])

    plan = build_docx_masking_plan(extracted, engine)

    from document_anonymizer.adapters.docx import DOCXAdapterError

    with pytest.raises(DOCXAdapterError):
        apply_docx_masking_plan(plan, source)

    assert source.exists()


def test_apply_docx_masking_rejects_existing_output(tmp_path):

    source = tmp_path / "source.docx"
    output = tmp_path / "existing.docx"

    document = Document()
    document.add_paragraph("Jane Doe")
    document.save(source)

    output.write_bytes(b"Existing file")

    extracted = extract_docx_text(source)
    engine = FakeEngine(["Jane Doe"])

    plan = build_docx_masking_plan(extracted, engine)

    from document_anonymizer.adapters.docx import DOCXAdapterError

    with pytest.raises(DOCXAdapterError):
        apply_docx_masking_plan(plan, output)

    assert output.read_bytes() == b"Existing file"


def test_docx_unmask_plan_without_tokens(tmp_path):

    path = tmp_path / "no-tokens.docx"

    document = Document()
    document.add_paragraph("Nothing masked here")
    document.save(path)

    extracted = extract_docx_text(path)
    engine = FakeUnmaskEngine({})

    plan = build_docx_unmasking_plan(
        extracted,
        engine,
    )

    assert isinstance(plan, DOCXUnmaskPlan)
    assert plan.restored_count == 0
    assert plan.unknown_tokens == ()
    assert plan.masked_text == plan.restored_text


def test_docx_unmask_plan_maps_single_run_token(tmp_path):

    path = tmp_path / "single-token.docx"

    token = f"[[MASK_{'A' * 32}]]"

    document = Document()
    document.add_paragraph(f"Hello {token}!")
    document.save(path)

    extracted = extract_docx_text(path)

    engine = FakeUnmaskEngine({
        token: "Jane Doe",
    })

    plan = build_docx_unmasking_plan(
        extracted,
        engine,
    )

    paragraph = next(
        p for p in plan.paragraphs
        if token in p.masked_text
    )

    restoration = paragraph.restorations[0]

    assert isinstance(
        restoration,
        DOCXMappedRestoration,
    )

    assert restoration.token == token
    assert restoration.original == "Jane Doe"

    assert len(restoration.run_slices) == 1

    assert restoration.run_slices[0].text == token

    assert paragraph.restored_text == "Hello Jane Doe!"

    assert plan.restored_count == 1


def test_docx_unmask_plan_maps_token_across_runs(tmp_path):

    path = tmp_path / "cross-run-token.docx"

    token = f"[[MASK_{'B' * 32}]]"

    document = Document()

    paragraph = document.add_paragraph()

    paragraph.add_run(
        "Hello " + token[:15]
    ).bold = True

    paragraph.add_run(
        token[15:] + "!"
    ).italic = True

    document.save(path)

    extracted = extract_docx_text(path)

    engine = FakeUnmaskEngine({
        token: "Jane Doe",
    })

    plan = build_docx_unmasking_plan(
        extracted,
        engine,
    )

    target = next(
        p for p in plan.paragraphs
        if token in p.masked_text
    )

    restoration = target.restorations[0]

    assert restoration.token == token

    assert len(restoration.run_slices) == 2

    assert "".join(
        item.text
        for item in restoration.run_slices
    ) == token

    assert target.restored_text == "Hello Jane Doe!"


def test_docx_unmask_plan_maps_multiple_tokens(tmp_path):

    path = tmp_path / "multiple-tokens.docx"

    token_a = f"[[MASK_{'A' * 32}]]"
    token_b = f"[[MASK_{'B' * 32}]]"

    document = Document()

    document.add_paragraph(
        f"{token_a} met {token_b} and {token_a}"
    )

    document.save(path)

    extracted = extract_docx_text(path)

    engine = FakeUnmaskEngine({
        token_a: "Jane Doe",
        token_b: "John Smith",
    })

    plan = build_docx_unmasking_plan(
        extracted,
        engine,
    )

    assert plan.restored_count == 3

    target = next(
        p for p in plan.paragraphs
        if token_a in p.masked_text
    )

    assert target.restored_text == (
        "Jane Doe met John Smith and Jane Doe"
    )

    assert len(target.restorations) == 3

    assert (
        target.restorations[0].original
        == target.restorations[2].original
    )


def test_docx_unmask_plan_preserves_unknown_tokens(tmp_path):

    path = tmp_path / "unknown-tokens.docx"

    known = f"[[MASK_{'A' * 32}]]"
    unknown = f"[[MASK_{'C' * 32}]]"

    document = Document()

    document.add_paragraph(
        f"{known} and {unknown}"
    )

    document.save(path)

    extracted = extract_docx_text(path)

    engine = FakeUnmaskEngine({
        known: "Jane Doe",
    })

    plan = build_docx_unmasking_plan(
        extracted,
        engine,
    )

    assert plan.restored_count == 1

    assert plan.unknown_tokens == (unknown,)

    target = next(
        p for p in plan.paragraphs
        if known in p.masked_text
    )

    assert target.restored_text == (
        f"Jane Doe and {unknown}"
    )

    assert target.unknown_tokens == (unknown,)


def test_docx_unmask_plan_maps_body_table_header_footer(
    tmp_path,
):

    path = tmp_path / "all-stories.docx"

    token = f"[[MASK_{'D' * 32}]]"

    document = Document()

    document.add_paragraph(
        f"{token} in body"
    )

    table = document.add_table(
        rows=1,
        cols=1,
    )

    table.cell(0, 0).text = (
        f"{token} in table"
    )

    document.sections[0].header.paragraphs[0].text = (
        f"{token} in header"
    )

    document.sections[0].footer.paragraphs[0].text = (
        f"{token} in footer"
    )

    document.save(path)

    extracted = extract_docx_text(path)

    engine = FakeUnmaskEngine({
        token: "Jane Doe",
    })

    plan = build_docx_unmasking_plan(
        extracted,
        engine,
    )

    stories = {
        paragraph.story
        for paragraph in plan.paragraphs
        if paragraph.restorations
    }

    assert "body" in stories
    assert "header" in stories
    assert "footer" in stories

    assert any(
        "table" in paragraph.location
        for paragraph in plan.paragraphs
        if paragraph.restorations
    )

    assert plan.restored_count == 4


def test_docx_unmask_plan_does_not_modify_source(tmp_path):

    path = tmp_path / "unchanged.docx"

    token = f"[[MASK_{'E' * 32}]]"

    document = Document()
    document.add_paragraph(token)
    document.save(path)

    original_bytes = path.read_bytes()

    extracted = extract_docx_text(path)

    engine = FakeUnmaskEngine({
        token: "Jane Doe",
    })

    build_docx_unmasking_plan(
        extracted,
        engine,
    )

    assert path.read_bytes() == original_bytes


def test_docx_unmask_plan_rejects_invalid_engine():

    with pytest.raises(TypeError):

        build_docx_unmasking_plan(
            None,
            object(),
        )


def test_docx_unmask_plan_rejects_vault_mismatch(tmp_path):

    path = tmp_path / "mismatch.docx"

    token = f"[[MASK_{'F' * 32}]]"

    document = Document()
    document.add_paragraph(token)
    document.save(path)

    extracted = extract_docx_text(path)

    engine = FakeUnmaskEngine(
        {token: "Jane Doe"},
        vault_id="vault-a",
        result_vault_id="vault-b",
    )

    with pytest.raises(DOCXAdapterError):

        build_docx_unmasking_plan(
            extracted,
            engine,
        )


def test_docx_unmask_plan_rejects_inconsistent_result(
    tmp_path,
):

    path = tmp_path / "inconsistent.docx"

    token = f"[[MASK_{'9' * 32}]]"

    document = Document()
    document.add_paragraph(token)
    document.save(path)

    extracted = extract_docx_text(path)

    class BadEngine(FakeUnmaskEngine):

        def unmask_text(self, text):

            result = super().unmask_text(text)

            return RestoreResult(
                vault_id=result.vault_id,
                restored_text="Incorrect output",
                restored_count=result.restored_count,
                unknown_tokens=result.unknown_tokens,
            )

    engine = BadEngine({
        token: "Jane Doe",
    })

    with pytest.raises(DOCXAdapterError):

        build_docx_unmasking_plan(
            extracted,
            engine,
        )


def test_docx_unmask_plan_rejects_invalid_engine(tmp_path):

    path = tmp_path / "invalid-engine.docx"

    document = Document()
    document.add_paragraph("Nothing masked")
    document.save(path)

    extracted = extract_docx_text(path)

    with pytest.raises(TypeError):
        build_docx_unmasking_plan(
            extracted,
            object(),
        )



def test_apply_docx_unmasking_single_run(tmp_path):

    source = tmp_path / "masked.docx"
    output = tmp_path / "restored.docx"

    token = f"[[MASK_{'A' * 32}]]"

    document = Document()
    document.add_paragraph(f"Hello {token}!")
    document.save(source)

    extracted = extract_docx_text(source)

    engine = FakeUnmaskEngine({
        token: "Jane Doe",
    })

    plan = build_docx_unmasking_plan(extracted, engine)

    result = apply_docx_unmasking_plan(plan, output)

    assert result == output.resolve()
    assert result.exists()

    restored = extract_docx_text(result)

    assert any(
        p.text == "Hello Jane Doe!"
        for p in restored.paragraphs
    )

    assert token not in restored.text


def test_apply_docx_unmasking_across_runs_preserves_formatting(
    tmp_path,
):

    source = tmp_path / "cross-run.docx"
    output = tmp_path / "cross-run-restored.docx"

    token = f"[[MASK_{'B' * 32}]]"

    document = Document()
    paragraph = document.add_paragraph()

    paragraph.add_run(
        "Hello " + token[:15]
    ).bold = True

    paragraph.add_run(
        token[15:] + "!"
    ).italic = True

    document.save(source)

    extracted = extract_docx_text(source)

    engine = FakeUnmaskEngine({
        token: "Jane Doe",
    })

    plan = build_docx_unmasking_plan(extracted, engine)

    apply_docx_unmasking_plan(plan, output)

    restored_document = Document(output)

    restored_paragraph = next(
        p for p in restored_document.paragraphs
        if p.text.startswith("Hello ")
    )

    assert restored_paragraph.text == "Hello Jane Doe!"

    # First affected run receives the original value.
    assert restored_paragraph.runs[0].text == "Hello Jane Doe"
    assert restored_paragraph.runs[0].bold is True

    # The unaffected text in the second run remains.
    assert restored_paragraph.runs[1].text == "!"
    assert restored_paragraph.runs[1].italic is True


def test_apply_docx_unmasking_multiple_replacements(tmp_path):

    source = tmp_path / "multiple.docx"
    output = tmp_path / "multiple-restored.docx"

    token_a = f"[[MASK_{'A' * 32}]]"
    token_b = f"[[MASK_{'B' * 32}]]"

    document = Document()
    document.add_paragraph(
        f"{token_a} met {token_b} and {token_a}"
    )
    document.save(source)

    extracted = extract_docx_text(source)

    engine = FakeUnmaskEngine({
        token_a: "Jane Doe",
        token_b: "John Smith",
    })

    plan = build_docx_unmasking_plan(extracted, engine)

    assert plan.restored_count == 3

    apply_docx_unmasking_plan(plan, output)

    restored = extract_docx_text(output)

    assert any(
        p.text == "Jane Doe met John Smith and Jane Doe"
        for p in restored.paragraphs
    )


def test_apply_docx_unmasking_preserves_unknown_tokens(
    tmp_path,
):

    source = tmp_path / "unknown.docx"
    output = tmp_path / "unknown-restored.docx"

    known = f"[[MASK_{'A' * 32}]]"
    unknown = f"[[MASK_{'C' * 32}]]"

    document = Document()
    document.add_paragraph(
        f"{known} and {unknown}"
    )
    document.save(source)

    extracted = extract_docx_text(source)

    engine = FakeUnmaskEngine({
        known: "Jane Doe",
    })

    plan = build_docx_unmasking_plan(extracted, engine)

    assert plan.unknown_tokens == (unknown,)

    apply_docx_unmasking_plan(plan, output)

    restored = extract_docx_text(output)

    assert any(
        p.text == f"Jane Doe and {unknown}"
        for p in restored.paragraphs
    )


def test_apply_docx_unmasking_body_table_header_footer(
    tmp_path,
):

    source = tmp_path / "stories.docx"
    output = tmp_path / "stories-restored.docx"

    token = f"[[MASK_{'D' * 32}]]"

    document = Document()

    document.add_paragraph(
        f"{token} in body"
    )

    table = document.add_table(rows=1, cols=1)

    table.cell(0, 0).text = (
        f"{token} in table"
    )

    document.sections[0].header.paragraphs[0].text = (
        f"{token} in header"
    )

    document.sections[0].footer.paragraphs[0].text = (
        f"{token} in footer"
    )

    document.save(source)

    extracted = extract_docx_text(source)

    engine = FakeUnmaskEngine({
        token: "Jane Doe",
    })

    plan = build_docx_unmasking_plan(extracted, engine)

    assert plan.restored_count == 4

    apply_docx_unmasking_plan(plan, output)

    restored = extract_docx_text(output)

    assert "Jane Doe" in restored.text
    assert token not in restored.text

    assert any(
        p.story == "body" and "Jane Doe" in p.text
        for p in restored.paragraphs
    )

    assert any(
        p.story == "header" and "Jane Doe" in p.text
        for p in restored.paragraphs
    )

    assert any(
        p.story == "footer" and "Jane Doe" in p.text
        for p in restored.paragraphs
    )


def test_apply_docx_unmasking_does_not_modify_source(
    tmp_path,
):

    source = tmp_path / "original.docx"
    output = tmp_path / "restored.docx"

    token = f"[[MASK_{'E' * 32}]]"

    document = Document()
    document.add_paragraph(token)
    document.save(source)

    original_bytes = source.read_bytes()

    extracted = extract_docx_text(source)

    engine = FakeUnmaskEngine({
        token: "Jane Doe",
    })

    plan = build_docx_unmasking_plan(extracted, engine)

    apply_docx_unmasking_plan(plan, output)

    assert source.read_bytes() == original_bytes
    assert output.exists()


def test_apply_docx_unmasking_rejects_changed_source(
    tmp_path,
):

    source = tmp_path / "changed.docx"
    output = tmp_path / "restored.docx"

    token = f"[[MASK_{'F' * 32}]]"

    document = Document()
    document.add_paragraph(token)
    document.save(source)

    extracted = extract_docx_text(source)

    engine = FakeUnmaskEngine({
        token: "Jane Doe",
    })

    plan = build_docx_unmasking_plan(extracted, engine)

    document = Document(source)
    document.add_paragraph("New content")
    document.save(source)

    with pytest.raises(DOCXAdapterError):
        apply_docx_unmasking_plan(plan, output)

    assert not output.exists()


def test_apply_docx_unmasking_rejects_same_source_and_output(
    tmp_path,
):

    source = tmp_path / "source.docx"

    token = f"[[MASK_{'9' * 32}]]"

    document = Document()
    document.add_paragraph(token)
    document.save(source)

    extracted = extract_docx_text(source)

    engine = FakeUnmaskEngine({
        token: "Jane Doe",
    })

    plan = build_docx_unmasking_plan(extracted, engine)

    with pytest.raises(DOCXAdapterError):
        apply_docx_unmasking_plan(plan, source)

    assert source.exists()


def test_apply_docx_unmasking_rejects_existing_output(
    tmp_path,
):

    source = tmp_path / "source.docx"
    output = tmp_path / "existing.docx"

    token = f"[[MASK_{'8' * 32}]]"

    document = Document()
    document.add_paragraph(token)
    document.save(source)

    output.write_bytes(b"Existing file")

    extracted = extract_docx_text(source)

    engine = FakeUnmaskEngine({
        token: "Jane Doe",
    })

    plan = build_docx_unmasking_plan(extracted, engine)

    with pytest.raises(DOCXAdapterError):
        apply_docx_unmasking_plan(plan, output)

    assert output.read_bytes() == b"Existing file"


def test_apply_docx_unmasking_without_tokens(tmp_path):

    source = tmp_path / "no-tokens.docx"
    output = tmp_path / "no-tokens-restored.docx"

    document = Document()
    document.add_paragraph("Nothing sensitive here")
    document.save(source)

    extracted = extract_docx_text(source)

    engine = FakeUnmaskEngine({})

    plan = build_docx_unmasking_plan(extracted, engine)

    assert plan.restored_count == 0

    apply_docx_unmasking_plan(plan, output)

    restored = extract_docx_text(output)

    assert restored.text == extracted.text



def test_docx_persian_normalization_preserves_exact_original_spelling(tmp_path):
    source = tmp_path / "persian.docx"
    masked = tmp_path / "persian-masked.docx"
    restored = tmp_path / "persian-restored.docx"

    document = Document()
    document.add_paragraph("نام مشتری: علي كاظم")
    document.save(source)

    rules = RuleSet((
        MaskingRule("customer-name", "literal", "علی کاظم"),
    ))
    engine = MaskingEngine(
        MaskingVault(rules, normalization="persian")
    )

    extracted = extract_docx_text(source)
    plan = build_docx_masking_plan(extracted, engine)

    assert plan.replacements_count == 1
    replacement = plan.paragraphs[0].replacements[0]
    assert replacement.original == "علي كاظم"
    assert plan.paragraphs[0].original_text[
        replacement.start:replacement.end
    ] == "علي كاظم"

    apply_docx_masking_plan(plan, masked)
    masked_text = extract_docx_text(masked).text
    assert "علي كاظم" not in masked_text
    assert replacement.token in masked_text

    unmask_plan = build_docx_unmasking_plan(
        extract_docx_text(masked),
        engine,
    )
    apply_docx_unmasking_plan(unmask_plan, restored)
    assert extract_docx_text(restored).text == extracted.text


def test_docx_persian_half_space_is_optional_but_restored_exactly(tmp_path):
    source = tmp_path / "persian-zwnj.docx"
    masked = tmp_path / "persian-zwnj-masked.docx"
    restored = tmp_path / "persian-zwnj-restored.docx"

    document = Document()
    document.add_paragraph("وضعیت: می‌روم")
    document.save(source)

    rules = RuleSet((
        MaskingRule("verb", "literal", "میروم"),
    ))
    engine = MaskingEngine(
        MaskingVault(
            rules,
            normalization="persian",
            ignore_zwnj=True,
        )
    )

    original_text = extract_docx_text(source).text
    plan = build_docx_masking_plan(extract_docx_text(source), engine)
    assert plan.replacements_count == 1
    assert plan.paragraphs[0].replacements[0].original == "می‌روم"

    apply_docx_masking_plan(plan, masked)
    unmask_plan = build_docx_unmasking_plan(
        extract_docx_text(masked),
        engine,
    )
    apply_docx_unmasking_plan(unmask_plan, restored)
    assert extract_docx_text(restored).text == original_text
