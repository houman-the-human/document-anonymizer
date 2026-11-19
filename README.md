# Document Anonymizer

A Python application for reversible masking of sensitive text in **PDF, DOCX, and XLSX** documents.

The package includes:
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

The vault contains the token-to-original mappings and masking rules. It is encrypted using the project's password-based Scrypt key derivation and AES-256-GCM authenticated encryption.

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

The PDF sidecar stores the original and masked file SHA-256 fingerprints and is encrypted with the same vault password. The CLI checks both fingerprints before restoration. Do not rename, edit, or replace the original or masked PDF between the two operations.

The restored PDF is produced as a new file. Neither the original nor masked PDF is modified.

## 6. Try the included nine-file demonstration

The project includes a demonstration runner that creates synthetic sample documents and executes the complete adapter workflow.

```powershell
python examples\run_round_trip_demo.py
```

The default output folder is `demo_output`. To use a different location:

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
```

## 7. Vault and password handling

- Each masking operation creates a vault ID unless you explicitly provide `--vault-id`.
- Store the vault directory safely and back it up. Without the correct vault and password, DOCX/XLSX tokens cannot be restored.
- The password is prompted using a hidden input prompt; it is not stored in shell history.
- Do not share vault files or passwords with untrusted parties.
- The vault is encrypted, but the source document, masked document, rules JSON, and restored document remain ordinary files. Protect them according to their sensitivity.
- For PDF, keep the `.pdfmeta` sidecar together with the `.vault` file. Both are required by the CLI's PDF restore workflow.
- If the vault password is lost, the encrypted vault cannot be recovered by this application.

## 8. Supported formats and current limitations

### PDF
- Supports text-based PDFs whose text can be extracted and mapped to page coordinates.
- Scanned/image-only PDFs and OCR are not supported.
- PDF masking permanently redacts the original text before inserting tokens.
- PDF restoration requires the exact original PDF and the encrypted `.pdfmeta` sidecar.
- Password-protected PDFs are not supported by this CLI workflow.

### DOCX
- Supports document body text, tables, nested tables, headers, and footers as implemented by the adapter.
- Replacements preserve unaffected run formatting where supported.
- Hyperlink text masking/restoration is rejected by the adapter for safety.

### XLSX
- Supports string-valued cells across worksheets, including hidden worksheets.
- Cell formatting and supported workbook metadata are preserved.
- Formula expressions, comments, and hyperlink targets are not masked as ordinary cell text; they are reported as skipped metadata by the adapter.
- Some advanced Excel features may not be fully preserved by `openpyxl`. Test workbooks containing advanced controls, slicers, or embedded objects before using them in production.

## 9. Run tests

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

## 10. Troubleshooting

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

## 11. Quick command reference

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

# Tests
python -m pytest
```
