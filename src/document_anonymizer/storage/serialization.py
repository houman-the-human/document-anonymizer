from __future__ import annotations

import json

from document_anonymizer.core.exceptions import (
    RuleValidationError,
    VaultFormatError,
    VaultIntegrityError,
)
from document_anonymizer.core.rules import (
    MaskingRule,
    RuleSet,
)
from document_anonymizer.core.vault import MaskingVault


SCHEMA_VERSION = 3

MAX_SERIALIZED_VAULT_BYTES = 64 * 1024 * 1024
MAX_RULES = 10_000
MAX_MAPPINGS = 1_000_000

ROOT_KEYS_V1 = {
    "schema_version",
    "vault_id",
    "rules",
    "mappings",
    "reserved_tokens",
}

ROOT_KEYS_V2 = ROOT_KEYS_V1 | {
    "normalization",
    "ignore_zwnj",
}

ROOT_KEYS_V3 = ROOT_KEYS_V2 | {"metadata_snapshot"}

RULE_KEYS = {
    "rule_id",
    "kind",
    "pattern",
    "priority",
    "case_sensitive",
    "flags",
    "enabled",
}


# -------------------------------------------------
# JSON helpers
# -------------------------------------------------

def _reject_duplicate_keys(pairs):

    result = {}

    for key, value in pairs:

        if key in result:
            raise ValueError(
                f"Duplicate JSON key: {key}"
            )

        result[key] = value

    return result


def _encode_json(data: dict) -> bytes:

    return json.dumps(
        data,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


# -------------------------------------------------
# Rule serialization
# -------------------------------------------------

def _serialize_rule(rule: MaskingRule) -> dict:

    return {
        "rule_id": rule.rule_id,
        "kind": rule.kind,
        "pattern": rule.pattern,
        "priority": rule.priority,
        "case_sensitive": rule.case_sensitive,
        "flags": list(rule.flags),
        "enabled": rule.enabled,
    }


def _deserialize_rule(data: dict) -> MaskingRule:

    if not isinstance(data, dict):
        raise VaultFormatError(
            "Rule must be a JSON object"
        )

    if set(data) != RULE_KEYS:
        raise VaultFormatError(
            "Invalid rule structure"
        )

    if not isinstance(data["flags"], list):
        raise VaultFormatError(
            "Rule flags must be a list"
        )

    try:
        return MaskingRule(
            rule_id=data["rule_id"],
            kind=data["kind"],
            pattern=data["pattern"],
            priority=data["priority"],
            case_sensitive=data["case_sensitive"],
            flags=tuple(data["flags"]),
            enabled=data["enabled"],
        )

    except (RuleValidationError, TypeError, ValueError) as exc:
        raise VaultFormatError(
            "Invalid masking rule in vault"
        ) from exc


# -------------------------------------------------
# Vault serialization
# -------------------------------------------------

def serialize_vault(vault: MaskingVault) -> bytes:
    """
    Convert a MaskingVault into UTF-8 JSON bytes.

    IMPORTANT:
        This is plaintext serialization.
        Encrypt the returned bytes before writing them to disk.
    """

    if not isinstance(vault, MaskingVault):
        raise TypeError(
            "Expected a MaskingVault instance"
        )

    rules = [
        _serialize_rule(rule)
        for rule in vault.ruleset.rules
    ]

    token_state = vault.export_token_state()

    payload = {
        "schema_version": SCHEMA_VERSION,
        "vault_id": vault.vault_id,
        "normalization": vault.normalization,
        "ignore_zwnj": vault.ignore_zwnj,
        "metadata_snapshot": vault.metadata_snapshot,
        "rules": rules,
        "mappings": token_state["mappings"],
        "reserved_tokens": token_state["reserved_tokens"],
    }

    serialized = _encode_json(payload)

    if len(serialized) > MAX_SERIALIZED_VAULT_BYTES:
        raise VaultFormatError(
            "Serialized vault exceeds maximum allowed size"
        )

    return serialized


# -------------------------------------------------
# Vault deserialization
# -------------------------------------------------

def deserialize_vault(data: bytes) -> MaskingVault:
    """
    Reconstruct a MaskingVault from serialized JSON bytes.

    Rules and mappings are validated before being accepted.
    """

    if not isinstance(data, bytes):
        raise TypeError(
            "Serialized vault must be bytes"
        )

    if len(data) > MAX_SERIALIZED_VAULT_BYTES:
        raise VaultFormatError(
            "Serialized vault exceeds maximum allowed size"
        )

    try:
        payload = json.loads(
            data.decode("utf-8"),
            object_pairs_hook=_reject_duplicate_keys,
        )

    except (UnicodeDecodeError, ValueError) as exc:
        raise VaultFormatError(
            "Invalid serialized vault JSON"
        ) from exc

    if not isinstance(payload, dict):
        raise VaultFormatError(
            "Vault payload must be a JSON object"
        )

    schema_version = payload.get("schema_version")
    if type(schema_version) is not int or schema_version not in (1, 2, SCHEMA_VERSION):
        raise VaultFormatError(
            "Unsupported vault schema version"
        )

    expected_root_keys = (
        ROOT_KEYS_V1 if schema_version == 1
        else ROOT_KEYS_V2 if schema_version == 2
        else ROOT_KEYS_V3
    )
    if set(payload) != expected_root_keys:
        raise VaultFormatError(
            "Invalid vault payload structure"
        )

    normalization = payload.get("normalization", "none")
    ignore_zwnj = payload.get("ignore_zwnj", False)
    if not isinstance(normalization, str) or not isinstance(ignore_zwnj, bool):
        raise VaultFormatError("Invalid vault normalization settings")

    metadata_snapshot = payload.get("metadata_snapshot")
    if schema_version == 3 and metadata_snapshot is not None:
        if not isinstance(metadata_snapshot, dict):
            raise VaultFormatError("Invalid metadata snapshot in vault")
        try:
            metadata_size = len(_encode_json(metadata_snapshot))
        except (TypeError, ValueError) as exc:
            raise VaultFormatError("Metadata snapshot is not valid JSON data") from exc
        if metadata_size > 16 * 1024 * 1024:
            raise VaultFormatError("Metadata snapshot exceeds maximum allowed size")

    vault_id = payload["vault_id"]

    if not isinstance(vault_id, str) or not vault_id.strip():
        raise VaultFormatError(
            "Invalid vault ID"
        )

    rules_data = payload["rules"]

    if not isinstance(rules_data, list):
        raise VaultFormatError(
            "Rules must be a list"
        )

    if len(rules_data) > MAX_RULES:
        raise VaultFormatError(
            "Too many masking rules"
        )

    rules = tuple(
        _deserialize_rule(rule)
        for rule in rules_data
    )

    try:
        ruleset = RuleSet(rules)

    except RuleValidationError as exc:
        raise VaultFormatError(
            "Invalid rule collection"
        ) from exc

    mappings = payload["mappings"]
    reserved_tokens = payload["reserved_tokens"]

    if not isinstance(mappings, list):
        raise VaultFormatError(
            "Mappings must be a list"
        )

    if len(mappings) > MAX_MAPPINGS:
        raise VaultFormatError(
            "Too many token mappings"
        )

    if not isinstance(reserved_tokens, list):
        raise VaultFormatError(
            "Reserved tokens must be a list"
        )

    if len(reserved_tokens) > MAX_MAPPINGS:
        raise VaultFormatError(
            "Too many reserved tokens"
        )

    try:
        vault = MaskingVault(
            ruleset=ruleset,
            vault_id=vault_id,
            normalization=normalization,
            ignore_zwnj=ignore_zwnj,
        )
    except (TypeError, ValueError) as exc:
        raise VaultFormatError(
            "Invalid vault normalization settings"
        ) from exc

    try:
        vault.restore_token_state(
            mappings=mappings,
            reserved_tokens=reserved_tokens,
        )
        if schema_version == 3:
            vault.restore_metadata_snapshot(metadata_snapshot)

    except (TypeError, ValueError) as exc:
        raise VaultIntegrityError(
            "Vault token mappings are inconsistent or corrupted"
        ) from exc

    return vault