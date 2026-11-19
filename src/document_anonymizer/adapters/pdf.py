from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import math

from document_anonymizer.core.engine import MaskingEngine

from collections import Counter
from statistics import median

import pymupdf

import hashlib
import os
import tempfile


def _sha256_file(path: Path) -> str:
    """Calculate a file's SHA-256 fingerprint."""

    digest = hashlib.sha256()

    with path.open("rb") as file:
        for chunk in iter(
            lambda: file.read(1024 * 1024),
            b"",
        ):
            digest.update(chunk)

    return digest.hexdigest()


class PDFAdapterError(Exception):
    """Base exception for PDF adapter errors."""


class PDFExtractionError(PDFAdapterError):
    """Raised when PDF text extraction fails."""


class PDFEncryptedError(PDFAdapterError):
    """Raised when a PDF requires a password."""


@dataclass(frozen=True, slots=True)
class PDFCharacter:
    """
    Represents an extracted PDF character.

    start and end refer to character positions
    inside the corresponding PDFPageText.text.
    """

    text: str

    start: int
    end: int

    bbox: tuple[float, float, float, float]

    origin: tuple[float, float] | None

    font_name: str | None
    font_size: float | None
    font_flags: int | None
    color: int | None
    line_index: int = 0
    direction: tuple[float, float] = (1.0, 0.0)


@dataclass(frozen=True, slots=True)
class PDFPageText:
    """Extracted text and metadata for one PDF page."""

    page_number: int

    text: str

    characters: tuple[PDFCharacter, ...]

    width: float
    height: float
    rotation: int

    @property
    def has_text(self) -> bool:
        return bool(self.text.strip())

    @property
    def character_count(self) -> int:
        return len(self.text)


@dataclass(frozen=True, slots=True)
class PDFTextDocument:
    """Extracted content of an entire PDF document."""

    source_path: Path

    pages: tuple[PDFPageText, ...]

    source_sha256: str | None = None

    @property
    def page_count(self) -> int:
        return len(self.pages)

    @property
    def has_extractable_text(self) -> bool:
        return any(page.has_text for page in self.pages)

    @property
    def pages_without_text(self) -> tuple[int, ...]:
        """
        Return 1-based page numbers without extractable text.

        This allows callers to distinguish blank or
        image-only pages from pages containing text.
        """

        return tuple(
            page.page_number
            for page in self.pages
            if not page.has_text
        )

    @property
    def total_character_count(self) -> int:
        return sum(
            page.character_count
            for page in self.pages
        )


def _extract_page(
    page: pymupdf.Page,
    page_number: int,
    sort: bool,
) -> PDFPageText:
    """Extract text and character coordinates from one page."""

    try:
        raw = page.get_text(
            "rawdict",
            sort=sort,
        )

    except Exception as exc:
        raise PDFExtractionError(
            f"Failed to extract page {page_number}"
        ) from exc

    text_parts: list[str] = []
    characters: list[PDFCharacter] = []

    cursor = 0

    line_index = 0

    for block in raw.get("blocks", []):

        if block.get("type") != 0:
            continue

        for line in block.get("lines", []):

            # Collect raw character information first.
            # Do not assign offsets yet.
            line_items = []

            raw_direction = line.get("dir") or (1.0, 0.0)

            if len(raw_direction) != 2:
                raise PDFExtractionError(
                    f"Invalid text direction on page {page_number}"
                )

            line_direction = tuple(
                float(value)
                for value in raw_direction
            )

            for span in line.get("spans", []):

                font_name = span.get("font")
                font_size = span.get("size")
                font_flags = span.get("flags")
                color = span.get("color")

                for char in span.get("chars", []):

                    char_text = char.get("c", "")

                    if not char_text:
                        continue

                    bbox = char.get("bbox")

                    if not bbox or len(bbox) != 4:
                        raise PDFExtractionError(
                            f"Character without valid bounding box "
                            f"on page {page_number}"
                        )

                    origin = char.get("origin")

                    if origin is not None:
                        origin = (
                            float(origin[0]),
                            float(origin[1]),
                        )

                    line_items.append({
                        "text": char_text,
                        "bbox": tuple(
                            float(value)
                            for value in bbox
                        ),
                        "origin": origin,
                        "font_name": font_name,
                        "font_size": font_size,
                        "font_flags": font_flags,
                        "color": color,
                        "direction": line_direction,
                    })

            if not line_items:
                continue

            # IMPORTANT:
            # Insert the separator BEFORE assigning
            # character offsets for the next line.
            if text_parts:
                text_parts.append("\n")
                cursor += 1

            line_text = []

            for item in line_items:

                char_text = item["text"]

                start = cursor
                end = start + len(char_text)

                character = PDFCharacter(
                    text=char_text,
                    start=start,
                    end=end,
                    bbox=item["bbox"],
                    origin=item["origin"],
                    font_name=item["font_name"],
                    font_size=(
                        float(item["font_size"])
                        if item["font_size"] is not None
                        else None
                    ),
                    font_flags=item["font_flags"],
                    color=item["color"],
                    line_index=line_index,
                    direction=item["direction"],
                )

                characters.append(character)

                line_text.append(char_text)

                cursor = end

            text_parts.append(
                "".join(line_text)
            )
            line_index += 1

    return PDFPageText(
        page_number=page_number,
        text="".join(text_parts),
        characters=tuple(characters),
        width=float(page.rect.width),
        height=float(page.rect.height),
        rotation=page.rotation,
    )


def extract_pdf_text(
    source: str | Path,
    *,
    password: str | None = None,
    sort: bool = True,
) -> PDFTextDocument:
    """
    Extract text and character coordinates from a text-based PDF.

    Args:
        source:
            Path to the input PDF.

        password:
            Optional password for an encrypted PDF.

        sort:
            Sort text blocks into a more natural reading order.

    Returns:
        PDFTextDocument containing page-level text,
        character coordinates and font metadata.

    Notes:
        - Scanned PDFs are not OCR-processed.
        - Pages without extractable text are reported.
        - Page boundaries are preserved.
        - Character positions are local to each page's text.
    """

    if not isinstance(source, (str, Path)):
        raise TypeError(
            "PDF source must be a string or Path"
        )

    if not isinstance(sort, bool):
        raise TypeError(
            "sort must be a boolean"
        )

    if password is not None and not isinstance(password, str):
        raise TypeError(
            "PDF password must be a string or None"
        )

    source_path = Path(source).expanduser()

    if not source_path.exists():
        raise FileNotFoundError(
            f"PDF file not found: {source_path}"
        )

    if not source_path.is_file():
        raise PDFExtractionError(
            "PDF source must be a regular file"
        )


    source_sha256 = _sha256_file(source_path)
    

    document = None

    try:
        document = pymupdf.open(
            str(source_path)
        )

        if not document.is_pdf:
            raise PDFExtractionError(
                "The supplied file is not a PDF"
            )

        if document.needs_pass:

            if password is None:
                raise PDFEncryptedError(
                    "PDF requires a password"
                )

            if not document.authenticate(password):
                raise PDFEncryptedError(
                    "Incorrect PDF password"
                )

        pages = []

        for index, page in enumerate(document):

            extracted_page = _extract_page(
                page=page,
                page_number=index + 1,
                sort=sort,
            )

            pages.append(extracted_page)

        return PDFTextDocument(
            source_path=source_path.resolve(),
            pages=tuple(pages),
            source_sha256=source_sha256,
        )

    except PDFAdapterError:
        raise

    except Exception as exc:
        raise PDFExtractionError(
            f"Unable to process PDF: {source_path}"
        ) from exc

    finally:
        if document is not None:
            document.close()


@dataclass(frozen=True, slots=True)
class PDFMappedReplacement:
    """One masking occurrence mapped to PDF geometry."""

    page_number: int

    start: int
    end: int

    original: str
    token: str
    rule_id: str

    # One bounding box per extracted character.
    character_boxes: tuple[
        tuple[float, float, float, float],
        ...
    ]

    characters: tuple[PDFCharacter, ...] = ()


@dataclass(frozen=True, slots=True)
class PDFPageMaskPlan:
    """Masking plan for one PDF page."""

    page_number: int

    original_text: str
    masked_text: str

    replacements: tuple[PDFMappedReplacement, ...]

    width: float | None = None
    height: float | None = None
    rotation: int = 0


@dataclass(frozen=True, slots=True)
class PDFMaskPlan:
    """Complete masking plan for a PDF document."""

    source_path: Path
    vault_id: str

    pages: tuple[PDFPageMaskPlan, ...]

    source_sha256: str = ""

    @property
    def total_replacements(self) -> int:
        return sum(
            len(page.replacements)
            for page in self.pages
        )


class PDFCoordinateMappingError(PDFAdapterError):
    """Raised when a text match cannot be mapped safely to PDF geometry."""


def _characters_for_span(
    page: PDFPageText,
    start: int,
    end: int,
    original: str,
) -> tuple[PDFCharacter, ...]:

    if (
        type(start) is not int
        or type(end) is not int
        or start < 0
        or end <= start
        or end > len(page.text)
    ):
        raise PDFCoordinateMappingError(
            f"Invalid text span on page {page.page_number}"
        )

    if page.text[start:end] != original:
        raise PDFCoordinateMappingError(
            f"Matched text does not agree with page offsets "
            f"on page {page.page_number}"
        )

    coverage = [0] * len(original)
    selected_characters = []

    for character in page.characters:

        if (
            character.end <= start
            or character.start >= end
        ):
            continue

        if (
            character.start < start
            or character.end > end
        ):
            raise PDFCoordinateMappingError(
                f"Match partially overlaps a PDF character "
                f"on page {page.page_number}"
            )

        if (
            page.text[character.start:character.end]
            != character.text
        ):
            raise PDFCoordinateMappingError(
                f"Character offset inconsistency "
                f"on page {page.page_number}"
            )

        relative_start = character.start - start
        relative_end = character.end - start

        for index in range(relative_start, relative_end):
            coverage[index] += 1

        bbox = character.bbox

        if bbox is None or len(bbox) != 4:
            raise PDFCoordinateMappingError(
                "Missing character bounding box"
            )

        if not all(math.isfinite(value) for value in bbox):
            raise PDFCoordinateMappingError(
                "Non-finite character coordinates"
            )

        x0, y0, x1, y1 = bbox

        if x1 < x0 or y1 < y0:
            raise PDFCoordinateMappingError(
                "Invalid character bounding box"
            )

        selected_characters.append(character)

    for index, value in enumerate(original):

        count = coverage[index]

        if value == "\n":
            if count not in (0, 1):
                raise PDFCoordinateMappingError(
                    "Ambiguous newline mapping"
                )

        elif count != 1:
            raise PDFCoordinateMappingError(
                f"Missing or ambiguous character geometry "
                f"at offset {start + index}"
            )

    if not selected_characters:
        raise PDFCoordinateMappingError(
            "Matched text has no usable character geometry"
        )

    return tuple(selected_characters)


def _character_boxes_for_span(
    page: PDFPageText,
    start: int,
    end: int,
    original: str,
) -> tuple[tuple[float, float, float, float], ...]:

    return tuple(
        character.bbox
        for character in _characters_for_span(
            page,
            start,
            end,
            original,
        )
    )


def build_pdf_masking_plan(
    document: PDFTextDocument,
    engine: MaskingEngine,
) -> PDFMaskPlan:
    """
    Match PDF text and map every replacement to character geometry.

    This function does not modify the PDF.

    It uses the existing MaskingEngine and its Matcher.
    The engine's vault receives the generated token mappings.
    """

    if not isinstance(document, PDFTextDocument):
        raise TypeError(
            "document must be a PDFTextDocument"
        )

    if not isinstance(engine, MaskingEngine):
        raise TypeError(
            "engine must be a MaskingEngine"
        )

    # -------------------------------------------------
    # Phase 1: Preflight every page.
    #
    # Validate coordinate mappings before generating
    # any token mappings in the vault.
    # -------------------------------------------------

    preflight = []

    for page in document.pages:

        matches = engine.matcher.find_matches(
            page.text
        )

        for match in matches:

            _character_boxes_for_span(
                page=page,
                start=match.start,
                end=match.end,
                original=match.value,
            )

        preflight.append(
            (page, matches)
        )

    # -------------------------------------------------
    # Phase 2: Mask and build mapped replacements.
    # -------------------------------------------------

    page_plans = []

    for page, expected_matches in preflight:

        result = engine.mask_text(
            page.text
        )

        if len(result.replacements) != len(expected_matches):
            raise PDFCoordinateMappingError(
                f"Matcher results changed while processing "
                f"page {page.page_number}"
            )

        mapped_replacements = []

        for match, replacement in zip(
            expected_matches,
            result.replacements,
        ):

            # Verify that the engine's replacement metadata
            # corresponds exactly to the preflight match.
            if (
                replacement.start != match.start
                or replacement.end != match.end
                or replacement.original != match.value
                or replacement.rule_id != match.rule_id
            ):
                raise PDFCoordinateMappingError(
                    f"Replacement metadata mismatch "
                    f"on page {page.page_number}"
                )

            characters = _characters_for_span(
                page=page,
                start=replacement.start,
                end=replacement.end,
                original=replacement.original,
            )

            boxes = tuple(
                character.bbox
                for character in characters
            )

            mapped_replacements.append(
                PDFMappedReplacement(
                    page_number=page.page_number,
                    start=replacement.start,
                    end=replacement.end,
                    original=replacement.original,
                    token=replacement.token,
                    rule_id=replacement.rule_id,
                    character_boxes=boxes,
                    characters=characters,
                )
            )

        page_plans.append(
            PDFPageMaskPlan(
                page_number=page.page_number,
                original_text=page.text,
                masked_text=result.masked_text,
                replacements=tuple(mapped_replacements),
                width=page.width,
                height=page.height,
                rotation=page.rotation,
            )
        )

    return PDFMaskPlan(
        source_path=document.source_path,
        vault_id=engine.vault.vault_id,
        pages=tuple(page_plans),
        source_sha256=(
            document.source_sha256
            or _sha256_file(document.source_path)
        ),
    )


class PDFRedactionError(PDFAdapterError):
    """Raised when PDF redaction cannot be completed safely."""


class PDFSourceChangedError(PDFRedactionError):
    """Raised when the source PDF differs from the extracted PDF."""


class PDFTokenInsertionError(PDFAdapterError):
    """Raised when PDF token insertion fails safely."""


class PDFTokenFontError(PDFTokenInsertionError):
    """Raised when a font cannot render the complete token."""


def redact_pdf(
    plan: PDFMaskPlan,
    output_path: str | Path,
) -> Path:
    """
    Permanently remove matched text from a PDF.

    This step intentionally does not insert replacement tokens.
    Token insertion will be implemented separately.

    The original source PDF is never modified.

    Safety features:
        - Source SHA-256 verification.
        - Page count and text verification.
        - Character geometry verification.
        - Protection against deleting unmatched adjacent text.
        - Actual PDF text redaction.
        - Post-save text extraction verification.
        - Atomic output replacement.
    """

    if not isinstance(plan, PDFMaskPlan):
        raise TypeError("plan must be a PDFMaskPlan")

    if not isinstance(output_path, (str, Path)):
        raise TypeError("output_path must be a string or Path")

    source_path = Path(plan.source_path).resolve()
    destination = Path(output_path).expanduser().resolve()

    if destination.suffix.lower() != ".pdf":
        raise ValueError("Output must have a .pdf extension")

    if source_path == destination:
        raise ValueError("Source and output paths must differ")

    if not source_path.is_file():
        raise FileNotFoundError(source_path)

    if not plan.source_sha256:
        raise PDFSourceChangedError(
            "Masking plan has no source fingerprint"
        )

    # Verify that the original PDF has not changed.
    current_digest = _sha256_file(source_path)

    if current_digest != plan.source_sha256:
        raise PDFSourceChangedError(
            "Source PDF changed after extraction. "
            "Extract and build a new masking plan."
        )

    destination.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    temporary_path = None
    document = None

    try:

        # -------------------------------------------------
        # Phase 1: Open and validate source PDF.
        # -------------------------------------------------

        document = pymupdf.open(str(source_path))

        if not document.is_pdf:
            raise PDFRedactionError(
                "Source is not a valid PDF"
            )

        # Encrypted PDF writing is intentionally deferred.
        if document.needs_pass:
            raise PDFEncryptedError(
                "Redaction of encrypted PDFs is not "
                "supported in this implementation."
            )

        if document.page_count != len(plan.pages):
            raise PDFSourceChangedError(
                "PDF page count does not match masking plan"
            )

        # -------------------------------------------------
        # Phase 2: Preflight all pages.
        # No PDF modifications occur in this phase.
        # -------------------------------------------------

        page_redaction_boxes = []

        for index, page_plan in enumerate(plan.pages):

            page = document[index]
            page_number = index + 1

            if (
                page_plan.width is not None
                and not math.isclose(
                    page.rect.width,
                    page_plan.width,
                    abs_tol=0.01,
                )
            ):
                raise PDFSourceChangedError(
                    f"Page width changed: {page_number}"
                )

            if (
                page_plan.height is not None
                and not math.isclose(
                    page.rect.height,
                    page_plan.height,
                    abs_tol=0.01,
                )
            ):
                raise PDFSourceChangedError(
                    f"Page height changed: {page_number}"
                )

            if page.rotation != page_plan.rotation:
                raise PDFSourceChangedError(
                    f"Page rotation changed: {page_number}"
                )

            if page_plan.page_number != page_number:
                raise PDFRedactionError(
                    "Invalid page numbering in masking plan"
                )

            extracted = _extract_page(
                page=page,
                page_number=page_number,
                sort=True,
            )

            if extracted.text != page_plan.original_text:
                raise PDFSourceChangedError(
                    f"Extracted text changed on page {page_number}"
                )

            target_characters = {}

            previous_end = -1

            for replacement in page_plan.replacements:

                if replacement.page_number != page_number:
                    raise PDFRedactionError(
                        "Replacement belongs to another page"
                    )

                if (
                    replacement.start < previous_end
                    or replacement.end > len(extracted.text)
                ):
                    raise PDFRedactionError(
                        "Invalid or overlapping replacement offsets"
                    )

                previous_end = replacement.end

                if (
                    extracted.text[
                        replacement.start:replacement.end
                    ] != replacement.original
                ):
                    raise PDFRedactionError(
                        "Replacement text does not match source PDF"
                    )

                selected = []

                for character in extracted.characters:

                    if (
                        character.end <= replacement.start
                        or character.start >= replacement.end
                    ):
                        continue

                    if (
                        character.start < replacement.start
                        or character.end > replacement.end
                    ):
                        raise PDFCoordinateMappingError(
                            "Replacement partially overlaps a PDF glyph"
                        )

                    selected.append(character)

                actual_boxes = tuple(
                    character.bbox
                    for character in selected
                )

                if actual_boxes != replacement.character_boxes:
                    raise PDFCoordinateMappingError(
                        f"Character geometry changed on page {page_number}"
                    )

                if not selected:
                    raise PDFCoordinateMappingError(
                        "Replacement has no character geometry"
                    )

                for character in selected:
                    target_characters[
                        (character.start, character.end)
                    ] = character

            # -------------------------------------------------
            # Phase 3: Build and validate redaction rectangles.
            # -------------------------------------------------

            rectangles = []

            seen_boxes = set()

            for character in target_characters.values():

                rect = pymupdf.Rect(character.bbox)

                if not rect.is_valid or rect.is_empty:
                    raise PDFCoordinateMappingError(
                        f"Invalid redaction rectangle "
                        f"on page {page_number}"
                    )

                # Do not delete any unmatched character.
                for other in extracted.characters:

                    key = (other.start, other.end)

                    if key in target_characters:
                        continue

                    other_rect = pymupdf.Rect(other.bbox)

                    if rect.intersects(other_rect):
                        raise PDFCoordinateMappingError(
                            "Redaction rectangle overlaps "
                            f"unmatched text on page {page_number}. "
                            "Manual review is required."
                        )

                box_key = tuple(character.bbox)

                if box_key not in seen_boxes:
                    rectangles.append(rect)
                    seen_boxes.add(box_key)

            page_redaction_boxes.append(
                tuple(rectangles)
            )

        # -------------------------------------------------
        # Phase 4: Apply actual redactions.
        # -------------------------------------------------

        for index, rectangles in enumerate(
            page_redaction_boxes
        ):

            if not rectangles:
                continue

            page = document[index]

            for rect in rectangles:

                page.add_redact_annot(
                    rect,
                    fill=(1, 1, 1),
                    cross_out=False,
                )

            page.apply_redactions(
                images=pymupdf.PDF_REDACT_IMAGE_PIXELS,
                graphics=pymupdf.PDF_REDACT_LINE_ART_REMOVE_IF_COVERED,
                text=pymupdf.PDF_REDACT_TEXT_REMOVE,
            )

        # -------------------------------------------------
        # Phase 5: Save to a temporary PDF.
        # -------------------------------------------------

        fd, temporary_name = tempfile.mkstemp(
            prefix=f".{destination.stem}.",
            suffix=".pdf",
            dir=destination.parent,
        )

        os.close(fd)

        temporary_path = Path(temporary_name)

        # Remove the initially created empty temporary file.
        temporary_path.unlink(missing_ok=True)

        # PyMuPDF writes the new document to this path.
        document.save(
            str(temporary_path),
            garbage=4,
            deflate=True,
        )

        document.close()
        document = None

        # -------------------------------------------------
        # Phase 6: Verify the saved PDF.
        # -------------------------------------------------

        verification = pymupdf.open(
            str(temporary_path)
        )

        try:

            if verification.page_count != len(plan.pages):
                raise PDFRedactionError(
                    "Output PDF page count changed"
                )

            for index, page_plan in enumerate(plan.pages):

                verified_page = _extract_page(
                    verification[index],
                    index + 1,
                    sort=True,
                )

                for replacement in page_plan.replacements:

                    if replacement.original in verified_page.text:
                        raise PDFRedactionError(
                            f"Original text remains in output "
                            f"on page {index + 1}"
                        )

        finally:
            verification.close()

        # -------------------------------------------------
        # Phase 7: Atomically publish verified output.
        # -------------------------------------------------

        os.replace(
            temporary_path,
            destination,
        )

        temporary_path = None

        return destination

    finally:

        if document is not None:
            document.close()

        if temporary_path is not None:
            try:
                temporary_path.unlink(
                    missing_ok=True
                )
            except OSError:
                pass


def _replacement_line_geometry(replacement):

    if not replacement.characters:
        raise PDFTokenInsertionError(
            "Replacement has no character metadata"
        )

    groups = {}

    for character in replacement.characters:

        groups.setdefault(
            character.line_index,
            [],
        ).append(character)

        dx, dy = character.direction

        if (
            abs(dx - 1.0) > 0.01
            or abs(dy) > 0.01
        ):
            raise PDFTokenInsertionError(
                "Rotated or vertical text is not supported."
            )

        if character.origin is None:
            raise PDFTokenInsertionError(
                "Character baseline information is missing"
            )

    line_index, line_chars = max(
        groups.items(),
        key=lambda item: (
            max(c.bbox[2] for c in item[1])
            - min(c.bbox[0] for c in item[1])
        ),
    )

    x_start = min(
        character.origin[0]
        for character in line_chars
    )

    baseline = median(
        character.origin[1]
        for character in line_chars
    )

    line_top = min(
        character.bbox[1]
        for character in line_chars
    )

    line_bottom = max(
        character.bbox[3]
        for character in line_chars
    )

    line_height = line_bottom - line_top

    font_sizes = [
        character.font_size
        for character in line_chars
        if (
            character.font_size is not None
            and character.font_size > 0
        )
    ]

    if not font_sizes or line_height <= 0:
        raise PDFTokenInsertionError(
            "Cannot determine original text dimensions"
        )

    original_font_size = median(font_sizes)

    first_character = min(
        line_chars,
        key=lambda character: character.start,
    )

    return (
        line_index,
        line_chars,
        x_start,
        baseline,
        line_height,
        original_font_size,
        first_character.font_flags,
    )


def _resolve_token_font(token, font_path, font_flags):
    """
    Resolve a suitable font for inserting a masking token.

    Returns:
        font_name: PyMuPDF font resource name
        font_file: Custom font path or None
        font: PyMuPDF Font object
    """

    if not isinstance(token, str) or not token:
        raise PDFTokenFontError("Invalid masking token")

    # A custom font was explicitly supplied.
    if font_path is not None:

        path = Path(font_path).expanduser().resolve()

        if not path.is_file():
            raise PDFTokenFontError(
                f"Custom font file does not exist: {path}"
            )

        try:
            font = pymupdf.Font(fontfile=str(path))

        except Exception as exc:
            raise PDFTokenFontError(
                f"Could not load custom font: {path}"
            ) from exc

        missing_glyphs = [
            character
            for character in set(token)
            if font.has_glyph(ord(character)) == 0
        ]

        if missing_glyphs:
            raise PDFTokenFontError(
                f"Custom font does not support: "
                f"{''.join(missing_glyphs)}"
            )

        return "TokenFont", str(path), font

    # Without a custom font, restrict tokens to ASCII.
    if not token.isascii():
        raise PDFTokenFontError(
            "Unicode tokens require an explicit custom font"
        )

    # PyMuPDF text flags:
    # 2  = italic
    # 4  = serif
    # 8  = monospaced
    # 16 = bold

    bold = bool(font_flags & 16)
    italic = bool(font_flags & 2)
    serif = bool(font_flags & 4)
    monospaced = bool(font_flags & 8)

    if monospaced:
        family = "cour"
    elif serif:
        family = "ti"
    else:
        family = "helv"

    fonts = {
        "helv": {
            (False, False): "helv",
            (True, False): "hebo",
            (False, True): "heit",
            (True, True): "hebi",
        },
        "ti": {
            (False, False): "tiro",
            (True, False): "tibo",
            (False, True): "tiit",
            (True, True): "tibi",
        },
        "cour": {
            (False, False): "cour",
            (True, False): "cobo",
            (False, True): "coit",
            (True, True): "cobi",
        },
    }

    font_name = fonts[family][(bold, italic)]

    try:
        font = pymupdf.Font(fontname=font_name)

    except Exception as exc:
        raise PDFTokenFontError(
            f"Could not resolve built-in font: {font_name}"
        ) from exc

    return font_name, None, font


def _fit_token_font_size(
    *,
    font,
    token: str,
    available_width: float,
    available_height: float,
    original_font_size: float,
    minimum_font_size: float,
) -> float:
    """
    Calculate the largest font size that fits a token
    inside the available width and height.

    The font size never exceeds the original font size
    and never falls below the configured minimum.
    """

    if not isinstance(token, str) or not token:
        raise PDFTokenFontError("Invalid masking token")

    dimensions = {
        "available_width": available_width,
        "available_height": available_height,
        "original_font_size": original_font_size,
        "minimum_font_size": minimum_font_size,
    }

    for name, value in dimensions.items():

        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(value)
            or value <= 0
        ):
            raise PDFTokenFontError(
                f"Invalid font sizing parameter: {name}"
            )

    # Measure token width at a reference font size of 1 pt.
    try:
        width_at_one = float(
            font.text_length(
                token,
                fontsize=1.0,
            )
        )

    except Exception as exc:
        raise PDFTokenFontError(
            "Unable to measure token width"
        ) from exc

    if (
        not math.isfinite(width_at_one)
        or width_at_one <= 0
    ):
        raise PDFTokenFontError(
            "Invalid token width"
        )

    # Determine the actual vertical glyph bounds.
    glyph_bounds = []

    glyph_bbox = getattr(font, "glyph_bbox", None)

    if callable(glyph_bbox):

        try:
            for character in token:

                rect = glyph_bbox(ord(character))

                y0 = float(rect.y0)
                y1 = float(rect.y1)

                if (
                    math.isfinite(y0)
                    and math.isfinite(y1)
                    and y1 > y0
                ):
                    glyph_bounds.append((y0, y1))

        except Exception:
            glyph_bounds = []

    # Fall back to the complete font metrics if necessary.
    if glyph_bounds:

        glyph_height_at_one = (
            max(y1 for _, y1 in glyph_bounds)
            - min(y0 for y0, _ in glyph_bounds)
        )

    else:

        try:
            glyph_height_at_one = (
                float(font.ascender)
                - float(font.descender)
            )

        except Exception as exc:
            raise PDFTokenFontError(
                "Unable to measure token height"
            ) from exc

    if (
        not math.isfinite(glyph_height_at_one)
        or glyph_height_at_one <= 0
    ):
        raise PDFTokenFontError(
            "Invalid token height"
        )

    # Calculate the maximum size allowed by each dimension.
    width_limited_size = (
        available_width / width_at_one
    )

    height_limited_size = (
        available_height / glyph_height_at_one
    )

    fitted_size = min(
        float(original_font_size),
        width_limited_size,
        height_limited_size,
    )

    # Never silently insert an unreadably small token.
    if fitted_size < minimum_font_size:
        raise PDFTokenFontError(
            f"Token cannot fit within the available region "
            f"at the minimum font size of "
            f"{minimum_font_size:g} pt"
        )

    return float(fitted_size)


def insert_pdf_tokens(
    redacted_pdf_path: str | Path,
    plan: PDFMaskPlan,
    output_path: str | Path,
    *,
    font_path: str | Path | None = None,
    min_font_size: float = 4.0,
) -> Path:
    """
    Insert masking tokens into an already-redacted PDF.

    The source PDF and redacted input are never modified.

    Tokens are inserted once per replacement occurrence.
    Multiline matches receive one token on their widest line.

    Unicode tokens require an explicit font file.
    """

    if not isinstance(plan, PDFMaskPlan):
        raise TypeError("plan must be a PDFMaskPlan")

    if not isinstance(redacted_pdf_path, (str, Path)):
        raise TypeError(
            "redacted_pdf_path must be a string or Path"
        )

    if not isinstance(output_path, (str, Path)):
        raise TypeError(
            "output_path must be a string or Path"
        )

    if (
        isinstance(min_font_size, bool)
        or not isinstance(min_font_size, (int, float))
        or not math.isfinite(min_font_size)
        or min_font_size <= 0
    ):
        raise ValueError("Invalid minimum font size")

    if font_path is not None and not isinstance(
        font_path, (str, Path)
    ):
        raise TypeError("font_path must be a string or Path")

    source_path = Path(plan.source_path).resolve()

    redacted_path = (
        Path(redacted_pdf_path)
        .expanduser()
        .resolve()
    )

    destination = (
        Path(output_path)
        .expanduser()
        .resolve()
    )

    if destination.suffix.lower() != ".pdf":
        raise ValueError("Output must have a .pdf extension")

    if redacted_path == source_path:
        raise ValueError(
            "Redacted input must differ from the original PDF"
        )

    if destination in (source_path, redacted_path):
        raise ValueError(
            "Output must differ from both input files"
        )

    if not source_path.is_file():
        raise FileNotFoundError(source_path)

    if not redacted_path.is_file():
        raise FileNotFoundError(redacted_path)

    if not plan.source_sha256:
        raise PDFSourceChangedError(
            "Masking plan has no source fingerprint"
        )

    if _sha256_file(source_path) != plan.source_sha256:
        raise PDFSourceChangedError(
            "Original source PDF has changed"
        )

    if (
        plan.total_replacements
        and _sha256_file(redacted_path) == plan.source_sha256
    ):
        raise PDFTokenInsertionError(
            "The supplied PDF does not appear to be redacted"
        )

    destination.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    document = None
    temporary_path = None

    try:

        # ---------------------------------------------
        # Phase 1: Open and validate redacted PDF.
        # ---------------------------------------------

        document = pymupdf.open(
            str(redacted_path)
        )

        if not document.is_pdf:
            raise PDFTokenInsertionError(
                "Input is not a valid PDF"
            )

        if document.needs_pass:
            raise PDFEncryptedError(
                "Encrypted PDF token insertion is unsupported"
            )

        if document.page_count != len(plan.pages):
            raise PDFSourceChangedError(
                "Page count does not match masking plan"
            )

        placements = []

        # ---------------------------------------------
        # Phase 2: Preflight all replacements.
        # ---------------------------------------------

        for index, page_plan in enumerate(plan.pages):

            page = document[index]
            page_number = index + 1

            if page.rotation != 0 or page_plan.rotation != 0:
                raise PDFTokenInsertionError(
                    f"Rotated pages are not supported: "
                    f"page {page_number}"
                )

            if (
                page_plan.width is not None
                and not math.isclose(
                    page.rect.width,
                    page_plan.width,
                    abs_tol=0.01,
                )
            ):
                raise PDFSourceChangedError(
                    f"Page width changed: {page_number}"
                )

            if (
                page_plan.height is not None
                and not math.isclose(
                    page.rect.height,
                    page_plan.height,
                    abs_tol=0.01,
                )
            ):
                raise PDFSourceChangedError(
                    f"Page height changed: {page_number}"
                )

            extracted = _extract_page(
                page,
                page_number,
                sort=True,
            )

            for replacement in page_plan.replacements:

                if (
                    page_plan.original_text[
                        replacement.start:replacement.end
                    ] != replacement.original
                ):
                    raise PDFTokenInsertionError(
                        "Replacement offsets do not match plan"
                    )

                # Verify that redaction really removed
                # the original value.
                for segment in replacement.original.splitlines():

                    if segment and segment in extracted.text:
                        raise PDFTokenInsertionError(
                            f"Original text remains on page "
                            f"{page_number}"
                        )

                if replacement.token in extracted.text:
                    raise PDFTokenInsertionError(
                        f"Token already exists on page {page_number}"
                    )

                if not replacement.characters:
                    raise PDFTokenInsertionError(
                        "Character metadata is missing"
                    )

                actual_boxes = tuple(
                    character.bbox
                    for character in replacement.characters
                )

                if actual_boxes != replacement.character_boxes:
                    raise PDFCoordinateMappingError(
                        "Character geometry does not match masking plan"
                    )

                reconstructed_original = "".join(
                    character.text
                    for character in replacement.characters
                )

                if reconstructed_original != (
                    replacement.original.replace("\n", "")
                ):
                    raise PDFCoordinateMappingError(
                        "Character metadata does not reconstruct "
                        "the original matched value"
                    )

                (
                    line_index,
                    line_chars,
                    x_start,
                    baseline,
                    line_height,
                    original_font_size,
                    font_flags,
                ) = _replacement_line_geometry(
                    replacement
                )

                # Stay within the original matched region.
                region_right = max(
                    character.bbox[2]
                    for character in line_chars
                )

                # Ensure no other planned replacement
                # on the same line is overlapped.
                for other in page_plan.replacements:

                    if other is replacement:
                        continue

                    for character in other.characters:

                        if character.line_index != line_index:
                            continue

                        if character.origin is None:
                            continue

                        if character.origin[0] > x_start + 0.1:
                            region_right = min(
                                region_right,
                                character.bbox[0] - 0.5,
                            )

                available_width = (
                    region_right - x_start - 0.5
                )

                if available_width <= 0:
                    raise PDFTokenInsertionError(
                        f"No safe insertion width on page {page_number}"
                    )

                font_name, font_file, font = _resolve_token_font(
                    replacement.token,
                    font_path,
                    font_flags,
                )

                font_size = _fit_token_font_size(
                    font=font,
                    token=replacement.token,
                    available_width=available_width,
                    available_height=line_height,
                    original_font_size=original_font_size,
                    minimum_font_size=float(min_font_size),
                )

                placements.append({
                    "page_index": index,
                    "point": (x_start, baseline),
                    "token": replacement.token,
                    "font_name": font_name,
                    "font_file": font_file,
                    "font_size": font_size,
                })

        # ---------------------------------------------
        # Phase 3: Insert tokens.
        # ---------------------------------------------

        for placement in placements:

            page = document[
                placement["page_index"]
            ]

            try:
                written = page.insert_text(
                    placement["point"],
                    placement["token"],
                    fontname=placement["font_name"],
                    fontfile=placement["font_file"],
                    fontsize=placement["font_size"],
                    color=(0, 0, 0),
                    overlay=True,
                )

            except Exception as exc:
                raise PDFTokenInsertionError(
                    "Unable to insert masking token"
                ) from exc

            if written != 1:
                raise PDFTokenInsertionError(
                    "Token was not inserted as one complete line"
                )

        # ---------------------------------------------
        # Phase 4: Save temporary output.
        # ---------------------------------------------

        fd, temporary_name = tempfile.mkstemp(
            prefix=f".{destination.stem}.",
            suffix=".pdf",
            dir=destination.parent,
        )

        os.close(fd)

        temporary_path = Path(temporary_name)
        temporary_path.unlink(missing_ok=True)

        document.save(
            str(temporary_path),
            garbage=4,
            deflate=True,
        )

        document.close()
        document = None

        # ---------------------------------------------
        # Phase 5: Verify final PDF.
        # ---------------------------------------------

        verification = pymupdf.open(
            str(temporary_path)
        )

        try:

            if verification.page_count != len(plan.pages):
                raise PDFTokenInsertionError(
                    "Output page count changed"
                )

            for index, page_plan in enumerate(plan.pages):

                verified_page = _extract_page(
                    verification[index],
                    index + 1,
                    sort=True,
                )

                expected_tokens = Counter(
                    replacement.token
                    for replacement in page_plan.replacements
                )

                for token, expected_count in expected_tokens.items():

                    actual_count = verified_page.text.count(token)

                    if actual_count < expected_count:
                        raise PDFTokenInsertionError(
                            f"Token verification failed on page "
                            f"{index + 1}: {token}"
                        )

                for replacement in page_plan.replacements:

                    for segment in replacement.original.splitlines():

                        if segment and segment in verified_page.text:
                            raise PDFTokenInsertionError(
                                f"Original value remains in final PDF "
                                f"on page {index + 1}"
                            )

        finally:
            verification.close()

        # ---------------------------------------------
        # Phase 6: Publish verified output atomically.
        # ---------------------------------------------

        os.replace(
            temporary_path,
            destination,
        )

        temporary_path = None

        return destination

    finally:

        if document is not None:
            document.close()

        if temporary_path is not None:
            try:
                temporary_path.unlink(missing_ok=True)
            except OSError:
                pass


class PDFRestorationError(PDFAdapterError):
    """Raised when a masked PDF cannot be safely restored."""


def restore_pdf(
    masked_pdf_path: str | Path,
    plan: PDFMaskPlan,
    output_path: str | Path,
) -> Path:
    """Restore sensitive PDF regions in a masked PDF.

    The original PDF must remain available at ``plan.source_path``.
    Restoration verifies the masked tokens, removes those tokens, and
    overlays the corresponding original page regions from the preserved
    source PDF. This is necessary because PDF masking uses permanent
    redaction; the original glyphs and their exact appearance cannot be
    reconstructed from the token text alone.

    Neither input PDF is modified. The restored output is written to a
    new path and verified before it is published.
    """

    if not isinstance(plan, PDFMaskPlan):
        raise TypeError("plan must be a PDFMaskPlan")

    if not isinstance(masked_pdf_path, (str, Path)):
        raise TypeError("masked_pdf_path must be a string or Path")

    if not isinstance(output_path, (str, Path)):
        raise TypeError("output_path must be a string or Path")

    source_path = Path(plan.source_path).expanduser().resolve()
    masked_path = Path(masked_pdf_path).expanduser().resolve()
    destination = Path(output_path).expanduser().resolve()

    if masked_path.suffix.lower() != ".pdf":
        raise ValueError("Masked input must have a .pdf extension")

    if destination.suffix.lower() != ".pdf":
        raise ValueError("Output must have a .pdf extension")

    if len({source_path, masked_path, destination}) != 3:
        raise PDFRestorationError(
            "Original, masked, and restored paths must be different"
        )

    if not source_path.is_file():
        raise FileNotFoundError(source_path)

    if not masked_path.is_file():
        raise FileNotFoundError(masked_path)

    if destination.exists() or destination.is_symlink():
        raise FileExistsError(destination)

    if not plan.source_sha256:
        raise PDFRestorationError(
            "Masking plan has no original source fingerprint"
        )

    if _sha256_file(source_path) != plan.source_sha256:
        raise PDFSourceChangedError(
            "Original source PDF changed after the masking plan was built"
        )

    if plan.total_replacements and _sha256_file(masked_path) == plan.source_sha256:
        raise PDFRestorationError(
            "The supplied masked PDF is identical to the original source"
        )

    destination.parent.mkdir(parents=True, exist_ok=True)

    original_document = None
    masked_document = None
    temporary_path = None

    try:
        original_document = pymupdf.open(str(source_path))
        masked_document = pymupdf.open(str(masked_path))

        if not original_document.is_pdf or not masked_document.is_pdf:
            raise PDFRestorationError("Both inputs must be valid PDF files")

        if original_document.needs_pass or masked_document.needs_pass:
            raise PDFEncryptedError(
                "Restoration of password-protected PDFs is unsupported"
            )

        if (
            original_document.page_count != len(plan.pages)
            or masked_document.page_count != len(plan.pages)
        ):
            raise PDFRestorationError(
                "PDF page count does not match the masking plan"
            )

        page_restorations = []

        # Preflight every page before changing the masked document.
        for index, page_plan in enumerate(plan.pages):
            original_page = original_document[index]
            masked_page = masked_document[index]
            page_number = index + 1

            if page_plan.page_number != page_number:
                raise PDFRestorationError("Invalid page numbering in masking plan")

            if original_page.rotation != 0 or masked_page.rotation != 0:
                raise PDFRestorationError(
                    f"Rotated pages are not supported: page {page_number}"
                )

            if (
                page_plan.width is not None
                and (
                    not math.isclose(
                        original_page.rect.width, page_plan.width, abs_tol=0.01
                    )
                    or not math.isclose(
                        masked_page.rect.width, page_plan.width, abs_tol=0.01
                    )
                )
            ):
                raise PDFSourceChangedError(
                    f"Original page width changed: {page_number}"
                )

            if (
                page_plan.height is not None
                and (
                    not math.isclose(
                        original_page.rect.height, page_plan.height, abs_tol=0.01
                    )
                    or not math.isclose(
                        masked_page.rect.height, page_plan.height, abs_tol=0.01
                    )
                )
            ):
                raise PDFSourceChangedError(
                    f"Original page height changed: {page_number}"
                )

            original_extracted = _extract_page(
                original_page, page_number, sort=True
            )
            masked_extracted = _extract_page(
                masked_page, page_number, sort=True
            )

            if original_extracted.text != page_plan.original_text:
                raise PDFSourceChangedError(
                    f"Original page text differs from the masking plan: {page_number}"
                )

            expected_tokens = Counter(
                replacement.token for replacement in page_plan.replacements
            )
            token_rectangles = {}

            for token, expected_count in expected_tokens.items():
                if masked_extracted.text.count(token) != expected_count:
                    raise PDFRestorationError(
                        f"Expected {expected_count} occurrences of {token} "
                        f"on page {page_number}, found "
                        f"{masked_extracted.text.count(token)}"
                    )

                rectangles = masked_page.search_for(token)
                if len(rectangles) != expected_count:
                    raise PDFRestorationError(
                        f"Could not map every occurrence of {token} "
                        f"to page geometry on page {page_number}"
                    )
                token_rectangles[token] = list(rectangles)

            assigned = Counter()
            restorations = []

            for replacement in page_plan.replacements:
                if replacement.page_number != page_number:
                    raise PDFRestorationError(
                        "A replacement belongs to a different page"
                    )

                if (
                    replacement.start < 0
                    or replacement.end > len(page_plan.original_text)
                    or page_plan.original_text[
                        replacement.start:replacement.end
                    ] != replacement.original
                ):
                    raise PDFRestorationError(
                        "Replacement offsets do not match original page text"
                    )

                if not replacement.characters:
                    raise PDFRestorationError(
                        "Original character geometry is missing"
                    )

                reconstructed = "".join(
                    character.text for character in replacement.characters
                )
                if reconstructed != replacement.original.replace("\n", ""):
                    raise PDFRestorationError(
                        "Character metadata does not reconstruct the original value"
                    )

                actual_boxes = tuple(
                    character.bbox for character in replacement.characters
                )
                if actual_boxes != replacement.character_boxes:
                    raise PDFRestorationError(
                        "Original character geometry does not match the masking plan"
                    )

                for segment in replacement.original.splitlines():
                    if segment and segment in masked_extracted.text:
                        raise PDFRestorationError(
                            f"Original text remains in masked PDF on page {page_number}"
                        )

                occurrence_index = assigned[replacement.token]
                rectangles = token_rectangles[replacement.token]
                if occurrence_index >= len(rectangles):
                    raise PDFRestorationError(
                        "A masking token occurrence could not be assigned"
                    )
                token_rect = rectangles[occurrence_index]
                assigned[replacement.token] += 1

                x0 = min(box[0] for box in replacement.character_boxes)
                y0 = min(box[1] for box in replacement.character_boxes)
                x1 = max(box[2] for box in replacement.character_boxes)
                y1 = max(box[3] for box in replacement.character_boxes)
                region = pymupdf.Rect(x0, y0, x1, y1) | token_rect
                region &= original_page.rect

                if region.is_empty or not region.is_valid:
                    raise PDFRestorationError(
                        "Restoration region is invalid or outside the page"
                    )

                restorations.append((token_rect, region))

            page_restorations.append(restorations)

        # All validation has passed. Remove tokens, then restore exact
        # source-page regions at their original coordinates.
        for index, restorations in enumerate(page_restorations):
            if not restorations:
                continue

            masked_page = masked_document[index]
            original_page = original_document[index]

            for token_rect, _region in restorations:
                masked_page.add_redact_annot(
                    token_rect,
                    fill=(1, 1, 1),
                    cross_out=False,
                )

            masked_page.apply_redactions(
                images=0,
                graphics=0,
                text=pymupdf.PDF_REDACT_TEXT_REMOVE,
            )

            for _token_rect, region in restorations:
                masked_page.show_pdf_page(
                    region,
                    original_document,
                    index,
                    clip=region,
                    overlay=True,
                )

        fd, temporary_name = tempfile.mkstemp(
            prefix=f".{destination.stem}.",
            suffix=".pdf",
            dir=destination.parent,
        )
        os.close(fd)
        temporary_path = Path(temporary_name)
        temporary_path.unlink(missing_ok=True)

        masked_document.save(
            str(temporary_path),
            garbage=4,
            deflate=True,
        )

        masked_document.close()
        masked_document = None

        # Verify that original values have returned and generated tokens
        # have disappeared before publishing the restored file.
        verification = pymupdf.open(str(temporary_path))
        try:
            if verification.page_count != len(plan.pages):
                raise PDFRestorationError("Restored PDF page count changed")

            for index, page_plan in enumerate(plan.pages):
                restored_page = _extract_page(
                    verification[index], index + 1, sort=True
                )
                for replacement in page_plan.replacements:
                    if replacement.token in restored_page.text:
                        raise PDFRestorationError(
                            f"Masking token remains after restoration on page {index + 1}"
                        )
                    if replacement.original not in restored_page.text:
                        raise PDFRestorationError(
                            f"Original value was not restored on page {index + 1}: "
                            f"{replacement.original!r}"
                        )
        finally:
            verification.close()

        os.replace(temporary_path, destination)
        temporary_path = None
        return destination

    except PDFAdapterError:
        raise
    except Exception as exc:
        raise PDFRestorationError("Failed to restore masked PDF") from exc
    finally:
        if original_document is not None:
            original_document.close()
        if masked_document is not None:
            masked_document.close()
        if temporary_path is not None:
            try:
                temporary_path.unlink(missing_ok=True)
            except OSError:
                pass
