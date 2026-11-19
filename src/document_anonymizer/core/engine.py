from __future__ import annotations

from collections import Counter

from .exceptions import (
    AlreadyMaskedInputError,
)
from .matcher import Matcher
from .models import (
    MaskResult,
    RestoreResult,
    MaskReplacement,
)
from .tokens import TOKEN_PATTERN
from .vault import MaskingVault


class MaskingEngine:
    """
    Main interface for reversible text masking.

    Responsibilities:
        - Find matching substrings.
        - Resolve overlapping matches.
        - Generate or reuse masking tokens.
        - Restore known tokens.
        - Report unknown tokens.

    Each engine is bound to exactly one vault.
    """

    def __init__(
        self,
        vault: MaskingVault,
        regex_timeout_seconds: float = 0.2,
    ):
        if not isinstance(vault, MaskingVault):
            raise TypeError(
                "vault must be a MaskingVault instance"
            )

        self.vault = vault

        self.matcher = Matcher(
            ruleset=vault.ruleset,
            regex_timeout_seconds=regex_timeout_seconds,
            normalization=vault.normalization,
            ignore_zwnj=vault.ignore_zwnj,
        )

    # -------------------------------------------------
    # Masking
    # -------------------------------------------------

    def mask_text(self, text: str) -> MaskResult:
        """
        Replace matching substrings with unique tokens.

        Repeated identical original values reuse the same token.

        The original text is never modified.
        """

        if not isinstance(text, str):
            raise TypeError("Input text must be a string")

        # Prevent generated tokens from colliding with
        # token-like strings already present in the source.
        self.vault.reserve_source_text(text)

        # Find and resolve all matching substrings.
        matches = self.matcher.find_matches(text)

        output = []
        cursor = 0

        used_tokens = []
        replacements_by_rule = Counter()
        replacement_details = []

        for match in matches:

            # Preserve text before the match.
            output.append(
                text[cursor:match.start]
            )

            # Retrieve or generate a token.
            token = self.vault.get_or_create_token(
                match.value
            )

            output.append(token)

            used_tokens.append(token)

            replacement_details.append(
                MaskReplacement(
                    start=match.start,
                    end=match.end,
                    original=match.value,
                    token=token,
                    rule_id=match.rule_id,
                )
            )

            replacements_by_rule[match.rule_id] += 1

            cursor = match.end

        # Preserve the remaining text.
        output.append(text[cursor:])

        masked_text = "".join(output)

        return MaskResult(
            vault_id=self.vault.vault_id,
            masked_text=masked_text,
            replacements_count=len(matches),
            unique_tokens=tuple(
                dict.fromkeys(used_tokens)
            ),
            replacements_by_rule=dict(
                replacements_by_rule
            ),
            replacements=tuple(replacement_details),
        )

    # -------------------------------------------------
    # Restoration
    # -------------------------------------------------


    def unmask_text(self, text: str) -> RestoreResult:
        """
        Restore known masking tokens.

        Unknown tokens are preserved and reported.

        Restoration uses one substitution pass.
        Original values inserted during restoration
        are never processed recursively.
        """

        if not isinstance(text, str):
            raise TypeError("Input text must be a string")

        # Snapshot this vault's mappings.
        token_mapping = dict(
            self.vault.token_to_original
        )

        # Find all token-like strings.
        encountered_tokens = tuple(
            dict.fromkeys(
                TOKEN_PATTERN.findall(text)
            )
        )

        # Identify tokens not belonging to this vault.
        unknown_tokens = tuple(
            token
            for token in encountered_tokens
            if token not in token_mapping
        )

        restored_count = 0

        def replace_token(match):
            nonlocal restored_count

            token = match.group(0)

            original = token_mapping.get(token)

            if original is None:
                return token

            restored_count += 1

            return original

        # Single-pass restoration.
        restored_text = TOKEN_PATTERN.sub(
            replace_token,
            text
        )

        return RestoreResult(
            vault_id=self.vault.vault_id,
            restored_text=restored_text,
            restored_count=restored_count,
            unknown_tokens=unknown_tokens,
        )