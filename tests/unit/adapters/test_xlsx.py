
import hashlib

import pytest

from openpyxl import Workbook, load_workbook
from openpyxl.comments import Comment

from document_anonymizer.adapters.xlsx import (
    XLSXCellText,
    XLSXFormulaCell,
    XLSXSkippedText,
    XLSXWorksheetText,
    XLSXTextDocument,
    XLSXExtractionError,
    extract_xlsx_text,
)

from copy import copy

from openpyxl.styles import Font 

from document_anonymizer.adapters.xlsx import (
    XLSXCellMaskPlan,
    XLSXWorksheetMaskPlan,
    XLSXMaskPlan,
    XLSXAdapterError,
    build_xlsx_masking_plan,
    apply_xlsx_masking_plan,
)

from document_anonymizer.adapters.xlsx import (
    XLSXMappedRestoration,
    XLSXCellUnmaskPlan,
    XLSXWorksheetUnmaskPlan,
    XLSXUnmaskPlan,
    build_xlsx_unmasking_plan,
    apply_xlsx_unmasking_plan,
)

from document_anonymizer.core.engine import MaskingEngine
from document_anonymizer.core.models import MaskResult
from document_anonymizer.core.rules import MaskingRule, RuleSet
from document_anonymizer.core.vault import MaskingVault

from document_anonymizer.core.models import RestoreResult
from document_anonymizer.core.tokens import TOKEN_PATTERN


def create_workbook(path):

    workbook = Workbook()

    ws = workbook.active
    ws.title = "Customers"

    ws["A1"] = "Jane Doe"
    ws["B1"] = "jane@example.com"
    ws["C1"] = "0912345678"

    ws["A2"] = "John Smith"
    ws["B2"] = "john@example.com"

    ws["C2"] = 12500
    ws["D2"] = 450.75

    ws["E2"] = "=SUM(C2,D2)"

    ws["F2"] = "2026-11-18"

    # Second worksheet.
    products = workbook.create_sheet("Products")

    products["A1"] = "Laptop"
    products["B1"] = "Electronics"

    # Hidden worksheet.
    hidden = workbook.create_sheet("Internal")
    hidden["A1"] = "Internal Customer Data"
    hidden.sheet_state = "hidden"

    workbook.save(path)

    return path


def test_extract_simple_xlsx_text(tmp_path):

    path = create_workbook(
        tmp_path / "simple.xlsx"
    )

    result = extract_xlsx_text(path)

    assert isinstance(result, XLSXTextDocument)

    assert result.worksheet_count == 3

    assert result.text_cell_count == 9

    assert result.formula_count == 1

    customers = result.worksheets[0]

    assert isinstance(customers, XLSXWorksheetText)

    assert customers.title == "Customers"

    assert customers.cells[0].value == "Jane Doe"

    assert customers.cells[0].coordinate == "A1"


def test_extract_multiple_worksheets(tmp_path):

    path = create_workbook(
        tmp_path / "multiple.xlsx"
    )

    result = extract_xlsx_text(path)

    assert [
        worksheet.title
        for worksheet in result.worksheets
    ] == [
        "Customers",
        "Products",
        "Internal",
    ]


def test_extract_hidden_worksheets(tmp_path):

    path = create_workbook(
        tmp_path / "hidden.xlsx"
    )

    result = extract_xlsx_text(path)

    hidden = result.worksheets[2]

    assert hidden.title == "Internal"

    assert hidden.state == "hidden"

    assert hidden.text_cell_count == 1

    assert hidden.cells[0].value == "Internal Customer Data"


def test_extract_cell_coordinates(tmp_path):

    path = create_workbook(
        tmp_path / "coordinates.xlsx"
    )

    result = extract_xlsx_text(path)

    customers = result.worksheets[0]

    cell = next(
        item for item in customers.cells
        if item.value == "jane@example.com"
    )

    assert cell.coordinate == "B1"

    assert cell.row == 1

    assert cell.column == 2

    assert cell.worksheet_index == 0


def test_extract_cell_style_information(tmp_path):

    path = tmp_path / "styles.xlsx"

    workbook = Workbook()

    ws = workbook.active

    ws["A1"] = "Jane Doe"

    ws["A1"].number_format = "@"

    ws["A1"].font = copy(ws["A1"].font)
    ws["A1"].font = ws["A1"].font + Font(bold=True)

    workbook.save(path)

    result = extract_xlsx_text(path)

    cell = result.worksheets[0].cells[0]

    assert cell.style_id > 0

    assert cell.number_format == "@"


def test_formula_cells_are_not_extracted_as_text(tmp_path):

    path = tmp_path / "formulas.xlsx"

    workbook = Workbook()

    ws = workbook.active

    ws["A1"] = "Revenue"

    ws["A2"] = 100

    ws["A3"] = 200

    ws["A4"] = "=SUM(A2:A3)"

    workbook.save(path)

    result = extract_xlsx_text(path)

    worksheet = result.worksheets[0]

    assert worksheet.text_cell_count == 1

    assert worksheet.formula_count == 1

    assert worksheet.formula_cells[0].coordinate == "A4"

    assert worksheet.formula_cells[0].formula == "=SUM(A2:A3)"

    assert all(
        cell.coordinate != "A4"
        for cell in worksheet.cells
    )


def test_formula_expressions_are_reported_as_skipped_text(
    tmp_path,
):

    path = tmp_path / "formula-skipped.xlsx"

    workbook = Workbook()

    workbook.active["A1"] = (
        '=HYPERLINK("mailto:jane@example.com","Contact")'
    )

    workbook.save(path)

    result = extract_xlsx_text(path)

    skipped = result.skipped_text_elements

    assert len(skipped) == 1

    assert skipped[0].kind == "formula"

    assert "jane@example.com" in skipped[0].text


def test_extract_merged_ranges(tmp_path):

    path = tmp_path / "merged.xlsx"

    workbook = Workbook()

    ws = workbook.active

    ws.merge_cells("A1:C1")

    ws["A1"] = "Customer Information"

    workbook.save(path)

    result = extract_xlsx_text(path)

    worksheet = result.worksheets[0]

    assert "A1:C1" in worksheet.merged_ranges

    assert worksheet.text_cell_count == 1

    assert worksheet.cells[0].coordinate == "A1"


def test_merged_cells_are_handled_safely(tmp_path):

    path = tmp_path / "merged-cells.xlsx"

    workbook = Workbook()

    ws = workbook.active

    ws.merge_cells("A1:C2")

    ws["A1"] = "Merged Customer Data"

    ws["D1"] = "Jane Doe"

    workbook.save(path)

    result = extract_xlsx_text(path)

    worksheet = result.worksheets[0]

    assert worksheet.text_cell_count == 2

    assert {
        cell.coordinate
        for cell in worksheet.cells
    } == {"A1", "D1"}


def test_hyperlink_display_text_is_extracted(
    tmp_path,
):

    path = tmp_path / "hyperlink.xlsx"

    workbook = Workbook()

    ws = workbook.active

    ws["A1"] = "Contact Jane"

    ws["A1"].hyperlink = (
        "https://example.com/contact"
    )

    workbook.save(path)

    result = extract_xlsx_text(path)

    cell = result.worksheets[0].cells[0]

    assert cell.value == "Contact Jane"

    assert cell.has_hyperlink is True

    assert cell.hyperlink_target == (
        "https://example.com/contact"
    )


def test_hyperlink_target_is_reported_as_skipped(
    tmp_path,
):

    path = tmp_path / "hyperlink-target.xlsx"

    workbook = Workbook()

    ws = workbook.active

    ws["A1"] = "Contact"

    ws["A1"].hyperlink = (
        "mailto:jane@example.com"
    )

    workbook.save(path)

    result = extract_xlsx_text(path)

    skipped = result.skipped_text_elements

    assert any(
        item.kind == "hyperlink_target"
        and item.text == "mailto:jane@example.com"
        for item in skipped
    )


def test_comments_are_reported_as_skipped(tmp_path):

    path = tmp_path / "comments.xlsx"

    workbook = Workbook()

    ws = workbook.active

    ws["A1"] = "Customer"

    ws["A1"].comment = Comment(
        "Customer email: jane@example.com",
        "Admin",
    )

    workbook.save(path)

    result = extract_xlsx_text(path)

    skipped = result.skipped_text_elements

    assert len(skipped) == 1

    assert skipped[0].kind == "comment"

    assert "jane@example.com" in skipped[0].text


def test_numeric_cells_are_ignored(tmp_path):

    path = tmp_path / "numeric.xlsx"

    workbook = Workbook()

    ws = workbook.active

    ws["A1"] = "Revenue"

    ws["A2"] = 1000

    ws["A3"] = 45.75

    ws["A4"] = True

    workbook.save(path)

    result = extract_xlsx_text(path)

    assert result.text_cell_count == 1

    assert result.worksheets[0].cells[0].value == "Revenue"


def test_unicode_text_is_preserved(tmp_path):

    path = tmp_path / "unicode.xlsx"

    workbook = Workbook()

    ws = workbook.active

    ws["A1"] = "علی رضایی"

    ws["A2"] = "東京"

    ws["A3"] = "Müller"

    workbook.save(path)

    result = extract_xlsx_text(path)

    assert result.text_cell_count == 3

    assert result.worksheets[0].cells[0].value == "علی رضایی"

    assert result.worksheets[0].cells[1].value == "東京"


def test_whitespace_only_string_is_preserved(tmp_path):

    path = tmp_path / "whitespace.xlsx"

    workbook = Workbook()

    workbook.active["A1"] = "   "

    workbook.save(path)

    result = extract_xlsx_text(path)

    assert result.text_cell_count == 1

    assert result.worksheets[0].cells[0].value == "   "


def test_empty_workbook_is_supported(tmp_path):

    path = tmp_path / "empty.xlsx"

    workbook = Workbook()

    workbook.save(path)

    result = extract_xlsx_text(path)

    assert result.worksheet_count == 1

    assert result.text_cell_count == 0

    assert result.total_character_count == 0


def test_source_path_is_absolute(tmp_path):

    path = create_workbook(
        tmp_path / "absolute.xlsx"
    )

    result = extract_xlsx_text(path)

    assert result.source_path.is_absolute()


def test_source_fingerprint_is_correct(tmp_path):

    path = create_workbook(
        tmp_path / "fingerprint.xlsx"
    )

    result = extract_xlsx_text(path)

    expected = hashlib.sha256(
        path.read_bytes()
    ).hexdigest()

    assert result.source_sha256 == expected


def test_extraction_does_not_modify_source(tmp_path):

    path = create_workbook(
        tmp_path / "unchanged.xlsx"
    )

    original_bytes = path.read_bytes()

    extract_xlsx_text(path)

    assert path.read_bytes() == original_bytes


def test_missing_xlsx_raises_error(tmp_path):

    path = tmp_path / "missing.xlsx"

    with pytest.raises(FileNotFoundError):

        extract_xlsx_text(path)


def test_non_xlsx_file_is_rejected(tmp_path):

    path = tmp_path / "document.docx"

    path.write_text("Not an Excel file")

    with pytest.raises(ValueError):

        extract_xlsx_text(path)


def test_invalid_xlsx_is_rejected(tmp_path):

    path = tmp_path / "invalid.xlsx"

    path.write_text(
        "This is not a valid Excel workbook"
    )

    with pytest.raises(XLSXExtractionError):

        extract_xlsx_text(path)


def test_invalid_source_type_is_rejected():

    with pytest.raises(TypeError):

        extract_xlsx_text(123)


def test_document_text_contains_cell_locations(tmp_path):

    path = tmp_path / "locations.xlsx"

    workbook = Workbook()

    ws = workbook.active

    ws.title = "Customers"

    ws["B3"] = "Jane Doe"

    workbook.save(path)

    result = extract_xlsx_text(path)

    assert result.text == (
        "[Customers!B3] Jane Doe"
    )


def test_formula_and_comment_metadata_are_preserved(
    tmp_path,
):

    path = tmp_path / "metadata.xlsx"

    workbook = Workbook()

    ws = workbook.active

    ws["A1"] = "Revenue"

    ws["A2"] = "=SUM(1,2)"

    ws["A1"].comment = Comment(
        "Internal note",
        "Admin",
    )

    workbook.save(path)

    result = extract_xlsx_text(path)

    assert result.formula_count == 1

    assert len(result.skipped_text_elements) == 2

    # Extraction must not alter workbook metadata.
    reopened = load_workbook(path, data_only=False)

    assert reopened.active["A2"].value == "=SUM(1,2)"

    assert reopened.active["A1"].comment.text == "Internal note"

    reopened.close()



def create_masking_engine():

    ruleset = RuleSet((
        MaskingRule(
            rule_id="person",
            kind="literal",
            pattern="Jane Doe",
            priority=200,
        ),
        MaskingRule(
            rule_id="email",
            kind="regex",
            pattern=r"[\w.+-]+@[\w.-]+\.\w+",
            priority=100,
            case_sensitive=False,
        ),
    ))

    vault = MaskingVault(
        ruleset,
        vault_id="xlsx-test-vault",
    )

    return MaskingEngine(vault)


class FakeXLSXUnmaskEngine:
    """Test double for XLSX restoration."""

    def __init__(
        self,
        token_mapping,
        vault_id="xlsx-test-vault",
        result_vault_id=None,
    ):

        self.vault = type(
            "FakeVault",
            (),
            {},
        )()

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


def create_plan_workbook(path):

    workbook = Workbook()

    ws = workbook.active
    ws.title = "Customers"

    ws["A1"] = "Jane Doe"
    ws["B1"] = "jane@example.com"
    ws["C1"] = "No sensitive data"

    ws["A2"] = "Jane Doe and Jane Doe"

    products = workbook.create_sheet("Products")

    products["A1"] = "Laptop"
    products["B1"] = "Jane Doe"

    workbook.save(path)

    return path


def test_build_xlsx_masking_plan(tmp_path):

    path = create_plan_workbook(
        tmp_path / "plan.xlsx"
    )

    extracted = extract_xlsx_text(path)

    engine = create_masking_engine()

    plan = build_xlsx_masking_plan(
        extracted,
        engine,
    )

    assert isinstance(plan, XLSXMaskPlan)

    assert plan.worksheet_count == 2

    assert plan.replacements_count == 5

    assert plan.masked_cell_count == 4


def test_xlsx_plan_masks_single_cell(tmp_path):

    path = tmp_path / "single.xlsx"

    workbook = Workbook()

    workbook.active["A1"] = "Customer: Jane Doe"

    workbook.save(path)

    extracted = extract_xlsx_text(path)

    plan = build_xlsx_masking_plan(
        extracted,
        create_masking_engine(),
    )

    cell = plan.worksheets[0].cells[0]

    assert cell.original_value == "Customer: Jane Doe"

    assert cell.masked_value != cell.original_value

    assert len(cell.replacements) == 1

    assert cell.replacements[0].original == "Jane Doe"

    assert cell.coordinate == "A1"


def test_xlsx_plan_masks_multiple_worksheets(tmp_path):

    path = create_plan_workbook(
        tmp_path / "multiple-plan.xlsx"
    )

    extracted = extract_xlsx_text(path)

    plan = build_xlsx_masking_plan(
        extracted,
        create_masking_engine(),
    )

    assert plan.worksheet_count == 2

    customers = plan.worksheets[0]
    products = plan.worksheets[1]

    assert customers.title == "Customers"

    assert products.title == "Products"

    assert customers.masked_cell_count == 3

    assert products.masked_cell_count == 1


def test_xlsx_plan_reuses_token_for_repeated_values(
    tmp_path,
):

    path = tmp_path / "repeated.xlsx"

    workbook = Workbook()

    ws = workbook.active

    ws["A1"] = "Jane Doe"

    ws["A2"] = "Jane Doe"

    workbook.save(path)

    plan = build_xlsx_masking_plan(
        extract_xlsx_text(path),
        create_masking_engine(),
    )

    first = plan.worksheets[0].cells[0]

    second = plan.worksheets[0].cells[1]

    assert first.masked_value == second.masked_value

    assert first.replacements[0].token == (
        second.replacements[0].token
    )

    assert len(plan.unique_tokens) == 1


def test_xlsx_plan_multiple_matches_in_one_cell(
    tmp_path,
):

    path = tmp_path / "multiple-matches.xlsx"

    workbook = Workbook()

    workbook.active["A1"] = (
        "Jane Doe contacted Jane Doe"
    )

    workbook.save(path)

    plan = build_xlsx_masking_plan(
        extract_xlsx_text(path),
        create_masking_engine(),
    )

    cell = plan.worksheets[0].cells[0]

    assert len(cell.replacements) == 2

    assert cell.replacements[0].start < (
        cell.replacements[1].start
    )

    assert cell.replacements[0].token == (
        cell.replacements[1].token
    )


def test_xlsx_plan_without_matches(tmp_path):

    path = tmp_path / "no-matches.xlsx"

    workbook = Workbook()

    workbook.active["A1"] = "Ordinary text"

    workbook.save(path)

    plan = build_xlsx_masking_plan(
        extract_xlsx_text(path),
        create_masking_engine(),
    )

    cell = plan.worksheets[0].cells[0]

    assert cell.original_value == cell.masked_value

    assert cell.replacements == ()

    assert plan.replacements_count == 0

    assert plan.masked_cell_count == 0


def test_xlsx_plan_preserves_cell_coordinates(
    tmp_path,
):

    path = tmp_path / "coordinates-plan.xlsx"

    workbook = Workbook()

    workbook.active["C5"] = "Jane Doe"

    workbook.save(path)

    plan = build_xlsx_masking_plan(
        extract_xlsx_text(path),
        create_masking_engine(),
    )

    cell = plan.worksheets[0].cells[0]

    assert cell.coordinate == "C5"

    assert cell.worksheet_index == 0

    assert cell.worksheet_title == "Sheet"


def test_xlsx_plan_preserves_source_cell_metadata(
    tmp_path,
):

    path = tmp_path / "metadata-plan.xlsx"

    workbook = Workbook()

    ws = workbook.active

    ws["A1"] = "Jane Doe"

    ws["A1"].number_format = "@"

    ws["A1"].font = Font(bold=True)

    workbook.save(path)

    plan = build_xlsx_masking_plan(
        extract_xlsx_text(path),
        create_masking_engine(),
    )

    cell = plan.worksheets[0].cells[0]

    assert cell.source_cell.number_format == "@"

    assert cell.source_cell.style_id > 0


def test_xlsx_plan_preserves_formula_metadata(
    tmp_path,
):

    path = tmp_path / "formula-plan.xlsx"

    workbook = Workbook()

    ws = workbook.active

    ws["A1"] = "Jane Doe"

    ws["A2"] = "=CONCAT(A1)"

    workbook.save(path)

    plan = build_xlsx_masking_plan(
        extract_xlsx_text(path),
        create_masking_engine(),
    )

    worksheet = plan.worksheets[0]

    assert worksheet.source_worksheet.formula_count == 1

    assert worksheet.source_worksheet.formula_cells[0].formula == (
        "=CONCAT(A1)"
    )


def test_xlsx_plan_does_not_modify_source(tmp_path):

    path = create_plan_workbook(
        tmp_path / "unchanged-plan.xlsx"
    )

    original_bytes = path.read_bytes()

    extracted = extract_xlsx_text(path)

    build_xlsx_masking_plan(
        extracted,
        create_masking_engine(),
    )

    assert path.read_bytes() == original_bytes


def test_xlsx_plan_reserves_entire_workbook_before_masking(
    tmp_path,
):

    path = tmp_path / "reservation.xlsx"

    workbook = Workbook()

    ws = workbook.active

    ws["A1"] = "Jane Doe"

    ws["A2"] = "John Smith"

    workbook.save(path)

    extracted = extract_xlsx_text(path)

    expected_text = "Jane Doe\nJohn Smith"

    class TrackingVault:

        vault_id = "tracking-vault"

        def __init__(self):
            self.reserved_texts = []

        def reserve_source_text(self, text):
            self.reserved_texts.append(text)

    class TrackingEngine:

        def __init__(self):
            self.vault = TrackingVault()
            self.mask_calls = 0

        def mask_text(self, text):

            self.mask_calls += 1

            assert self.vault.reserved_texts == [
                expected_text
            ]

            return MaskResult(
                vault_id=self.vault.vault_id,
                masked_text=text,
                replacements_count=0,
            )

    engine = TrackingEngine()

    build_xlsx_masking_plan(
        extracted,
        engine,
    )

    assert engine.vault.reserved_texts == [
        expected_text
    ]

    assert engine.mask_calls == 2


def test_xlsx_plan_rejects_wrong_vault_result(
    tmp_path,
):

    path = tmp_path / "wrong-vault.xlsx"

    workbook = Workbook()

    workbook.active["A1"] = "Jane Doe"

    workbook.save(path)

    class FakeVault:

        vault_id = "expected-vault"

        def reserve_source_text(self, text):
            pass

    class FakeEngine:

        vault = FakeVault()

        def mask_text(self, text):

            return MaskResult(
                vault_id="another-vault",
                masked_text=text,
                replacements_count=0,
            )

    with pytest.raises(XLSXAdapterError):

        build_xlsx_masking_plan(
            extract_xlsx_text(path),
            FakeEngine(),
        )


def test_xlsx_plan_rejects_invalid_engine(tmp_path):

    path = tmp_path / "invalid-engine.xlsx"

    workbook = Workbook()

    workbook.active["A1"] = "Jane Doe"

    workbook.save(path)

    with pytest.raises(TypeError):

        build_xlsx_masking_plan(
            extract_xlsx_text(path),
            object(),
        )


def test_xlsx_plan_rejects_invalid_extracted_document():

    with pytest.raises(TypeError):

        build_xlsx_masking_plan(
            "not an XLSX document",
            create_masking_engine(),
        )


def test_xlsx_plan_rejects_masked_hyperlink(
    tmp_path,
):

    path = tmp_path / "hyperlink-plan.xlsx"

    workbook = Workbook()

    cell = workbook.active["A1"]

    cell.value = "Jane Doe"

    cell.hyperlink = "mailto:jane@example.com"

    workbook.save(path)

    with pytest.raises(
        XLSXAdapterError,
        match="hyperlink display text",
    ):

        build_xlsx_masking_plan(
            extract_xlsx_text(path),
            create_masking_engine(),
        )


def test_xlsx_plan_original_and_masked_text(
    tmp_path,
):

    path = tmp_path / "text-properties.xlsx"

    workbook = Workbook()

    ws = workbook.active

    ws["A1"] = "Jane Doe"

    ws["A2"] = "Ordinary text"

    workbook.save(path)

    plan = build_xlsx_masking_plan(
        extract_xlsx_text(path),
        create_masking_engine(),
    )

    assert "[Sheet!A1] Jane Doe" in plan.original_text

    assert "[Sheet!A1] Jane Doe" not in plan.masked_text

    assert "[Sheet!A2] Ordinary text" in plan.masked_text



def test_apply_xlsx_masking_plan_creates_masked_copy(tmp_path):

    source = create_plan_workbook(
        tmp_path / "source.xlsx"
    )

    original_bytes = source.read_bytes()

    plan = build_xlsx_masking_plan(
        extract_xlsx_text(source),
        create_masking_engine(),
    )

    output = tmp_path / "masked.xlsx"

    result = apply_xlsx_masking_plan(
        plan,
        output,
    )

    assert result == output.resolve()
    assert output.exists()

    # Original must remain unchanged.
    assert source.read_bytes() == original_bytes

    workbook = load_workbook(output, data_only=False)

    try:

        for worksheet_plan in plan.worksheets:

            worksheet = workbook[worksheet_plan.title]

            for cell_plan in worksheet_plan.cells:

                assert (
                    worksheet[cell_plan.coordinate].value
                    == cell_plan.masked_value
                )

    finally:
        workbook.close()


def test_apply_xlsx_masking_plan_preserves_workbook_metadata(
    tmp_path,
):

    source = tmp_path / "metadata.xlsx"

    workbook = Workbook()

    ws = workbook.active
    ws.title = "Customers"

    ws["A1"] = "Jane Doe"
    ws["A1"].font = Font(bold=True)
    ws["A1"].number_format = "@"

    ws["B1"] = "Contact"
    ws["B1"].hyperlink = "https://example.com/contact"

    ws["C1"] = "=SUM(1,2)"

    ws["D1"] = "Commented cell"
    ws["D1"].comment = Comment(
        "Internal note",
        "Admin",
    )

    ws.merge_cells("E1:F1")
    ws["E1"] = "Merged content"

    hidden = workbook.create_sheet("Hidden")
    hidden["A1"] = "Private data"
    hidden.sheet_state = "hidden"

    workbook.save(source)

    plan = build_xlsx_masking_plan(
        extract_xlsx_text(source),
        create_masking_engine(),
    )

    output = tmp_path / "metadata-masked.xlsx"

    apply_xlsx_masking_plan(plan, output)

    result = load_workbook(output, data_only=False)

    try:

        customers = result["Customers"]

        assert customers["A1"].value != "Jane Doe"
        assert customers["A1"].font.bold is True
        assert customers["A1"].number_format == "@"

        assert customers["B1"].hyperlink.target == (
            "https://example.com/contact"
        )

        assert customers["C1"].value == "=SUM(1,2)"

        assert customers["D1"].comment.text == "Internal note"

        assert "E1:F1" in [
            str(item)
            for item in customers.merged_cells.ranges
        ]

        assert result["Hidden"].sheet_state == "hidden"

        assert result.sheetnames == [
            "Customers",
            "Hidden",
        ]

    finally:
        result.close()


def test_apply_xlsx_masking_plan_rejects_modified_source(
    tmp_path,
):

    source = create_plan_workbook(
        tmp_path / "modified.xlsx"
    )

    plan = build_xlsx_masking_plan(
        extract_xlsx_text(source),
        create_masking_engine(),
    )

    workbook = load_workbook(source)

    workbook["Customers"]["A1"] = "Modified Data"

    workbook.save(source)
    workbook.close()

    output = tmp_path / "masked.xlsx"

    with pytest.raises(
        XLSXAdapterError,
        match="changed",
    ):

        apply_xlsx_masking_plan(plan, output)

    assert not output.exists()


def test_apply_xlsx_masking_plan_does_not_overwrite_output(
    tmp_path,
):

    source = create_plan_workbook(
        tmp_path / "source.xlsx"
    )

    plan = build_xlsx_masking_plan(
        extract_xlsx_text(source),
        create_masking_engine(),
    )

    output = tmp_path / "existing.xlsx"

    output.write_text("Existing content")

    with pytest.raises(FileExistsError):

        apply_xlsx_masking_plan(plan, output)

    assert output.read_text() == "Existing content"


def test_apply_xlsx_masking_plan_rejects_source_as_output(
    tmp_path,
):

    source = create_plan_workbook(
        tmp_path / "source.xlsx"
    )

    plan = build_xlsx_masking_plan(
        extract_xlsx_text(source),
        create_masking_engine(),
    )

    original_bytes = source.read_bytes()

    with pytest.raises(
        XLSXAdapterError,
        match="different",
    ):

        apply_xlsx_masking_plan(plan, source)

    assert source.read_bytes() == original_bytes



def test_xlsx_unmask_plan_without_tokens(tmp_path):

    path = tmp_path / "no-tokens.xlsx"

    workbook = Workbook()

    workbook.active["A1"] = "Nothing masked here"

    workbook.save(path)

    engine = FakeXLSXUnmaskEngine({})

    plan = build_xlsx_unmasking_plan(
        extract_xlsx_text(path),
        engine,
    )

    assert isinstance(plan, XLSXUnmaskPlan)

    assert plan.restored_count == 0

    assert plan.unknown_tokens == ()

    assert plan.masked_text == plan.restored_text


def test_xlsx_unmask_plan_maps_single_token(tmp_path):

    path = tmp_path / "single-token.xlsx"

    token = f"[[MASK_{'A' * 32}]]"

    workbook = Workbook()

    workbook.active["A1"] = f"Hello {token}!"

    workbook.save(path)

    engine = FakeXLSXUnmaskEngine({
        token: "Jane Doe",
    })

    plan = build_xlsx_unmasking_plan(
        extract_xlsx_text(path),
        engine,
    )

    cell = plan.worksheets[0].cells[0]

    assert isinstance(cell, XLSXCellUnmaskPlan)

    assert cell.masked_value == f"Hello {token}!"

    assert cell.restored_value == "Hello Jane Doe!"

    assert cell.restored_count == 1

    restoration = cell.restorations[0]

    assert isinstance(
        restoration,
        XLSXMappedRestoration,
    )

    assert restoration.token == token

    assert restoration.original == "Jane Doe"

    assert restoration.coordinate == "A1"


def test_xlsx_unmask_plan_maps_multiple_tokens(tmp_path):

    path = tmp_path / "multiple-tokens.xlsx"

    token_a = f"[[MASK_{'A' * 32}]]"
    token_b = f"[[MASK_{'B' * 32}]]"

    workbook = Workbook()

    workbook.active["A1"] = (
        f"{token_a} met {token_b} and {token_a}"
    )

    workbook.save(path)

    engine = FakeXLSXUnmaskEngine({
        token_a: "Jane Doe",
        token_b: "John Smith",
    })

    plan = build_xlsx_unmasking_plan(
        extract_xlsx_text(path),
        engine,
    )

    cell = plan.worksheets[0].cells[0]

    assert plan.restored_count == 3

    assert cell.restored_value == (
        "Jane Doe met John Smith and Jane Doe"
    )

    assert len(cell.restorations) == 3

    assert (
        cell.restorations[0].original
        == cell.restorations[2].original
    )


def test_xlsx_unmask_plan_preserves_unknown_tokens(
    tmp_path,
):

    path = tmp_path / "unknown-tokens.xlsx"

    known = f"[[MASK_{'A' * 32}]]"
    unknown = f"[[MASK_{'C' * 32}]]"

    workbook = Workbook()

    workbook.active["A1"] = (
        f"{known} and {unknown}"
    )

    workbook.save(path)

    engine = FakeXLSXUnmaskEngine({
        known: "Jane Doe",
    })

    plan = build_xlsx_unmasking_plan(
        extract_xlsx_text(path),
        engine,
    )

    cell = plan.worksheets[0].cells[0]

    assert plan.restored_count == 1

    assert plan.unknown_tokens == (unknown,)

    assert cell.restored_value == (
        f"Jane Doe and {unknown}"
    )

    assert cell.unknown_tokens == (unknown,)


def test_xlsx_unmask_plan_maps_multiple_worksheets(
    tmp_path,
):

    path = tmp_path / "multiple-sheets.xlsx"

    token = f"[[MASK_{'D' * 32}]]"

    workbook = Workbook()

    ws1 = workbook.active

    ws1.title = "Customers"

    ws1["A1"] = token

    ws2 = workbook.create_sheet("Products")

    ws2["A1"] = token

    workbook.save(path)

    engine = FakeXLSXUnmaskEngine({
        token: "Jane Doe",
    })

    plan = build_xlsx_unmasking_plan(
        extract_xlsx_text(path),
        engine,
    )

    assert plan.worksheet_count == 2

    assert plan.restored_count == 2

    assert plan.worksheets[0].title == "Customers"

    assert plan.worksheets[1].title == "Products"

    assert (
        plan.worksheets[0].cells[0].restored_value
        == "Jane Doe"
    )

    assert (
        plan.worksheets[1].cells[0].restored_value
        == "Jane Doe"
    )


def test_xlsx_unmask_plan_preserves_source_locations(
    tmp_path,
):

    path = tmp_path / "locations.xlsx"

    token = f"[[MASK_{'E' * 32}]]"

    workbook = Workbook()

    workbook.active["C5"] = token

    workbook.save(path)

    plan = build_xlsx_unmasking_plan(
        extract_xlsx_text(path),
        FakeXLSXUnmaskEngine({
            token: "Jane Doe",
        }),
    )

    cell = plan.worksheets[0].cells[0]

    assert cell.coordinate == "C5"

    assert cell.worksheet_index == 0

    assert cell.worksheet_title == "Sheet"


def test_xlsx_unmask_plan_does_not_modify_source(
    tmp_path,
):

    path = tmp_path / "unchanged.xlsx"

    token = f"[[MASK_{'F' * 32}]]"

    workbook = Workbook()

    workbook.active["A1"] = token

    workbook.save(path)

    original_bytes = path.read_bytes()

    build_xlsx_unmasking_plan(
        extract_xlsx_text(path),
        FakeXLSXUnmaskEngine({
            token: "Jane Doe",
        }),
    )

    assert path.read_bytes() == original_bytes


def test_xlsx_unmask_plan_rejects_invalid_engine():

    with pytest.raises(TypeError):

        build_xlsx_unmasking_plan(
            None,
            object(),
        )


def test_xlsx_unmask_plan_rejects_invalid_document(
    tmp_path,
):

    path = tmp_path / "invalid-document.xlsx"

    workbook = Workbook()

    workbook.active["A1"] = "Some text"

    workbook.save(path)

    with pytest.raises(TypeError):

        build_xlsx_unmasking_plan(
            "not an XLSX document",
            FakeXLSXUnmaskEngine({}),
        )


def test_xlsx_unmask_plan_rejects_vault_mismatch(
    tmp_path,
):

    path = tmp_path / "vault-mismatch.xlsx"

    token = f"[[MASK_{'9' * 32}]]"

    workbook = Workbook()

    workbook.active["A1"] = token

    workbook.save(path)

    engine = FakeXLSXUnmaskEngine(
        {token: "Jane Doe"},
        vault_id="vault-a",
        result_vault_id="vault-b",
    )

    with pytest.raises(XLSXAdapterError):

        build_xlsx_unmasking_plan(
            extract_xlsx_text(path),
            engine,
        )


def test_xlsx_unmask_plan_rejects_inconsistent_result(
    tmp_path,
):

    path = tmp_path / "inconsistent.xlsx"

    token = f"[[MASK_{'8' * 32}]]"

    workbook = Workbook()

    workbook.active["A1"] = token

    workbook.save(path)

    class BadEngine(FakeXLSXUnmaskEngine):

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

    with pytest.raises(XLSXAdapterError):

        build_xlsx_unmasking_plan(
            extract_xlsx_text(path),
            engine,
        )


def test_xlsx_unmask_plan_rejects_invalid_vault_token(
    tmp_path,
):

    path = tmp_path / "invalid-mapping.xlsx"

    workbook = Workbook()

    workbook.active["A1"] = "Some text"

    workbook.save(path)

    engine = FakeXLSXUnmaskEngine({})

    engine.vault.token_to_original = {
        "NOT_A_VALID_TOKEN": "Jane Doe",
    }

    with pytest.raises(
        XLSXAdapterError,
        match="invalid token",
    ):

        build_xlsx_unmasking_plan(
            extract_xlsx_text(path),
            engine,
        )


def test_xlsx_unmask_plan_rejects_invalid_restoration_count(
    tmp_path,
):

    path = tmp_path / "invalid-count.xlsx"

    token = f"[[MASK_{'7' * 32}]]"

    workbook = Workbook()

    workbook.active["A1"] = token

    workbook.save(path)

    class BadCountEngine(FakeXLSXUnmaskEngine):

        def unmask_text(self, text):

            result = super().unmask_text(text)

            return RestoreResult(
                vault_id=result.vault_id,
                restored_text=result.restored_text,
                restored_count=99,
                unknown_tokens=result.unknown_tokens,
            )

    engine = BadCountEngine({
        token: "Jane Doe",
    })

    with pytest.raises(
        XLSXAdapterError,
        match="Restoration count",
    ):

        build_xlsx_unmasking_plan(
            extract_xlsx_text(path),
            engine,
        )

# -------------------------------------------------
# Applying XLSX unmasking plans
# -------------------------------------------------


def test_apply_xlsx_unmasking_plan_creates_restored_copy(
    tmp_path,
):
    source = tmp_path / "masked-source.xlsx"

    known = f"[[MASK_{'A' * 32}]]"
    unknown = f"[[MASK_{'B' * 32}]]"

    workbook = Workbook()
    worksheet = workbook.active
    worksheet.title = "Customers"
    worksheet["A1"] = f"Customer: {known}"
    worksheet["B1"] = f"Unknown: {unknown}"
    workbook.save(source)

    original_bytes = source.read_bytes()

    plan = build_xlsx_unmasking_plan(
        extract_xlsx_text(source),
        FakeXLSXUnmaskEngine({
            known: "Jane Doe",
        }),
    )

    output = tmp_path / "restored.xlsx"

    result = apply_xlsx_unmasking_plan(plan, output)

    assert result == output.resolve()
    assert output.exists()
    assert source.read_bytes() == original_bytes

    restored = load_workbook(output, data_only=False)

    try:
        worksheet = restored["Customers"]

        assert worksheet["A1"].value == "Customer: Jane Doe"
        assert worksheet["B1"].value == f"Unknown: {unknown}"

    finally:
        restored.close()


def test_apply_xlsx_unmasking_plan_preserves_workbook_metadata(
    tmp_path,
):
    source = tmp_path / "metadata-masked.xlsx"

    token = f"[[MASK_{'C' * 32}]]"

    workbook = Workbook()
    worksheet = workbook.active
    worksheet.title = "Customers"

    worksheet["A1"] = token
    worksheet["A1"].font = Font(bold=True)
    worksheet["A1"].number_format = "@"

    worksheet["B1"] = "Contact"
    worksheet["B1"].hyperlink = "https://example.com/contact"

    worksheet["C1"] = "=SUM(1,2)"

    worksheet["D1"] = "Commented cell"
    worksheet["D1"].comment = Comment(
        "Internal note",
        "Admin",
    )

    worksheet.merge_cells("E1:F1")
    worksheet["E1"] = "Merged content"
    worksheet.freeze_panes = "B2"

    hidden = workbook.create_sheet("Hidden")
    hidden["A1"] = token
    hidden.sheet_state = "hidden"

    workbook.properties.creator = "Document Anonymizer Tests"
    workbook.properties.title = "Customer Workbook"
    workbook.properties.subject = "Restoration metadata test"

    workbook.save(source)
    workbook.close()

    plan = build_xlsx_unmasking_plan(
        extract_xlsx_text(source),
        FakeXLSXUnmaskEngine({
            token: "Jane Doe",
        }),
    )

    output = tmp_path / "metadata-restored.xlsx"

    apply_xlsx_unmasking_plan(plan, output)

    restored = load_workbook(output, data_only=False)

    try:
        customers = restored["Customers"]

        assert customers["A1"].value == "Jane Doe"
        assert customers["A1"].font.bold is True
        assert customers["A1"].number_format == "@"

        assert customers["B1"].hyperlink.target == (
            "https://example.com/contact"
        )

        assert customers["C1"].value == "=SUM(1,2)"
        assert customers["D1"].comment.text == "Internal note"

        assert "E1:F1" in [
            str(item)
            for item in customers.merged_cells.ranges
        ]

        assert customers.freeze_panes == "B2"

        assert restored["Hidden"]["A1"].value == "Jane Doe"
        assert restored["Hidden"].sheet_state == "hidden"

        assert restored.sheetnames == [
            "Customers",
            "Hidden",
        ]

        assert restored.properties.creator == (
            "Document Anonymizer Tests"
        )
        assert restored.properties.title == "Customer Workbook"
        assert restored.properties.subject == (
            "Restoration metadata test"
        )

    finally:
        restored.close()


def test_apply_xlsx_unmasking_plan_preserves_unknown_tokens(
    tmp_path,
):
    source = tmp_path / "unknown-token-source.xlsx"

    known = f"[[MASK_{'D' * 32}]]"
    unknown = f"[[MASK_{'E' * 32}]]"

    workbook = Workbook()
    workbook.active["A1"] = f"{known} / {unknown}"
    workbook.save(source)

    plan = build_xlsx_unmasking_plan(
        extract_xlsx_text(source),
        FakeXLSXUnmaskEngine({
            known: "Jane Doe",
        }),
    )

    output = tmp_path / "unknown-token-restored.xlsx"

    apply_xlsx_unmasking_plan(plan, output)

    restored = load_workbook(output, data_only=False)

    try:
        assert restored.active["A1"].value == (
            f"Jane Doe / {unknown}"
        )
    finally:
        restored.close()


def test_apply_xlsx_unmasking_plan_rejects_modified_source(
    tmp_path,
):
    source = tmp_path / "modified-source.xlsx"

    token = f"[[MASK_{'F' * 32}]]"

    workbook = Workbook()
    workbook.active["A1"] = token
    workbook.save(source)

    plan = build_xlsx_unmasking_plan(
        extract_xlsx_text(source),
        FakeXLSXUnmaskEngine({
            token: "Jane Doe",
        }),
    )

    changed = load_workbook(source)
    changed.active["A1"] = "Changed after planning"
    changed.save(source)
    changed.close()

    output = tmp_path / "should-not-exist.xlsx"

    with pytest.raises(XLSXAdapterError, match="changed"):
        apply_xlsx_unmasking_plan(plan, output)

    assert not output.exists()


def test_apply_xlsx_unmasking_plan_rejects_structure_mismatch(
    tmp_path,
):
    from dataclasses import replace

    source = tmp_path / "structure-source.xlsx"

    token = f"[[MASK_{'1' * 32}]]"

    workbook = Workbook()
    workbook.active.title = "Customers"
    workbook.active["A1"] = token
    workbook.save(source)

    plan = build_xlsx_unmasking_plan(
        extract_xlsx_text(source),
        FakeXLSXUnmaskEngine({
            token: "Jane Doe",
        }),
    )

    changed = load_workbook(source)
    changed.active.title = "Renamed"
    changed.save(source)
    changed.close()

    plan_with_current_fingerprint = replace(
        plan,
        source_sha256=hashlib.sha256(
            source.read_bytes()
        ).hexdigest(),
    )

    output = tmp_path / "structure-output.xlsx"

    with pytest.raises(XLSXAdapterError, match="metadata mismatch"):
        apply_xlsx_unmasking_plan(
            plan_with_current_fingerprint,
            output,
        )

    assert not output.exists()


def test_apply_xlsx_unmasking_plan_does_not_overwrite_output(
    tmp_path,
):
    source = tmp_path / "existing-source.xlsx"

    token = f"[[MASK_{'2' * 32}]]"

    workbook = Workbook()
    workbook.active["A1"] = token
    workbook.save(source)

    plan = build_xlsx_unmasking_plan(
        extract_xlsx_text(source),
        FakeXLSXUnmaskEngine({
            token: "Jane Doe",
        }),
    )

    output = tmp_path / "existing-output.xlsx"
    output.write_text("Existing content")

    with pytest.raises(FileExistsError):
        apply_xlsx_unmasking_plan(plan, output)

    assert output.read_text() == "Existing content"


def test_apply_xlsx_unmasking_plan_rejects_source_as_output(
    tmp_path,
):
    source = tmp_path / "same-source-output.xlsx"

    token = f"[[MASK_{'3' * 32}]]"

    workbook = Workbook()
    workbook.active["A1"] = token
    workbook.save(source)

    original_bytes = source.read_bytes()

    plan = build_xlsx_unmasking_plan(
        extract_xlsx_text(source),
        FakeXLSXUnmaskEngine({
            token: "Jane Doe",
        }),
    )

    with pytest.raises(XLSXAdapterError, match="different"):
        apply_xlsx_unmasking_plan(plan, source)

    assert source.read_bytes() == original_bytes


def test_apply_xlsx_unmasking_plan_rejects_missing_output_directory(
    tmp_path,
):
    source = tmp_path / "missing-directory-source.xlsx"

    token = f"[[MASK_{'4' * 32}]]"

    workbook = Workbook()
    workbook.active["A1"] = token
    workbook.save(source)

    plan = build_xlsx_unmasking_plan(
        extract_xlsx_text(source),
        FakeXLSXUnmaskEngine({
            token: "Jane Doe",
        }),
    )

    output = tmp_path / "missing" / "restored.xlsx"

    with pytest.raises(FileNotFoundError):
        apply_xlsx_unmasking_plan(plan, output)

    assert not output.parent.exists()


def test_apply_xlsx_unmasking_plan_rejects_invalid_plan():
    with pytest.raises(TypeError):
        apply_xlsx_unmasking_plan(
            "not an unmask plan",
            "restored.xlsx",
        )


def test_apply_xlsx_unmasking_plan_rejects_invalid_output_extension(
    tmp_path,
):
    source = tmp_path / "extension-source.xlsx"

    token = f"[[MASK_{'5' * 32}]]"

    workbook = Workbook()
    workbook.active["A1"] = token
    workbook.save(source)

    plan = build_xlsx_unmasking_plan(
        extract_xlsx_text(source),
        FakeXLSXUnmaskEngine({
            token: "Jane Doe",
        }),
    )

    with pytest.raises(ValueError, match="xlsx"):
        apply_xlsx_unmasking_plan(
            plan,
            tmp_path / "restored.xls",
        )



def test_apply_xlsx_unmasking_plan_cleans_temporary_file_on_failure(
    tmp_path,
    monkeypatch,
):
    import document_anonymizer.adapters.xlsx as xlsx_adapter

    source = tmp_path / "cleanup-source.xlsx"
    token = f"[[MASK_{'6' * 32}]]"

    workbook = Workbook()
    workbook.active["A1"] = token
    workbook.save(source)

    plan = build_xlsx_unmasking_plan(
        extract_xlsx_text(source),
        FakeXLSXUnmaskEngine({
            token: "Jane Doe",
        }),
    )

    def fail_verification(*args, **kwargs):
        raise XLSXAdapterError("forced verification failure")

    monkeypatch.setattr(
        xlsx_adapter,
        "_verify_xlsx_unmasked_workbook",
        fail_verification,
    )

    output = tmp_path / "cleanup-restored.xlsx"

    with pytest.raises(
        XLSXAdapterError,
        match="forced verification failure",
    ):
        apply_xlsx_unmasking_plan(plan, output)

    assert not output.exists()
    assert list(tmp_path.glob(".cleanup-restored.*.tmp.xlsx")) == []



def test_xlsx_unmasking_ignores_writer_generated_modified_timestamp(
    tmp_path,
    monkeypatch,
):
    from datetime import datetime as RealDateTime
    import openpyxl.writer.excel as excel_writer

    source = tmp_path / "writer-modified-source.xlsx"
    token = f"[[MASK_{'7' * 32}]]"

    workbook = Workbook()
    workbook.active["A1"] = token
    workbook.properties.creator = "Original Creator"
    workbook.properties.title = "Original Workbook"
    workbook.save(source)
    workbook.close()

    plan = build_xlsx_unmasking_plan(
        extract_xlsx_text(source),
        FakeXLSXUnmaskEngine({
            token: "Jane Doe",
        }),
    )

    class LaterDateTime(RealDateTime):
        @classmethod
        def now(cls, tz=None):
            return RealDateTime(2035, 1, 2, 3, 4, 5, tzinfo=tz)

    # openpyxl intentionally refreshes the core-property modified timestamp
    # during save. Force it to a different second to make the race deterministic.
    monkeypatch.setattr(
        excel_writer.datetime,
        "datetime",
        LaterDateTime,
    )

    output = tmp_path / "writer-modified-restored.xlsx"
    apply_xlsx_unmasking_plan(plan, output)

    restored = load_workbook(output, data_only=False)
    try:
        assert restored.active["A1"].value == "Jane Doe"
        assert restored.properties.creator == "Original Creator"
        assert restored.properties.title == "Original Workbook"
        assert restored.properties.modified == RealDateTime(
            2035, 1, 2, 3, 4, 5
        )
    finally:
        restored.close()


def test_xlsx_persian_normalization_preserves_exact_original_spelling(tmp_path):
    source = tmp_path / "persian.xlsx"
    masked = tmp_path / "persian-masked.xlsx"
    restored = tmp_path / "persian-restored.xlsx"

    workbook = Workbook()
    worksheet = workbook.active
    worksheet.title = "مشتریان"
    worksheet["A1"] = "نام مشتری"
    worksheet["B1"] = "علي كاظم"
    workbook.save(source)
    workbook.close()

    rules = RuleSet((
        MaskingRule("customer-name", "literal", "علی کاظم"),
    ))
    engine = MaskingEngine(
        MaskingVault(rules, normalization="persian")
    )

    extracted = extract_xlsx_text(source)
    plan = build_xlsx_masking_plan(extracted, engine)

    assert plan.replacements_count == 1
    cell_plan = next(
        cell
        for worksheet_plan in plan.worksheets
        for cell in worksheet_plan.cells
        if cell.source_cell.coordinate == "B1"
    )
    assert cell_plan.source_cell.value == "علي كاظم"
    assert cell_plan.masked_value == engine.vault.get_token("علي كاظم")

    apply_xlsx_masking_plan(plan, masked)
    masked_workbook = load_workbook(masked)
    assert "علي كاظم" not in str(masked_workbook["مشتریان"]["B1"].value)
    masked_workbook.close()

    unmask_plan = build_xlsx_unmasking_plan(
        extract_xlsx_text(masked),
        engine,
    )
    apply_xlsx_unmasking_plan(unmask_plan, restored)

    restored_workbook = load_workbook(restored)
    assert restored_workbook["مشتریان"]["B1"].value == "علي كاظم"
    restored_workbook.close()


def test_xlsx_persian_zwnj_optional_matching_preserves_source(tmp_path):
    source = tmp_path / "persian-zwnj.xlsx"
    masked = tmp_path / "persian-zwnj-masked.xlsx"

    workbook = Workbook()
    workbook.active["A1"] = "می‌روم"
    workbook.save(source)
    workbook.close()

    rules = RuleSet((MaskingRule("verb", "literal", "میروم"),))
    engine = MaskingEngine(
        MaskingVault(
            rules,
            normalization="persian",
            ignore_zwnj=True,
        )
    )
    plan = build_xlsx_masking_plan(extract_xlsx_text(source), engine)

    assert plan.replacements_count == 1
    cell_plan = plan.worksheets[0].cells[0]
    assert cell_plan.source_cell.value == "می‌روم"
    apply_xlsx_masking_plan(plan, masked)

    masked_workbook = load_workbook(masked)
    assert "می‌روم" not in str(masked_workbook.active["A1"].value)
    masked_workbook.close()
