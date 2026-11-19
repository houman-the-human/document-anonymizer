from __future__ import annotations

import unicodedata
from dataclasses import dataclass

import regex

from .exceptions import UnsafeNormalizedMatchError


NORMALIZATION_MODES = ("none", "unicode", "persian")


@dataclass(frozen=True, slots=True)
class NormalizedText:
    """Normalized matching text with a reversible source-span map.

    Each normalized code point maps to the complete original grapheme
    cluster from which it came. Matches are only mappable when their start
    and end align with grapheme-cluster boundaries.
    """

    original: str
    text: str
    source_spans: tuple[tuple[int, int], ...]

    def __post_init__(self) -> None:
        if len(self.text) != len(self.source_spans):
            raise ValueError("Normalized text and source map lengths differ")

    def source_span(self, start: int, end: int) -> tuple[int, int]:
        """Map a non-empty normalized span to its exact original span."""
        if (
            type(start) is not int
            or type(end) is not int
            or start < 0
            or end <= start
            or end > len(self.text)
        ):
            raise ValueError("Invalid normalized match span")

        if start > 0 and self.source_spans[start - 1] == self.source_spans[start]:
            raise UnsafeNormalizedMatchError(
                "Normalized match starts inside an original grapheme cluster"
            )

        if end < len(self.text) and self.source_spans[end - 1] == self.source_spans[end]:
            raise UnsafeNormalizedMatchError(
                "Normalized match ends inside an original grapheme cluster"
            )

        source_start = self.source_spans[start][0]
        source_end = self.source_spans[end - 1][1]

        if source_end <= source_start:
            raise UnsafeNormalizedMatchError(
                "Normalized match does not map to a non-empty source span"
            )

        return source_start, source_end


def validate_normalization(
    mode: str,
    ignore_zwnj: bool = False,
) -> tuple[str, bool]:
    """Validate and canonicalize a normalization configuration."""
    if mode not in NORMALIZATION_MODES:
        raise ValueError(
            f"Unsupported normalization mode {mode!r}; "
            f"choose one of {', '.join(NORMALIZATION_MODES)}"
        )

    if not isinstance(ignore_zwnj, bool):
        raise TypeError("ignore_zwnj must be a boolean")

    if ignore_zwnj and mode != "persian":
        raise ValueError(
            "ignore_zwnj can only be enabled with normalization='persian'"
        )

    return mode, ignore_zwnj


_PERSIAN_TRANSLATION = str.maketrans({
    "\u064a": "\u06cc",  # Arabic Yeh -> Persian Yeh
    "\u0643": "\u06a9",  # Arabic Kaf -> Persian Kaf
})


def _normalize_cluster(
    cluster: str,
    mode: str,
    ignore_zwnj: bool,
) -> str:
    if mode == "none":
        return cluster

    normalized = unicodedata.normalize("NFC", cluster)

    if mode == "persian":
        # PDF text extractors may expose Arabic presentation-form glyphs
        # instead of their base letters. Expand only those Arabic presentation
        # forms (plus NBSP) rather than applying compatibility normalization
        # to every character in the document.
        expanded = []
        for character in normalized:
            codepoint = ord(character)
            if (
                0xFB50 <= codepoint <= 0xFDFF
                or 0xFE70 <= codepoint <= 0xFEFF
                or character == "\u00a0"
            ):
                character = unicodedata.normalize("NFKC", character)
            expanded.append(character)

        normalized = "".join(expanded).translate(_PERSIAN_TRANSLATION)
        if ignore_zwnj:
            normalized = normalized.replace("\u200c", "")
        normalized = unicodedata.normalize("NFC", normalized)

    return normalized


def normalize_text(
    text: str,
    mode: str = "none",
    *,
    ignore_zwnj: bool = False,
) -> NormalizedText:
    """Build a normalized view while retaining original grapheme spans.

    Grapheme clusters are normalized independently. This makes every
    normalized code point traceable to a complete original cluster. A later
    match that cuts through a cluster is rejected rather than mapped
    approximately.
    """
    if not isinstance(text, str):
        raise TypeError("Text to normalize must be a string")

    mode, ignore_zwnj = validate_normalization(mode, ignore_zwnj)

    if mode == "none":
        return NormalizedText(
            original=text,
            text=text,
            source_spans=tuple((index, index + 1) for index in range(len(text))),
        )

    normalized_parts: list[str] = []
    source_spans: list[tuple[int, int]] = []
    cursor = 0

    for cluster in regex.findall(r"\X", text):
        start = cursor
        end = start + len(cluster)
        cursor = end

        normalized_cluster = _normalize_cluster(
            cluster,
            mode,
            ignore_zwnj,
        )
        if not normalized_cluster:
            continue

        normalized_parts.append(normalized_cluster)
        source_spans.extend(
            [(start, end)] * len(normalized_cluster)
        )

    return NormalizedText(
        original=text,
        text="".join(normalized_parts),
        source_spans=tuple(source_spans),
    )


def normalize_pattern(
    pattern: str,
    mode: str = "none",
    *,
    ignore_zwnj: bool = False,
) -> str:
    """Normalize a literal or regex source string using the same policy."""
    return normalize_text(
        pattern,
        mode,
        ignore_zwnj=ignore_zwnj,
    ).text
