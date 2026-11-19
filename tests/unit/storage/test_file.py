import base64
import json

import pytest

import document_anonymizer.storage.file as file_module

from document_anonymizer.core.engine import MaskingEngine
from document_anonymizer.core.exceptions import (
    VaultAuthenticationError,
    VaultFormatError,
    VaultMismatchError,
    VaultNotFoundError,
)
from document_anonymizer.core.rules import (
    MaskingRule,
    RuleSet,
)
from document_anonymizer.core.vault import MaskingVault

from document_anonymizer.storage.file import FileVaultStore


PASSWORD = "Strong-Test-Password-2026"


def create_vault(
    vault_id="vault-test-001",
    original="Jane Doe",
):
    ruleset = RuleSet((
        MaskingRule(
            "person",
            "literal",
            original,
        ),
    ))

    vault = MaskingVault(
        ruleset,
        vault_id=vault_id,
    )

    vault.reserve_source_text(original)
    vault.get_or_create_token(original)

    return vault


def test_save_creates_encrypted_file(tmp_path):

    store = FileVaultStore(tmp_path)

    vault = create_vault()

    path = store.save(vault, PASSWORD)

    assert path.exists()
    assert path.is_file()
    assert path.suffix == ".vault"


def test_save_does_not_expose_original_text(tmp_path):

    store = FileVaultStore(tmp_path)

    vault = create_vault(
        original="VERY-SECRET-PERSON-NAME",
    )

    path = store.save(vault, PASSWORD)

    encrypted = path.read_bytes()

    assert b"VERY-SECRET-PERSON-NAME" not in encrypted


def test_load_restores_vault_metadata_and_mappings(tmp_path):

    store = FileVaultStore(tmp_path)

    original = create_vault()

    store.save(original, PASSWORD)

    loaded = store.load(
        original.vault_id,
        PASSWORD,
    )

    assert loaded.vault_id == original.vault_id
    assert loaded.ruleset == original.ruleset

    assert dict(loaded.original_to_token) == dict(
        original.original_to_token
    )

    assert dict(loaded.token_to_original) == dict(
        original.token_to_original
    )


def test_load_rejects_wrong_password(tmp_path):

    store = FileVaultStore(tmp_path)

    vault = create_vault()

    store.save(vault, PASSWORD)

    with pytest.raises(VaultAuthenticationError):

        store.load(
            vault.vault_id,
            "Wrong-Password",
        )


def test_load_rejects_corrupted_ciphertext(tmp_path):

    store = FileVaultStore(tmp_path)

    vault = create_vault()

    path = store.save(vault, PASSWORD)

    envelope = json.loads(
        path.read_bytes()
    )

    ciphertext = bytearray(
        base64.urlsafe_b64decode(
            envelope["ciphertext"]
        )
    )

    ciphertext[0] ^= 1

    envelope["ciphertext"] = (
        base64.urlsafe_b64encode(ciphertext)
        .decode("ascii")
    )

    path.write_text(
        json.dumps(envelope),
        encoding="utf-8",
    )

    with pytest.raises(VaultAuthenticationError):

        store.load(
            vault.vault_id,
            PASSWORD,
        )


def test_load_rejects_malformed_file(tmp_path):

    store = FileVaultStore(tmp_path)

    vault = create_vault()

    path = store._vault_path(vault.vault_id)

    path.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    path.write_bytes(b"invalid")

    with pytest.raises(VaultFormatError):

        store.load(
            vault.vault_id,
            PASSWORD,
        )


def test_exists(tmp_path):

    store = FileVaultStore(tmp_path)

    vault = create_vault()

    assert not store.exists(vault.vault_id)

    store.save(vault, PASSWORD)

    assert store.exists(vault.vault_id)


def test_delete_existing_vault(tmp_path):

    store = FileVaultStore(tmp_path)

    vault = create_vault()

    store.save(vault, PASSWORD)

    assert store.delete(vault.vault_id) is True

    assert not store.exists(vault.vault_id)


def test_delete_missing_vault(tmp_path):

    store = FileVaultStore(tmp_path)

    assert store.delete("missing-vault") is False


def test_load_missing_vault(tmp_path):

    store = FileVaultStore(tmp_path)

    with pytest.raises(VaultNotFoundError):

        store.load(
            "missing-vault",
            PASSWORD,
        )


def test_vault_filename_does_not_expose_vault_id(tmp_path):

    store = FileVaultStore(tmp_path)

    vault = create_vault(
        vault_id="my-sensitive-vault-id",
    )

    path = store.save(vault, PASSWORD)

    assert "my-sensitive-vault-id" not in path.name

    assert len(path.stem) == 64


def test_path_traversal_is_prevented(tmp_path):

    store = FileVaultStore(tmp_path)

    path = store._vault_path(
        "../../outside/../../secret"
    )

    assert path.parent == tmp_path
    assert path.name.endswith(".vault")


def test_invalid_vault_id_is_rejected(tmp_path):

    store = FileVaultStore(tmp_path)

    with pytest.raises(ValueError):
        store.exists("")


def test_requested_vault_id_must_match_file_contents(tmp_path):

    store = FileVaultStore(tmp_path)

    vault_a = create_vault(
        vault_id="vault-a",
    )

    store.save(vault_a, PASSWORD)

    path_a = store._vault_path("vault-a")
    path_b = store._vault_path("vault-b")

    path_b.write_bytes(
        path_a.read_bytes()
    )

    with pytest.raises(VaultMismatchError):

        store.load(
            "vault-b",
            PASSWORD,
        )


def test_save_updates_existing_vault(tmp_path):

    store = FileVaultStore(tmp_path)

    first = create_vault(
        original="Jane Doe",
    )

    store.save(first, PASSWORD)

    second = create_vault(
        original="John Smith",
    )

    # Use the same identity to update the existing vault.
    second = MaskingVault(
        second.ruleset,
        vault_id=first.vault_id,
    )

    second.reserve_source_text("John Smith")
    second.get_or_create_token("John Smith")

    store.save(second, PASSWORD)

    loaded = store.load(
        first.vault_id,
        PASSWORD,
    )

    assert loaded.get_token("John Smith") is not None
    assert loaded.get_token("Jane Doe") is None


def test_failed_atomic_save_preserves_previous_file(
    tmp_path,
    monkeypatch,
):

    store = FileVaultStore(tmp_path)

    original = create_vault(
        original="Jane Doe",
    )

    path = store.save(original, PASSWORD)

    original_bytes = path.read_bytes()

    updated = create_vault(
        original="John Smith",
    )

    updated = MaskingVault(
        updated.ruleset,
        vault_id=original.vault_id,
    )

    updated.reserve_source_text("John Smith")
    updated.get_or_create_token("John Smith")

    def fail_replace(source, destination):
        raise OSError("Simulated replacement failure")

    monkeypatch.setattr(
        file_module.os,
        "replace",
        fail_replace,
    )

    with pytest.raises(OSError):

        store.save(
            updated,
            PASSWORD,
        )

    # Original vault remains intact.
    assert path.read_bytes() == original_bytes

    loaded = store.load(
        original.vault_id,
        PASSWORD,
    )

    assert loaded.get_token("Jane Doe") is not None

    # Temporary files should be cleaned up.
    temporary_files = [
        item
        for item in tmp_path.iterdir()
        if item.suffix == ".tmp"
    ]

    assert temporary_files == []


def test_store_representation(tmp_path):

    store = FileVaultStore(tmp_path)

    assert "FileVaultStore" in repr(store)