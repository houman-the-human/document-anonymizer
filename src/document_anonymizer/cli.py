from __future__ import annotations

import argparse
import getpass
import hashlib
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path
from uuid import uuid4

from document_anonymizer import __version__
from document_anonymizer.adapters.docx import (
    apply_docx_masking_plan,
    apply_docx_unmasking_plan,
    build_docx_masking_plan,
    build_docx_unmasking_plan,
    extract_docx_text,
)
from document_anonymizer.adapters.pdf import (
    build_pdf_masking_plan,
    extract_pdf_text,
    insert_pdf_tokens,
    redact_pdf,
    restore_pdf,
)
from document_anonymizer.adapters.xlsx import (
    apply_xlsx_masking_plan,
    apply_xlsx_unmasking_plan,
    build_xlsx_masking_plan,
    build_xlsx_unmasking_plan,
    extract_xlsx_text,
)
from document_anonymizer.core.engine import MaskingEngine
from document_anonymizer.core.rules import MaskingRule, RuleSet
from document_anonymizer.core.vault import MaskingVault
from document_anonymizer.storage.encryption import decrypt_bytes, encrypt_bytes
from document_anonymizer.storage.file import FileVaultStore


SUPPORTED_EXTENSIONS = {".pdf", ".docx", ".xlsx"}
PDF_METADATA_SCHEMA_VERSION = 1
DEFAULT_VAULT_DIR = Path.home() / ".document-anonymizer" / "vaults"


class CLIError(Exception):
    """Raised when a CLI operation cannot be completed safely."""


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _vault_key(vault_id: str) -> str:
    return hashlib.sha256(vault_id.encode("utf-8")).hexdigest()


def _pdf_metadata_path(vault_dir: Path, vault_id: str) -> Path:
    return vault_dir / f"{_vault_key(vault_id)}.pdfmeta"


def _load_ruleset(path: Path) -> RuleSet:
    if not path.is_file():
        raise CLIError(f"Rules file does not exist: {path}")

    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CLIError(f"Unable to read rules JSON: {path}") from exc

    if isinstance(payload, dict):
        if set(payload) != {"rules"}:
            raise CLIError("Rules JSON object must contain only a 'rules' field")
        rules_data = payload["rules"]
    else:
        rules_data = payload

    if not isinstance(rules_data, list) or not rules_data:
        raise CLIError("Rules file must contain a non-empty list of rules")

    allowed = {
        "rule_id",
        "kind",
        "pattern",
        "priority",
        "case_sensitive",
        "flags",
        "enabled",
    }
    rules = []

    for index, item in enumerate(rules_data, start=1):
        if not isinstance(item, dict):
            raise CLIError(f"Rule #{index} must be a JSON object")
        unknown = set(item) - allowed
        required = {"rule_id", "kind", "pattern"}
        missing = required - set(item)
        if unknown:
            raise CLIError(f"Rule #{index} has unknown fields: {sorted(unknown)}")
        if missing:
            raise CLIError(f"Rule #{index} is missing fields: {sorted(missing)}")

        flags = item.get("flags", [])
        if not isinstance(flags, list):
            raise CLIError(f"Rule #{index} 'flags' must be a JSON array")

        try:
            rules.append(
                MaskingRule(
                    rule_id=item["rule_id"],
                    kind=item["kind"],
                    pattern=item["pattern"],
                    priority=item.get("priority", 100),
                    case_sensitive=item.get("case_sensitive", True),
                    flags=tuple(flags),
                    enabled=item.get("enabled", True),
                )
            )
        except (TypeError, ValueError) as exc:
            raise CLIError(f"Invalid rule #{index}: {exc}") from exc

    try:
        return RuleSet(tuple(rules))
    except (TypeError, ValueError) as exc:
        raise CLIError(f"Invalid rules collection: {exc}") from exc


def _prompt_new_password() -> str:
    password = getpass.getpass("Create vault password: ")
    confirmation = getpass.getpass("Confirm vault password: ")
    if not password:
        raise CLIError("Vault password cannot be empty")
    if password != confirmation:
        raise CLIError("Passwords do not match")
    return password


def _prompt_password() -> str:
    password = getpass.getpass("Vault password: ")
    if not password:
        raise CLIError("Vault password cannot be empty")
    return password


def _validate_document_paths(
    input_path: Path,
    output_path: Path,
    *,
    expected_extension: str | None = None,
) -> str:
    input_path = input_path.expanduser().resolve()
    output_path = output_path.expanduser().resolve()

    if not input_path.exists() or not input_path.is_file():
        raise CLIError(f"Input file does not exist: {input_path}")

    extension = input_path.suffix.lower()
    if extension not in SUPPORTED_EXTENSIONS:
        raise CLIError(
            "Unsupported input format. Supported extensions: .pdf, .docx, .xlsx"
        )

    if expected_extension is not None and extension != expected_extension:
        raise CLIError(
            f"Expected a {expected_extension} file, got {extension or 'no extension'}"
        )

    if output_path.suffix.lower() != extension:
        raise CLIError(
            f"Output extension must match input extension ({extension})"
        )

    if input_path == output_path:
        raise CLIError("Input and output paths must be different")

    if output_path.exists() or output_path.is_symlink():
        raise CLIError(f"Output already exists; refusing to overwrite: {output_path}")

    output_path.parent.mkdir(parents=True, exist_ok=True)
    return extension


def _write_pdf_metadata(
    vault_dir: Path,
    vault_id: str,
    password: str,
    original_sha256: str,
    masked_sha256: str,
) -> Path:
    vault_dir.mkdir(parents=True, exist_ok=True)
    destination = _pdf_metadata_path(vault_dir, vault_id)

    if destination.exists() or destination.is_symlink():
        raise CLIError("PDF metadata already exists for this vault ID")

    metadata = {
        "schema_version": PDF_METADATA_SCHEMA_VERSION,
        "vault_id": vault_id,
        "original_sha256": original_sha256,
        "masked_sha256": masked_sha256,
    }
    encrypted = encrypt_bytes(
        json.dumps(metadata, sort_keys=True, separators=(",", ":")).encode("utf-8"),
        password,
    )

    temporary_path = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb",
            dir=vault_dir,
            prefix=f".{destination.name}.",
            suffix=".tmp",
            delete=False,
        ) as temporary:
            temporary_path = Path(temporary.name)
            temporary.write(encrypted)
            temporary.flush()
            os.fsync(temporary.fileno())

        try:
            os.link(temporary_path, destination)
        except OSError:
            # Exclusive creation remains safe on filesystems without hard-link support.
            with temporary_path.open("rb") as source:
                with destination.open("xb") as target:
                    shutil.copyfileobj(source, target)
                    target.flush()
                    os.fsync(target.fileno())
        return destination
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)


def _read_pdf_metadata(
    vault_dir: Path,
    vault_id: str,
    password: str,
) -> dict:
    path = _pdf_metadata_path(vault_dir, vault_id)
    if path.is_symlink() or not path.is_file():
        raise CLIError(
            "PDF restoration metadata is missing. Keep the encrypted .pdfmeta "
            "file created during masking in the vault directory."
        )

    try:
        encrypted = path.read_bytes()
        payload = json.loads(decrypt_bytes(encrypted, password).decode("utf-8"))
    except Exception as exc:
        raise CLIError(
            "Unable to authenticate or read PDF restoration metadata"
        ) from exc

    expected_keys = {
        "schema_version",
        "vault_id",
        "original_sha256",
        "masked_sha256",
    }
    if not isinstance(payload, dict) or set(payload) != expected_keys:
        raise CLIError("Invalid PDF restoration metadata")

    if (
        payload["schema_version"] != PDF_METADATA_SCHEMA_VERSION
        or payload["vault_id"] != vault_id
        or not all(
            isinstance(payload[key], str) and len(payload[key]) == 64
            for key in ("original_sha256", "masked_sha256")
        )
    ):
        raise CLIError("PDF restoration metadata is inconsistent")

    return payload


def _mask_document(
    input_path: Path,
    output_path: Path,
    vault: MaskingVault,
) -> int:
    engine = MaskingEngine(vault)
    extension = input_path.suffix.lower()

    if extension == ".pdf":
        extracted = extract_pdf_text(input_path)
        if not extracted.has_extractable_text:
            raise CLIError(
                "This CLI currently supports text-based PDFs with extractable text only"
            )
        plan = build_pdf_masking_plan(extracted, engine)
        with tempfile.TemporaryDirectory(
            prefix="document-anonymizer-pdf-",
            dir=output_path.parent,
        ) as temporary_dir:
            redacted_path = Path(temporary_dir) / "redacted.pdf"
            redact_pdf(plan, redacted_path)
            insert_pdf_tokens(redacted_path, plan, output_path)
        return plan.total_replacements

    if extension == ".docx":
        extracted = extract_docx_text(input_path)
        plan = build_docx_masking_plan(extracted, engine)
        apply_docx_masking_plan(plan, output_path)
        return plan.replacements_count

    if extension == ".xlsx":
        extracted = extract_xlsx_text(input_path)
        plan = build_xlsx_masking_plan(extracted, engine)
        apply_xlsx_masking_plan(plan, output_path)
        return plan.replacements_count

    raise CLIError(f"Unsupported format: {extension}")


def _restore_document(
    input_path: Path,
    output_path: Path,
    vault: MaskingVault,
    *,
    original_path: Path | None,
    vault_dir: Path,
    password: str,
) -> int:
    engine = MaskingEngine(vault)
    extension = input_path.suffix.lower()

    if extension == ".pdf":
        if original_path is None:
            raise CLIError(
                "PDF restoration requires --original because PDF masking "
                "permanently redacts the original glyphs."
            )
        original_path = original_path.expanduser().resolve()
        if not original_path.is_file():
            raise CLIError(f"Original PDF does not exist: {original_path}")
        if original_path.suffix.lower() != ".pdf":
            raise CLIError("--original must be a .pdf file")
        if original_path in {input_path, output_path}:
            raise CLIError(
                "Original, masked, and restored PDF paths must be different"
            )

        metadata = _read_pdf_metadata(vault_dir, vault.vault_id, password)
        if _sha256_file(original_path) != metadata["original_sha256"]:
            raise CLIError(
                "Original PDF fingerprint does not match the file used for masking"
            )
        if _sha256_file(input_path) != metadata["masked_sha256"]:
            raise CLIError(
                "Masked PDF fingerprint does not match the file created during masking"
            )

        extracted = extract_pdf_text(original_path)
        plan = build_pdf_masking_plan(extracted, engine)
        restore_pdf(input_path, plan, output_path)
        return plan.total_replacements

    if original_path is not None:
        raise CLIError("--original is only used when restoring PDF files")

    if extension == ".docx":
        extracted = extract_docx_text(input_path)
        plan = build_docx_unmasking_plan(extracted, engine)
        apply_docx_unmasking_plan(plan, output_path)
        return plan.restored_count

    if extension == ".xlsx":
        extracted = extract_xlsx_text(input_path)
        plan = build_xlsx_unmasking_plan(extracted, engine)
        apply_xlsx_unmasking_plan(plan, output_path)
        return plan.restored_count

    raise CLIError(f"Unsupported format: {extension}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="document-anonymizer",
        description=(
            "Reversibly mask sensitive text in PDF, DOCX, and XLSX documents."
        ),
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"%(prog)s {__version__}",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    mask_parser = subparsers.add_parser(
        "mask",
        help="Mask sensitive text and save an encrypted vault",
        description="Create a masked copy and an encrypted vault for later restoration.",
    )
    mask_parser.add_argument("--input", required=True, type=Path, help="Source .pdf, .docx, or .xlsx file")
    mask_parser.add_argument("--output", required=True, type=Path, help="New masked output path")
    mask_parser.add_argument("--rules", required=True, type=Path, help="JSON file containing masking rules")
    mask_parser.add_argument(
        "--vault-dir",
        type=Path,
        default=DEFAULT_VAULT_DIR,
        help=f"Encrypted vault directory (default: {DEFAULT_VAULT_DIR})",
    )
    mask_parser.add_argument(
        "--vault-id",
        help="Optional vault ID; generated automatically when omitted",
    )

    restore_parser = subparsers.add_parser(
        "restore",
        help="Restore known tokens using an encrypted vault",
        description=(
            "Create a restored copy. PDF additionally requires the exact original PDF."
        ),
    )
    restore_parser.add_argument("--input", required=True, type=Path, help="Masked .pdf, .docx, or .xlsx file")
    restore_parser.add_argument("--output", required=True, type=Path, help="New restored output path")
    restore_parser.add_argument("--vault-id", required=True, help="Vault ID printed by the mask command")
    restore_parser.add_argument(
        "--vault-dir",
        type=Path,
        default=DEFAULT_VAULT_DIR,
        help=f"Encrypted vault directory (default: {DEFAULT_VAULT_DIR})",
    )
    restore_parser.add_argument(
        "--original",
        type=Path,
        help="Required for PDF restoration: exact original PDF used during masking",
    )

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    try:
        if args.command == "mask":
            input_path = args.input.expanduser().resolve()
            output_path = args.output.expanduser().resolve()
            extension = _validate_document_paths(input_path, output_path)

            ruleset = _load_ruleset(args.rules.expanduser().resolve())
            vault_id = args.vault_id or str(uuid4())
            vault = MaskingVault(ruleset, vault_id=vault_id)
            store = FileVaultStore(args.vault_dir.expanduser())

            if store.exists(vault_id):
                raise CLIError(
                    f"Vault ID already exists: {vault_id}. Choose another ID."
                )

            password = _prompt_new_password()
            original_sha256 = _sha256_file(input_path) if extension == ".pdf" else None

            try:
                replacement_count = _mask_document(
                    input_path,
                    output_path,
                    vault,
                )
                if extension == ".pdf":
                    if _sha256_file(input_path) != original_sha256:
                        raise CLIError("Source PDF changed during masking")
                    masked_sha256 = _sha256_file(output_path)
                    store.save(vault, password)
                    _write_pdf_metadata(
                        store.directory,
                        vault_id,
                        password,
                        original_sha256,
                        masked_sha256,
                    )
                else:
                    store.save(vault, password)
            except Exception:
                output_path.unlink(missing_ok=True)
                if store.exists(vault_id):
                    store.delete(vault_id)
                _pdf_metadata_path(store.directory, vault_id).unlink(missing_ok=True)
                raise

            print("Masking completed successfully.")
            print(f"Format: {extension[1:].upper()}")
            print(f"Replacements: {replacement_count}")
            print(f"Vault ID: {vault_id}")
            print(f"Encrypted vault directory: {store.directory.resolve()}")
            print(f"Masked output: {output_path}")
            if extension == ".pdf":
                print(
                    "PDF restore note: keep the original PDF and encrypted "
                    "vault metadata (.pdfmeta) for restoration."
                )
            return 0

        input_path = args.input.expanduser().resolve()
        output_path = args.output.expanduser().resolve()
        extension = _validate_document_paths(input_path, output_path)
        original_path = args.original.expanduser().resolve() if args.original else None

        if extension == ".pdf" and original_path is None:
            raise CLIError("PDF restoration requires --original")
        if extension != ".pdf" and original_path is not None:
            raise CLIError("--original is only valid for PDF restoration")

        vault_dir = args.vault_dir.expanduser()
        store = FileVaultStore(vault_dir)
        password = _prompt_password()
        vault = store.load(args.vault_id, password)

        restored_count = _restore_document(
            input_path,
            output_path,
            vault,
            original_path=original_path,
            vault_dir=vault_dir,
            password=password,
        )

        print("Restoration completed successfully.")
        print(f"Format: {extension[1:].upper()}")
        print(f"Restored occurrences: {restored_count}")
        print(f"Vault ID: {vault.vault_id}")
        print(f"Restored output: {output_path}")
        return 0

    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
