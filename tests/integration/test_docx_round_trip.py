
import base64
import json

import pytest
from docx import Document

from document_anonymizer.adapters.docx import (
    extract_docx_text,
    build_docx_masking_plan,
    apply_docx_masking_plan,
    build_docx_unmasking_plan,
    apply_docx_unmasking_plan,
)

from document_anonymizer.core.engine import MaskingEngine

from document_anonymizer.core.exceptions import (
    VaultAuthenticationError,
)

from document_anonymizer.core.rules import (
    MaskingRule,
    RuleSet,
)

from document_anonymizer.core.vault import MaskingVault

from document_anonymizer.storage.file import FileVaultStore


PASSWORD = "DOCX-Integration-Test-Password-2026"

VAULT_ID = "docx-integration-vault-001"


def create_rules():

    return RuleSet((
        MaskingRule(
            rule_id="person",
            kind="literal",
            pattern="Jane Doe",
            priority=200,
        ),

        MaskingRule(
            rule_id="email",
            kind="regex",
            pattern=r"\b[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}\b",
            case_sensitive=False,
        ),

        MaskingRule(
            rule_id="phone",
            kind="regex",
            pattern=r"\b\d{10}\b",
        ),
    ))


def create_source_docx(path):

    document = Document()

    # Body paragraph with a match spanning multiple runs.
    paragraph = document.add_paragraph()

    paragraph.add_run("Customer: ")

    paragraph.add_run("Jane ").bold = True

    paragraph.add_run("Doe").italic = True

    suffix = paragraph.add_run(" | Status: active")
    suffix.underline = True

    # Another body paragraph with regex matches.
    document.add_paragraph(
        "Email: jane@example.com | "
        "Phone: 0912345678 | "
        "Customer: Jane Doe"
    )

    # Table.
    table = document.add_table(
        rows=1,
        cols=1,
    )

    table.cell(0, 0).text = (
        "Jane Doe — jane@example.com"
    )

    # Nested table.
    nested_table = table.cell(0, 0).add_table(
        rows=1,
        cols=1,
    )

    nested_table.cell(0, 0).text = (
        "Phone: 0912345678"
    )

    # Header.
    document.sections[0].header.paragraphs[0].text = (
        "Confidential: Jane Doe"
    )

    # Footer.
    document.sections[0].footer.paragraphs[0].text = (
        "Contact: jane@example.com"
    )

    document.save(path)

    return path


def document_snapshot(extracted):

    """
    Capture document structure and extracted text.

    This allows us to compare the original and restored
    documents without comparing DOCX ZIP metadata.
    """

    return tuple(
        (
            paragraph.story,
            paragraph.section_index,
            paragraph.location,
            paragraph.text,
        )
        for paragraph in extracted.paragraphs
    )


def create_vault(vault_id=VAULT_ID):

    return MaskingVault(
        ruleset=create_rules(),
        vault_id=vault_id,
    )


# -------------------------------------------------
# Complete DOCX round trip
# -------------------------------------------------

def test_complete_docx_encrypted_vault_round_trip(tmp_path):

    source = create_source_docx(
        tmp_path / "original.docx"
    )

    masked_path = tmp_path / "masked.docx"

    restored_path = tmp_path / "restored.docx"

    vault_directory = tmp_path / "vaults"

    store = FileVaultStore(vault_directory)

    # -------------------------------------------------
    # Session 1: Extract and mask.
    # -------------------------------------------------

    original = extract_docx_text(source)

    original_snapshot = document_snapshot(original)

    original_bytes = source.read_bytes()

    vault = create_vault()

    engine = MaskingEngine(vault)

    masking_plan = build_docx_masking_plan(
        original,
        engine,
    )

    assert masking_plan.replacements_count > 0

    # Confirm that at least one match spans multiple runs.
    assert any(
        len(replacement.run_slices) > 1
        for paragraph in masking_plan.paragraphs
        for replacement in paragraph.replacements
    )

    apply_docx_masking_plan(
        masking_plan,
        masked_path,
    )

    masked = extract_docx_text(masked_path)

    # Original sensitive values must not remain in the masked file.
    for sensitive_value in (
        "Jane Doe",
        "jane@example.com",
        "0912345678",
    ):

        assert sensitive_value not in masked.text

    # -------------------------------------------------
    # Save encrypted vault.
    # -------------------------------------------------

    vault_path = store.save(
        vault,
        PASSWORD,
    )

    assert vault_path.exists()

    encrypted_bytes = vault_path.read_bytes()

    # Verify that original values are not visible in storage.
    for sensitive_value in (
        "Jane Doe",
        "jane@example.com",
        "0912345678",
    ):

        assert sensitive_value.encode("utf-8") not in encrypted_bytes

    # -------------------------------------------------
    # Simulate application restart.
    # -------------------------------------------------

    del engine
    del vault

    loaded_vault = store.load(
        VAULT_ID,
        PASSWORD,
    )

    assert loaded_vault.vault_id == VAULT_ID

    new_engine = MaskingEngine(loaded_vault)

    # -------------------------------------------------
    # Session 2: Unmask.
    # -------------------------------------------------

    masked_extracted = extract_docx_text(masked_path)

    unmasking_plan = build_docx_unmasking_plan(
        masked_extracted,
        new_engine,
    )

    assert unmasking_plan.restored_count == (
        masking_plan.replacements_count
    )

    assert unmasking_plan.unknown_tokens == ()

    apply_docx_unmasking_plan(
        unmasking_plan,
        restored_path,
    )

    # -------------------------------------------------
    # Verify restored document.
    # -------------------------------------------------

    restored = extract_docx_text(restored_path)

    restored_snapshot = document_snapshot(restored)

    assert restored_snapshot == original_snapshot

    assert restored.text == original.text

    # Verify source and masked documents remain unchanged.
    assert source.read_bytes() == original_bytes

    assert masked_path.exists()

    # Verify formatting of unaffected text.
    restored_document = Document(restored_path)

    restored_paragraph = restored_document.paragraphs[0]

    assert restored_paragraph.runs[0].text == "Customer: "

    assert restored_paragraph.runs[0].bold is None

    assert restored_paragraph.runs[-1].text == (
        " | Status: active"
    )

    assert restored_paragraph.runs[-1].underline is True


# -------------------------------------------------
# Wrong password
# -------------------------------------------------

def test_docx_vault_rejects_wrong_password(tmp_path):

    store = FileVaultStore(tmp_path / "vaults")

    vault = create_vault()

    store.save(
        vault,
        PASSWORD,
    )

    with pytest.raises(VaultAuthenticationError):

        store.load(
            vault.vault_id,
            "Wrong-Password",
        )


# -------------------------------------------------
# Tampered encrypted vault
# -------------------------------------------------

def test_docx_vault_rejects_tampered_ciphertext(tmp_path):

    store = FileVaultStore(tmp_path / "vaults")

    vault = create_vault()

    vault_path = store.save(
        vault,
        PASSWORD,
    )

    envelope = json.loads(
        vault_path.read_bytes()
    )

    ciphertext = bytearray(
        base64.urlsafe_b64decode(
            envelope["ciphertext"]
        )
    )

    # Modify one ciphertext byte.
    ciphertext[-1] ^= 1

    envelope["ciphertext"] = (
        base64.urlsafe_b64encode(ciphertext)
        .decode("ascii")
    )

    vault_path.write_text(
        json.dumps(envelope),
        encoding="utf-8",
    )

    with pytest.raises(VaultAuthenticationError):

        store.load(
            vault.vault_id,
            PASSWORD,
        )


# -------------------------------------------------
# Wrong vault cannot restore another vault's tokens.
# -------------------------------------------------

def test_docx_wrong_vault_preserves_unknown_tokens(tmp_path):

    source = tmp_path / "original.docx"

    masked_path = tmp_path / "masked.docx"

    output_path = tmp_path / "wrong-vault-restored.docx"

    document = Document()

    document.add_paragraph(
        "Customer: Jane Doe"
    )

    document.save(source)

    # Vault A creates the token.
    vault_a = create_vault("vault-a")

    engine_a = MaskingEngine(vault_a)

    original = extract_docx_text(source)

    masking_plan = build_docx_masking_plan(
        original,
        engine_a,
    )

    apply_docx_masking_plan(
        masking_plan,
        masked_path,
    )

    masked = extract_docx_text(masked_path)

    # Vault B has identical rules but no token mappings.
    vault_b = create_vault("vault-b")

    engine_b = MaskingEngine(vault_b)

    unmasking_plan = build_docx_unmasking_plan(
        masked,
        engine_b,
    )

    assert unmasking_plan.restored_count == 0

    assert len(unmasking_plan.unknown_tokens) > 0

    apply_docx_unmasking_plan(
        unmasking_plan,
        output_path,
    )

    result = extract_docx_text(output_path)

    # Vault B must not restore Vault A's token.
    assert result.text == masked.text
