from __future__ import annotations

from dataclasses import dataclass
from io import BytesIO
from pathlib import Path
import hashlib
import os
import shutil
import tempfile
from copy import deepcopy

from openpyxl import load_workbook
from openpyxl.utils.exceptions import InvalidFileException

from ..core.models import MaskReplacement, MaskResult

from collections.abc import Mapping

from ..core.models import RestoreResult
from ..core.tokens import TOKEN_PATTERN


class XLSXAdapterError(Exception):
    """Base exception for XLSX adapter errors."""


class XLSXExtractionError(XLSXAdapterError):
    """Raised when XLSX extraction fails."""


@dataclass(frozen=True, slots=True)
class XLSXCellText:
    """Extracted string value and location of one Excel cell."""

    worksheet_index: int
    worksheet_title: str
    coordinate: str

    row: int
    column: int

    value: str

    data_type: str
    style_id: int
    number_format: str

    has_hyperlink: bool = False
    hyperlink_target: str | None = None

    def __post_init__(self):

        if self.worksheet_index < 0:
            raise ValueError("Invalid worksheet index")

        if not self.worksheet_title:
            raise ValueError("Worksheet title cannot be empty")

        if not self.coordinate:
            raise ValueError("Cell coordinate cannot be empty")

        if self.row < 1 or self.column < 1:
            raise ValueError("Invalid cell position")

        if not isinstance(self.value, str) or not self.value:
            raise ValueError("Cell value must be a non-empty string")


@dataclass(frozen=True, slots=True)
class XLSXFormulaCell:
    """Formula cell recorded but never treated as ordinary text."""

    worksheet_index: int
    worksheet_title: str
    coordinate: str
    formula: str

    def __post_init__(self):

        if self.worksheet_index < 0:
            raise ValueError("Invalid worksheet index")

        if not self.coordinate:
            raise ValueError("Invalid cell coordinate")

        if not isinstance(self.formula, str) or not self.formula.startswith("="):
            raise ValueError("Invalid formula")


@dataclass(frozen=True, slots=True)
class XLSXSkippedText:
    """Text-bearing content intentionally excluded from masking."""

    worksheet_index: int
    worksheet_title: str
    coordinate: str | None
    kind: str
    text: str

    def __post_init__(self):

        if self.worksheet_index < 0:
            raise ValueError("Invalid worksheet index")

        if not self.kind:
            raise ValueError("Skipped element kind cannot be empty")

        if not isinstance(self.text, str) or not self.text:
            raise ValueError("Skipped text cannot be empty")


@dataclass(frozen=True, slots=True)
class XLSXWorksheetText:
    """Extracted information from one worksheet."""

    worksheet_index: int
    title: str
    state: str

    max_row: int
    max_column: int

    merged_ranges: tuple[str, ...]

    cells: tuple[XLSXCellText, ...]
    formula_cells: tuple[XLSXFormulaCell, ...] = ()
    skipped_text_elements: tuple[XLSXSkippedText, ...] = ()

    def __post_init__(self):

        object.__setattr__(
            self,
            "merged_ranges",
            tuple(self.merged_ranges),
        )

        object.__setattr__(
            self,
            "cells",
            tuple(self.cells),
        )

        object.__setattr__(
            self,
            "formula_cells",
            tuple(self.formula_cells),
        )

        object.__setattr__(
            self,
            "skipped_text_elements",
            tuple(self.skipped_text_elements),
        )

    @property
    def text_cell_count(self) -> int:
        return len(self.cells)

    @property
    def formula_count(self) -> int:
        return len(self.formula_cells)

    @property
    def text(self) -> str:
        return "\n".join(
            cell.value for cell in self.cells
        )


@dataclass(frozen=True, slots=True)
class XLSXTextDocument:
    """Extracted text and metadata from a complete XLSX workbook."""

    source_path: Path
    source_sha256: str

    worksheets: tuple[XLSXWorksheetText, ...]

    def __post_init__(self):

        object.__setattr__(
            self,
            "worksheets",
            tuple(self.worksheets),
        )

    @property
    def worksheet_count(self) -> int:
        return len(self.worksheets)

    @property
    def text_cell_count(self) -> int:
        return sum(
            worksheet.text_cell_count
            for worksheet in self.worksheets
        )

    @property
    def formula_count(self) -> int:
        return sum(
            worksheet.formula_count
            for worksheet in self.worksheets
        )

    @property
    def total_character_count(self) -> int:
        return sum(
            len(cell.value)
            for worksheet in self.worksheets
            for cell in worksheet.cells
        )

    @property
    def skipped_text_elements(self) -> tuple[XLSXSkippedText, ...]:
        return tuple(
            item
            for worksheet in self.worksheets
            for item in worksheet.skipped_text_elements
        )

    @property
    def text(self) -> str:
        """Return extracted cell text with worksheet/cell locations."""

        return "\n".join(
            f"[{cell.worksheet_title}!{cell.coordinate}] {cell.value}"
            for worksheet in self.worksheets
            for cell in worksheet.cells
        )


def _extract_worksheet(
    worksheet,
    worksheet_index: int,
) -> XLSXWorksheetText:

    cells = []
    formulas = []
    skipped = []

    for row in worksheet.iter_rows():

        for cell in row:

            value = cell.value

            # -----------------------------------------
            # Formula cells are recorded, not masked.
            # -----------------------------------------

            if cell.data_type == "f":

                if isinstance(value, str):

                    formulas.append(
                        XLSXFormulaCell(
                            worksheet_index=worksheet_index,
                            worksheet_title=worksheet.title,
                            coordinate=cell.coordinate,
                            formula=value,
                        )
                    )

                    skipped.append(
                        XLSXSkippedText(
                            worksheet_index=worksheet_index,
                            worksheet_title=worksheet.title,
                            coordinate=cell.coordinate,
                            kind="formula",
                            text=value,
                        )
                    )

            # -----------------------------------------
            # Ordinary string-valued cells.
            # -----------------------------------------

            elif isinstance(value, str) and value:

                hyperlink = getattr(cell, "hyperlink", None)

                if hyperlink is not None:

                    target = (
                        hyperlink
                        if isinstance(hyperlink, str)
                        else hyperlink.target
                    )

                    if target:

                        skipped.append(
                            XLSXSkippedText(
                                worksheet_index=worksheet_index,
                                worksheet_title=worksheet.title,
                                coordinate=cell.coordinate,
                                kind="hyperlink_target",
                                text=target,
                            )
                        )

                cells.append(
                    XLSXCellText(
                        worksheet_index=worksheet_index,
                        worksheet_title=worksheet.title,
                        coordinate=cell.coordinate,
                        row=cell.row,
                        column=cell.column,
                        value=value,
                        data_type=cell.data_type,
                        style_id=cell.style_id,
                        number_format=cell.number_format,
                        has_hyperlink=hyperlink is not None,
                        hyperlink_target=(
                            target if hyperlink is not None else None
                        ),
                    )
                )

            # -----------------------------------------
            # Comments are not modified in this stage.
            # Report them instead of silently ignoring.
            # -----------------------------------------

            comment = getattr(cell, "comment", None)

            if comment is not None:

                comment_text = comment.text

                if comment_text:

                    skipped.append(
                        XLSXSkippedText(
                            worksheet_index=worksheet_index,
                            worksheet_title=worksheet.title,
                            coordinate=cell.coordinate,
                            kind="comment",
                            text=comment_text,
                        )
                    )

    return XLSXWorksheetText(
        worksheet_index=worksheet_index,
        title=worksheet.title,
        state=worksheet.sheet_state,
        max_row=worksheet.max_row,
        max_column=worksheet.max_column,
        merged_ranges=tuple(
            str(item)
            for item in worksheet.merged_cells.ranges
        ),
        cells=tuple(cells),
        formula_cells=tuple(formulas),
        skipped_text_elements=tuple(skipped),
    )


def extract_xlsx_text(
    source: str | Path,
) -> XLSXTextDocument:
    """
    Extract string-valued cells from an XLSX workbook.

    The original workbook is never modified.

    Supported:
        - Multiple worksheets
        - Visible and hidden worksheets
        - String-valued cells
        - Cell coordinates
        - Merged range metadata
        - Formula metadata
        - Hyperlink display text

    Formulas, comments and hyperlink targets are reported
    as skipped text-bearing elements.

    Numbers, dates, booleans and empty cells are ignored.
    """

    if not isinstance(source, (str, Path)):
        raise TypeError(
            "Source must be a string or Path"
        )

    source_path = Path(source).expanduser().resolve()

    if source_path.suffix.lower() != ".xlsx":
        raise ValueError(
            "Source must have a .xlsx extension"
        )

    if not source_path.exists():
        raise FileNotFoundError(source_path)

    if not source_path.is_file():
        raise XLSXExtractionError(
            "Source path must point to a file"
        )

    try:

        source_bytes = source_path.read_bytes()

        source_sha256 = hashlib.sha256(
            source_bytes
        ).hexdigest()

        workbook = load_workbook(
            BytesIO(source_bytes),
            read_only=False,
            data_only=False,
            keep_links=True,
        )

    except (
        InvalidFileException,
        OSError,
        ValueError,
        KeyError,
    ) as exc:

        raise XLSXExtractionError(
            f"Unable to open XLSX file: {source_path}"
        ) from exc

    except Exception as exc:

        raise XLSXExtractionError(
            "Failed while loading XLSX workbook"
        ) from exc

    try:

        worksheets = tuple(
            _extract_worksheet(
                worksheet=worksheet,
                worksheet_index=index,
            )
            for index, worksheet in enumerate(
                workbook.worksheets
            )
        )

        return XLSXTextDocument(
            source_path=source_path,
            source_sha256=source_sha256,
            worksheets=worksheets,
        )

    except XLSXAdapterError:
        raise

    except Exception as exc:

        raise XLSXExtractionError(
            "Failed while extracting XLSX content"
        ) from exc

    finally:

        workbook.close()



@dataclass(frozen=True, slots=True)
class XLSXCellMaskPlan:
    """Masking plan for one Excel cell."""

    source_cell: XLSXCellText
    masked_value: str

    replacements: tuple[MaskReplacement, ...] = ()

    def __post_init__(self):

        if not isinstance(self.source_cell, XLSXCellText):
            raise TypeError("source_cell must be an XLSXCellText")

        if not isinstance(self.masked_value, str):
            raise TypeError("masked_value must be a string")

        object.__setattr__(
            self,
            "replacements",
            tuple(self.replacements),
        )

        cursor = 0
        output = []

        for replacement in self.replacements:

            if not isinstance(replacement, MaskReplacement):
                raise TypeError("Invalid replacement type")

            if replacement.start < cursor:
                raise ValueError("Overlapping replacements")

            if replacement.end > len(self.source_cell.value):
                raise ValueError("Replacement exceeds cell text")

            if (
                self.source_cell.value[
                    replacement.start:replacement.end
                ] != replacement.original
            ):
                raise ValueError(
                    "Replacement does not match original cell text"
                )

            output.append(
                self.source_cell.value[cursor:replacement.start]
            )

            output.append(replacement.token)

            cursor = replacement.end

        output.append(self.source_cell.value[cursor:])

        if "".join(output) != self.masked_value:
            raise ValueError(
                "Masked cell value is inconsistent"
            )

    @property
    def original_value(self) -> str:
        return self.source_cell.value

    @property
    def worksheet_index(self) -> int:
        return self.source_cell.worksheet_index

    @property
    def worksheet_title(self) -> str:
        return self.source_cell.worksheet_title

    @property
    def coordinate(self) -> str:
        return self.source_cell.coordinate

    @property
    def is_masked(self) -> bool:
        return self.original_value != self.masked_value


@dataclass(frozen=True, slots=True)
class XLSXWorksheetMaskPlan:
    """Masking plan for one worksheet."""

    source_worksheet: XLSXWorksheetText

    cells: tuple[XLSXCellMaskPlan, ...]

    def __post_init__(self):

        if not isinstance(
            self.source_worksheet,
            XLSXWorksheetText,
        ):
            raise TypeError(
                "source_worksheet must be an XLSXWorksheetText"
            )

        object.__setattr__(
            self,
            "cells",
            tuple(self.cells),
        )

        if len(self.cells) != len(self.source_worksheet.cells):
            raise ValueError(
                "Cell count does not match source worksheet"
            )

        for source_cell, cell_plan in zip(
            self.source_worksheet.cells,
            self.cells,
        ):

            if not isinstance(cell_plan, XLSXCellMaskPlan):
                raise TypeError("Invalid cell masking plan")

            if cell_plan.source_cell != source_cell:
                raise ValueError(
                    "Cell plan does not match source worksheet"
                )

    @property
    def worksheet_index(self) -> int:
        return self.source_worksheet.worksheet_index

    @property
    def title(self) -> str:
        return self.source_worksheet.title

    @property
    def replacements_count(self) -> int:
        return sum(
            len(cell.replacements)
            for cell in self.cells
        )

    @property
    def masked_cell_count(self) -> int:
        return sum(
            cell.is_masked
            for cell in self.cells
        )


@dataclass(frozen=True, slots=True)
class XLSXMaskPlan:
    """Complete masking plan for one XLSX workbook."""

    source_path: Path
    source_sha256: str
    vault_id: str

    worksheets: tuple[XLSXWorksheetMaskPlan, ...]

    def __post_init__(self):

        object.__setattr__(
            self,
            "worksheets",
            tuple(self.worksheets),
        )

        if not self.source_sha256:
            raise ValueError("Missing source fingerprint")

        if not self.vault_id:
            raise ValueError("Missing vault ID")

        indices = [
            worksheet.worksheet_index
            for worksheet in self.worksheets
        ]

        if len(indices) != len(set(indices)):
            raise ValueError("Duplicate worksheet indices")

    @property
    def worksheet_count(self) -> int:
        return len(self.worksheets)

    @property
    def cell_count(self) -> int:
        return sum(
            len(worksheet.cells)
            for worksheet in self.worksheets
        )

    @property
    def replacements_count(self) -> int:
        return sum(
            worksheet.replacements_count
            for worksheet in self.worksheets
        )

    @property
    def masked_cell_count(self) -> int:
        return sum(
            worksheet.masked_cell_count
            for worksheet in self.worksheets
        )

    @property
    def unique_tokens(self) -> tuple[str, ...]:

        return tuple(
            dict.fromkeys(
                replacement.token
                for worksheet in self.worksheets
                for cell in worksheet.cells
                for replacement in cell.replacements
            )
        )

    @property
    def original_text(self) -> str:

        return "\n".join(
            f"[{cell.worksheet_title}!{cell.coordinate}] "
            f"{cell.original_value}"
            for worksheet in self.worksheets
            for cell in worksheet.cells
        )

    @property
    def masked_text(self) -> str:

        return "\n".join(
            f"[{cell.worksheet_title}!{cell.coordinate}] "
            f"{cell.masked_value}"
            for worksheet in self.worksheets
            for cell in worksheet.cells
        )


def build_xlsx_masking_plan(
    extracted: XLSXTextDocument,
    engine,
) -> XLSXMaskPlan:
    """
    Build a reversible masking plan for an extracted XLSX workbook.

    The original workbook is never modified.

    Matching, overlap resolution and token generation are
    delegated entirely to the existing MaskingEngine.

    All workbook text is reserved before token generation
    to prevent collisions with tokens in later cells.

    Hyperlink display text is not allowed to be masked because
    its hyperlink destination may contain sensitive information.
    """

    if not isinstance(extracted, XLSXTextDocument):
        raise TypeError(
            "extracted must be an XLSXTextDocument"
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
        raise XLSXAdapterError(
            "Engine vault has an invalid vault ID"
        )

    # -------------------------------------------------
    # Phase 1: Reserve the complete workbook text.
    # -------------------------------------------------

    complete_source_text = "\n".join(
        cell.value
        for worksheet in extracted.worksheets
        for cell in worksheet.cells
    )

    reserve_source_text(complete_source_text)

    # -------------------------------------------------
    # Phase 2: Mask each extracted cell.
    # -------------------------------------------------

    worksheet_plans = []

    for worksheet in extracted.worksheets:

        cell_plans = []

        for cell in worksheet.cells:

            result = engine.mask_text(cell.value)

            if not isinstance(result, MaskResult):
                raise XLSXAdapterError(
                    "Masking engine returned an invalid result"
                )

            if result.vault_id != vault_id:
                raise XLSXAdapterError(
                    "Masking result belongs to another vault"
                )

            if not isinstance(result.masked_text, str):
                raise XLSXAdapterError(
                    "Masking engine returned invalid text"
                )

            if result.replacements_count != len(result.replacements):
                raise XLSXAdapterError(
                    "Replacement count does not match replacement metadata"
                )

            # A masked hyperlink display value could leave
            # sensitive information in its destination.
            if cell.has_hyperlink and result.replacements:
                raise XLSXAdapterError(
                    "Cannot safely mask hyperlink display text "
                    f"at {cell.worksheet_title}!{cell.coordinate}"
                )

            cell_plans.append(
                XLSXCellMaskPlan(
                    source_cell=cell,
                    masked_value=result.masked_text,
                    replacements=tuple(result.replacements),
                )
            )

        worksheet_plans.append(
            XLSXWorksheetMaskPlan(
                source_worksheet=worksheet,
                cells=tuple(cell_plans),
            )
        )

    # -------------------------------------------------
    # Phase 3: Return immutable document-level plan.
    # -------------------------------------------------

    return XLSXMaskPlan(
        source_path=extracted.source_path,
        source_sha256=extracted.source_sha256,
        vault_id=vault_id,
        worksheets=tuple(worksheet_plans),
    )


def apply_xlsx_masking_plan(
    plan: XLSXMaskPlan,
    output_path: str | Path,
) -> Path:
    """
    Apply a masking plan to an XLSX workbook.

    The source workbook is never modified.
    Existing output files are never overwritten.
    Cell formatting and supported workbook metadata
    are preserved.
    """

    if not isinstance(plan, XLSXMaskPlan):
        raise TypeError("plan must be an XLSXMaskPlan")

    if not isinstance(output_path, (str, Path)):
        raise TypeError("output_path must be a string or Path")

    source_path = Path(plan.source_path).expanduser().resolve()
    destination = Path(output_path).expanduser().resolve()

    if source_path.suffix.lower() != ".xlsx":
        raise XLSXAdapterError("Source must be an XLSX file")

    if destination.suffix.lower() != ".xlsx":
        raise ValueError("Output must have a .xlsx extension")

    if destination == source_path:
        raise XLSXAdapterError(
            "Output path must be different from source path"
        )

    if not source_path.exists():
        raise FileNotFoundError(source_path)

    if not source_path.is_file():
        raise XLSXAdapterError("Source path must be a file")

    if destination.exists():
        raise FileExistsError(destination)

    if not destination.parent.exists():
        raise FileNotFoundError(destination.parent)

    if not destination.parent.is_dir():
        raise NotADirectoryError(destination.parent)

    # -------------------------------------------------
    # Phase 1: Verify source fingerprint.
    # -------------------------------------------------

    source_bytes = source_path.read_bytes()

    actual_sha256 = hashlib.sha256(
        source_bytes
    ).hexdigest()

    if actual_sha256 != plan.source_sha256:
        raise XLSXAdapterError(
            "Source workbook changed after the masking plan "
            "was built"
        )

    # -------------------------------------------------
    # Phase 2: Load original workbook.
    # -------------------------------------------------

    try:
        workbook = load_workbook(
            BytesIO(source_bytes),
            read_only=False,
            data_only=False,
            keep_links=True,
        )

    except Exception as exc:
        raise XLSXAdapterError(
            "Unable to load source workbook"
        ) from exc

    output_created = False

    try:

        # -------------------------------------------------
        # Phase 3: Validate worksheet structure.
        # -------------------------------------------------

        if len(workbook.worksheets) != plan.worksheet_count:
            raise XLSXAdapterError(
                "Worksheet count does not match masking plan"
            )

        expected_indexes = tuple(
            range(len(workbook.worksheets))
        )

        actual_indexes = tuple(
            item.worksheet_index
            for item in plan.worksheets
        )

        if actual_indexes != expected_indexes:
            raise XLSXAdapterError(
                "Masking plan worksheet order is invalid"
            )

        # -------------------------------------------------
        # Phase 4: Apply masking.
        # -------------------------------------------------

        for worksheet_plan in plan.worksheets:

            index = worksheet_plan.worksheet_index

            worksheet = workbook.worksheets[index]

            current_source = _extract_worksheet(
                worksheet,
                index,
            )

            if current_source != worksheet_plan.source_worksheet:
                raise XLSXAdapterError(
                    f"Worksheet metadata mismatch: {worksheet.title}"
                )

            for cell_plan in worksheet_plan.cells:

                cell = worksheet[cell_plan.coordinate]

                if cell.value != cell_plan.original_value:
                    raise XLSXAdapterError(
                        "Source cell value mismatch at "
                        f"{worksheet.title}!{cell.coordinate}"
                    )

                if cell_plan.is_masked:

                    # Never mask hyperlink display text.
                    if cell.hyperlink is not None:
                        raise XLSXAdapterError(
                            "Cannot safely mask hyperlink display text "
                            f"at {worksheet.title}!{cell.coordinate}"
                        )

                    # Only change the cell value.
                    # Formatting remains untouched.
                    cell.value = cell_plan.masked_value

        # -------------------------------------------------
        # Phase 5: Save to a new output file.
        # -------------------------------------------------

        # Exclusive creation prevents accidental overwriting.
        with destination.open("xb") as output_file:

            output_created = True

            workbook.save(output_file)

    except FileExistsError:
        raise

    except XLSXAdapterError:

        if output_created:
            destination.unlink(missing_ok=True)

        raise

    except Exception as exc:

        if output_created:
            destination.unlink(missing_ok=True)

        raise XLSXAdapterError(
            "Failed to create masked XLSX workbook"
        ) from exc

    finally:
        workbook.close()

    # -------------------------------------------------
    # Phase 6: Verify generated workbook.
    # -------------------------------------------------

    verified_workbook = None

    try:

        verified_workbook = load_workbook(
            destination,
            read_only=False,
            data_only=False,
            keep_links=True,
        )

        expected_names = [
            item.title
            for item in plan.worksheets
        ]

        if verified_workbook.sheetnames != expected_names:
            raise XLSXAdapterError(
                "Generated workbook worksheet names mismatch"
            )

        for worksheet_plan in plan.worksheets:

            source_worksheet = worksheet_plan.source_worksheet

            worksheet = verified_workbook.worksheets[
                worksheet_plan.worksheet_index
            ]

            # Worksheet visibility.
            if worksheet.sheet_state != source_worksheet.state:
                raise XLSXAdapterError(
                    f"Worksheet state mismatch: {worksheet.title}"
                )

            # Merged cells.
            actual_merged_ranges = tuple(
                str(item)
                for item in worksheet.merged_cells.ranges
            )

            if actual_merged_ranges != source_worksheet.merged_ranges:
                raise XLSXAdapterError(
                    f"Merged ranges mismatch: {worksheet.title}"
                )

            # Cell values and formatting.
            for cell_plan in worksheet_plan.cells:

                cell = worksheet[cell_plan.coordinate]

                if cell.value != cell_plan.masked_value:
                    raise XLSXAdapterError(
                        "Masked cell verification failed at "
                        f"{worksheet.title}!{cell.coordinate}"
                    )

                if cell.style_id != cell_plan.source_cell.style_id:
                    raise XLSXAdapterError(
                        "Cell style was not preserved at "
                        f"{worksheet.title}!{cell.coordinate}"
                    )

                if (
                    cell.number_format
                    != cell_plan.source_cell.number_format
                ):
                    raise XLSXAdapterError(
                        "Cell number format was not preserved"
                    )

            # Formula preservation.
            for formula in source_worksheet.formula_cells:

                if worksheet[formula.coordinate].value != formula.formula:
                    raise XLSXAdapterError(
                        "Formula changed at "
                        f"{worksheet.title}!{formula.coordinate}"
                    )

            # Comments and hyperlink targets.
            for skipped in source_worksheet.skipped_text_elements:

                if skipped.kind == "comment":

                    comment = worksheet[skipped.coordinate].comment

                    if comment is None or comment.text != skipped.text:
                        raise XLSXAdapterError(
                            "Comment was not preserved"
                        )

                elif skipped.kind == "hyperlink_target":

                    hyperlink = worksheet[skipped.coordinate].hyperlink

                    target = (
                        hyperlink
                        if isinstance(hyperlink, str)
                        else hyperlink.target
                        if hyperlink is not None
                        else None
                    )

                    if target != skipped.text:
                        raise XLSXAdapterError(
                            "Hyperlink target was not preserved"
                        )

    except XLSXAdapterError:

        destination.unlink(missing_ok=True)

        raise

    except Exception as exc:

        destination.unlink(missing_ok=True)

        raise XLSXAdapterError(
            "Generated masked workbook failed verification"
        ) from exc

    finally:

        if verified_workbook is not None:
            verified_workbook.close()

    return destination


@dataclass(frozen=True, slots=True)
class XLSXMappedRestoration:
    """One masking token occurrence mapped to an Excel cell."""

    worksheet_index: int
    worksheet_title: str
    coordinate: str

    start: int
    end: int

    token: str
    original: str

    def __post_init__(self):

        if self.worksheet_index < 0:
            raise ValueError("Invalid worksheet index")

        if not self.worksheet_title or not self.coordinate:
            raise ValueError("Invalid cell location")

        if self.start < 0 or self.end <= self.start:
            raise ValueError("Invalid restoration offsets")

        if self.end - self.start != len(self.token):
            raise ValueError("Restoration offsets do not match token")

        if TOKEN_PATTERN.fullmatch(self.token) is None:
            raise ValueError("Invalid masking token")

        if not isinstance(self.original, str) or not self.original:
            raise ValueError("Invalid original value")


@dataclass(frozen=True, slots=True)
class XLSXCellUnmaskPlan:
    """Unmasking plan for one Excel cell."""

    source_cell: XLSXCellText

    restored_value: str

    restorations: tuple[XLSXMappedRestoration, ...] = ()
    unknown_tokens: tuple[str, ...] = ()

    def __post_init__(self):

        if not isinstance(self.source_cell, XLSXCellText):
            raise TypeError("source_cell must be an XLSXCellText")

        if not isinstance(self.restored_value, str):
            raise TypeError("restored_value must be a string")

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

        for token in self.unknown_tokens:

            if (
                not isinstance(token, str)
                or TOKEN_PATTERN.fullmatch(token) is None
                or token not in self.source_cell.value
            ):
                raise ValueError("Invalid unknown token")

        cursor = 0
        output = []

        for restoration in self.restorations:

            if not isinstance(
                restoration,
                XLSXMappedRestoration,
            ):
                raise TypeError("Invalid restoration type")

            if (
                restoration.worksheet_index
                != self.source_cell.worksheet_index
                or restoration.worksheet_title
                != self.source_cell.worksheet_title
                or restoration.coordinate
                != self.source_cell.coordinate
            ):
                raise ValueError(
                    "Restoration belongs to another cell"
                )

            if restoration.start < cursor:
                raise ValueError("Overlapping restorations")

            if self.source_cell.value[
                restoration.start:restoration.end
            ] != restoration.token:
                raise ValueError(
                    "Restoration does not match cell text"
                )

            output.append(
                self.source_cell.value[cursor:restoration.start]
            )

            output.append(restoration.original)

            cursor = restoration.end

        output.append(self.source_cell.value[cursor:])

        if "".join(output) != self.restored_value:
            raise ValueError(
                "Restored cell value is inconsistent"
            )

    @property
    def masked_value(self) -> str:
        return self.source_cell.value

    @property
    def worksheet_index(self) -> int:
        return self.source_cell.worksheet_index

    @property
    def worksheet_title(self) -> str:
        return self.source_cell.worksheet_title

    @property
    def coordinate(self) -> str:
        return self.source_cell.coordinate

    @property
    def restored_count(self) -> int:
        return len(self.restorations)

    @property
    def is_restored(self) -> bool:
        return self.masked_value != self.restored_value


@dataclass(frozen=True, slots=True)
class XLSXWorksheetUnmaskPlan:
    """Unmasking plan for one worksheet."""

    source_worksheet: XLSXWorksheetText

    cells: tuple[XLSXCellUnmaskPlan, ...]

    def __post_init__(self):

        if not isinstance(
            self.source_worksheet,
            XLSXWorksheetText,
        ):
            raise TypeError(
                "source_worksheet must be an XLSXWorksheetText"
            )

        object.__setattr__(
            self,
            "cells",
            tuple(self.cells),
        )

        if len(self.cells) != len(self.source_worksheet.cells):
            raise ValueError(
                "Cell count does not match source worksheet"
            )

        for source_cell, cell_plan in zip(
            self.source_worksheet.cells,
            self.cells,
        ):

            if not isinstance(cell_plan, XLSXCellUnmaskPlan):
                raise TypeError("Invalid cell unmasking plan")

            if cell_plan.source_cell != source_cell:
                raise ValueError(
                    "Cell plan does not match source worksheet"
                )

    @property
    def worksheet_index(self) -> int:
        return self.source_worksheet.worksheet_index

    @property
    def title(self) -> str:
        return self.source_worksheet.title

    @property
    def restored_count(self) -> int:
        return sum(
            cell.restored_count
            for cell in self.cells
        )

    @property
    def restored_cell_count(self) -> int:
        return sum(
            cell.is_restored
            for cell in self.cells
        )


@dataclass(frozen=True, slots=True)
class XLSXUnmaskPlan:
    """Complete unmasking plan for one XLSX workbook."""

    source_path: Path
    source_sha256: str
    vault_id: str

    worksheets: tuple[XLSXWorksheetUnmaskPlan, ...]

    def __post_init__(self):

        object.__setattr__(
            self,
            "worksheets",
            tuple(self.worksheets),
        )

        if not self.source_sha256:
            raise ValueError("Missing source fingerprint")

        if not self.vault_id:
            raise ValueError("Missing vault ID")

        indices = [
            worksheet.worksheet_index
            for worksheet in self.worksheets
        ]

        if len(indices) != len(set(indices)):
            raise ValueError("Duplicate worksheet indices")

    @property
    def worksheet_count(self) -> int:
        return len(self.worksheets)

    @property
    def restored_count(self) -> int:
        """Number of known token occurrences restored."""

        return sum(
            worksheet.restored_count
            for worksheet in self.worksheets
        )

    @property
    def restored_cell_count(self) -> int:
        return sum(
            worksheet.restored_cell_count
            for worksheet in self.worksheets
        )

    @property
    def unknown_tokens(self) -> tuple[str, ...]:
        """Unique unknown tokens in workbook order."""

        return tuple(
            dict.fromkeys(
                token
                for worksheet in self.worksheets
                for cell in worksheet.cells
                for token in cell.unknown_tokens
            )
        )

    @property
    def masked_text(self) -> str:
        return "\n".join(
            f"[{cell.worksheet_title}!{cell.coordinate}] "
            f"{cell.masked_value}"
            for worksheet in self.worksheets
            for cell in worksheet.cells
        )

    @property
    def restored_text(self) -> str:
        return "\n".join(
            f"[{cell.worksheet_title}!{cell.coordinate}] "
            f"{cell.restored_value}"
            for worksheet in self.worksheets
            for cell in worksheet.cells
        )

    
def build_xlsx_unmasking_plan(
    extracted: XLSXTextDocument,
    engine,
) -> XLSXUnmaskPlan:
    """
    Build a reversible unmasking plan for an extracted XLSX.

    The original workbook is never modified.

    Responsibilities:
        - Find masking tokens in string-valued cells.
        - Restore known tokens through MaskingEngine.
        - Preserve unknown tokens.
        - Map every restoration to its cell coordinates.
        - Validate vault identity and restoration results.
    """

    if not isinstance(extracted, XLSXTextDocument):
        raise TypeError(
            "extracted must be an XLSXTextDocument"
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
        raise XLSXAdapterError(
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

    # -------------------------------------------------
    # Phase 1: Validate vault mappings.
    # -------------------------------------------------

    for token, original in token_mapping.items():

        if (
            not isinstance(token, str)
            or TOKEN_PATTERN.fullmatch(token) is None
        ):
            raise XLSXAdapterError(
                "Vault contains an invalid token"
            )

        if not isinstance(original, str) or not original:
            raise XLSXAdapterError(
                "Vault contains an invalid original value"
            )

    # -------------------------------------------------
    # Phase 2: Process every worksheet and cell.
    # -------------------------------------------------

    worksheet_plans = []

    for worksheet in extracted.worksheets:

        cell_plans = []

        for cell in worksheet.cells:

            result = engine.unmask_text(cell.value)

            if not isinstance(result, RestoreResult):
                raise XLSXAdapterError(
                    "Masking engine returned an invalid restoration result"
                )

            if result.vault_id != vault_id:
                raise XLSXAdapterError(
                    "Restoration result belongs to another vault"
                )

            if not isinstance(result.restored_text, str):
                raise XLSXAdapterError(
                    "Masking engine returned invalid restored text"
                )

            restorations = []
            unknown_tokens = []

            expected_output = []
            cursor = 0

            # Find all tokens in their original order.
            for match in TOKEN_PATTERN.finditer(cell.value):

                token = match.group(0)

                original = token_mapping.get(token)

                # Unknown tokens remain unchanged.
                if original is None:

                    if token not in unknown_tokens:
                        unknown_tokens.append(token)

                    continue

                expected_output.append(
                    cell.value[cursor:match.start()]
                )

                expected_output.append(original)

                restorations.append(
                    XLSXMappedRestoration(
                        worksheet_index=cell.worksheet_index,
                        worksheet_title=cell.worksheet_title,
                        coordinate=cell.coordinate,
                        start=match.start(),
                        end=match.end(),
                        token=token,
                        original=original,
                    )
                )

                cursor = match.end()

            expected_output.append(
                cell.value[cursor:]
            )

            expected_restored_value = "".join(
                expected_output
            )

            # -------------------------------------------------
            # Phase 3: Validate engine output.
            # -------------------------------------------------

            if (
                type(result.restored_count) is not int
                or result.restored_count != len(restorations)
            ):
                raise XLSXAdapterError(
                    "Restoration count does not match token mappings"
                )

            if tuple(result.unknown_tokens) != tuple(unknown_tokens):
                raise XLSXAdapterError(
                    "Unknown token information is inconsistent"
                )

            if result.restored_text != expected_restored_value:
                raise XLSXAdapterError(
                    "Restored text does not match vault mappings"
                )

            cell_plans.append(
                XLSXCellUnmaskPlan(
                    source_cell=cell,
                    restored_value=result.restored_text,
                    restorations=tuple(restorations),
                    unknown_tokens=tuple(unknown_tokens),
                )
            )

        worksheet_plans.append(
            XLSXWorksheetUnmaskPlan(
                source_worksheet=worksheet,
                cells=tuple(cell_plans),
            )
        )

    # -------------------------------------------------
    # Phase 4: Return immutable workbook-level plan.
    # -------------------------------------------------

    return XLSXUnmaskPlan(
        source_path=extracted.source_path,
        source_sha256=extracted.source_sha256,
        vault_id=vault_id,
        worksheets=tuple(worksheet_plans),
    )


def _xlsx_defined_names_snapshot(workbook) -> tuple:
    """Return a stable snapshot of workbook defined names."""

    return tuple(
        (
            name,
            getattr(item, "attr_text", None),
            getattr(item, "localSheetId", None),
            getattr(item, "hidden", None),
            getattr(item, "function", None),
            getattr(item, "vbProcedure", None),
        )
        for name, item in workbook.defined_names.items()
    )


def _verify_xlsx_unmasked_workbook(
    workbook,
    plan: XLSXUnmaskPlan,
    metadata_snapshot: dict,
) -> None:
    """Verify restored values and supported workbook metadata."""

    expected_names = [
        worksheet_plan.title
        for worksheet_plan in plan.worksheets
    ]

    if workbook.sheetnames != expected_names:
        raise XLSXAdapterError(
            "Generated workbook worksheet names mismatch"
        )

    for worksheet_plan in plan.worksheets:

        source_worksheet = worksheet_plan.source_worksheet
        worksheet = workbook.worksheets[
            worksheet_plan.worksheet_index
        ]

        if worksheet.title != source_worksheet.title:
            raise XLSXAdapterError(
                f"Worksheet title mismatch: {source_worksheet.title}"
            )

        if worksheet.sheet_state != source_worksheet.state:
            raise XLSXAdapterError(
                f"Worksheet state mismatch: {worksheet.title}"
            )

        if (
            worksheet.max_row != source_worksheet.max_row
            or worksheet.max_column != source_worksheet.max_column
        ):
            raise XLSXAdapterError(
                f"Worksheet dimensions changed: {worksheet.title}"
            )

        actual_merged_ranges = tuple(
            str(item)
            for item in worksheet.merged_cells.ranges
        )

        if actual_merged_ranges != source_worksheet.merged_ranges:
            raise XLSXAdapterError(
                f"Merged ranges mismatch: {worksheet.title}"
            )

        for cell_plan in worksheet_plan.cells:

            cell = worksheet[cell_plan.coordinate]

            if cell.value != cell_plan.restored_value:
                raise XLSXAdapterError(
                    "Restored cell verification failed at "
                    f"{worksheet.title}!{cell.coordinate}"
                )

            if cell.style_id != cell_plan.source_cell.style_id:
                raise XLSXAdapterError(
                    "Cell style was not preserved at "
                    f"{worksheet.title}!{cell.coordinate}"
                )

            if (
                cell.number_format
                != cell_plan.source_cell.number_format
            ):
                raise XLSXAdapterError(
                    "Cell number format was not preserved at "
                    f"{worksheet.title}!{cell.coordinate}"
                )

        for formula in source_worksheet.formula_cells:

            if worksheet[formula.coordinate].value != formula.formula:
                raise XLSXAdapterError(
                    "Formula changed at "
                    f"{worksheet.title}!{formula.coordinate}"
                )

        for skipped in source_worksheet.skipped_text_elements:

            if skipped.kind == "comment":

                comment = worksheet[skipped.coordinate].comment

                if comment is None or comment.text != skipped.text:
                    raise XLSXAdapterError(
                        "Comment was not preserved at "
                        f"{worksheet.title}!{skipped.coordinate}"
                    )

            elif skipped.kind == "hyperlink_target":

                hyperlink = worksheet[skipped.coordinate].hyperlink

                target = (
                    hyperlink
                    if isinstance(hyperlink, str)
                    else hyperlink.target
                    if hyperlink is not None
                    else None
                )

                if target != skipped.text:
                    raise XLSXAdapterError(
                        "Hyperlink target was not preserved at "
                        f"{worksheet.title}!{skipped.coordinate}"
                    )

    if vars(workbook.properties) != metadata_snapshot["properties"]:
        raise XLSXAdapterError(
            "Workbook document properties were not preserved"
        )

    if vars(workbook.calculation) != metadata_snapshot["calculation"]:
        raise XLSXAdapterError(
            "Workbook calculation settings were not preserved"
        )

    if (
        _xlsx_defined_names_snapshot(workbook)
        != metadata_snapshot["defined_names"]
    ):
        raise XLSXAdapterError(
            "Workbook defined names were not preserved"
        )


def apply_xlsx_unmasking_plan(
    plan: XLSXUnmaskPlan,
    output_path: str | Path,
) -> Path:
    """
    Apply a restoration plan to a separate XLSX workbook.

    The source workbook is never modified.
    Existing output files are never overwritten.
    Unknown masking tokens remain unchanged.
    Cell formatting and supported workbook metadata are preserved.
    The completed temporary workbook is reopened and verified before
    it is published at the requested destination.
    """

    if not isinstance(plan, XLSXUnmaskPlan):
        raise TypeError("plan must be an XLSXUnmaskPlan")

    if not isinstance(output_path, (str, Path)):
        raise TypeError("output_path must be a string or Path")

    try:
        source_path = Path(plan.source_path).expanduser().resolve()
        destination = Path(output_path).expanduser().resolve()
    except (TypeError, ValueError, OSError) as exc:
        raise XLSXAdapterError("Invalid source or output path") from exc

    if source_path.suffix.lower() != ".xlsx":
        raise XLSXAdapterError("Source must be an XLSX file")

    if destination.suffix.lower() != ".xlsx":
        raise ValueError("Output must have a .xlsx extension")

    if destination == source_path:
        raise XLSXAdapterError(
            "Output path must be different from source path"
        )

    if not source_path.exists():
        raise FileNotFoundError(source_path)

    if not source_path.is_file():
        raise XLSXAdapterError("Source path must be a file")

    if destination.exists() or destination.is_symlink():
        raise FileExistsError(destination)

    if not destination.parent.exists():
        raise FileNotFoundError(destination.parent)

    if not destination.parent.is_dir():
        raise NotADirectoryError(destination.parent)

    if (
        not isinstance(plan.source_sha256, str)
        or len(plan.source_sha256) != 64
        or any(
            char not in "0123456789abcdefABCDEF"
            for char in plan.source_sha256
        )
    ):
        raise XLSXAdapterError(
            "Unmasking plan contains an invalid source fingerprint"
        )

    try:
        source_bytes = source_path.read_bytes()
    except OSError as exc:
        raise XLSXAdapterError(
            "Unable to read source workbook"
        ) from exc

    actual_sha256 = hashlib.sha256(source_bytes).hexdigest()

    if actual_sha256 != plan.source_sha256:
        raise XLSXAdapterError(
            "Source workbook changed after the unmasking plan "
            "was built"
        )

    try:
        workbook = load_workbook(
            BytesIO(source_bytes),
            read_only=False,
            data_only=False,
            keep_links=True,
        )
    except Exception as exc:
        raise XLSXAdapterError(
            "Unable to load source workbook"
        ) from exc

    temporary_path = None
    verified_workbook = None
    published = False

    try:
        if len(workbook.worksheets) != plan.worksheet_count:
            raise XLSXAdapterError(
                "Worksheet count does not match unmasking plan"
            )

        expected_indexes = tuple(range(len(workbook.worksheets)))
        actual_indexes = tuple(
            item.worksheet_index
            for item in plan.worksheets
        )

        if actual_indexes != expected_indexes:
            raise XLSXAdapterError(
                "Unmasking plan worksheet order is invalid"
            )

        if any(
            not isinstance(item, XLSXWorksheetUnmaskPlan)
            for item in plan.worksheets
        ):
            raise XLSXAdapterError(
                "Unmasking plan contains an invalid worksheet plan"
            )

        metadata_snapshot = {
            "properties": deepcopy(vars(workbook.properties)),
            "calculation": deepcopy(vars(workbook.calculation)),
            "defined_names": _xlsx_defined_names_snapshot(workbook),
        }

        for worksheet_plan in plan.worksheets:

            index = worksheet_plan.worksheet_index
            worksheet = workbook.worksheets[index]

            current_source = _extract_worksheet(
                worksheet,
                index,
            )

            if current_source != worksheet_plan.source_worksheet:
                raise XLSXAdapterError(
                    "Worksheet metadata mismatch: "
                    f"{worksheet.title}"
                )

            for cell_plan in worksheet_plan.cells:

                if not isinstance(cell_plan, XLSXCellUnmaskPlan):
                    raise XLSXAdapterError(
                        "Unmasking plan contains an invalid cell plan"
                    )

                cell = worksheet[cell_plan.coordinate]

                if cell.value != cell_plan.masked_value:
                    raise XLSXAdapterError(
                        "Source cell value mismatch at "
                        f"{worksheet.title}!{cell.coordinate}"
                    )

                if cell_plan.is_restored:
                    cell.value = cell_plan.restored_value

        try:
            descriptor, temporary_name = tempfile.mkstemp(
                prefix=f".{destination.stem}.",
                suffix=".tmp.xlsx",
                dir=destination.parent,
            )
            temporary_path = Path(temporary_name)
            os.close(descriptor)

            workbook.save(temporary_path)

        except Exception as exc:
            raise XLSXAdapterError(
                "Failed to save temporary restored XLSX workbook"
            ) from exc

        finally:
            workbook.close()

        try:
            verified_workbook = load_workbook(
                temporary_path,
                read_only=False,
                data_only=False,
                keep_links=True,
            )

            _verify_xlsx_unmasked_workbook(
                verified_workbook,
                plan,
                metadata_snapshot,
            )

        except XLSXAdapterError:
            raise

        except Exception as exc:
            raise XLSXAdapterError(
                "Unable to verify generated restored workbook"
            ) from exc

        finally:
            if verified_workbook is not None:
                verified_workbook.close()
                verified_workbook = None

        try:
            final_source_sha256 = hashlib.sha256(
                source_path.read_bytes()
            ).hexdigest()
        except OSError as exc:
            raise XLSXAdapterError(
                "Unable to recheck source workbook"
            ) from exc

        if final_source_sha256 != plan.source_sha256:
            raise XLSXAdapterError(
                "Source workbook changed while the restored "
                "workbook was being generated"
            )

        try:
            # Atomic publication on filesystems that support hard links.
            # The operation fails rather than replacing an existing file.
            os.link(temporary_path, destination)
            published = True

        except FileExistsError:
            raise

        except OSError:
            # Some filesystems disallow hard links. Exclusive creation
            # is a safe fallback that still prevents overwriting.
            try:
                with temporary_path.open("rb") as source_file:
                    with destination.open("xb") as output_file:
                        published = True
                        shutil.copyfileobj(source_file, output_file)
                        output_file.flush()
                        os.fsync(output_file.fileno())

            except FileExistsError:
                raise

            except Exception as exc:
                if published:
                    destination.unlink(missing_ok=True)
                    published = False

                raise XLSXAdapterError(
                    "Unable to publish restored XLSX workbook"
                ) from exc

        return destination

    except FileExistsError:
        raise

    except XLSXAdapterError:
        raise

    except Exception as exc:
        raise XLSXAdapterError(
            "Failed to create restored XLSX workbook"
        ) from exc

    finally:
        try:
            workbook.close()
        except Exception:
            pass

        if verified_workbook is not None:
            try:
                verified_workbook.close()
            except Exception:
                pass

        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)
