from __future__ import annotations

import math
from dataclasses import dataclass

import regex

from .exceptions import (
    RegexTimeoutError,
    UnsafeNormalizedMatchError,
    ZeroWidthMatchError,
)
from .normalization import normalize_text, validate_normalization
from .models import MatchCandidate
from .rules import RuleSet, compile_rule


@dataclass(frozen=True, slots=True)
class _CompiledRule:
    """Internal representation of a compiled masking rule."""

    rule_index: int
    rule_id: str
    priority: int
    pattern: regex.Pattern


class Matcher:
    """
    Finds literal and regex matches and resolves overlaps.

    Precedence:
        1. Higher priority
        2. Longer match
        3. Earlier text position
        4. Earlier rule declaration

    Returned matches are always ordered by their position
    in the original text.
    """

    def __init__(
        self,
        ruleset: RuleSet,
        regex_timeout_seconds: float = 0.2,
        normalization: str = "none",
        *,
        ignore_zwnj: bool = False,
    ):
        if not isinstance(ruleset, RuleSet):
            raise TypeError("ruleset must be a RuleSet")

        if (
            isinstance(regex_timeout_seconds, bool)
            or not isinstance(regex_timeout_seconds, (int, float))
            or not math.isfinite(regex_timeout_seconds)
            or regex_timeout_seconds <= 0
        ):
            raise ValueError(
                "Regex timeout must be a positive finite number"
            )

        self.ruleset = ruleset
        self.regex_timeout = float(regex_timeout_seconds)
        self.normalization, self.ignore_zwnj = validate_normalization(
            normalization,
            ignore_zwnj,
        )

        self._compiled_rules = self._compile_rules()

    def _compile_rules(self) -> tuple[_CompiledRule, ...]:
        """Compile all enabled rules once during initialization."""

        compiled_rules = []

        for index, rule in enumerate(self.ruleset.rules):

            if not rule.enabled:
                continue

            compiled = compile_rule(
                rule,
                self.normalization,
                ignore_zwnj=self.ignore_zwnj,
            )

            compiled_rules.append(
                _CompiledRule(
                    rule_index=index,
                    rule_id=rule.rule_id,
                    priority=rule.priority,
                    pattern=compiled,
                )
            )

        return tuple(compiled_rules)

    def find_candidates(
        self,
        text: str,
    ) -> tuple[MatchCandidate, ...]:
        """
        Collect every potential match.

        Overlapping matches are intentionally retained here.
        No replacement or overlap resolution happens yet.
        """

        if not isinstance(text, str):
            raise TypeError("Input text must be a string")

        candidates = []
        normalized = normalize_text(
            text,
            self.normalization,
            ignore_zwnj=self.ignore_zwnj,
        )

        for compiled_rule in self._compiled_rules:

            try:
                matches = compiled_rule.pattern.finditer(
                    normalized.text,
                    overlapped=True,
                    timeout=self.regex_timeout,
                )

                for match in matches:

                    normalized_start, normalized_end = match.span(0)

                    # Empty matches cannot be masked safely.
                    if normalized_start == normalized_end:
                        raise ZeroWidthMatchError(
                            f"Rule '{compiled_rule.rule_id}' "
                            "produced a zero-width match"
                        )

                    try:
                        start, end = normalized.source_span(
                            normalized_start,
                            normalized_end,
                        )
                    except UnsafeNormalizedMatchError as exc:
                        raise UnsafeNormalizedMatchError(
                            f"Rule '{compiled_rule.rule_id}' produced a match "
                            "that cuts through a normalized grapheme cluster"
                        ) from exc

                    # Keep the exact original spelling and source offsets.
                    original_value = text[start:end]

                    candidates.append(
                        MatchCandidate(
                            start=start,
                            end=end,
                            value=original_value,
                            rule_id=compiled_rule.rule_id,
                            priority=compiled_rule.priority,
                            rule_index=compiled_rule.rule_index,
                        )
                    )

            except ZeroWidthMatchError:
                raise

            except TimeoutError as exc:
                raise RegexTimeoutError(
                    f"Regex execution timed out for rule "
                    f"'{compiled_rule.rule_id}'"
                ) from exc

        # Return candidates in their original text order.
        candidates.sort(
            key=lambda item: (
                item.start,
                item.end,
                item.rule_index,
            )
        )

        return tuple(candidates)

    def resolve_overlaps(
        self,
        candidates: tuple[MatchCandidate, ...],
    ) -> tuple[MatchCandidate, ...]:
        """
        Select non-overlapping candidates according to precedence.

        This operation does not modify the original text.
        """

        # Higher priority, longer match, earlier position,
        # then earlier rule declaration.
        ordered = sorted(
            candidates,
            key=lambda item: (
                -item.priority,
                -item.length,
                item.start,
                item.rule_index,
            ),
        )

        # One byte per character in the input.
        # This tracks positions already occupied by accepted matches.
        occupied = bytearray(
            max((item.end for item in candidates), default=0)
        )

        selected = []

        for candidate in ordered:

            start = candidate.start
            end = candidate.end

            if any(occupied[start:end]):
                continue

            occupied[start:end] = (
                b"\x01" * (end - start)
            )

            selected.append(candidate)

        # Restore original document order.
        selected.sort(
            key=lambda item: (
                item.start,
                item.end,
            )
        )

        return tuple(selected)

    def find_matches(
        self,
        text: str,
    ) -> tuple[MatchCandidate, ...]:
        """
        Find all matches and return the final non-overlapping set.
        """

        candidates = self.find_candidates(text)

        return self.resolve_overlaps(candidates)
