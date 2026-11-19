"""Generate Persian/Arabic Original, Masked, and Restored samples.

Run from the repository root:
    python examples/run_persian_round_trip_demo.py

On Windows, if automatic Arabic-font discovery fails:
    python examples/run_persian_round_trip_demo.py --pdf-font C:\\Windows\\Fonts\\tahoma.ttf
"""

from __future__ import annotations

import argparse
from pathlib import Path
import sys
import tempfile

import pymupdf
from docx import Document
from openpyxl import Workbook, load_workbook

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
from document_anonymizer.core.normalization import normalize_text  # noqa: E402
from document_anonymizer.core.rules import MaskingRule, RuleSet  # noqa: E402
from document_anonymizer.core.vault import MaskingVault  # noqa: E402


SENSITIVE_VALUES = (
    "علی رضایی",
    "۰۹۱۲۳۴۵۶۷۸۹",
    "خیابان ولیعصر",
    "میروم",
    "ali@example.test",
)
PDF_SENSITIVE_VALUES = (
    "علی رضایی",
)


def find_pdf_font(explicit: Path | None) -> Path:
    candidates = (
        explicit,
        Path("/usr/share/fonts/truetype/noto/NotoNaskhArabic-Regular.ttf"),
        Path("/usr/share/fonts/truetype/noto/NotoSansArabic-Regular.ttf"),
        Path("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"),
        Path("C:/Windows/Fonts/tahoma.ttf"),
        Path("C:/Windows/Fonts/arial.ttf"),
    )
    font = next((item for item in candidates if item and item.is_file()), None)
    if font is None:
        raise RuntimeError(
            "An Arabic-capable font is required for the PDF sample. "
            "Pass --pdf-font with a TTF/OTF font that supports Persian, "
            "such as Tahoma or Noto Naskh Arabic."
        )
    return font.resolve()


def make_engine() -> MaskingEngine:
    rules = RuleSet((
        MaskingRule("persian-name", "literal", "علی رضایی", priority=200),
        MaskingRule("persian-mobile", "regex", r"۰۹[۰-۹]{9}", priority=150),
        MaskingRule("persian-address", "literal", "خیابان ولیعصر", priority=150),
        MaskingRule("zwnj-phrase", "literal", "میروم", priority=100),
        MaskingRule("email", "regex", r"[\w.-]+@[\w.-]+\.\w+", case_sensitive=False),
    ))
    vault = MaskingVault(
        rules,
        normalization="persian",
        ignore_zwnj=True,
    )
    return MaskingEngine(vault)


def normalized(text: str) -> str:
    return normalize_text(
        text,
        "persian",
        ignore_zwnj=True,
    ).text


def verify_masked(
    masked_text: str,
    engine: MaskingEngine,
    label: str,
    values: tuple[str, ...] = SENSITIVE_VALUES,
) -> None:
    normalized_masked = normalized(masked_text)
    for value in values:
        if normalized(value) in normalized_masked:
            raise RuntimeError(f"{label}: sensitive value remains masked: {value}")

    for token in engine.vault.token_to_original:
        if token not in masked_text:
            raise RuntimeError(f"{label}: token missing from masked output: {token}")


def verify_restored(
    restored_text: str,
    engine: MaskingEngine,
    label: str,
    values: tuple[str, ...] = SENSITIVE_VALUES,
) -> None:
    normalized_restored = normalized(restored_text)
    for value in values:
        if normalized(value) not in normalized_restored:
            raise RuntimeError(f"{label}: original value was not restored: {value}")

    for token in engine.vault.token_to_original:
        if token in restored_text:
            raise RuntimeError(f"{label}: token remains in restored output: {token}")


def create_pdf(path: Path, font_path: Path) -> None:
    document = pymupdf.open()
    page = document.new_page(width=595, height=500)
    html = """
    <div style="font-family: PersianDemo; font-size: 14pt;">PERSIAN DOCUMENT DEMO</div>
    <div dir="rtl" style="font-family: PersianDemo; font-size: 20pt;">نام: علی رضایی</div>
    <div style="font-family: PersianDemo; font-size: 12pt;">KEEP THIS LINE</div>
    """
    css = (
        f'@font-face {{font-family: PersianDemo; '
        f'src: url("{font_path.as_posix()}");}}'
    )
    page.insert_htmlbox(
        pymupdf.Rect(35, 25, 560, 465),
        html,
        css=css,
    )
    document.save(path)
    document.close()


def run_pdf(folder: Path, font_path: Path) -> None:
    original = folder / "Original.pdf"
    masked = folder / "Masked.pdf"
    restored = folder / "Restored.pdf"

    create_pdf(original, font_path)
    engine = make_engine()
    extracted = extract_pdf_text(original)
    plan = build_pdf_masking_plan(extracted, engine)

    if plan.total_replacements != len(PDF_SENSITIVE_VALUES):
        raise RuntimeError(
            f"PDF: expected {len(PDF_SENSITIVE_VALUES)} replacements, "
            f"got {plan.total_replacements}"
        )

    with tempfile.TemporaryDirectory(prefix="persian-pdf-redaction-") as temp:
        redacted = Path(temp) / "redacted.pdf"
        redact_pdf(plan, redacted)
        insert_pdf_tokens(redacted, plan, masked)

    masked_text = "\n".join(page.text for page in extract_pdf_text(masked).pages)
    verify_masked(masked_text, engine, "PDF", PDF_SENSITIVE_VALUES)

    restore_pdf(masked, plan, restored)
    restored_text = "\n".join(page.text for page in extract_pdf_text(restored).pages)
    verify_restored(restored_text, engine, "PDF", PDF_SENSITIVE_VALUES)


def create_docx(path: Path) -> None:
    document = Document()
    document.add_heading("پرونده مشتری", level=1)
    document.add_paragraph("نام: علي رضايي")
    document.add_paragraph("موبایل: ۰۹۱۲۳۴۵۶۷۸۹")
    document.add_paragraph("آدرس: خیابان ولیعصر")
    document.add_paragraph("عبارت: می‌روم")
    document.add_paragraph("Email: ali@example.test")
    document.add_paragraph("KEEP THIS LINE")
    document.save(path)


def run_docx(folder: Path) -> None:
    original = folder / "Original.docx"
    masked = folder / "Masked.docx"
    restored = folder / "Restored.docx"
    create_docx(original)

    engine = make_engine()
    extracted = extract_docx_text(original)
    plan = build_docx_masking_plan(extracted, engine)
    if plan.replacements_count != len(SENSITIVE_VALUES):
        raise RuntimeError(
            f"DOCX: expected {len(SENSITIVE_VALUES)} replacements, "
            f"got {plan.replacements_count}"
        )
    apply_docx_masking_plan(plan, masked)

    masked_text = extract_docx_text(masked).text
    verify_masked(masked_text, engine, "DOCX")

    unmask_plan = build_docx_unmasking_plan(
        extract_docx_text(masked),
        engine,
    )
    apply_docx_unmasking_plan(unmask_plan, restored)
    restored_text = extract_docx_text(restored).text
    verify_restored(restored_text, engine, "DOCX")
    if restored_text != extracted.text:
        raise RuntimeError("DOCX: restored text differs from original text")


def create_xlsx(path: Path) -> None:
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "مشتریان"
    rows = (
        ("فیلد", "مقدار"),
        ("نام", "علي رضايي"),
        ("موبایل", "۰۹۱۲۳۴۵۶۷۸۹"),
        ("آدرس", "خیابان ولیعصر"),
        ("عبارت", "می‌روم"),
        ("Email", "ali@example.test"),
        ("یادداشت", "KEEP THIS LINE"),
    )
    for row in rows:
        sheet.append(row)
    workbook.save(path)
    workbook.close()


def run_xlsx(folder: Path) -> None:
    original = folder / "Original.xlsx"
    masked = folder / "Masked.xlsx"
    restored = folder / "Restored.xlsx"
    create_xlsx(original)

    engine = make_engine()
    extracted = extract_xlsx_text(original)
    plan = build_xlsx_masking_plan(extracted, engine)
    if plan.replacements_count != len(SENSITIVE_VALUES):
        raise RuntimeError(
            f"XLSX: expected {len(SENSITIVE_VALUES)} replacements, "
            f"got {plan.replacements_count}"
        )
    apply_xlsx_masking_plan(plan, masked)

    masked_text = extract_xlsx_text(masked).text
    verify_masked(masked_text, engine, "XLSX")

    unmask_plan = build_xlsx_unmasking_plan(
        extract_xlsx_text(masked),
        engine,
    )
    apply_xlsx_unmasking_plan(unmask_plan, restored)
    restored_text = extract_xlsx_text(restored).text
    verify_restored(restored_text, engine, "XLSX")
    if restored_text != extracted.text:
        raise RuntimeError("XLSX: restored text differs from original text")


def write_readme(output: Path, font: Path) -> None:
    content = f"""# Persian / Arabic round-trip sample documents

These are synthetic test documents. They demonstrate Persian/Arabic-aware
masking and restoration in PDF, DOCX, and XLSX.

## Contents

- `PDF/Original.pdf`, `PDF/Masked.pdf`, `PDF/Restored.pdf`
- `DOCX/Original.docx`, `DOCX/Masked.docx`, `DOCX/Restored.docx`
- `XLSX/Original.xlsx`, `XLSX/Masked.xlsx`, `XLSX/Restored.xlsx`

The PDF contains embedded font data based on: `{font.name}`.

## Matching behavior

The sample uses `normalization="persian"` and `ignore_zwnj=True`:
- Arabic Yeh/Kaf variants match Persian Yeh/Kaf rules.
- Arabic presentation forms extracted from the PDF are normalized for matching.
- Persian digits are matched by a regex written for Persian digits.
- The ZWNJ/half-space is optional for the configured phrase.
- The vault retains exact original strings for restoration.

## Generate again

From the repository root:

```powershell
python examples\\run_persian_round_trip_demo.py
```

If an Arabic-capable font is not discovered automatically:

```powershell
python examples\\run_persian_round_trip_demo.py --pdf-font C:\\\\Windows\\\\Fonts\\\\tahoma.ttf
```

The output folder defaults to `persian_demo_output`.
"""
    (output / "README.md").write_text(content, encoding="utf-8")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Generate Persian/Arabic round-trip samples for all supported formats."
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=PROJECT_ROOT / "persian_demo_output",
    )
    parser.add_argument(
        "--pdf-font",
        type=Path,
        default=None,
        help="Arabic-capable TTF/OTF font for embedding in the PDF sample.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    output = args.output_dir.expanduser().resolve()
    if output.exists() and any(output.iterdir()):
        print(
            f"ERROR: output directory is not empty: {output}\\n"
            "Choose another --output-dir or move the existing files first.",
            file=sys.stderr,
        )
        return 2

    font = find_pdf_font(args.pdf_font)
    output.mkdir(parents=True, exist_ok=True)
    pdf_dir = output / "PDF"
    docx_dir = output / "DOCX"
    xlsx_dir = output / "XLSX"
    for folder in (pdf_dir, docx_dir, xlsx_dir):
        folder.mkdir(parents=True, exist_ok=True)

    run_pdf(pdf_dir, font)
    print("[OK] Persian PDF round trip")
    run_docx(docx_dir)
    print("[OK] Persian DOCX round trip")
    run_xlsx(xlsx_dir)
    print("[OK] Persian XLSX round trip")
    write_readme(output, font)
    print(f"All 9 Persian sample documents verified: {output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
