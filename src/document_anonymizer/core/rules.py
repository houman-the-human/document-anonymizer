from dataclasses import dataclass
from typing import Literal

import regex

from .exceptions import (
    RegexTimeoutError,
    RuleValidationError,
    ZeroWidthMatchError,
)


RuleKind = Literal["literal", "regex"]

# Regex flags explicitly supported by the application.
# IGNORECASE is controlled through case_sensitive.
ALLOWED_REGEX_FLAGS = {
    "MULTILINE": regex.MULTILINE,
    "DOTALL": regex.DOTALL,
    "VERBOSE": regex.VERBOSE,
    "ASCII": regex.ASCII,
}

RULE_VALIDATION_TIMEOUT = 0.2


@dataclass(frozen=True, slots=True)
class MaskingRule:
    """
    Represents one user-defined masking rule.

    Literal rules:
        Match an exact string.

    Regex rules:
        Match text using a regular expression.

    Higher priority values take precedence over lower values.
    """

    rule_id: str
    kind: RuleKind
    pattern: str

    priority: int = 100
    case_sensitive: bool = True
    flags: tuple[str, ...] = ()
    enabled: bool = True

    def __post_init__(self):

        # Validate rule ID.
        if not isinstance(self.rule_id, str):
            raise RuleValidationError(
                "Rule ID must be a string"
            )

        if not self.rule_id.strip():
            raise RuleValidationError(
                "Rule ID cannot be empty"
            )

        # Validate rule type.
        if self.kind not in ("literal", "regex"):
            raise RuleValidationError(
                f"Unsupported rule type: {self.kind}"
            )

        # Validate pattern.
        if not isinstance(self.pattern, str):
            raise RuleValidationError(
                "Rule pattern must be a string"
            )

        if self.pattern == "":
            raise RuleValidationError(
                "Rule pattern cannot be empty"
            )

        # Validate priority.
        if isinstance(self.priority, bool) or not isinstance(
            self.priority, int
        ):
            raise RuleValidationError(
                "Rule priority must be an integer"
            )

        # Validate boolean options.
        if not isinstance(self.case_sensitive, bool):
            raise RuleValidationError(
                "case_sensitive must be boolean"
            )

        if not isinstance(self.enabled, bool):
            raise RuleValidationError(
                "enabled must be boolean"
            )

        # Normalize flags into an immutable tuple.
        try:
            normalized_flags = tuple(self.flags)
        except TypeError as exc:
            raise RuleValidationError(
                "Flags must be an iterable of strings"
            ) from exc

        if any(not isinstance(flag, str) for flag in normalized_flags):
            raise RuleValidationError(
                "Every regex flag must be a string"
            )

        if len(normalized_flags) != len(set(normalized_flags)):
            raise RuleValidationError(
                "Duplicate regex flags are not allowed"
            )

        object.__setattr__(self, "flags", normalized_flags)

        # Literal rules do not support regex flags.
        if self.kind == "literal" and normalized_flags:
            raise RuleValidationError(
                "Literal rules cannot have regex flags"
            )

        # Validate regex-specific options.
        if self.kind == "regex":
            self._validate_regex()

    def _validate_regex(self):
        """Validate regex syntax and basic empty-match behavior."""

        unknown_flags = (
            set(self.flags) - ALLOWED_REGEX_FLAGS.keys()
        )

        if unknown_flags:
            raise RuleValidationError(
                f"Unsupported regex flags: {unknown_flags}"
            )

        flags = get_regex_flags(self)

        try:
            compiled = regex.compile(
                self.pattern,
                flags
            )

            # Reject patterns that can match an empty string
            # without consuming any characters.
            empty_match = compiled.search(
                "",
                timeout=RULE_VALIDATION_TIMEOUT
            )

            if (
                empty_match is not None
                and empty_match.start() == empty_match.end()
            ):
                raise ZeroWidthMatchError(
                    f"Rule '{self.rule_id}' can match an empty string"
                )

        except (ZeroWidthMatchError, RuleValidationError):
            raise

        except TimeoutError as exc:
            raise RegexTimeoutError(
                f"Regex validation timed out for rule '{self.rule_id}'"
            ) from exc

        except regex.error as exc:
            raise RuleValidationError(
                f"Invalid regex in rule '{self.rule_id}': {exc}"
            ) from exc


@dataclass(frozen=True, slots=True)
class RuleSet:
    """
    Collection of masking rules belonging to one vault.

    Rule IDs must be unique within the collection.
    Declaration order is preserved.
    """

    rules: tuple[MaskingRule, ...]

    def __post_init__(self):

        try:
            normalized_rules = tuple(self.rules)
        except TypeError as exc:
            raise RuleValidationError(
                "Rules must be iterable"
            ) from exc

        if not all(
            isinstance(rule, MaskingRule)
            for rule in normalized_rules
        ):
            raise RuleValidationError(
                "Every entry must be a MaskingRule"
            )

        rule_ids = [
            rule.rule_id
            for rule in normalized_rules
        ]

        if len(rule_ids) != len(set(rule_ids)):
            raise RuleValidationError(
                "Duplicate rule IDs are not allowed"
            )

        object.__setattr__(
            self,
            "rules",
            normalized_rules
        )

    @property
    def enabled_rules(self) -> tuple[MaskingRule, ...]:
        """Return only enabled rules."""

        return tuple(
            rule
            for rule in self.rules
            if rule.enabled
        )


def get_regex_flags(rule: MaskingRule) -> int:
    """Convert rule configuration into regex engine flags."""

    flags = 0

    if not rule.case_sensitive:
        flags |= regex.IGNORECASE

    for flag in rule.flags:
        flags |= ALLOWED_REGEX_FLAGS[flag]

    return flags


def compile_rule(rule: MaskingRule) -> regex.Pattern:
    """
    Compile a masking rule into a regex pattern.

    Literal patterns are escaped so special characters
    are treated as ordinary text.
    """

    expression = (
        regex.escape(rule.pattern)
        if rule.kind == "literal"
        else rule.pattern
    )

    try:
        return regex.compile(
            expression,
            get_regex_flags(rule)
        )

    except regex.error as exc:
        raise RuleValidationError(
            f"Could not compile rule '{rule.rule_id}': {exc}"
        ) from exc