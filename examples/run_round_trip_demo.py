r"""Create real Original/Masked/Restored samples for PDF, DOCX, and XLSX.

Run from the repository root:
    python examples/run_round_trip_demo.py

Optional output directory:
    python examples/run_round_trip_demo.py --output-dir .\demo-output
"""

from __future__ import annotations

import argparse
from pathlib import Path
import sys
import tempfile

import pymupdf
from docx import Document
from openpyxl import Workbook

# Support direct execution from a source checkout without requiring an
# editable install. The documented setup still installs the package.
PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = PROJECT_ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from document_anonymizer.adapters.docx import (  # noqa: E402
    apply_docx_masking_plan,
    apply_docx_unmasking_plan,
    build_docx_masking_plan,
    build_docx_unmasking_plan,
    extract_docx_text,
)
from document_anonymizer.adapters.pdf import (  # noqa: E402
    build_pdf_masking_plan,
    extract_pdf_text,
    insert_pdf_tokens,
    redact_pdf,
    restore_pdf,
)
from document_anonymizer.adapters.xlsx import (  # noqa: E402
    apply_xlsx_masking_plan,
    apply_xlsx_unmasking_plan,
    build_xlsx_masking_plan,
    build_xlsx_unmasking_plan,
    extract_xlsx_text,
)
from document_anonymizer.core.engine import MaskingEngine  # noqa: E402
from document_anonymizer.core.rules import MaskingRule, RuleSet  # noqa: E402
from document_anonymizer.core.vault import MaskingVault  # noqa: E402


SENSITIVE_VALUES = (
    "Alex Morgan",
    "alex.morgan@example.test",
    "+1-202-555-0147",
    "ID-EXAMPLE-2048",
    "42 Example Avenue",
)


def make_engine() -> MaskingEngine:
    rules = RuleSet(
        tuple(
            MaskingRule(
                rule_id=f"demo-{index + 1}",
                kind="literal",
                pattern=value,
            )
            for index, value in enumerate(SENSITIVE_VALUES)
        )
    )
    return MaskingEngine(MaskingVault(rules))


def assert_masked_and_restored(
    masked_text: str,
    restored_text: str,
    engine: MaskingEngine,
    format_name: str,
) -> None:
    for value in SENSITIVE_VALUES:
        if value in masked_text:
            raise RuntimeError(
                f"{format_name}: sensitive value remains in masked output: {value}"
            )
        if value not in restored_text:
            raise RuntimeError(
                f"{format_name}: sensitive value missing after restoration: {value}"
            )

    for token in engine.vault.token_to_original:
        if token not in masked_text:
            raise RuntimeError(
                f"{format_name}: expected masking token missing: {token}"
            )
        if token in restored_text:
            raise RuntimeError(
                f"{format_name}: masking token remains after restoration: {token}"
            )


def create_pdf_original(path: Path) -> None:
    document = pymupdf.open()
    page = document.new_page(width=595, height=420)
    page.insert_text((60, 55), "DOCUMENT ANONYMIZER - PDF DEMO", fontsize=16)

    lines = (
        "Customer: Alex Morgan",
        "Email: alex.morgan@example.test",
        "Phone: +1-202-555-0147",
        "Reference: ID-EXAMPLE-2048",
        "Address: 42 Example Avenue",
        "Secondary contact: Alex Morgan",
    )
    for index, line in enumerate(lines):
        page.insert_text((60, 105 + index * 38), line, fontsize=12)

    document.save(path)
    document.close()


def run_pdf_demo(output_dir: Path) -> None:
    original = output_dir / "Original.pdf"
    masked = output_dir / "Masked.pdf"
    restored = output_dir / "Restored.pdf"
    create_pdf_original(original)

    engine = make_engine()
    extracted = extract_pdf_text(original)
    plan = build_pdf_masking_plan(extracted, engine)

    with tempfile.TemporaryDirectory(prefix="document-anonymizer-pdf-") as temp:
        redacted = Path(temp) / "redacted.pdf"
        redact_pdf(plan, redacted)
        insert_pdf_tokens(redacted, plan, masked)

    original_text = "\n".join(page.text for page in extracted.pages)
    masked_text = "\n".join(page.text for page in extract_pdf_text(masked).pages)
    restore_pdf(masked, plan, restored)
    restored_text = "\n".join(page.text for page in extract_pdf_text(restored).pages)
    assert_masked_and_restored(masked_text, restored_text, engine, "PDF")
    if " ".join(original_text.split()) != " ".join(restored_text.split()):
        raise RuntimeError("PDF: restored text does not match the original content")


def create_docx_original(path: Path) -> None:
    document = Document()
    document.add_heading("Customer Record", level=1)
    document.add_paragraph("Customer: Alex Morgan")
    document.add_paragraph("Email: alex.morgan@example.test")
    document.add_paragraph("Phone: +1-202-555-0147")
    document.add_paragraph("Reference: ID-EXAMPLE-2048")
    document.add_paragraph("Address: 42 Example Avenue")
    document.add_paragraph("Secondary contact: Alex Morgan")

    table = document.add_table(rows=2, cols=2)
    table.cell(0, 0).text = "Field"
    table.cell(0, 1).text = "Value"
    table.cell(1, 0).text = "Customer"
    table.cell(1, 1).text = "Alex Morgan"

    section = document.sections[0]
    section.header.paragraphs[0].text = "Confidential customer file"
    section.footer.paragraphs[0].text = "Contact: alex.morgan@example.test"
    document.save(path)


def run_docx_demo(output_dir: Path) -> None:
    original = output_dir / "Original.docx"
    masked = output_dir / "Masked.docx"
    restored = output_dir / "Restored.docx"
    create_docx_original(original)

    engine = make_engine()
    extracted = extract_docx_text(original)
    masking_plan = build_docx_masking_plan(extracted, engine)
    apply_docx_masking_plan(masking_plan, masked)

    masked_extracted = extract_docx_text(masked)
    unmasking_plan = build_docx_unmasking_plan(masked_extracted, engine)
    apply_docx_unmasking_plan(unmasking_plan, restored)

    masked_text = extract_docx_text(masked).text
    restored_text = extract_docx_text(restored).text
    assert_masked_and_restored(masked_text, restored_text, engine, "DOCX")
    if restored_text != extracted.text:
        raise RuntimeError("DOCX: restored text does not match the original content")


def create_xlsx_original(path: Path) -> None:
    workbook = Workbook()
    customers = workbook.active
    customers.title = "Customers"
    rows = (
        ("Field", "Value"),
        ("Customer", "Alex Morgan"),
        ("Email", "alex.morgan@example.test"),
        ("Phone", "+1-202-555-0147"),
        ("Reference", "ID-EXAMPLE-2048"),
        ("Address", "42 Example Avenue"),
        ("Secondary contact", "Alex Morgan"),
    )
    for row in rows:
        customers.append(row)

    notes = workbook.create_sheet("Notes")
    notes["A1"] = "Follow-up contact"
    notes["B1"] = "alex.morgan@example.test"
    notes["A2"] = "Customer name"
    notes["B2"] = "Alex Morgan"
    workbook.save(path)
    workbook.close()


def run_xlsx_demo(output_dir: Path) -> None:
    original = output_dir / "Original.xlsx"
    masked = output_dir / "Masked.xlsx"
    restored = output_dir / "Restored.xlsx"
    create_xlsx_original(original)

    engine = make_engine()
    extracted = extract_xlsx_text(original)
    masking_plan = build_xlsx_masking_plan(extracted, engine)
    apply_xlsx_masking_plan(masking_plan, masked)

    masked_extracted = extract_xlsx_text(masked)
    unmasking_plan = build_xlsx_unmasking_plan(masked_extracted, engine)
    apply_xlsx_unmasking_plan(unmasking_plan, restored)

    masked_text = extract_xlsx_text(masked).text
    restored_text = extract_xlsx_text(restored).text
    assert_masked_and_restored(masked_text, restored_text, engine, "XLSX")
    if restored_text != extracted.text:
        raise RuntimeError("XLSX: restored text does not match the original content")


def write_demo_readme(output_root: Path) -> None:
    content = r"""# Document Anonymizer — Round-Trip Demo

This package contains nine real files generated through the project's PDF,
DOCX, and XLSX adapters. All personal-looking data is synthetic.

## Files

- `PDF/Original.pdf`, `PDF/Masked.pdf`, `PDF/Restored.pdf`
- `DOCX/Original.docx`, `DOCX/Masked.docx`, `DOCX/Restored.docx`
- `XLSX/Original.xlsx`, `XLSX/Masked.xlsx`, `XLSX/Restored.xlsx`

## What is demonstrated

- **PDF:** text extraction, coordinate mapping, permanent redaction, token
  insertion, and restoration of the original page regions. PDF restoration
  requires the preserved original PDF because redaction permanently removes
  the original glyphs from the masked copy. The new `restore_pdf()` API checks
  the masking tokens and source fingerprint before restoring those regions.
- **DOCX:** masking and restoration in a separate copy, including body text,
  a table, header, and footer.
- **XLSX:** masking and restoration in a separate copy across multiple sheets.

The original input files are never overwritten. The masked and restored files
are separate outputs.

## Run the demonstration manually (Windows PowerShell)

From the repository root:

```powershell
conda activate document-anonymizer
python -m pip install -e ".[dev]"
python examples\run_round_trip_demo.py
```

By default, outputs are written to `demo_output` in the repository root. To
choose another folder:

```powershell
python examples\run_round_trip_demo.py --output-dir .\my-demo-output
```

The chosen output directory must be empty or not yet exist. The script refuses
to overwrite existing sample files.

## Run tests

```powershell
python -m pytest
```

Run only one adapter's tests:

```powershell
python -m pytest tests\\unit\\adapters\\test_pdf.py -v
python -m pytest tests\\unit\\adapters\\test_docx.py -v
python -m pytest tests\\unit\\adapters\\test_xlsx.py -v
```
"""
    (output_root / "README.md").write_text(content, encoding="utf-8")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Generate PDF, DOCX, and XLSX round-trip demo files."
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=PROJECT_ROOT / "demo_output",
        help="Output folder (default: ./demo_output)",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    output_root = args.output_dir.expanduser().resolve()

    if output_root.exists() and any(output_root.iterdir()):
        print(
            f"ERROR: output directory is not empty: {output_root}\n"
            "Choose another --output-dir or move the existing files first.",
            file=sys.stderr,
        )
        return 2

    output_root.mkdir(parents=True, exist_ok=True)
    pdf_dir = output_root / "PDF"
    docx_dir = output_root / "DOCX"
    xlsx_dir = output_root / "XLSX"
    for folder in (pdf_dir, docx_dir, xlsx_dir):
        folder.mkdir(parents=True, exist_ok=True)

    run_pdf_demo(pdf_dir)
    print("[OK] PDF: Original.pdf, Masked.pdf, Restored.pdf")

    run_docx_demo(docx_dir)
    print("[OK] DOCX: Original.docx, Masked.docx, Restored.docx")

    run_xlsx_demo(xlsx_dir)
    print("[OK] XLSX: Original.xlsx, Masked.xlsx, Restored.xlsx")

    write_demo_readme(output_root)
    print(f"\nAll 9 sample files were created and verified in:\n{output_root}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
