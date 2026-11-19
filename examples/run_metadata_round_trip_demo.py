"""Create verified metadata Original/Masked/Restored samples.

Run from the repository root:
    python examples/run_metadata_round_trip_demo.py
"""

from __future__ import annotations

import argparse
from pathlib import Path
import shutil
import sys
import tempfile
import re

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
from document_anonymizer.adapters.metadata import (  # noqa: E402
    inspect_document_metadata,
    mask_document_metadata,
    restore_document_metadata,
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
from document_anonymizer.storage.file import FileVaultStore  # noqa: E402


PASSWORD = "sample-metadata-vault-password"
RULESET = RuleSet((
    MaskingRule("person-name", "literal", "Alex Morgan", priority=200),
    MaskingRule("email", "literal", "alex.morgan@example.test", priority=150),
))


def create_vault() -> MaskingVault:
    return MaskingVault(
        RULESET,
        normalization="persian",
        ignore_zwnj=True,
    )


def create_docx(path: Path) -> None:
    document = Document()
    document.add_heading("Customer record", level=1)
    document.add_paragraph("Customer: Alex Morgan")
    document.add_paragraph("Email: alex.morgan@example.test")
    document.add_paragraph("Additional text remains unchanged.")
    properties = document.core_properties
    properties.author = "علی رضایی"
    properties.last_modified_by = "Alex Morgan"
    properties.title = "قرارداد محرمانه مشتری"
    properties.subject = "Customer identity and contact details"
    properties.keywords = "private, customer, personnel"
    properties.comments = "Prepared by Alex Morgan"
    properties.category = "Confidential"
    properties.identifier = "DOC-META-001"
    properties.language = "fa-IR"
    properties.revision = 4
    document.save(path)


def create_xlsx(path: Path) -> None:
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "Customers"
    sheet.append(["Field", "Value"])
    sheet.append(["Customer", "Alex Morgan"])
    sheet.append(["Email", "alex.morgan@example.test"])
    sheet.append(["Status", "Active"])
    properties = workbook.properties
    properties.creator = "علی رضایی"
    properties.lastModifiedBy = "Alex Morgan"
    properties.title = "فهرست محرمانه مشتریان"
    properties.subject = "Customer identity and contact details"
    properties.description = "Prepared by Alex Morgan"
    properties.keywords = "private, customer, personnel"
    properties.category = "Confidential"
    properties.identifier = "XLS-META-001"
    properties.language = "fa-IR"
    properties.revision = "4"
    workbook.save(path)
    workbook.close()


def create_pdf(path: Path) -> None:
    document = pymupdf.open()
    page = document.new_page(width=595, height=300)
    page.insert_text((50, 70), "Customer: Alex Morgan", fontsize=14)
    page.insert_text((50, 105), "Email: alex.morgan@example.test", fontsize=14)
    page.insert_text((50, 140), "Additional text remains unchanged.", fontsize=12)
    document.set_metadata({
        "title": "قرارداد محرمانه مشتری",
        "author": "علی رضایی",
        "subject": "Customer identity and contact details",
        "keywords": "private, customer, personnel",
        "creator": "Alex Morgan",
        "producer": "Document Anonymizer sample generator",
        "creationDate": "D:20250412101010",
        "modDate": "D:20250513101010",
        "trapped": "False",
    })
    document.set_xml_metadata("""<?xpacket begin="﻿" id="W5M0MpCehiHzreSzNTczkc9d"?>
<x:xmpmeta xmlns:x="adobe:ns:meta/">
 <rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#">
  <rdf:Description rdf:about="" xmlns:dc="http://purl.org/dc/elements/1.1/">
   <dc:title><rdf:Alt><rdf:li xml:lang="x-default">اطلاعات محرمانه مشتری</rdf:li></rdf:Alt></dc:title>
   <dc:creator><rdf:Seq><rdf:li>علی رضایی</rdf:li></rdf:Seq></dc:creator>
  </rdf:Description>
 </rdf:RDF>
</x:xmpmeta>
<?xpacket end="w"?>""")
    document.save(path)
    document.close()


def _assert_metadata_cleared(path: Path) -> None:
    snapshot = inspect_document_metadata(path)
    if path.suffix.lower() == ".pdf":
        if any(value not in (None, "") for value in snapshot["properties"].values()):
            raise RuntimeError("Masked PDF still exposes standard metadata")
        if snapshot["xmp"]:
            raise RuntimeError("Masked PDF still exposes XMP metadata")
    else:
        if snapshot["properties"]["docProps/core.xml"]:
            raise RuntimeError(f"Masked {path.suffix} still exposes core properties")
        if snapshot["properties"]["docProps/app.xml"]:
            raise RuntimeError(f"Masked {path.suffix} still exposes extended properties")


def _assert_metadata_restored(original: Path, restored: Path) -> None:
    if inspect_document_metadata(original) != inspect_document_metadata(restored):
        raise RuntimeError(f"Metadata round trip differs for {original.suffix}")


def run_docx(folder: Path, vault_store: FileVaultStore) -> None:
    original = folder / "Original.docx"
    masked = folder / "Masked.docx"
    restored = folder / "Restored.docx"
    create_docx(original)

    vault = create_vault()
    engine = MaskingEngine(vault)
    extracted = extract_docx_text(original)
    plan = build_docx_masking_plan(extracted, engine)
    apply_docx_masking_plan(plan, masked)
    metadata_count = mask_document_metadata(original, masked, vault)
    if metadata_count == 0:
        raise RuntimeError("DOCX metadata sample did not contain metadata to sanitize")

    _assert_metadata_cleared(masked)
    vault_store.save(vault, PASSWORD)
    loaded_vault = vault_store.load(vault.vault_id, PASSWORD)

    unmask_plan = build_docx_unmasking_plan(
        extract_docx_text(masked),
        MaskingEngine(loaded_vault),
    )
    apply_docx_unmasking_plan(unmask_plan, restored)
    restore_document_metadata(restored, loaded_vault)

    if extract_docx_text(restored).text != extracted.text:
        raise RuntimeError("DOCX content was not restored exactly")
    _assert_metadata_restored(original, restored)


def run_xlsx(folder: Path, vault_store: FileVaultStore) -> None:
    original = folder / "Original.xlsx"
    masked = folder / "Masked.xlsx"
    restored = folder / "Restored.xlsx"
    create_xlsx(original)

    vault = create_vault()
    engine = MaskingEngine(vault)
    extracted = extract_xlsx_text(original)
    plan = build_xlsx_masking_plan(extracted, engine)
    apply_xlsx_masking_plan(plan, masked)
    metadata_count = mask_document_metadata(original, masked, vault)
    if metadata_count == 0:
        raise RuntimeError("XLSX metadata sample did not contain metadata to sanitize")

    _assert_metadata_cleared(masked)
    vault_store.save(vault, PASSWORD)
    loaded_vault = vault_store.load(vault.vault_id, PASSWORD)

    unmask_plan = build_xlsx_unmasking_plan(
        extract_xlsx_text(masked),
        MaskingEngine(loaded_vault),
    )
    apply_xlsx_unmasking_plan(unmask_plan, restored)
    restore_document_metadata(restored, loaded_vault)

    if extract_xlsx_text(restored).text != extracted.text:
        raise RuntimeError("XLSX content was not restored exactly")
    _assert_metadata_restored(original, restored)


def run_pdf(folder: Path, vault_store: FileVaultStore) -> None:
    original = folder / "Original.pdf"
    masked = folder / "Masked.pdf"
    restored = folder / "Restored.pdf"
    create_pdf(original)

    vault = create_vault()
    engine = MaskingEngine(vault)
    extracted = extract_pdf_text(original)
    plan = build_pdf_masking_plan(extracted, engine)
    with tempfile.TemporaryDirectory(prefix="metadata-pdf-demo-") as temp:
        redacted = Path(temp) / "redacted.pdf"
        redact_pdf(plan, redacted)
        insert_pdf_tokens(redacted, plan, masked)

    metadata_count = mask_document_metadata(original, masked, vault)
    if metadata_count == 0:
        raise RuntimeError("PDF metadata sample did not contain metadata to sanitize")

    _assert_metadata_cleared(masked)
    vault_store.save(vault, PASSWORD)
    loaded_vault = vault_store.load(vault.vault_id, PASSWORD)

    restored_plan = build_pdf_masking_plan(
        extract_pdf_text(original),
        MaskingEngine(loaded_vault),
    )
    restore_pdf(masked, restored_plan, restored)
    restore_document_metadata(restored, loaded_vault)

    original_text = "\n".join(page.text for page in extract_pdf_text(original).pages)
    restored_text = "\n".join(page.text for page in extract_pdf_text(restored).pages)
    for expected in ("Alex Morgan", "alex.morgan@example.test"):
        if expected not in restored_text:
            raise RuntimeError(f"PDF content value was not restored: {expected}")
    if re.sub(r"\s+", " ", original_text).strip() != re.sub(r"\s+", " ", restored_text).strip():
        raise RuntimeError("PDF text differs after restoration (ignoring line-break layout)")
    _assert_metadata_restored(original, restored)


def write_readme(output: Path) -> None:
    (output / "README.md").write_text(
        f"""# Metadata anonymization round-trip samples

These synthetic files demonstrate standard metadata sanitization and
restoration for PDF, DOCX, and XLSX.

Each format directory contains:
- `Original`: sample content and identifying metadata.
- `Masked`: content tokens plus cleared standard core/extended metadata.
- `Restored`: original content and original metadata restored from an encrypted vault.

The sample vault password used internally by this demo is:
`{PASSWORD}`

This password is only for the generated synthetic demo. Do not reuse it for
real documents.

## Run again

```powershell
python examples\\run_metadata_round_trip_demo.py
```

The output directory defaults to `metadata_demo_output`. It must be empty or
not exist. The demo writes temporary encrypted vault files outside the
document folders and deletes them when finished.

Unsupported comments, tracked changes, custom properties, hyperlinks,
attachments, annotations and related surfaces are intentionally not included
in these sample documents; the CLI rejects detected unsupported surfaces.
""",
        encoding="utf-8",
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Generate verified metadata masking/restoration samples."
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=PROJECT_ROOT / "metadata_demo_output",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    output = args.output_dir.expanduser().resolve()
    if output.exists() and any(output.iterdir()):
        print(
            f"ERROR: output directory is not empty: {output}. "
            "Choose another --output-dir.",
            file=sys.stderr,
        )
        return 2

    output.mkdir(parents=True, exist_ok=True)
    folders = {kind: output / kind.upper() for kind in ("docx", "xlsx", "pdf")}
    for folder in folders.values():
        folder.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory(prefix="document-anonymizer-metadata-vaults-") as vault_dir:
        store = FileVaultStore(Path(vault_dir))
        run_docx(folders["docx"], store)
        print("[OK] DOCX metadata/content round trip")
        run_xlsx(folders["xlsx"], store)
        print("[OK] XLSX metadata/content round trip")
        run_pdf(folders["pdf"], store)
        print("[OK] PDF metadata/content round trip")

    write_readme(output)
    print(f"All nine sample documents verified: {output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
