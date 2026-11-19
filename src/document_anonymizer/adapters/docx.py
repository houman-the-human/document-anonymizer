from __future__ import annotations

from dataclasses import dataclass
from io import BytesIO
from pathlib import Path
import hashlib

from docx import Document
from docx.table import Table
from docx.text.hyperlink import Hyperlink
from docx.text.paragraph import Paragraph
from docx.text.run import Run

import os
import tempfile

from docx.oxml.ns import qn

from collections.abc import Mapping

from ..core.models import RestoreResult
from ..core.tokens import TOKEN_PATTERN


class DOCXAdapterError(Exception):
    """Base exception for DOCX adapter errors."""


class DOCXExtractionError(DOCXAdapterError):
    """Raised when DOCX text extraction fails."""


@dataclass(frozen=True, slots=True)
class DOCXRunText:
    """Text and offsets belonging to one Word run."""

    run_index: int
    start: int
    end: int
    text: str

    is_hyperlink: bool = False
    hyperlink_target: str | None = None

    def __post_init__(self):

        if self.run_index < 0:
            raise ValueError("Run index cannot be negative")

        if self.start < 0 or self.end < self.start:
            raise ValueError("Invalid run offsets")

        if self.end - self.start != len(self.text):
            raise ValueError("Run offsets do not match text length")


@dataclass(frozen=True, slots=True)
class DOCXParagraphText:
    """Extracted text and run mapping for one Word paragraph."""

    paragraph_index: int
    story: str
    section_index: int | None
    location: tuple[str | int, ...]

    text: str
    runs: tuple[DOCXRunText, ...]

    style_name: str | None = None

    def __post_init__(self):

        if self.paragraph_index < 0:
            raise ValueError("Paragraph index cannot be negative")

        if not isinstance(self.text, str):
            raise TypeError("Paragraph text must be a string")

        object.__setattr__(self, "runs", tuple(self.runs))

        cursor = 0

        for run in self.runs:

            if run.start != cursor:
                raise ValueError(
                    "Run offsets must be contiguous"
                )

            cursor = run.end

        if cursor != len(self.text):
            raise ValueError(
                "Run mapping does not cover paragraph text"
            )

    @property
    def has_text(self) -> bool:
        return bool(self.text.strip())

    @property
    def character_count(self) -> int:
        return len(self.text)


@dataclass(frozen=True, slots=True)
class DOCXTextDocument:
    """Extracted text from a complete DOCX document."""

    source_path: Path
    paragraphs: tuple[DOCXParagraphText, ...]
    source_sha256: str

    def __post_init__(self):
        object.__setattr__(
            self,
            "paragraphs",
            tuple(self.paragraphs),
        )

    @property
    def paragraph_count(self) -> int:
        return len(self.paragraphs)

    @property
    def has_text(self) -> bool:
        return any(
            paragraph.has_text
            for paragraph in self.paragraphs
        )

    @property
    def total_character_count(self) -> int:
        return sum(
            paragraph.character_count
            for paragraph in self.paragraphs
        )

    @property
    def text(self) -> str:
        """Return extracted paragraphs separated by newlines."""

        return "\n".join(
            paragraph.text
            for paragraph in self.paragraphs
        )


@dataclass(frozen=True, slots=True)
class DOCXRunSlice:
    """The portion of one Word run covered by a match."""

    run_index: int
    paragraph_start: int
    paragraph_end: int
    run_start: int
    run_end: int
    text: str

    is_hyperlink: bool = False
    hyperlink_target: str | None = None

    def __post_init__(self):
        if self.run_index < 0:
            raise ValueError("Invalid run index")

        if self.paragraph_start < 0:
            raise ValueError("Invalid paragraph start")

        if self.paragraph_end <= self.paragraph_start:
            raise ValueError("Invalid paragraph end")

        if self.run_start < 0 or self.run_end <= self.run_start:
            raise ValueError("Invalid run offsets")

        if self.paragraph_end - self.paragraph_start != len(self.text):
            raise ValueError("Paragraph offsets do not match text")

        if self.run_end - self.run_start != len(self.text):
            raise ValueError("Run offsets do not match text")


@dataclass(frozen=True, slots=True)
class DOCXMappedReplacement:
    """One masking occurrence mapped to its Word runs."""

    paragraph_index: int
    story: str
    section_index: int | None
    location: tuple[str | int, ...]

    start: int
    end: int
    original: str
    token: str
    rule_id: str

    run_slices: tuple[DOCXRunSlice, ...]

    def __post_init__(self):
        if self.paragraph_index < 0:
            raise ValueError("Invalid paragraph index")

        if self.start < 0 or self.end <= self.start:
            raise ValueError("Invalid replacement offsets")

        if self.end - self.start != len(self.original):
            raise ValueError("Replacement offsets do not match original")

        if not self.original or not self.token or not self.rule_id:
            raise ValueError("Invalid replacement data")

        object.__setattr__(self, "run_slices", tuple(self.run_slices))

        if not self.run_slices:
            raise ValueError("Replacement must map to at least one run")

        cursor = self.start
        reconstructed = []

        for item in self.run_slices:
            if item.paragraph_start != cursor:
                raise ValueError("Run slices are not contiguous")

            cursor = item.paragraph_end
            reconstructed.append(item.text)

        if cursor != self.end:
            raise ValueError("Run slices do not cover replacement")

        if "".join(reconstructed) != self.original:
            raise ValueError("Run slices do not reconstruct original")


@dataclass(frozen=True, slots=True)
class DOCXParagraphMaskPlan:
    """Masking plan for one extracted Word paragraph."""

    paragraph_index: int
    story: str
    section_index: int | None
    location: tuple[str | int, ...]

    original_text: str
    masked_text: str

    replacements: tuple[DOCXMappedReplacement, ...] = ()

    def __post_init__(self):
        object.__setattr__(
            self,
            "replacements",
            tuple(self.replacements),
        )

        cursor = 0
        output = []

        for replacement in self.replacements:
            if replacement.paragraph_index != self.paragraph_index:
                raise ValueError("Replacement belongs to another paragraph")

            if replacement.start < cursor:
                raise ValueError("Overlapping replacements")

            if self.original_text[
                replacement.start:replacement.end
            ] != replacement.original:
                raise ValueError("Replacement does not match paragraph text")

            output.append(self.original_text[cursor:replacement.start])
            output.append(replacement.token)
            cursor = replacement.end

        output.append(self.original_text[cursor:])

        if "".join(output) != self.masked_text:
            raise ValueError("Masked paragraph text is inconsistent")


@dataclass(frozen=True, slots=True)
class DOCXMaskPlan:
    """Complete masking plan for one DOCX document."""

    source_path: Path
    source_sha256: str
    vault_id: str

    paragraphs: tuple[DOCXParagraphMaskPlan, ...]

    def __post_init__(self):
        object.__setattr__(
            self,
            "paragraphs",
            tuple(self.paragraphs),
        )

        if not self.source_sha256:
            raise ValueError("Missing source fingerprint")

        if not self.vault_id:
            raise ValueError("Missing vault ID")

        indices = [
            paragraph.paragraph_index
            for paragraph in self.paragraphs
        ]

        if len(indices) != len(set(indices)):
            raise ValueError("Duplicate paragraph indices")

    @property
    def replacements_count(self) -> int:
        return sum(
            len(paragraph.replacements)
            for paragraph in self.paragraphs
        )

    @property
    def original_text(self) -> str:
        return "\n".join(
            paragraph.original_text
            for paragraph in self.paragraphs
        )

    @property
    def masked_text(self) -> str:
        return "\n".join(
            paragraph.masked_text
            for paragraph in self.paragraphs
        )


@dataclass(frozen=True, slots=True)
class DOCXMappedRestoration:
    """One token occurrence mapped to its Word runs."""

    paragraph_index: int
    story: str
    section_index: int | None
    location: tuple[str | int, ...]

    start: int
    end: int
    token: str
    original: str

    run_slices: tuple[DOCXRunSlice, ...]

    def __post_init__(self):
        if self.paragraph_index < 0:
            raise ValueError("Invalid paragraph index")

        if self.start < 0 or self.end <= self.start:
            raise ValueError("Invalid restoration offsets")

        if self.end - self.start != len(self.token):
            raise ValueError("Restoration offsets do not match token")

        if not TOKEN_PATTERN.fullmatch(self.token):
            raise ValueError("Invalid masking token")

        if not isinstance(self.original, str) or not self.original:
            raise ValueError("Invalid original value")

        object.__setattr__(
            self,
            "run_slices",
            tuple(self.run_slices),
        )

        if not self.run_slices:
            raise ValueError("Restoration must map to at least one run")

        cursor = self.start
        reconstructed = []

        for item in self.run_slices:
            if item.paragraph_start != cursor:
                raise ValueError("Run slices are not contiguous")

            cursor = item.paragraph_end
            reconstructed.append(item.text)

        if cursor != self.end:
            raise ValueError("Run slices do not cover token")

        if "".join(reconstructed) != self.token:
            raise ValueError("Run slices do not reconstruct token")


@dataclass(frozen=True, slots=True)
class DOCXParagraphUnmaskPlan:
    """Unmasking plan for one Word paragraph."""

    paragraph_index: int
    story: str
    section_index: int | None
    location: tuple[str | int, ...]

    masked_text: str
    restored_text: str

    restorations: tuple[DOCXMappedRestoration, ...] = ()
    unknown_tokens: tuple[str, ...] = ()

    def __post_init__(self):
        object.__setattr__(
            self,
            "restorations",
            tuple(self.restorations),
        )

        object.__setattr__(
            self,
            "unknown_tokens",
            tuple(self.unknown_tokens),
        )

        if len(self.unknown_tokens) != len(set(self.unknown_tokens)):
            raise ValueError("Duplicate unknown tokens")

        cursor = 0
        output = []

        for restoration in self.restorations:

            if restoration.paragraph_index != self.paragraph_index:
                raise ValueError("Restoration belongs to another paragraph")

            if restoration.start < cursor:
                raise ValueError("Overlapping restorations")

            if self.masked_text[
                restoration.start:restoration.end
            ] != restoration.token:
                raise ValueError("Restoration does not match paragraph")

            output.append(
                self.masked_text[cursor:restoration.start]
            )

            output.append(restoration.original)
            cursor = restoration.end

        output.append(self.masked_text[cursor:])

        if "".join(output) != self.restored_text:
            raise ValueError("Restored paragraph text is inconsistent")


@dataclass(frozen=True, slots=True)
class DOCXUnmaskPlan:
    """Complete unmasking plan for one DOCX document."""

    source_path: Path
    source_sha256: str
    vault_id: str

    paragraphs: tuple[DOCXParagraphUnmaskPlan, ...]

    def __post_init__(self):
        object.__setattr__(
            self,
            "paragraphs",
            tuple(self.paragraphs),
        )

        if not self.source_sha256:
            raise ValueError("Missing source fingerprint")

        if not self.vault_id:
            raise ValueError("Missing vault ID")

        indices = [
            paragraph.paragraph_index
            for paragraph in self.paragraphs
        ]

        if len(indices) != len(set(indices)):
            raise ValueError("Duplicate paragraph indices")

    @property
    def restored_count(self) -> int:
        """Number of token occurrences restored."""

        return sum(
            len(paragraph.restorations)
            for paragraph in self.paragraphs
        )

    @property
    def unknown_tokens(self) -> tuple[str, ...]:
        """Unique unknown tokens in document order."""

        return tuple(
            dict.fromkeys(
                token
                for paragraph in self.paragraphs
                for token in paragraph.unknown_tokens
            )
        )

    @property
    def masked_text(self) -> str:
        return "\n".join(
            paragraph.masked_text
            for paragraph in self.paragraphs
        )

    @property
    def restored_text(self) -> str:
        return "\n".join(
            paragraph.restored_text
            for paragraph in self.paragraphs
        )


def _iter_blocks(container, location_prefix=()):
    """
    Recursively iterate paragraphs inside document containers.

    Supports:
        - Main document body
        - Tables
        - Nested tables
        - Table cells
        - Headers
        - Footers
    """

    for block_index, block in enumerate(
        container.iter_inner_content()
    ):

        if isinstance(block, Paragraph):

            location = location_prefix + (
                "paragraph",
                block_index,
            )

            yield block, location

        elif isinstance(block, Table):

            for row_index, row in enumerate(block.rows):

                seen_cells = set()

                for cell_index, cell in enumerate(row.cells):

                    # Merged cells can appear more than once.
                    cell_identity = id(cell._tc)

                    if cell_identity in seen_cells:
                        continue

                    seen_cells.add(cell_identity)

                    cell_location = location_prefix + (
                        "table",
                        block_index,
                        "row",
                        row_index,
                        "cell",
                        cell_index,
                    )

                    yield from _iter_blocks(
                        cell,
                        cell_location,
                    )


def _iter_run_items(paragraph):
    """
    Yield ordinary and hyperlink runs in paragraph order.

    Hyperlink text is included in extraction.
    """

    for item in paragraph.iter_inner_content():

        if isinstance(item, Run):

            yield item, False, None

        elif isinstance(item, Hyperlink):

            target = item.url

            for run in item.runs:

                yield run, True, target


def _extract_paragraph(
    paragraph,
    paragraph_index: int,
    story: str,
    section_index: int | None,
    location: tuple[str | int, ...],
) -> DOCXParagraphText:

    text_parts = []
    run_records = []

    cursor = 0
    run_index = 0

    for run, is_hyperlink, hyperlink_target in (
        _iter_run_items(paragraph)
    ):

        run_text = run.text

        if not run_text:
            run_index += 1
            continue

        start = cursor
        end = start + len(run_text)

        run_records.append(
            DOCXRunText(
                run_index=run_index,
                start=start,
                end=end,
                text=run_text,
                is_hyperlink=is_hyperlink,
                hyperlink_target=hyperlink_target,
            )
        )

        text_parts.append(run_text)

        cursor = end
        run_index += 1

    try:
        style_name = paragraph.style.name
    except (AttributeError, KeyError):
        style_name = None

    return DOCXParagraphText(
        paragraph_index=paragraph_index,
        story=story,
        section_index=section_index,
        location=location,
        text="".join(text_parts),
        runs=tuple(run_records),
        style_name=style_name,
    )



def _iter_document_paragraphs(document):
    """
    Yield live Word paragraphs in exactly the same order
    and locations used by extract_docx_text().
    """

    # Main document body.
    for paragraph, location in _iter_blocks(document):
        yield (
            paragraph,
            "body",
            None,
            ("body",) + location,
        )

    # Headers and footers.
    seen_story_parts = set()

    story_types = (
        "header",
        "first_page_header",
        "even_page_header",
        "footer",
        "first_page_footer",
        "even_page_footer",
    )

    for section_index, section in enumerate(document.sections):

        for story_type in story_types:
            container = getattr(section, story_type)

            part_name = str(container.part.partname)

            if part_name in seen_story_parts:
                continue

            seen_story_parts.add(part_name)

            for paragraph, location in _iter_blocks(container):
                yield (
                    paragraph,
                    story_type,
                    section_index,
                    (story_type, section_index) + location,
                )


def _validate_replaceable_run(run):
    """
    Ensure replacing run.text will not destroy unsupported
    non-text Word objects.
    """

    allowed_tags = {
        qn("w:rPr"),
        qn("w:t"),
        qn("w:tab"),
        qn("w:br"),
        qn("w:cr"),
    }

    unsupported = [
        child.tag
        for child in run._r
        if child.tag not in allowed_tags
    ]

    if unsupported:
        raise DOCXAdapterError(
            "Cannot safely modify a Word run containing "
            f"unsupported XML elements: {unsupported}"
        )


def extract_docx_text(
    source: str | Path,
) -> DOCXTextDocument:
    """
    Extract text and run-level mappings from a DOCX file.

    The original document is never modified.

    Currently supported:
        - Body paragraphs
        - Tables and nested tables
        - Headers
        - Footers
        - Hyperlink text
        - Run-level character offsets

    Offsets use Python string positions:
        start inclusive, end exclusive.
    """

    if not isinstance(source, (str, Path)):
        raise TypeError(
            "Source must be a string or Path"
        )

    source_path = Path(source).expanduser().resolve()

    if source_path.suffix.lower() != ".docx":
        raise ValueError(
            "Source must have a .docx extension"
        )

    if not source_path.exists():
        raise FileNotFoundError(source_path)

    if not source_path.is_file():
        raise DOCXExtractionError(
            "Source path must point to a file"
        )

    try:
        source_bytes = source_path.read_bytes()

        source_sha256 = hashlib.sha256(
            source_bytes
        ).hexdigest()

        document = Document(
            BytesIO(source_bytes)
        )

    except Exception as exc:
        raise DOCXExtractionError(
            f"Unable to open DOCX file: {source_path}"
        ) from exc

    extracted_paragraphs = []

    # -------------------------------------------------
    # Main document body.
    # -------------------------------------------------

    try:

        for paragraph, location in _iter_blocks(document):

            extracted_paragraphs.append(
                _extract_paragraph(
                    paragraph=paragraph,
                    paragraph_index=len(extracted_paragraphs),
                    story="body",
                    section_index=None,
                    location=("body",) + location,
                )
            )

        # -------------------------------------------------
        # Headers and footers.
        # -------------------------------------------------

        seen_story_parts = set()

        story_types = (
            "header",
            "first_page_header",
            "even_page_header",
            "footer",
            "first_page_footer",
            "even_page_footer",
        )

        for section_index, section in enumerate(
            document.sections
        ):

            for story_type in story_types:

                container = getattr(
                    section,
                    story_type,
                )

                # Linked headers/footers share the same
                # underlying XML part. Process each part once.
                part_name = str(
                    container.part.partname
                )

                if part_name in seen_story_parts:
                    continue

                seen_story_parts.add(part_name)

                for paragraph, location in _iter_blocks(
                    container
                ):

                    extracted_paragraphs.append(
                        _extract_paragraph(
                            paragraph=paragraph,
                            paragraph_index=len(
                                extracted_paragraphs
                            ),
                            story=story_type,
                            section_index=section_index,
                            location=(
                                story_type,
                                section_index,
                            ) + location,
                        )
                    )

    except DOCXAdapterError:
        raise

    except Exception as exc:
        raise DOCXExtractionError(
            "Failed while extracting DOCX content"
        ) from exc

    return DOCXTextDocument(
        source_path=source_path,
        paragraphs=tuple(extracted_paragraphs),
        source_sha256=source_sha256,
    )



def _map_replacement_to_runs(
    paragraph: DOCXParagraphText,
    replacement,
) -> DOCXMappedReplacement:
    """
    Map one engine replacement to its corresponding
    run-local character ranges.
    """

    start = replacement.start
    end = replacement.end

    if (
        type(start) is not int
        or type(end) is not int
        or start < 0
        or end <= start
        or end > len(paragraph.text)
    ):
        raise DOCXAdapterError(
            "Invalid replacement offsets"
        )

    if paragraph.text[start:end] != replacement.original:
        raise DOCXAdapterError(
            "Replacement does not match paragraph text"
        )

    slices = []

    for run in paragraph.runs:

        overlap_start = max(start, run.start)
        overlap_end = min(end, run.end)

        if overlap_start >= overlap_end:
            continue

        local_start = overlap_start - run.start
        local_end = overlap_end - run.start

        run_text = run.text[local_start:local_end]

        slices.append(
            DOCXRunSlice(
                run_index=run.run_index,
                paragraph_start=overlap_start,
                paragraph_end=overlap_end,
                run_start=local_start,
                run_end=local_end,
                text=run_text,
                is_hyperlink=run.is_hyperlink,
                hyperlink_target=run.hyperlink_target,
            )
        )

    if not slices:
        raise DOCXAdapterError(
            "Replacement has no corresponding Word runs"
        )

    reconstructed = "".join(
        item.text for item in slices
    )

    if reconstructed != replacement.original:
        raise DOCXAdapterError(
            "Run mapping failed to reconstruct original text"
        )

    return DOCXMappedReplacement(
        paragraph_index=paragraph.paragraph_index,
        story=paragraph.story,
        section_index=paragraph.section_index,
        location=paragraph.location,
        start=start,
        end=end,
        original=replacement.original,
        token=replacement.token,
        rule_id=replacement.rule_id,
        run_slices=tuple(slices),
    )


def _map_token_to_runs(
    paragraph: DOCXParagraphText,
    start: int,
    end: int,
    token: str,
    original: str,
) -> DOCXMappedRestoration:
    """
    Map one masking token to its corresponding Word runs.

    Supports tokens spanning multiple runs.
    """

    if (
        type(start) is not int
        or type(end) is not int
        or start < 0
        or end <= start
        or end > len(paragraph.text)
    ):
        raise DOCXAdapterError(
            "Invalid token offsets"
        )

    if paragraph.text[start:end] != token:
        raise DOCXAdapterError(
            "Token does not match paragraph text"
        )

    slices = []

    for run in paragraph.runs:

        overlap_start = max(start, run.start)
        overlap_end = min(end, run.end)

        if overlap_start >= overlap_end:
            continue

        local_start = overlap_start - run.start
        local_end = overlap_end - run.start

        run_text = run.text[local_start:local_end]

        slices.append(
            DOCXRunSlice(
                run_index=run.run_index,
                paragraph_start=overlap_start,
                paragraph_end=overlap_end,
                run_start=local_start,
                run_end=local_end,
                text=run_text,
                is_hyperlink=run.is_hyperlink,
                hyperlink_target=run.hyperlink_target,
            )
        )

    if not slices:
        raise DOCXAdapterError(
            "Token has no corresponding Word runs"
        )

    reconstructed = "".join(
        item.text for item in slices
    )

    if reconstructed != token:
        raise DOCXAdapterError(
            "Run mapping failed to reconstruct token"
        )

    return DOCXMappedRestoration(
        paragraph_index=paragraph.paragraph_index,
        story=paragraph.story,
        section_index=paragraph.section_index,
        location=paragraph.location,
        start=start,
        end=end,
        token=token,
        original=original,
        run_slices=tuple(slices),
    )


def build_docx_masking_plan(
    extracted: DOCXTextDocument,
    engine,
) -> DOCXMaskPlan:
    """
    Build a reversible masking plan for an extracted DOCX.

    This function does not modify the original Word document.

    Matching, overlap resolution and token generation are
    delegated entirely to the existing MaskingEngine.

    All document text is reserved before token generation
    to prevent collisions with tokens in later paragraphs.
    """

    if not isinstance(extracted, DOCXTextDocument):
        raise TypeError(
            "extracted must be a DOCXTextDocument"
        )

    if engine is None or not callable(
        getattr(engine, "mask_text", None)
    ):
        raise TypeError(
            "engine must provide a mask_text method"
        )

    vault = getattr(engine, "vault", None)

    if vault is None:
        raise TypeError(
            "Engine must have an associated vault"
        )

    reserve_source_text = getattr(
        vault,
        "reserve_source_text",
        None,
    )

    if not callable(reserve_source_text):
        raise TypeError(
            "Engine vault must support source reservation"
        )

    vault_id = getattr(vault, "vault_id", None)

    if not isinstance(vault_id, str) or not vault_id:
        raise DOCXAdapterError(
            "Engine vault has an invalid vault ID"
        )

    # -------------------------------------------------
    # Phase 1: Reserve the complete document text.
    # -------------------------------------------------

    complete_source_text = "\n".join(
        paragraph.text
        for paragraph in extracted.paragraphs
    )

    reserve_source_text(complete_source_text)

    # -------------------------------------------------
    # Phase 2: Match each paragraph using the core engine.
    # -------------------------------------------------

    paragraph_plans = []

    for paragraph in extracted.paragraphs:

        result = engine.mask_text(
            paragraph.text
        )

        if result.vault_id != vault_id:
            raise DOCXAdapterError(
                "Masking result belongs to another vault"
            )

        if result.masked_text is None:
            raise DOCXAdapterError(
                "Masking engine returned invalid text"
            )

        mapped_replacements = tuple(
            _map_replacement_to_runs(
                paragraph,
                replacement,
            )
            for replacement in result.replacements
        )

        paragraph_plans.append(
            DOCXParagraphMaskPlan(
                paragraph_index=paragraph.paragraph_index,
                story=paragraph.story,
                section_index=paragraph.section_index,
                location=paragraph.location,
                original_text=paragraph.text,
                masked_text=result.masked_text,
                replacements=mapped_replacements,
            )
        )

    # -------------------------------------------------
    # Phase 3: Return immutable document-level plan.
    # -------------------------------------------------

    return DOCXMaskPlan(
        source_path=extracted.source_path,
        source_sha256=extracted.source_sha256,
        vault_id=vault_id,
        paragraphs=tuple(paragraph_plans),
    )


def build_docx_unmasking_plan(
    extracted: DOCXTextDocument,
    engine,
) -> DOCXUnmaskPlan:
    """
    Build a reversible unmasking plan for an extracted DOCX.

    Does not modify the original document.

    Responsibilities:
        - Find known masking tokens.
        - Restore their original values through MaskingEngine.
        - Map token occurrences to Word runs.
        - Support tokens spanning multiple runs.
        - Identify unknown tokens.
        - Validate restoration results.
    """

    if not isinstance(extracted, DOCXTextDocument):
        raise TypeError(
            "extracted must be a DOCXTextDocument"
        )

    if engine is None or not callable(
        getattr(engine, "unmask_text", None)
    ):
        raise TypeError(
            "engine must provide an unmask_text method"
        )

    vault = getattr(engine, "vault", None)

    if vault is None:
        raise TypeError(
            "Engine must have an associated vault"
        )

    vault_id = getattr(vault, "vault_id", None)

    if not isinstance(vault_id, str) or not vault_id:
        raise DOCXAdapterError(
            "Engine vault has an invalid vault ID"
        )

    token_mapping = getattr(
        vault,
        "token_to_original",
        None,
    )

    if not isinstance(token_mapping, Mapping):
        raise TypeError(
            "Engine vault must expose token_to_original mapping"
        )

    # Validate the vault mapping before processing.
    for token, original in token_mapping.items():

        if (
            not isinstance(token, str)
            or not TOKEN_PATTERN.fullmatch(token)
        ):
            raise DOCXAdapterError(
                "Vault contains an invalid token"
            )

        if not isinstance(original, str) or not original:
            raise DOCXAdapterError(
                "Vault contains an invalid original value"
            )

    paragraph_plans = []

    # Process each extracted paragraph independently.
    for paragraph in extracted.paragraphs:

        result = engine.unmask_text(
            paragraph.text
        )

        if not isinstance(result, RestoreResult):
            raise DOCXAdapterError(
                "Masking engine returned an invalid restoration result"
            )

        if result.vault_id != vault_id:
            raise DOCXAdapterError(
                "Restoration result belongs to another vault"
            )

        if not isinstance(result.restored_text, str):
            raise DOCXAdapterError(
                "Masking engine returned invalid restored text"
            )

        restorations = []
        unknown_tokens = []

        expected_output = []
        cursor = 0

        # Find every token occurrence in the original paragraph.
        for match in TOKEN_PATTERN.finditer(paragraph.text):

            token = match.group(0)

            original = token_mapping.get(token)

            # Unknown tokens remain unchanged.
            if original is None:

                if token not in unknown_tokens:
                    unknown_tokens.append(token)

                continue

            # Preserve text preceding this token.
            expected_output.append(
                paragraph.text[cursor:match.start()]
            )

            expected_output.append(original)

            # Map this token to its Word runs.
            restorations.append(
                _map_token_to_runs(
                    paragraph=paragraph,
                    start=match.start(),
                    end=match.end(),
                    token=token,
                    original=original,
                )
            )

            cursor = match.end()

        expected_output.append(
            paragraph.text[cursor:]
        )

        expected_restored_text = "".join(
            expected_output
        )

        # -------------------------------------------------
        # Validate the engine result.
        # -------------------------------------------------

        if (
            type(result.restored_count) is not int
            or result.restored_count != len(restorations)
        ):
            raise DOCXAdapterError(
                "Restoration count does not match token mapping"
            )

        if tuple(result.unknown_tokens) != tuple(unknown_tokens):
            raise DOCXAdapterError(
                "Unknown token information is inconsistent"
            )

        if result.restored_text != expected_restored_text:
            raise DOCXAdapterError(
                "Restored text does not match vault mappings"
            )

        # -------------------------------------------------
        # Create paragraph-level plan.
        # -------------------------------------------------

        paragraph_plans.append(
            DOCXParagraphUnmaskPlan(
                paragraph_index=paragraph.paragraph_index,
                story=paragraph.story,
                section_index=paragraph.section_index,
                location=paragraph.location,
                masked_text=paragraph.text,
                restored_text=result.restored_text,
                restorations=tuple(restorations),
                unknown_tokens=tuple(unknown_tokens),
            )
        )

    return DOCXUnmaskPlan(
        source_path=extracted.source_path,
        source_sha256=extracted.source_sha256,
        vault_id=vault_id,
        paragraphs=tuple(paragraph_plans),
    )


def apply_docx_masking_plan(
    plan: DOCXMaskPlan,
    output_path: str | Path,
) -> Path:
    """
    Apply a previously generated masking plan to a DOCX file.

    The source document is never modified.

    Features:
        - SHA-256 source verification.
        - Single-run replacements.
        - Cross-run replacements.
        - Multiple replacements per paragraph.
        - Body, table and nested-table support.
        - Header and footer support.
        - Preservation of unaffected run formatting.
        - Output verification before publication.

    Tokens inherit the formatting of the first affected run.

    Hyperlink text replacements are rejected because the
    hyperlink target may contain sensitive information.

    Returns:
        Absolute path to the masked DOCX file.
    """

    if not isinstance(plan, DOCXMaskPlan):
        raise TypeError("plan must be a DOCXMaskPlan")

    if not isinstance(output_path, (str, Path)):
        raise TypeError(
            "output_path must be a string or Path"
        )

    source_path = plan.source_path.expanduser().resolve()
    destination = Path(output_path).expanduser().resolve()

    # -------------------------------------------------
    # 1. Validate paths.
    # -------------------------------------------------

    if source_path.suffix.lower() != ".docx":
        raise DOCXAdapterError(
            "Source must be a DOCX file"
        )

    if destination.suffix.lower() != ".docx":
        raise DOCXAdapterError(
            "Output must have a .docx extension"
        )

    if source_path == destination:
        raise DOCXAdapterError(
            "Source and output paths must be different"
        )

    if not source_path.is_file():
        raise FileNotFoundError(source_path)

    if not destination.parent.is_dir():
        raise DOCXAdapterError(
            "Output directory does not exist"
        )

    if destination.exists():
        raise DOCXAdapterError(
            "Output file already exists; refusing to overwrite"
        )

    # -------------------------------------------------
    # 2. Read and verify source fingerprint.
    # -------------------------------------------------

    source_bytes = source_path.read_bytes()

    actual_fingerprint = hashlib.sha256(
        source_bytes
    ).hexdigest()

    if actual_fingerprint != plan.source_sha256:
        raise DOCXAdapterError(
            "Source document has changed since the "
            "masking plan was generated"
        )

    try:
        document = Document(BytesIO(source_bytes))

    except Exception as exc:
        raise DOCXAdapterError(
            "Unable to reopen source DOCX"
        ) from exc

    # -------------------------------------------------
    # 3. Resolve live paragraphs and validate the plan.
    # -------------------------------------------------

    live_paragraphs = list(
        _iter_document_paragraphs(document)
    )

    if len(live_paragraphs) != len(plan.paragraphs):
        raise DOCXAdapterError(
            "Paragraph count does not match masking plan"
        )

    # Each run index maps to a list of:
    # (local_start, local_end, replacement_text)
    paragraph_edits = []

    for expected_index, (
        paragraph_plan,
        live_item,
    ) in enumerate(
        zip(plan.paragraphs, live_paragraphs)
    ):

        paragraph, story, section_index, location = live_item

        if paragraph_plan.paragraph_index != expected_index:
            raise DOCXAdapterError(
                "Paragraph indices do not match document order"
            )

        if (
            paragraph_plan.story != story
            or paragraph_plan.section_index != section_index
            or paragraph_plan.location != location
        ):
            raise DOCXAdapterError(
                "Paragraph location does not match masking plan"
            )

        run_items = list(_iter_run_items(paragraph))

        run_offsets = {}
        extracted_parts = []
        cursor = 0

        for run_index, (run, is_hyperlink, target) in enumerate(
            run_items
        ):

            run_text = run.text

            run_offsets[run_index] = (
                cursor,
                cursor + len(run_text),
                run,
                is_hyperlink,
                target,
            )

            extracted_parts.append(run_text)
            cursor += len(run_text)

        current_text = "".join(extracted_parts)

        if current_text != paragraph_plan.original_text:
            raise DOCXAdapterError(
                "Paragraph content does not match masking plan"
            )

        edits = {}

        previous_end = 0

        for replacement in paragraph_plan.replacements:

            if (
                replacement.story != story
                or replacement.section_index != section_index
                or replacement.location != location
            ):
                raise DOCXAdapterError(
                    "Replacement location mismatch"
                )

            if replacement.start < previous_end:
                raise DOCXAdapterError(
                    "Overlapping or unordered replacements"
                )

            if (
                replacement.end > len(current_text)
                or current_text[
                    replacement.start:replacement.end
                ] != replacement.original
            ):
                raise DOCXAdapterError(
                    "Replacement does not match source paragraph"
                )

            previous_end = replacement.end

            reconstructed = []

            for slice_index, run_slice in enumerate(
                replacement.run_slices
            ):

                run_data = run_offsets.get(
                    run_slice.run_index
                )

                if run_data is None:
                    raise DOCXAdapterError(
                        "Replacement references an unknown run"
                    )

                (
                    run_global_start,
                    run_global_end,
                    run,
                    is_hyperlink,
                    hyperlink_target,
                ) = run_data

                if is_hyperlink or run_slice.is_hyperlink:
                    raise DOCXAdapterError(
                        "Masking inside hyperlinks is not supported. "
                        "The hyperlink target may contain sensitive data."
                    )

                if run_slice.hyperlink_target != hyperlink_target:
                    raise DOCXAdapterError(
                        "Hyperlink metadata mismatch"
                    )

                if (
                    run_slice.run_start < 0
                    or run_slice.run_end > len(run.text)
                    or run_slice.run_end <= run_slice.run_start
                ):
                    raise DOCXAdapterError(
                        "Invalid run-local offsets"
                    )

                if (
                    run_slice.paragraph_start
                    != run_global_start + run_slice.run_start
                    or run_slice.paragraph_end
                    != run_global_start + run_slice.run_end
                ):
                    raise DOCXAdapterError(
                        "Run and paragraph offsets are inconsistent"
                    )

                actual_slice = run.text[
                    run_slice.run_start:run_slice.run_end
                ]

                if actual_slice != run_slice.text:
                    raise DOCXAdapterError(
                        "Run content has changed"
                    )

                reconstructed.append(actual_slice)

                _validate_replaceable_run(run)

                replacement_text = (
                    replacement.token
                    if slice_index == 0
                    else ""
                )

                edits.setdefault(
                    run_slice.run_index,
                    [],
                ).append(
                    (
                        run_slice.run_start,
                        run_slice.run_end,
                        replacement_text,
                    )
                )

            if "".join(reconstructed) != replacement.original:
                raise DOCXAdapterError(
                    "Run slices do not reconstruct replacement"
                )

        paragraph_edits.append(edits)

    # -------------------------------------------------
    # 4. Apply replacements from right to left per run.
    # -------------------------------------------------

    for live_item, edits in zip(
        live_paragraphs,
        paragraph_edits,
    ):

        paragraph = live_item[0]

        run_items = list(_iter_run_items(paragraph))

        for run_index, run_edits in edits.items():

            run = run_items[run_index][0]
            new_text = run.text

            run_edits.sort(
                key=lambda item: item[0],
                reverse=True,
            )

            previous_start = len(new_text) + 1

            for start, end, replacement_text in run_edits:

                if end > previous_start:
                    raise DOCXAdapterError(
                        "Overlapping run-local edits"
                    )

                new_text = (
                    new_text[:start]
                    + replacement_text
                    + new_text[end:]
                )

                previous_start = start

            # Run formatting is preserved by python-docx.
            run.text = new_text

    # -------------------------------------------------
    # 5. Save to a temporary file.
    # -------------------------------------------------

    temporary_path = None

    try:
        with tempfile.NamedTemporaryFile(
            suffix=".docx",
            prefix=".docx-mask-",
            dir=destination.parent,
            delete=False,
        ) as temporary_file:

            temporary_path = Path(temporary_file.name)

        document.save(temporary_path)

        # -------------------------------------------------
        # 6. Reopen and verify the resulting document.
        # -------------------------------------------------

        verified = extract_docx_text(temporary_path)

        if len(verified.paragraphs) != len(plan.paragraphs):
            raise DOCXAdapterError(
                "Output paragraph count changed"
            )

        for expected, actual in zip(
            plan.paragraphs,
            verified.paragraphs,
        ):

            if (
                expected.story != actual.story
                or expected.section_index != actual.section_index
                or expected.location != actual.location
            ):
                raise DOCXAdapterError(
                    "Output paragraph structure changed"
                )

            if actual.text != expected.masked_text:
                raise DOCXAdapterError(
                    "Output verification failed: "
                    "masked paragraph does not match plan"
                )

        # Check that the source has not changed during processing.
        if hashlib.sha256(
            source_path.read_bytes()
        ).hexdigest() != plan.source_sha256:
            raise DOCXAdapterError(
                "Source changed during masking"
            )

        # -------------------------------------------------
        # 7. Publish without overwriting existing files.
        # -------------------------------------------------

        masked_bytes = temporary_path.read_bytes()

        descriptor = os.open(
            str(destination),
            os.O_CREAT | os.O_EXCL | os.O_WRONLY,
            0o666,
        )

        try:
            with os.fdopen(descriptor, "wb") as output_file:
                output_file.write(masked_bytes)

        except Exception:
            destination.unlink(missing_ok=True)
            raise

        return destination

    except DOCXAdapterError:
        raise

    except Exception as exc:
        raise DOCXAdapterError(
            "Failed to apply DOCX masking plan"
        ) from exc

    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)


def apply_docx_unmasking_plan(
    plan: DOCXUnmaskPlan,
    output_path: str | Path,
) -> Path:
    """
    Apply a previously generated unmasking plan to a DOCX file.

    The source document is never modified.

    Features:
        - SHA-256 source verification.
        - Single-run and cross-run restoration.
        - Multiple restorations per paragraph.
        - Body, tables, nested tables, headers and footers.
        - Preservation of unaffected run formatting.
        - Unknown tokens remain unchanged.
        - Output verification before publication.

    Restored text inherits the formatting of the first
    affected run.

    Hyperlink text restoration is rejected for safety.

    Returns:
        Absolute path to the restored DOCX file.
    """

    if not isinstance(plan, DOCXUnmaskPlan):
        raise TypeError("plan must be a DOCXUnmaskPlan")

    if not isinstance(output_path, (str, Path)):
        raise TypeError(
            "output_path must be a string or Path"
        )

    source_path = Path(plan.source_path).expanduser().resolve()
    destination = Path(output_path).expanduser().resolve()

    # -------------------------------------------------
    # 1. Validate paths.
    # -------------------------------------------------

    if source_path.suffix.lower() != ".docx":
        raise DOCXAdapterError(
            "Source must be a DOCX file"
        )

    if destination.suffix.lower() != ".docx":
        raise DOCXAdapterError(
            "Output must have a .docx extension"
        )

    if source_path == destination:
        raise DOCXAdapterError(
            "Source and output paths must be different"
        )

    if not source_path.is_file():
        raise FileNotFoundError(source_path)

    if not destination.parent.is_dir():
        raise DOCXAdapterError(
            "Output directory does not exist"
        )

    if destination.exists():
        raise DOCXAdapterError(
            "Output file already exists; refusing to overwrite"
        )

    if (
        not isinstance(plan.source_sha256, str)
        or len(plan.source_sha256) != 64
    ):
        raise DOCXAdapterError(
            "Invalid source fingerprint"
        )

    if not isinstance(plan.vault_id, str) or not plan.vault_id:
        raise DOCXAdapterError(
            "Invalid vault ID"
        )

    # -------------------------------------------------
    # 2. Read and verify source fingerprint.
    # -------------------------------------------------

    source_bytes = source_path.read_bytes()

    actual_fingerprint = hashlib.sha256(
        source_bytes
    ).hexdigest()

    if actual_fingerprint != plan.source_sha256:
        raise DOCXAdapterError(
            "Source document has changed since the "
            "unmasking plan was generated"
        )

    try:
        document = Document(BytesIO(source_bytes))

    except Exception as exc:
        raise DOCXAdapterError(
            "Unable to reopen source DOCX"
        ) from exc

    # -------------------------------------------------
    # 3. Resolve live paragraphs.
    # -------------------------------------------------

    live_paragraphs = list(
        _iter_document_paragraphs(document)
    )

    if len(live_paragraphs) != len(plan.paragraphs):
        raise DOCXAdapterError(
            "Paragraph count does not match unmasking plan"
        )

    paragraph_edits = []

    for expected_index, (
        paragraph_plan,
        live_item,
    ) in enumerate(
        zip(plan.paragraphs, live_paragraphs)
    ):

        paragraph, story, section_index, location = live_item

        if paragraph_plan.paragraph_index != expected_index:
            raise DOCXAdapterError(
                "Paragraph indices do not match document order"
            )

        if (
            paragraph_plan.story != story
            or paragraph_plan.section_index != section_index
            or paragraph_plan.location != location
        ):
            raise DOCXAdapterError(
                "Paragraph location does not match unmasking plan"
            )

        # -------------------------------------------------
        # 4. Reconstruct current paragraph text and run map.
        # -------------------------------------------------

        run_items = list(
            _iter_run_items(paragraph)
        )

        run_offsets = {}
        extracted_parts = []
        cursor = 0

        for run_index, (
            run,
            is_hyperlink,
            hyperlink_target,
        ) in enumerate(run_items):

            run_text = run.text

            run_offsets[run_index] = (
                cursor,
                cursor + len(run_text),
                run,
                is_hyperlink,
                hyperlink_target,
            )

            extracted_parts.append(run_text)
            cursor += len(run_text)

        current_text = "".join(extracted_parts)

        if current_text != paragraph_plan.masked_text:
            raise DOCXAdapterError(
                "Paragraph content does not match unmasking plan"
            )

        # -------------------------------------------------
        # 5. Validate all token occurrences.
        # -------------------------------------------------

        restorations = tuple(
            sorted(
                paragraph_plan.restorations,
                key=lambda item: item.start,
            )
        )

        restoration_by_span = {}

        for restoration in restorations:

            span = (
                restoration.start,
                restoration.end,
            )

            if span in restoration_by_span:
                raise DOCXAdapterError(
                    "Duplicate restoration span"
                )

            restoration_by_span[span] = restoration

        encountered_unknown = []

        for match in TOKEN_PATTERN.finditer(current_text):

            token = match.group(0)

            span = (
                match.start(),
                match.end(),
            )

            restoration = restoration_by_span.get(span)

            if restoration is not None:

                if restoration.token != token:
                    raise DOCXAdapterError(
                        "Restoration token does not match source"
                    )

            else:

                if token not in paragraph_plan.unknown_tokens:
                    raise DOCXAdapterError(
                        "Unmapped token is missing from unknown tokens"
                    )

                if token not in encountered_unknown:
                    encountered_unknown.append(token)

        if tuple(encountered_unknown) != tuple(
            paragraph_plan.unknown_tokens
        ):
            raise DOCXAdapterError(
                "Unknown token information is inconsistent"
            )

        # -------------------------------------------------
        # 6. Validate restoration offsets and map edits.
        # -------------------------------------------------

        edits = {}
        expected_output = []

        previous_end = 0

        for restoration in restorations:

            if (
                restoration.story != story
                or restoration.section_index != section_index
                or restoration.location != location
                or restoration.paragraph_index != expected_index
            ):
                raise DOCXAdapterError(
                    "Restoration location mismatch"
                )

            if (
                restoration.start < previous_end
                or restoration.start < 0
                or restoration.end > len(current_text)
                or restoration.end <= restoration.start
            ):
                raise DOCXAdapterError(
                    "Invalid or overlapping restoration offsets"
                )

            if (
                current_text[
                    restoration.start:restoration.end
                ] != restoration.token
            ):
                raise DOCXAdapterError(
                    "Restoration token does not match source text"
                )

            if not TOKEN_PATTERN.fullmatch(restoration.token):
                raise DOCXAdapterError(
                    "Invalid restoration token"
                )

            if (
                not isinstance(restoration.original, str)
                or not restoration.original
            ):
                raise DOCXAdapterError(
                    "Invalid original restoration value"
                )

            expected_output.append(
                current_text[previous_end:restoration.start]
            )

            expected_output.append(
                restoration.original
            )

            previous_end = restoration.end

            # Map the restoration to its original Word runs.
            reconstructed = []
            slice_cursor = restoration.start

            for slice_index, run_slice in enumerate(
                restoration.run_slices
            ):

                run_data = run_offsets.get(
                    run_slice.run_index
                )

                if run_data is None:
                    raise DOCXAdapterError(
                        "Restoration references an unknown run"
                    )

                (
                    run_global_start,
                    run_global_end,
                    run,
                    is_hyperlink,
                    hyperlink_target,
                ) = run_data

                if is_hyperlink or run_slice.is_hyperlink:
                    raise DOCXAdapterError(
                        "Restoration inside hyperlinks is not supported"
                    )

                if run_slice.hyperlink_target != hyperlink_target:
                    raise DOCXAdapterError(
                        "Hyperlink metadata mismatch"
                    )

                if (
                    run_slice.paragraph_start != slice_cursor
                    or run_slice.run_start < 0
                    or run_slice.run_end > len(run.text)
                    or run_slice.run_end <= run_slice.run_start
                    or run_slice.paragraph_start
                    != run_global_start + run_slice.run_start
                    or run_slice.paragraph_end
                    != run_global_start + run_slice.run_end
                ):
                    raise DOCXAdapterError(
                        "Run and paragraph offsets are inconsistent"
                    )

                actual_slice = run.text[
                    run_slice.run_start:run_slice.run_end
                ]

                if actual_slice != run_slice.text:
                    raise DOCXAdapterError(
                        "Run content has changed"
                    )

                reconstructed.append(actual_slice)

                slice_cursor = run_slice.paragraph_end

                _validate_replaceable_run(run)

                # Original value goes into the first affected run.
                # Remaining token fragments are removed.
                replacement_text = (
                    restoration.original
                    if slice_index == 0
                    else ""
                )

                edits.setdefault(
                    run_slice.run_index,
                    [],
                ).append(
                    (
                        run_slice.run_start,
                        run_slice.run_end,
                        replacement_text,
                    )
                )

            if slice_cursor != restoration.end:
                raise DOCXAdapterError(
                    "Run slices do not cover restoration"
                )

            if "".join(reconstructed) != restoration.token:
                raise DOCXAdapterError(
                    "Run slices do not reconstruct token"
                )

        expected_output.append(
            current_text[previous_end:]
        )

        expected_restored_text = "".join(
            expected_output
        )

        if expected_restored_text != paragraph_plan.restored_text:
            raise DOCXAdapterError(
                "Restored paragraph text is inconsistent"
            )

        paragraph_edits.append(edits)

    # -------------------------------------------------
    # 7. Apply restorations from right to left.
    # -------------------------------------------------

    for live_item, edits in zip(
        live_paragraphs,
        paragraph_edits,
    ):

        paragraph = live_item[0]

        run_items = list(
            _iter_run_items(paragraph)
        )

        for run_index, run_edits in edits.items():

            run = run_items[run_index][0]
            new_text = run.text

            run_edits.sort(
                key=lambda item: item[0],
                reverse=True,
            )

            previous_start = len(new_text) + 1

            for start, end, replacement_text in run_edits:

                if end > previous_start:
                    raise DOCXAdapterError(
                        "Overlapping run-local restorations"
                    )

                new_text = (
                    new_text[:start]
                    + replacement_text
                    + new_text[end:]
                )

                previous_start = start

            # Formatting of the original run is retained.
            run.text = new_text

    # -------------------------------------------------
    # 8. Save to a temporary file.
    # -------------------------------------------------

    temporary_path = None

    try:

        with tempfile.NamedTemporaryFile(
            suffix=".docx",
            prefix=".docx-unmask-",
            dir=destination.parent,
            delete=False,
        ) as temporary_file:

            temporary_path = Path(
                temporary_file.name
            )

        document.save(temporary_path)

        # -------------------------------------------------
        # 9. Reopen and verify restored document.
        # -------------------------------------------------

        verified = extract_docx_text(
            temporary_path
        )

        if len(verified.paragraphs) != len(plan.paragraphs):
            raise DOCXAdapterError(
                "Output paragraph count changed"
            )

        for expected, actual in zip(
            plan.paragraphs,
            verified.paragraphs,
        ):

            if (
                expected.story != actual.story
                or expected.section_index != actual.section_index
                or expected.location != actual.location
            ):
                raise DOCXAdapterError(
                    "Output paragraph structure changed"
                )

            if actual.text != expected.restored_text:
                raise DOCXAdapterError(
                    "Output verification failed: "
                    "restored paragraph does not match plan"
                )

        # Confirm the original source has not changed.
        if hashlib.sha256(
            source_path.read_bytes()
        ).hexdigest() != plan.source_sha256:
            raise DOCXAdapterError(
                "Source changed during unmasking"
            )

        # -------------------------------------------------
        # 10. Publish without overwriting existing files.
        # -------------------------------------------------

        restored_bytes = temporary_path.read_bytes()

        descriptor = os.open(
            str(destination),
            os.O_CREAT | os.O_EXCL | os.O_WRONLY,
            0o666,
        )

        try:

            with os.fdopen(
                descriptor,
                "wb",
            ) as output_file:

                output_file.write(restored_bytes)

        except Exception:
            destination.unlink(missing_ok=True)
            raise

        return destination

    except DOCXAdapterError:
        raise

    except Exception as exc:
        raise DOCXAdapterError(
            "Failed to apply DOCX unmasking plan"
        ) from exc

    finally:

        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)
