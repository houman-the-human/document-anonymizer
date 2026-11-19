from document_anonymizer.core.engine import MaskingEngine
from document_anonymizer.core.rules import (
    MaskingRule,
    RuleSet,
)
from document_anonymizer.core.vault import MaskingVault

from document_anonymizer.storage.file import FileVaultStore


PASSWORD = "Integration-Test-Password-2026"



def test_mask_save_restart_load_and_restore(tmp_path):

    ruleset = RuleSet((
        MaskingRule("person", "literal", "Jane Doe"),
        MaskingRule("phone", "regex", r"\b\d{10}\b"),
    ))

    vault = MaskingVault(
        ruleset,
        vault_id="integration-vault-001",
    )

    engine = MaskingEngine(vault)

    original = (
        "Jane Doe called 0912345678.\n"
        "Jane Doe called again."
    )

    # Session 1: mask and save.
    masked = engine.mask_text(original)
    masked_document = masked.masked_text

    store = FileVaultStore(tmp_path)
    store.save(vault, PASSWORD)

    # Simulate restart: discard the original objects.
    del engine
    del vault

    # Session 2: load and restore.
    loaded_vault = store.load(
        "integration-vault-001",
        PASSWORD,
    )

    new_engine = MaskingEngine(loaded_vault)

    restored = new_engine.unmask_text(
        masked_document
    )

    assert restored.restored_text == original
    assert restored.restored_count == 3
    assert restored.unknown_tokens == ()



def test_loaded_vault_can_continue_masking(tmp_path):

    ruleset = RuleSet((
        MaskingRule(
            "person",
            "literal",
            "Jane Doe",
        ),
    ))

    vault = MaskingVault(
        ruleset,
        vault_id="continuation-vault",
    )

    engine = MaskingEngine(vault)

    first = engine.mask_text("Jane Doe signed a document.")

    store = FileVaultStore(tmp_path)

    store.save(vault, PASSWORD)

    # Load into a fresh vault instance.
    loaded_vault = store.load(
        vault.vault_id,
        PASSWORD,
    )

    loaded_engine = MaskingEngine(loaded_vault)

    second = loaded_engine.mask_text(
        "Jane Doe signed another document."
    )

    # Token should be reused across application restarts.
    assert first.unique_tokens == second.unique_tokens

    restored = loaded_engine.unmask_text(
        second.masked_text
    )

    assert (
        restored.restored_text
        == "Jane Doe signed another document."
    )