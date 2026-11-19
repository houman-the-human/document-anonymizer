from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Mapping


@dataclass(frozen=True, slots=True)
class MatchCandidate:
    """
    Represents a potential match found by a masking rule.

    Positions use Python string offsets:
    start is inclusive, end is exclusive.
    """

    start: int
    end: int
    value: str
    rule_id: str
    priority: int
    rule_index: int

    def __post_init__(self):
        if self.start < 0:
            raise ValueError("Match start cannot be negative")

        if self.end <= self.start:
            raise ValueError("Match end must be greater than start")

        if not self.value:
            raise ValueError("Matched value cannot be empty")

        if not self.rule_id:
            raise ValueError("Rule ID cannot be empty")

        if self.end - self.start != len(self.value):
            raise ValueError("Match positions do not match value length")

        if self.rule_index < 0:
            raise ValueError("Rule index cannot be negative")

    @property
    def length(self) -> int:
        return self.end - self.start


@dataclass(frozen=True, slots=True)
class MaskReplacement:
    """Metadata for one individual masking occurrence."""

    start: int
    end: int

    original: str
    token: str

    rule_id: str

    def __post_init__(self):

        if type(self.start) is not int or self.start < 0:
            raise ValueError("Invalid replacement start")

        if type(self.end) is not int or self.end <= self.start:
            raise ValueError("Invalid replacement end")

        if not isinstance(self.original, str) or not self.original:
            raise ValueError("Invalid original value")

        if self.end - self.start != len(self.original):
            raise ValueError("Replacement positions do not match original length")

        if not isinstance(self.token, str) or not self.token:
            raise ValueError("Invalid replacement token")

        if not isinstance(self.rule_id, str) or not self.rule_id:
            raise ValueError("Invalid rule ID")


@dataclass(frozen=True, slots=True)
class MaskResult:
    """Result returned by a masking operation."""

    vault_id: str
    masked_text: str
    replacements_count: int

    unique_tokens: tuple[str, ...] = ()

    replacements_by_rule: Mapping[str, int] = field(
        default_factory=dict
    )

    replacements: tuple[MaskReplacement, ...] = ()

    def __post_init__(self):
        if not self.vault_id:
            raise ValueError("Vault ID cannot be empty")

        if self.replacements_count < 0:
            raise ValueError("Replacement count cannot be negative")

        object.__setattr__(
            self,
            "unique_tokens",
            tuple(self.unique_tokens)
        )

        counts = dict(self.replacements_by_rule)

        if any(value < 0 for value in counts.values()):
            raise ValueError("Rule replacement counts cannot be negative")

        object.__setattr__(
            self,
            "replacements_by_rule",
            MappingProxyType(counts)
        )


@dataclass(frozen=True, slots=True)
class RestoreResult:
    """Result returned by a restoration operation."""

    vault_id: str
    restored_text: str
    restored_count: int

    unknown_tokens: tuple[str, ...] = ()

    def __post_init__(self):
        if not self.vault_id:
            raise ValueError("Vault ID cannot be empty")

        if self.restored_count < 0:
            raise ValueError("Restored count cannot be negative")

        object.__setattr__(
            self,
            "unknown_tokens",
            tuple(self.unknown_tokens)
        )
