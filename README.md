# Document Anonymizer

A Python application for reversible masking of sensitive text in **PDF, DOCX, and XLSX** documents.

The package includes:
- Reversible sanitization of standard DOCX, XLSX, and PDF metadata, stored in the encrypted vault.
- A reusable masking engine with literal and regular-expression rules.
- Per-operation token mappings held in an isolated vault.
- AES-256-GCM encrypted vault persistence.
- Format-specific extraction, masking, and restoration adapters.
- A command-line interface for manual use.
- A round-trip demonstration that creates Original, Masked, and Restored files for all three formats.

## 1. Requirements

- Windows 10/11 or another supported Python platform.
- Python 3.10 or newer.
- Anaconda or Miniconda (recommended for the Windows instructions below).

The commands in this guide use **Windows PowerShell** and the Conda environment name `document-anonymizer`.

## 2. Install with Anaconda

Open PowerShell in the extracted project directory (the folder containing `pyproject.toml`).

Create the environment once:

```powershell
conda create -n document-anonymizer python=3.11 -y
```

Activate it:

```powershell
conda activate document-anonymizer
```

Install the project and its development dependencies:

```powershell
python -m pip install --upgrade pip
python -m pip install -e ".[dev]"
```

The editable install registers the `document-anonymizer` command. You can also run the same CLI as a Python module.

Check the installation:

```powershell
document-anonymizer --help
python -m document_anonymizer --help
document-anonymizer --version
```

If PowerShell cannot find `document-anonymizer` immediately after installation, close and reopen the terminal after activating the Conda environment, or use:

```powershell
python -m document_anonymizer --help
```

## 3. Prepare masking rules

The `mask` command requires a JSON rules file. An example matching the synthetic data in the included round-trip demo is provided at:

```text
examples/rules.example.json
```

Example:

```json
{
  "rules": [
    {
      "rule_id": "person-name",
      "kind": "literal",
      "pattern": "Alex Morgan"
    },
    {
      "rule_id": "email",
      "kind": "literal",
      "pattern": "alex.morgan@example.test"
    }
  ]
}
```

Each rule requires:
- `rule_id`: unique rule name.
- `kind`: `literal` or `regex`.
- `pattern`: exact text for a literal rule, or a regular expression for a regex rule.

Optional fields:
- `priority`: integer; defaults to `100`.
- `case_sensitive`: boolean; defaults to `true`.
- `flags`: array of supported regex flags (`MULTILINE`, `DOTALL`, `VERBOSE`, `ASCII`); defaults to `[]`.
- `enabled`: boolean; defaults to `true`.

You can copy the example before editing it:

```powershell
Copy-Item .\examples\rules.example.json .\my-rules.json
notepad .\my-rules.json
```

**Important:** Literal rule patterns may themselves contain sensitive values. Keep your rules file private and protect it like the original document. The vault saved by the application is encrypted; the JSON rules file is not.

## 4. Mask a document

General syntax:

```powershell
document-anonymizer mask `
  --input <source-file> `
  --output <new-masked-file> `
  --rules <rules.json> `
  [--vault-dir <directory>] `
  [--vault-id <id>]
```

The supported input/output extensions are `.pdf`, `.docx`, and `.xlsx`. The output extension must match the input extension.

### Word example

```powershell
document-anonymizer mask `
  --input .\Original.docx `
  --output .\output\Masked.docx `
  --rules .\examples\rules.example.json `
  --vault-dir .\vaults
```

### Excel example

```powershell
document-anonymizer mask `
  --input .\Original.xlsx `
  --output .\output\Masked.xlsx `
  --rules .\examples\rules.example.json `
  --vault-dir .\vaults
```

### PDF example

```powershell
document-anonymizer mask `
  --input .\Original.pdf `
  --output .\output\Masked.pdf `
  --rules .\examples\rules.example.json `
  --vault-dir .\vaults
```

After masking, the CLI prints:
- The number of replacements.
- The generated vault ID.
- The masked output path.
- The encrypted vault directory.

**Copy the vault ID from the output.** You will need it for restoration.

If you do not specify `--vault-dir`, the default is:

```text
%USERPROFILE%\.document-anonymizer\vaults
```

A password is requested twice when creating the vault. It is not accepted as a command-line argument and is not written to the console.

The vault contains the token-to-original mappings, masking rules, original metadata snapshots, and masked-file fingerprints. It is encrypted using the project's password-based Scrypt key derivation and AES-256-GCM authenticated encryption.

The CLI refuses to overwrite an existing input or output file. Choose a new output filename if the destination already exists.

## 5. Restore a document

General syntax:

```powershell
document-anonymizer restore `
  --input <masked-file> `
  --output <new-restored-file> `
  --vault-id <vault-id-printed-during-masking> `
  [--vault-dir <directory>]
```

The CLI prompts once for the vault password.

### Restore Word

```powershell
document-anonymizer restore `
  --input .\output\Masked.docx `
  --output .\output\Restored.docx `
  --vault-id YOUR-VAULT-ID `
  --vault-dir .\vaults
```

### Restore Excel

```powershell
document-anonymizer restore `
  --input .\output\Masked.xlsx `
  --output .\output\Restored.xlsx `
  --vault-id YOUR-VAULT-ID `
  --vault-dir .\vaults
```

### Restore PDF

PDF restoration is different because PDF masking uses **permanent redaction** to remove the original text. The original glyphs cannot be recovered from the token alone.

You must keep:
1. The exact original PDF used during masking.
2. The masked PDF.
3. The encrypted vault file.
4. Its encrypted `.pdfmeta` sidecar in the same vault directory.

Restore using:

```powershell
document-anonymizer restore `
  --input .\output\Masked.pdf `
  --original .\Original.pdf `
  --output .\output\Restored.pdf `
  --vault-id YOUR-VAULT-ID `
  --vault-dir .\vaults
```

The PDF sidecar stores the original and masked file SHA-256 fingerprints and is encrypted with the same vault password. The CLI checks both fingerprints before restoration. Do not rename, edit, or replace the original or masked PDF between the two operations. The masked DOCX and XLSX files are fingerprint-checked too; do not edit them before restoration.

The restored PDF is produced as a new file. Neither the original nor masked PDF is modified.

## 6. Try the included round-trip demonstrations

The project includes demonstration runners that create synthetic sample documents and execute the complete adapter workflow, including metadata sanitization and restoration.

```powershell
python examples\run_round_trip_demo.py
```

The content-only demo output folder is `demo_output`. To use a different location:

```powershell
python examples\run_round_trip_demo.py --output-dir .\my-demo-output
```

It creates:

```text
demo_output/
  PDF/
    Original.pdf
    Masked.pdf
    Restored.pdf
  DOCX/
    Original.docx
    Masked.docx
    Restored.docx
  XLSX/
    Original.xlsx
    Masked.xlsx
    Restored.xlsx
  README.md
```

The sample data is synthetic. The runner checks that:
- Sensitive source strings are absent from masked outputs.
- Tokens are present in masked outputs.
- Original values return in restored outputs.
- Tokens disappear from restored outputs.
- The restored text matches the original extracted text (subject to each adapter's representation).

The demonstration output directory must be empty or not exist. The script refuses to overwrite previous demo files.

Open the generated files:

```powershell
explorer .\demo_output


Run the metadata demonstration separately:

```powershell
python examples\run_metadata_round_trip_demo.py
```

It creates `metadata_demo_output/PDF`, `metadata_demo_output/DOCX`, and
`metadata_demo_output/XLSX`, each containing `Original`, `Masked`, and
`Restored` files. The runner reloads metadata snapshots from encrypted vaults
before restoration and verifies the resulting metadata after reopening files.
```

## 7. Vault and password handling

- Each masking operation creates a vault ID unless you explicitly provide `--vault-id`.
- Store the vault directory safely and back it up. Without the correct vault and password, DOCX/XLSX tokens cannot be restored.
- The password is prompted using a hidden input prompt; it is not stored in shell history.
- Do not share vault files or passwords with untrusted parties.
- The vault is encrypted, but the source document, masked document, rules JSON, and restored document remain ordinary files. Protect them according to their sensitivity.
- For PDF, keep the `.pdfmeta` sidecar together with the `.vault` file. Both are required by the CLI's PDF restore workflow.
- If the vault password is lost, the encrypted vault cannot be recovered by this application.

## 8. Reversible metadata sanitization

Masking also sanitizes standard document metadata. This happens independently
of the text rules: standard metadata fields are cleared even when their values
do not match a content rule. This avoids leaving an author or title behind
simply because it was not listed in the rules JSON.

The original metadata is stored inside the **encrypted vault**. It is not
written to a plaintext metadata sidecar. The vault uses schema version 3 for metadata snapshots and also records
SHA-256 fingerprints of the source and masked artifact; restoration refuses
a masked file that has changed since masking. Older schema v1/v2 vaults
remain loadable, but naturally contain no metadata snapshot. During restoration, the CLI restores the
original metadata together with the document content.

### Standard metadata handled

| Format | Metadata sanitized in the masked copy |
|---|---|
| DOCX | `docProps/core.xml` and `docProps/app.xml`, including author/creator, last modified by, title, subject, keywords, comments/description, category, identifier, language, revision, version, created/modified/last-printed dates, company/manager and extended application properties |
| XLSX | `docProps/core.xml` and `docProps/app.xml`, including creator, last modified by, title, subject, description, keywords, category, identifier, language, revision, version, dates and extended application properties |
| PDF | Standard document-info fields (author, title, subject, keywords, creator, producer, creation/modification dates and trapped) plus the XMP metadata packet |

For DOCX and XLSX, the original `docProps/core.xml` and `docProps/app.xml`
parts are stored byte-for-byte in the encrypted vault. The masked copy has
those property elements cleared. A DOCX preview thumbnail is replaced with a
neutral 1x1 image and the original thumbnail bytes are stored in the vault.
On restoration, the original metadata parts and thumbnail are put back
byte-for-byte.

For PDF, the standard document-info dictionary and XMP packet are stored in
the encrypted vault, cleared in the masked PDF, and restored from the vault.

### Metadata-bearing features that are explicitly rejected

Metadata sanitization is integrated into the CLI `mask`/`restore` workflow.
Calling the low-level content adapter functions directly does not automatically
sanitize metadata. The CLI refuses to mask a document when it detects the following features,
because this version does not yet sanitize and restore them safely:

- **DOCX:** Word comments/comment-author data, tracked changes, custom
  document properties, external relationships/hyperlinks, and OOXML digital
  signatures.
- **XLSX:** cell comments/threaded comments, custom document properties,
  hyperlinks and hyperlink targets, external workbook links/relationships,
  and OOXML digital signatures.
- **PDF:** annotations, form fields/widgets, links, bookmarks/outlines,
  embedded files and attachments.

Remove these features from a working copy of the source file and retry. The
original input is never changed. The refusal is intentional: the program does
not silently claim that unsupported metadata-bearing features were sanitized.

### Additional limitations

- Embedded image EXIF/IPTC metadata is not inspected or removed.
- Text in Word text boxes, tracked changes, comments, Excel formulas, Excel
  comments, hyperlink targets, PDF annotations and PDF attachments is not
  treated as ordinary body/cell text by the content adapters.
- Custom XML parts and arbitrary application-specific package extensions are
  not comprehensively inspected.
- OOXML files with unsupported features may be rejected as described above.
- PDF support remains limited to text-based PDFs with extractable text;
  scanned PDFs and OCR are not supported.

**Important:** A successful metadata sanitization does not guarantee that every
possible identifier in every embedded object or proprietary extension has
been removed. Review documents containing embedded media or unusual Office
features before external publication.

## 9. Supported formats and current limitations

### PDF
- Supports text-based PDFs whose text can be extracted and mapped to page coordinates.
- Scanned/image-only PDFs and OCR are not supported.
- PDF masking permanently redacts the original text before inserting tokens. Standard document-info and XMP metadata are cleared during masking and restored from the encrypted vault.
- PDF restoration requires the exact original PDF and the encrypted `.pdfmeta` sidecar.
- Password-protected PDFs are not supported by this CLI workflow.

### DOCX
- Supports document body text, tables, nested tables, headers, and footers as implemented by the adapter.
- Replacements preserve unaffected run formatting where supported. Standard core and extended properties are cleared during masking and restored from the encrypted vault.
- Files containing comments, tracked changes, custom properties or external relationships are rejected by the CLI until those surfaces can be handled safely.

### XLSX
- Supports string-valued cells across worksheets, including hidden worksheets.
- Cell formatting is preserved. Standard workbook metadata is cleared during masking and restored from the encrypted vault.
- Formula expressions are not masked as ordinary cell text. Files with comments, hyperlinks, custom properties or external links are rejected by the CLI until those surfaces can be handled safely.
- Some advanced Excel features may not be fully preserved by `openpyxl`. Test workbooks containing advanced controls, slicers, or embedded objects before using them in production.

## 10. Run tests

Run the complete test suite:

```powershell
python -m pytest
```

Run the CLI tests:

```powershell
python -m pytest tests\unit\test_cli.py -v
```

Run individual adapter tests:

```powershell
python -m pytest tests\unit\adapters\test_pdf.py -v
python -m pytest tests\unit\adapters\test_docx.py -v
python -m pytest tests\unit\adapters\test_xlsx.py -v
```

Run integration tests:

```powershell
python -m pytest tests\integration -v
```

## 11. Troubleshooting

**`document-anonymizer` is not recognized**

Activate the Conda environment and reinstall the project:

```powershell
conda activate document-anonymizer
python -m pip install -e ".[dev]"
```

Alternatively, run `python -m document_anonymizer ...`.

**Wrong password or vault authentication error**

Check that you entered the same password used during masking. The application does not store or recover vault passwords.

**Vault not found**

Use the same `--vault-dir` used during masking and the exact vault ID printed by the mask command.

**PDF restore metadata missing**

Keep the `.pdfmeta` file alongside the `.vault` file in the vault directory. Also provide the exact original PDF used during masking.

**Output already exists**

The application will not overwrite it. Choose another output path or move the existing output yourself.

---

## 12. Quick command reference

```powershell
# Help
document-anonymizer --help
document-anonymizer mask --help
document-anonymizer restore --help

# Mask
document-anonymizer mask --input .\Original.docx --output .\Masked.docx --rules .\my-rules.json

# Restore Word / Excel
document-anonymizer restore --input .\Masked.docx --output .\Restored.docx --vault-id YOUR-VAULT-ID

# Restore PDF (the original is mandatory)
document-anonymizer restore --input .\Masked.pdf --original .\Original.pdf --output .\Restored.pdf --vault-id YOUR-VAULT-ID

# Full demonstration
python examples\run_round_trip_demo.py
python examples\run_metadata_round_trip_demo.py

# Tests
python -m pytest
```


## 13. Multilingual text and Persian/Arabic normalization

The matching engine works on Unicode strings. DOCX and XLSX string cells can
therefore contain Persian, Arabic, English, and mixed-language text. PDF
support depends on the text layer and the character mapping embedded in the PDF.

Normalization is **opt-in** so existing exact-match behavior remains unchanged.
Choose the policy when masking:

| Mode | Behavior |
|---|---|
| `none` | Exact code-point matching. This is the default. |
| `unicode` | NFC canonical normalization (for example, composed/decomposed accents). |
| `persian` | NFC, Arabic Yeh `ي` to Persian Yeh `ی`, Arabic Kaf `ك` to Persian Kaf `ک`, and normalization of Arabic presentation-form glyphs commonly returned by PDF text extractors. |

Persian mode does not automatically convert Persian, Arabic, and Western
numerals into one another. It also does not convert Arabic Alef Maksura `ى`
to Persian Yeh, because those characters can have different linguistic
meanings.

### Optional half-space matching

By default, ZWNJ/half-space (`‌`, U+200C) remains significant. To let a rule
match text with or without the half-space, add `--ignore-zwnj`:

```powershell
document-anonymizer mask `
  --input .\Original.docx `
  --output .\Masked.docx `
  --rules .\examples\rules.persian.example.json `
  --vault-dir .\vaults `
  --normalization persian `
  --ignore-zwnj
```

For Arabic/Persian Yeh and Kaf equivalence without changing half-space
behavior:

```powershell
document-anonymizer mask `
  --input .\Original.xlsx `
  --output .\Masked.xlsx `
  --rules .\examples\rules.persian.example.json `
  --vault-dir .\vaults `
  --normalization persian
```

The selected normalization mode is stored inside the encrypted vault. Restore
uses the stored policy automatically; you do not need to repeat the flags.
For regex rules, write Arabic/Persian characters literally in the pattern when
you want the normalization policy to apply. Escaped regex code-point forms
such as `\u064a` are regex syntax and are not rewritten by the text normalizer:

```powershell
document-anonymizer restore `
  --input .\Masked.docx `
  --output .\Restored.docx `
  --vault-id YOUR-VAULT-ID `
  --vault-dir .\vaults
```

### Exact source preservation and offsets

Normalization is used only for matching. The engine maps normalized matches
back to complete original grapheme clusters before creating replacements.
The vault stores the exact source spelling, including original Arabic/Persian
characters and ZWNJ. Restoration therefore returns the original text, not the
normalized form.

If a regex match would cut through part of an original grapheme cluster after
normalization, the engine raises an error instead of guessing at an unsafe
source offset. This is especially important for PDF character-coordinate
mapping and redaction.

Vault serialization schema version 2 stores normalization settings. Existing
schema version 1 vaults remain readable and default to `none`.

### Persian/Arabic PDF limitations

- The PDF must contain extractable text; scanned/image-only PDFs still require
  OCR, which is not included.
- Correct Persian matching depends on the PDF's embedded font mappings and
  logical text order. Some PDFs expose presentation-form glyphs or visual-order
  text; Persian mode handles common Arabic presentation forms, but it cannot
  reliably reconstruct every malformed or incorrectly ordered text layer.
- PDF redaction uses extracted character coordinates. Review the masked PDF
  visually before sharing it, especially for complex RTL layouts, ligatures,
  overlapping text, or unusual fonts.
- The PDF output's visual RTL rendering depends on the source PDF and font
  embedding. The anonymizer does not re-typeset or repair the original layout.

### Persian sample rules

A starter rules file is provided at:

```text
examples/rules.persian.example.json
```

You can copy and edit it:

```powershell
Copy-Item .\examples\rules.persian.example.json .\my-persian-rules.json
notepad .\my-persian-rules.json
```

Use literal rules for exact names and addresses. Use regex rules for structured
values such as phone numbers and national identifiers. Review regex patterns
against the numeral systems present in your source documents; numeral systems
are not silently converted.


### Generate Persian round-trip sample files

The project also includes a Persian/Arabic round-trip runner. It generates
`Original`, `Masked`, and `Restored` files for PDF, DOCX, and XLSX:

```powershell
python examples\run_persian_round_trip_demo.py
```

The output is written to `persian_demo_output`. The PDF sample embeds an
Arabic-capable font. The runner looks for Noto Arabic fonts on Linux and
Tahoma/Arial on Windows. If automatic discovery does not find a suitable font,
pass one explicitly:

```powershell
python examples\run_persian_round_trip_demo.py --pdf-font C:\Windows\Fonts\tahoma.ttf
```

The PDF demonstration deliberately uses a simple RTL text line and one matched
Persian name so its coordinate mapping can be checked precisely. DOCX and XLSX
demonstrate the broader set of Persian values, Persian digits, and optional
half-space matching.
