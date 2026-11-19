
import json

import pytest

from document_anonymizer.core.exceptions import (
    VaultFormatError,
    VaultIntegrityError,
)

from document_anonymizer.core.rules import (
    MaskingRule,
    RuleSet,
)

from document_anonymizer.core.vault import MaskingVault

from document_anonymizer.storage.serialization import (
    serialize_vault,
    deserialize_vault,
)


def create_vault():

    ruleset = RuleSet((
        MaskingRule(
            rule_id="person",
            kind="literal",
            pattern="Jane Doe",
            priority=200,
        ),
        MaskingRule(
            rule_id="email",
            kind="regex",
            pattern=r"[\w.-]+@[\w.-]+\.\w+",
            case_sensitive=False,
            flags=("MULTILINE",),
        ),
        MaskingRule(
            rule_id="disabled",
            kind="literal",
            pattern="Inactive",
            enabled=False,
        ),
    ))

    vault = MaskingVault(
        ruleset,
        vault_id="vault-test-001",
    )

    vault.reserve_source_text(
        "Existing [[MASK_" + ("F" * 32) + "]]"
    )

    vault.get_or_create_token("Jane Doe")
    vault.get_or_create_token("jane@example.com")

    return vault


def test_vault_serialization_round_trip():

    original = create_vault()

    serialized = serialize_vault(original)

    restored = deserialize_vault(serialized)

    assert restored.vault_id == original.vault_id
    assert restored.ruleset == original.ruleset

    assert dict(restored.original_to_token) == dict(
        original.original_to_token
    )

    assert dict(restored.token_to_original) == dict(
        original.token_to_original
    )


def test_rules_are_preserved():

    original = create_vault()

    restored = deserialize_vault(
        serialize_vault(original)
    )

    assert len(restored.ruleset.rules) == 3

    assert restored.ruleset.rules[0].priority == 200

    assert restored.ruleset.rules[1].flags == ("MULTILINE",)

    assert restored.ruleset.rules[2].enabled is False


def test_reserved_tokens_are_preserved():

    original = create_vault()

    restored = deserialize_vault(
        serialize_vault(original)
    )

    original_state = original.export_token_state()
    restored_state = restored.export_token_state()

    assert (
        restored_state["reserved_tokens"]
        == original_state["reserved_tokens"]
    )


def test_restored_vault_can_restore_previous_tokens():

    original = create_vault()

    token = original.get_token("Jane Doe")

    restored = deserialize_vault(
        serialize_vault(original)
    )

    assert restored.get_original(token) == "Jane Doe"


def test_empty_vault_round_trip():

    vault = MaskingVault(
        RuleSet(()),
        vault_id="empty-vault",
    )

    restored = deserialize_vault(
        serialize_vault(vault)
    )

    assert restored.vault_id == "empty-vault"
    assert restored.token_count == 0
    assert restored.ruleset.rules == ()


def test_serialization_returns_bytes():

    vault = create_vault()

    result = serialize_vault(vault)

    assert isinstance(result, bytes)


def test_serialization_does_not_modify_vault():

    vault = create_vault()

    before = dict(vault.token_to_original)

    serialize_vault(vault)

    after = dict(vault.token_to_original)

    assert before == after


def test_invalid_input_type():

    with pytest.raises(TypeError):

        deserialize_vault("not-bytes")


def test_invalid_json():

    with pytest.raises(VaultFormatError):

        deserialize_vault(b"invalid json")


def test_duplicate_json_keys_rejected():

    with pytest.raises(VaultFormatError):

        deserialize_vault(
            b'{"schema_version":1,"schema_version":1}'
        )


def test_unknown_root_field_rejected():

    payload = json.loads(
        serialize_vault(create_vault())
    )

    payload["unknown"] = "value"

    with pytest.raises(VaultFormatError):

        deserialize_vault(
            json.dumps(payload).encode()
        )


def test_unsupported_schema_version_rejected():

    payload = json.loads(
        serialize_vault(create_vault())
    )

    payload["schema_version"] = 999

    with pytest.raises(VaultFormatError):

        deserialize_vault(
            json.dumps(payload).encode()
        )


def test_invalid_rule_rejected():

    payload = json.loads(
        serialize_vault(create_vault())
    )

    payload["rules"][0]["pattern"] = "("
    payload["rules"][0]["kind"] = "regex"

    with pytest.raises(VaultFormatError):

        deserialize_vault(
            json.dumps(payload).encode()
        )


def test_duplicate_token_mapping_rejected():

    payload = json.loads(
        serialize_vault(create_vault())
    )

    duplicate = dict(payload["mappings"][0])

    duplicate["original"] = "Different value"

    payload["mappings"].append(duplicate)

    with pytest.raises(VaultIntegrityError):

        deserialize_vault(
            json.dumps(payload).encode()
        )


def test_duplicate_original_mapping_rejected():

    payload = json.loads(
        serialize_vault(create_vault())
    )

    duplicate = dict(payload["mappings"][0])

    duplicate["token"] = (
        "[[MASK_" + ("A" * 32) + "]]"
    )

    payload["mappings"].append(duplicate)

    with pytest.raises(VaultIntegrityError):

        deserialize_vault(
            json.dumps(payload).encode()
        )


def test_invalid_token_format_rejected():

    payload = json.loads(
        serialize_vault(create_vault())
    )

    payload["mappings"][0]["token"] = "INVALID"

    with pytest.raises(VaultIntegrityError):

        deserialize_vault(
            json.dumps(payload).encode()
        )


def test_reserved_token_mapping_conflict_rejected():

    payload = json.loads(
        serialize_vault(create_vault())
    )

    existing_token = payload["mappings"][0]["token"]

    payload["reserved_tokens"].append(existing_token)

    with pytest.raises(VaultIntegrityError):

        deserialize_vault(
            json.dumps(payload).encode()
        )


def test_duplicate_reserved_tokens_rejected():

    payload = json.loads(
        serialize_vault(create_vault())
    )

    existing = payload["reserved_tokens"][0]

    payload["reserved_tokens"].append(existing)

    with pytest.raises(VaultIntegrityError):

        deserialize_vault(
            json.dumps(payload).encode()
        )


def test_invalid_vault_id_rejected():

    payload = json.loads(
        serialize_vault(create_vault())
    )

    payload["vault_id"] = ""

    with pytest.raises(VaultFormatError):

        deserialize_vault(
            json.dumps(payload).encode()
        )


def test_legacy_token_mappings_are_restored():

    payload = json.loads(
        serialize_vault(create_vault())
    )

    legacy_token = f"[[MASK_{'A' * 32}]]"

    payload["mappings"][0]["token"] = legacy_token

    restored = deserialize_vault(
        json.dumps(payload).encode("utf-8")
    )

    assert restored.get_original(legacy_token) == "Jane Doe"

    assert restored.get_token("Jane Doe") == legacy_token


def test_normalization_settings_are_preserved():
    ruleset = RuleSet((
        MaskingRule("person", "literal", "علی"),
    ))
    original = MaskingVault(
        ruleset,
        normalization="persian",
        ignore_zwnj=True,
    )
    restored = deserialize_vault(serialize_vault(original))

    assert restored.normalization == "persian"
    assert restored.ignore_zwnj is True


def test_legacy_schema_v1_vault_defaults_to_no_normalization():
    payload = json.loads(serialize_vault(create_vault()))
    payload["schema_version"] = 1
    payload.pop("normalization")
    payload.pop("ignore_zwnj")
    payload.pop("metadata_snapshot")

    restored = deserialize_vault(
        json.dumps(payload, ensure_ascii=False).encode("utf-8")
    )

    assert restored.normalization == "none"
    assert restored.ignore_zwnj is False


def test_invalid_normalization_settings_are_rejected():
    payload = json.loads(serialize_vault(create_vault()))
    payload["normalization"] = "unsafe-mode"

    with pytest.raises(VaultFormatError, match="normalization"):
        deserialize_vault(
            json.dumps(payload, ensure_ascii=False).encode("utf-8")
        )


def test_zwnj_setting_requires_persian_mode():
    payload = json.loads(serialize_vault(create_vault()))
    payload["normalization"] = "unicode"
    payload["ignore_zwnj"] = True

    with pytest.raises(VaultFormatError, match="normalization"):
        deserialize_vault(
            json.dumps(payload, ensure_ascii=False).encode("utf-8")
        )



def test_persian_original_mapping_survives_vault_serialization_exactly():
    ruleset = RuleSet((
        MaskingRule("person", "literal", "علی رضایی"),
    ))
    original_text = "علي رضايي"
    vault = MaskingVault(
        ruleset,
        normalization="persian",
    )
    token = vault.get_or_create_token(original_text)

    restored = deserialize_vault(serialize_vault(vault))

    assert restored.normalization == "persian"
    assert restored.get_original(token) == original_text
    assert restored.get_token(original_text) == token


def test_legacy_schema_v2_vault_defaults_to_no_metadata_snapshot():
    payload = json.loads(serialize_vault(create_vault()))
    payload["schema_version"] = 2
    payload.pop("metadata_snapshot")

    restored = deserialize_vault(
        json.dumps(payload, ensure_ascii=False).encode("utf-8")
    )
    assert restored.metadata_snapshot is None
