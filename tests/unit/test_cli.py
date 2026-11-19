from __future__ import annotations

import json
from pathlib import Path

import pymupdf
import pytest
from docx import Document
from openpyxl import Workbook

from document_anonymizer import cli
from document_anonymizer.adapters.docx import extract_docx_text
from document_anonymizer.adapters.pdf import extract_pdf_text
from document_anonymizer.adapters.xlsx import extract_xlsx_text


PASSWORD = "cli-test-password"
SENSITIVE = "Alex Morgan"


def _rules_file(tmp_path: Path) -> Path:
    path = tmp_path / "rules.json"
    path.write_text(
        json.dumps(
            {
                "rules": [
                    {
                        "rule_id": "person-name",
                        "kind": "literal",
                        "pattern": SENSITIVE,
                    },
                    {
                        "rule_id": "email",
                        "kind": "literal",
                        "pattern": "alex.morgan@example.test",
                    },
                ]
            }
        ),
        encoding="utf-8",
    )
    return path


def _create_docx(path: Path) -> None:
    document = Document()
    document.add_paragraph(f"Customer: {SENSITIVE}")
    document.add_paragraph("Email: alex.morgan@example.test")
    document.save(path)


def _create_xlsx(path: Path) -> None:
    workbook = Workbook()
    worksheet = workbook.active
    worksheet.title = "Customers"
    worksheet["A1"] = "Customer"
    worksheet["B1"] = SENSITIVE
    worksheet["A2"] = "Email"
    worksheet["B2"] = "alex.morgan@example.test"
    workbook.save(path)
    workbook.close()


def _create_pdf(path: Path) -> None:
    document = pymupdf.open()
    page = document.new_page(width=595, height=300)
    page.insert_text((60, 80), f"Customer: {SENSITIVE}", fontsize=14)
    page.insert_text(
        (60, 120),
        "Email: alex.morgan@example.test",
        fontsize=14,
    )
    document.save(path)
    document.close()


def _mask(
    monkeypatch,
    *,
    input_path: Path,
    output_path: Path,
    rules_path: Path,
    vault_dir: Path,
    capsys,
) -> str:
    prompts = iter((PASSWORD, PASSWORD))
    monkeypatch.setattr(
        cli.getpass,
        "getpass",
        lambda _prompt: next(prompts),
    )
    result = cli.main(
        [
            "mask",
            "--input", str(input_path),
            "--output", str(output_path),
            "--rules", str(rules_path),
            "--vault-dir", str(vault_dir),
        ]
    )
    assert result == 0
    output = capsys.readouterr().out
    vault_line = next(
        line for line in output.splitlines()
        if line.startswith("Vault ID:")
    )
    return vault_line.split(":", 1)[1].strip()


def _restore(
    monkeypatch,
    *,
    input_path: Path,
    output_path: Path,
    vault_id: str,
    vault_dir: Path,
    capsys,
    original_path: Path | None = None,
) -> int:
    monkeypatch.setattr(
        cli.getpass,
        "getpass",
        lambda _prompt: PASSWORD,
    )
    arguments = [
        "restore",
        "--input", str(input_path),
        "--output", str(output_path),
        "--vault-id", vault_id,
        "--vault-dir", str(vault_dir),
    ]
    if original_path is not None:
        arguments.extend(["--original", str(original_path)])
    return cli.main(arguments)


@pytest.mark.parametrize("extension", [".docx", ".xlsx", ".pdf"])
def test_cli_mask_and_restore_round_trip(
    tmp_path,
    monkeypatch,
    capsys,
    extension,
):
    original = tmp_path / f"Original{extension}"
    masked = tmp_path / f"Masked{extension}"
    restored = tmp_path / f"Restored{extension}"
    vault_dir = tmp_path / "vaults"
    rules_path = _rules_file(tmp_path)

    if extension == ".docx":
        _create_docx(original)
    elif extension == ".xlsx":
        _create_xlsx(original)
    else:
        _create_pdf(original)

    original_bytes = original.read_bytes()
    vault_id = _mask(
        monkeypatch,
        input_path=original,
        output_path=masked,
        rules_path=rules_path,
        vault_dir=vault_dir,
        capsys=capsys,
    )

    assert masked.is_file()
    assert original.read_bytes() == original_bytes
    assert vault_id
    assert list(vault_dir.glob("*.vault"))

    if extension == ".pdf":
        assert list(vault_dir.glob("*.pdfmeta"))
        masked_text = "\n".join(
            page.text for page in extract_pdf_text(masked).pages
        )
        original_path = original
    elif extension == ".docx":
        masked_text = extract_docx_text(masked).text
        original_path = None
    else:
        masked_text = extract_xlsx_text(masked).text
        original_path = None

    assert SENSITIVE not in masked_text
    assert "alex.morgan@example.test" not in masked_text

    result = _restore(
        monkeypatch,
        input_path=masked,
        output_path=restored,
        vault_id=vault_id,
        vault_dir=vault_dir,
        capsys=capsys,
        original_path=original_path,
    )
    assert result == 0
    assert restored.is_file()

    if extension == ".pdf":
        restored_text = "\n".join(
            page.text for page in extract_pdf_text(restored).pages
        )
    elif extension == ".docx":
        restored_text = extract_docx_text(restored).text
    else:
        restored_text = extract_xlsx_text(restored).text

    assert SENSITIVE in restored_text
    assert "alex.morgan@example.test" in restored_text


def test_cli_requires_original_for_pdf_restore(tmp_path, monkeypatch, capsys):
    masked = tmp_path / "Masked.pdf"
    masked.write_bytes(b"not opened because validation should stop first")
    result = cli.main(
        [
            "restore",
            "--input", str(masked),
            "--output", str(tmp_path / "Restored.pdf"),
            "--vault-id", "vault-id",
            "--vault-dir", str(tmp_path / "vaults"),
        ]
    )
    assert result == 2
    assert "requires --original" in capsys.readouterr().err


def test_cli_refuses_to_overwrite_output(tmp_path, monkeypatch, capsys):
    source = tmp_path / "source.docx"
    output = tmp_path / "masked.docx"
    _create_docx(source)
    output.write_text("keep me", encoding="utf-8")

    result = cli.main(
        [
            "mask",
            "--input", str(source),
            "--output", str(output),
            "--rules", str(_rules_file(tmp_path)),
            "--vault-dir", str(tmp_path / "vaults"),
        ]
    )
    assert result == 2
    assert output.read_text(encoding="utf-8") == "keep me"


def test_cli_refuses_wrong_vault_password(tmp_path, monkeypatch, capsys):
    source = tmp_path / "source.docx"
    masked = tmp_path / "masked.docx"
    restored = tmp_path / "restored.docx"
    vault_dir = tmp_path / "vaults"
    _create_docx(source)

    vault_id = _mask(
        monkeypatch,
        input_path=source,
        output_path=masked,
        rules_path=_rules_file(tmp_path),
        vault_dir=vault_dir,
        capsys=capsys,
    )

    monkeypatch.setattr(
        cli.getpass,
        "getpass",
        lambda _prompt: "incorrect-password",
    )
    result = cli.main(
        [
            "restore",
            "--input", str(masked),
            "--output", str(restored),
            "--vault-id", vault_id,
            "--vault-dir", str(vault_dir),
        ]
    )
    assert result == 2
    assert not restored.exists()


def test_cli_rejects_invalid_rules_file(tmp_path, capsys):
    source = tmp_path / "source.docx"
    _create_docx(source)
    rules = tmp_path / "bad-rules.json"
    rules.write_text('{"not_rules": []}', encoding="utf-8")

    result = cli.main(
        [
            "mask",
            "--input", str(source),
            "--output", str(tmp_path / "masked.docx"),
            "--rules", str(rules),
            "--vault-dir", str(tmp_path / "vaults"),
        ]
    )
    assert result == 2
    assert "rules" in capsys.readouterr().err.lower()


def test_cli_parser_exposes_mask_and_restore_commands():
    parser = cli.build_parser()
    mask_args = parser.parse_args(
        [
            "mask",
            "--input", "input.docx",
            "--output", "masked.docx",
            "--rules", "rules.json",
        ]
    )
    restore_args = parser.parse_args(
        [
            "restore",
            "--input", "masked.docx",
            "--output", "restored.docx",
            "--vault-id", "vault-123",
        ]
    )
    assert mask_args.command == "mask"
    assert restore_args.command == "restore"
