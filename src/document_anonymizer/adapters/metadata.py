from __future__ import annotations

import base64
import hashlib
import os
import re
import tempfile
import zipfile
from pathlib import Path
from typing import Any
from xml.etree import ElementTree as ET

import pymupdf


class MetadataSanitizationError(Exception):
    """Raised when metadata cannot be sanitized without risking data loss."""


OOXML_XML_METADATA_PARTS = (
    "docProps/core.xml",
    "docProps/app.xml",
)
OOXML_METADATA_PARTS = OOXML_XML_METADATA_PARTS + ("docProps/thumbnail.jpeg",)

# A 1x1 white JPEG placeholder with no identifying image metadata.
_BLANK_THUMBNAIL_JPEG = base64.b64decode(
    "/9j/4AAQSkZJRgABAQAAAQABAAD/2wBDAAoHBwgHBgoICAgLCgoLDhgQDg0NDh0VFhEYIx8lJCIfIiEmKzcvJik0KSEiMEExNDk7Pj4+JS5ESUM8SDc9Pjv/2wBDAQoLCw4NDhwQEBw7KCIoOzs7Ozs7Ozs7Ozs7Ozs7Ozs7Ozs7Ozs7Ozs7Ozs7Ozs7Ozs7Ozs7Ozs7Ozs7Ozs7Ozv/wAARCAABAAEDASIAAhEBAxEB/8QAFQABAQAAAAAAAAAAAAAAAAAAAAf/xAAUEAEAAAAAAAAAAAAAAAAAAAAA/8QAFAEBAAAAAAAAAAAAAAAAAAAAAP/EABQRAQAAAAAAAAAAAAAAAAAAAAD/2gAMAwEAAhEDEQA/ALMAD//Z"
)

PDF_METADATA_FIELDS = (
    "title",
    "author",
    "subject",
    "keywords",
    "creator",
    "producer",
    "creationDate",
    "modDate",
    "trapped",
)

TRACKED_CHANGE_PATTERN = re.compile(
    rb"<(?:[A-Za-z_][\w.-]*:)?"
    rb"(?:ins|del|moveFrom|moveTo|commentRangeStart|commentRangeEnd|commentReference)\b"
)
HYPERLINK_PATTERN = re.compile(
    rb"<(?:[A-Za-z_][\w.-]*:)?hyperlink\b",
    re.IGNORECASE,
)


def _extension(path: Path) -> str:
    extension = path.suffix.lower()
    if extension not in {".docx", ".xlsx", ".pdf"}:
        raise MetadataSanitizationError(
            "Metadata handling supports only .docx, .xlsx, and .pdf files"
        )
    return extension


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _read_zip(path: Path) -> dict[str, bytes]:
    """Read only XML/package parts relevant to metadata inspection.

    Large embedded images and media are deliberately not loaded into memory.
    """
    try:
        with zipfile.ZipFile(path, "r") as archive:
            names = archive.namelist()
            selected = []
            for name in names:
                relevant = (
                    name in OOXML_METADATA_PARTS
                    or name == "docProps/custom.xml"
                    or name.endswith(".rels")
                    or (name.startswith("word/") and name.endswith(".xml"))
                    or (name.startswith("xl/worksheets/") and name.endswith(".xml"))
                    or name.startswith("xl/comments")
                    or name.startswith("xl/threadedComments/")
                    or name.startswith("xl/persons/")
                    or name.startswith("xl/externalLinks/")
                    or name.startswith("_xmlsignatures/")
                    or name.startswith("docProps/thumbnail")
                )
                if relevant:
                    selected.append(name)
            return {name: archive.read(name) for name in selected}
    except (OSError, zipfile.BadZipFile, KeyError) as exc:
        raise MetadataSanitizationError(
            f"Unable to inspect OOXML package: {path}"
        ) from exc


def _external_relationships(parts: dict[str, bytes]) -> bool:
    for name, data in parts.items():
        if not name.endswith(".rels"):
            continue
        if re.search(rb'TargetMode\s*=\s*["\']External["\']', data, re.IGNORECASE):
            return True
    return False


def _unsupported_ooxml_surfaces(path: Path, extension: str) -> tuple[str, ...]:
    parts = _read_zip(path)
    names = set(parts)
    unsupported: list[str] = []

    if "docProps/custom.xml" in names:
        unsupported.append("custom document properties (docProps/custom.xml)")
    if any(
        name.startswith("docProps/thumbnail")
        and name != "docProps/thumbnail.jpeg"
        for name in names
    ):
        unsupported.append("unsupported embedded document preview thumbnail format")

    if extension == ".docx":
        if any(
            name.startswith("word/comments")
            or name.startswith("word/people")
            for name in names
        ):
            unsupported.append("Word comments and comment-author data")

        if any(
            name.startswith("_xmlsignatures/")
            for name in names
        ):
            unsupported.append("OOXML digital signatures")

        for name, data in parts.items():
            if name.startswith("word/") and name.endswith(".xml"):
                if TRACKED_CHANGE_PATTERN.search(data):
                    unsupported.append("tracked changes or comment markup")
                    break

    else:
        if any(
            name.startswith("xl/comments")
            or name.startswith("xl/threadedComments/")
            or name.startswith("xl/persons/")
            for name in names
        ):
            unsupported.append("Excel cell comments or threaded comments")

        if any(name.startswith("xl/externalLinks/") for name in names):
            unsupported.append("external workbook links")

        if any(
            name.startswith("xl/worksheets/")
            and name.endswith(".xml")
            and HYPERLINK_PATTERN.search(data)
            for name, data in parts.items()
        ):
            unsupported.append("Excel hyperlinks and hyperlink targets")

        if any(name.startswith("_xmlsignatures/") for name in names):
            unsupported.append("OOXML digital signatures")

    if _external_relationships(parts):
        unsupported.append("external OOXML relationships or hyperlinks")

    return tuple(dict.fromkeys(unsupported))


def _unsupported_pdf_surfaces(path: Path) -> tuple[str, ...]:
    unsupported: list[str] = []
    document = None
    try:
        document = pymupdf.open(str(path))
        if document.needs_pass:
            raise MetadataSanitizationError(
                "Password-protected PDFs are not supported by metadata sanitization"
            )

        try:
            if document.embfile_count() > 0:
                unsupported.append("embedded files and attachments")
        except Exception:
            unsupported.append("uninspectable PDF embedded-file collection")

        try:
            if document.get_toc():
                unsupported.append("PDF bookmarks and outlines")
        except Exception:
            unsupported.append("uninspectable PDF bookmarks and outlines")

        for page_number, page in enumerate(document, start=1):
            try:
                if page.annots() is not None:
                    annotations = tuple(page.annots())
                    if annotations:
                        unsupported.append(
                            f"PDF annotations on page {page_number}"
                        )
            except Exception:
                unsupported.append(
                    f"uninspectable PDF annotations on page {page_number}"
                )

            try:
                if page.widgets() is not None:
                    widgets = tuple(page.widgets())
                    if widgets:
                        unsupported.append(
                            f"PDF form fields on page {page_number}"
                        )
            except Exception:
                unsupported.append(
                    f"uninspectable PDF form fields on page {page_number}"
                )

            try:
                if page.get_links():
                    unsupported.append(
                        f"PDF links on page {page_number}"
                    )
            except Exception:
                unsupported.append(
                    f"uninspectable PDF links on page {page_number}"
                )
    except MetadataSanitizationError:
        raise
    except Exception as exc:
        raise MetadataSanitizationError(
            f"Unable to inspect PDF metadata surfaces: {path}"
        ) from exc
    finally:
        if document is not None:
            document.close()

    return tuple(dict.fromkeys(unsupported))


def inspect_unsupported_metadata_surfaces(
    source: str | Path,
) -> tuple[str, ...]:
    """Return unsupported metadata-bearing surfaces found in a source file."""
    path = Path(source).expanduser().resolve()
    if not path.is_file():
        raise FileNotFoundError(path)

    extension = _extension(path)
    if extension == ".pdf":
        return _unsupported_pdf_surfaces(path)
    return _unsupported_ooxml_surfaces(path, extension)


def assert_metadata_surfaces_supported(source: str | Path) -> None:
    """Refuse files with metadata-bearing surfaces this version cannot restore."""
    unsupported = inspect_unsupported_metadata_surfaces(source)
    if unsupported:
        details = "; ".join(unsupported)
        raise MetadataSanitizationError(
            "This document contains metadata-bearing features that this version "
            f"does not sanitize or restore: {details}. Remove these features "
            "from a copy of the source document and retry. The source file was "
            "not modified."
        )


def _xml_without_properties(data: bytes, part_name: str) -> tuple[bytes, int]:
    try:
        root = ET.fromstring(data)
    except ET.ParseError as exc:
        raise MetadataSanitizationError(
            f"Unable to parse metadata XML part: {part_name}"
        ) from exc

    count = sum(
        1
        for element in root.iter()
        if element is not root and (element.text or "").strip()
    )

    # Core and extended properties are entirely optional. Keeping the root
    # element but removing its children ensures no standard property values
    # survive in the masked package.
    root.clear()
    sanitized = ET.tostring(
        root,
        encoding="utf-8",
        xml_declaration=True,
    )
    return sanitized, count


def _rewrite_zip_parts(
    path: Path,
    replacements: dict[str, bytes],
    removals: set[str] | None = None,
) -> None:
    removals = removals or set()
    temporary_path: Path | None = None
    try:
        fd, temporary_name = tempfile.mkstemp(
            prefix=f".{path.stem}.metadata.",
            suffix=path.suffix,
            dir=path.parent,
        )
        os.close(fd)
        temporary_path = Path(temporary_name)

        with zipfile.ZipFile(path, "r") as source:
            with zipfile.ZipFile(temporary_path, "w") as destination:
                destination.comment = source.comment
                seen: set[str] = set()
                for info in source.infolist():
                    name = info.filename
                    if name in removals:
                        continue
                    data = replacements.get(name)
                    if data is None:
                        data = source.read(name)
                    destination.writestr(info, data)
                    seen.add(name)

                # Some OOXML writers may omit a source metadata part while
                # rebuilding the package. Re-add sanitized replacement parts.
                for name in sorted(set(replacements) - seen):
                    destination.writestr(
                        name,
                        replacements[name],
                        compress_type=zipfile.ZIP_DEFLATED,
                    )

        os.replace(temporary_path, path)
        temporary_path = None
    except MetadataSanitizationError:
        raise
    except Exception as exc:
        raise MetadataSanitizationError(
            f"Unable to rewrite metadata in {path}"
        ) from exc
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)


def _mask_ooxml_metadata(
    source_path: Path,
    output_path: Path,
    extension: str,
) -> tuple[dict[str, Any], int]:
    assert_metadata_surfaces_supported(source_path)
    source_parts = _read_zip(source_path)
    output_parts = _read_zip(output_path)

    snapshot_parts: dict[str, str | None] = {}
    replacements: dict[str, bytes] = {}
    removals: set[str] = set()
    sanitized_count = 0

    for part_name in OOXML_METADATA_PARTS:
        original = source_parts.get(part_name)
        snapshot_parts[part_name] = (
            base64.b64encode(original).decode("ascii")
            if original is not None
            else None
        )

        output_part = output_parts.get(part_name)
        if part_name == "docProps/thumbnail.jpeg":
            if output_part is not None or original is not None:
                replacements[part_name] = _BLANK_THUMBNAIL_JPEG
                sanitized_count += 1
            continue

        if original is None:
            if output_part is not None:
                removals.add(part_name)
            continue

        if output_part is None:
            raise MetadataSanitizationError(
                f"Output package is missing metadata part: {part_name}"
            )

        sanitized, count = _xml_without_properties(
            output_part,
            part_name,
        )
        replacements[part_name] = sanitized
        sanitized_count += count

    _rewrite_zip_parts(
        output_path,
        replacements=replacements,
        removals=removals,
    )

    snapshot = {
        "schema_version": 1,
        "format": extension[1:],
        "parts": snapshot_parts,
    }
    return snapshot, sanitized_count


def _mask_pdf_metadata(
    source_path: Path,
    output_path: Path,
) -> tuple[dict[str, Any], int]:
    assert_metadata_surfaces_supported(source_path)

    source = None
    output = None
    temporary_path: Path | None = None
    try:
        source = pymupdf.open(str(source_path))
        output = pymupdf.open(str(output_path))

        original_metadata = source.metadata or {}
        xmp = source.get_xml_metadata() or ""
        properties = {
            field: original_metadata.get(field)
            for field in PDF_METADATA_FIELDS
        }

        metadata_count = sum(
            1 for value in properties.values()
            if value not in (None, "")
        ) + (1 if xmp else 0)

        output.set_metadata({
            field: ""
            for field in PDF_METADATA_FIELDS
        })
        output.set_xml_metadata("")

        fd, temporary_name = tempfile.mkstemp(
            prefix=f".{output_path.stem}.metadata.",
            suffix=".pdf",
            dir=output_path.parent,
        )
        os.close(fd)
        temporary_path = Path(temporary_name)
        output.save(
            str(temporary_path),
            garbage=4,
            deflate=True,
        )
        output.close()
        output = None
        os.replace(temporary_path, output_path)
        temporary_path = None

        snapshot = {
            "schema_version": 1,
            "format": "pdf",
            "properties": properties,
            "xmp": xmp,
        }
        return snapshot, metadata_count
    except MetadataSanitizationError:
        raise
    except Exception as exc:
        raise MetadataSanitizationError(
            f"Unable to sanitize PDF metadata: {output_path}"
        ) from exc
    finally:
        if source is not None:
            source.close()
        if output is not None:
            output.close()
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)


def mask_document_metadata(
    source: str | Path,
    masked_output: str | Path,
    vault,
) -> int:
    """Sanitize standard metadata and store its exact original form in the vault.

    All standard core/extended metadata values are cleared, not just values
    matched by content rules. The original metadata is stored inside the
    encrypted vault when the caller persists it.
    """
    source_path = Path(source).expanduser().resolve()
    output_path = Path(masked_output).expanduser().resolve()
    if source_path == output_path:
        raise MetadataSanitizationError(
            "Source and masked output paths must be different"
        )
    if not source_path.is_file() or not output_path.is_file():
        raise FileNotFoundError(
            "Both source and masked output files must exist"
        )
    extension = _extension(source_path)
    if output_path.suffix.lower() != extension:
        raise MetadataSanitizationError(
            "Source and output formats must match"
        )

    if extension == ".pdf":
        snapshot, count = _mask_pdf_metadata(source_path, output_path)
    else:
        snapshot, count = _mask_ooxml_metadata(
            source_path,
            output_path,
            extension,
        )

    snapshot["source_sha256"] = _sha256_file(source_path)
    snapshot["masked_sha256"] = _sha256_file(output_path)

    vault.set_metadata_snapshot(snapshot)
    return count


def _restore_ooxml_metadata(
    output_path: Path,
    snapshot: dict[str, Any],
) -> int:
    parts = snapshot.get("parts")
    if not isinstance(parts, dict) or set(parts) != set(OOXML_METADATA_PARTS):
        raise MetadataSanitizationError(
            "Invalid OOXML metadata snapshot in vault"
        )

    replacements: dict[str, bytes] = {}
    removals: set[str] = set()
    restored_count = 0

    for part_name, encoded in parts.items():
        if encoded is None:
            if part_name == "docProps/thumbnail.jpeg":
                # Keep a non-identifying placeholder if the OOXML writer
                # generated a preview part during the content round trip.
                replacements[part_name] = _BLANK_THUMBNAIL_JPEG
            else:
                removals.add(part_name)
            continue
        if not isinstance(encoded, str):
            raise MetadataSanitizationError(
                "Invalid metadata part encoding in vault"
            )
        try:
            raw = base64.b64decode(encoded, validate=True)
            if part_name != "docProps/thumbnail.jpeg":
                ET.fromstring(raw)
        except Exception as exc:
            raise MetadataSanitizationError(
                f"Invalid encrypted metadata part: {part_name}"
            ) from exc
        replacements[part_name] = raw
        restored_count += 1

    _rewrite_zip_parts(
        output_path,
        replacements=replacements,
        removals=removals,
    )
    return restored_count


def _restore_pdf_metadata(
    output_path: Path,
    snapshot: dict[str, Any],
) -> int:
    properties = snapshot.get("properties")
    xmp = snapshot.get("xmp")
    if (
        not isinstance(properties, dict)
        or set(properties) != set(PDF_METADATA_FIELDS)
        or (xmp is not None and not isinstance(xmp, str))
    ):
        raise MetadataSanitizationError(
            "Invalid PDF metadata snapshot in vault"
        )

    document = None
    temporary_path: Path | None = None
    try:
        document = pymupdf.open(str(output_path))
        document.set_metadata({
            field: "" if properties[field] is None else str(properties[field])
            for field in PDF_METADATA_FIELDS
        })
        document.set_xml_metadata(xmp or "")

        fd, temporary_name = tempfile.mkstemp(
            prefix=f".{output_path.stem}.restore-metadata.",
            suffix=".pdf",
            dir=output_path.parent,
        )
        os.close(fd)
        temporary_path = Path(temporary_name)
        document.save(
            str(temporary_path),
            garbage=4,
            deflate=True,
        )
        document.close()
        document = None
        os.replace(temporary_path, output_path)
        temporary_path = None
        return sum(
            1 for value in properties.values()
            if value not in (None, "")
        ) + (1 if xmp else 0)
    except Exception as exc:
        raise MetadataSanitizationError(
            f"Unable to restore PDF metadata: {output_path}"
        ) from exc
    finally:
        if document is not None:
            document.close()
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)


def verify_masked_document(
    masked_input: str | Path,
    vault,
) -> None:
    """Verify the exact masked artifact before attempting restoration.

    New vaults carry a SHA-256 fingerprint for every format. Legacy vaults
    without a metadata snapshot remain compatible and are not fingerprinted
    by this helper.
    """
    path = Path(masked_input).expanduser().resolve()
    snapshot = vault.metadata_snapshot
    if snapshot is None:
        return
    expected = snapshot.get("masked_sha256")
    if (
        not isinstance(expected, str)
        or len(expected) != 64
        or any(character not in "0123456789abcdef" for character in expected)
    ):
        raise MetadataSanitizationError(
            "Vault metadata snapshot has an invalid masked-file fingerprint"
        )
    actual = _sha256_file(path)
    if actual != expected:
        raise MetadataSanitizationError(
            "Masked document fingerprint does not match the encrypted vault. "
            "The masked file may have been modified after anonymization."
        )


def restore_document_metadata(
    restored_output: str | Path,
    vault,
) -> int:
    """Restore original metadata from the encrypted vault snapshot."""
    output_path = Path(restored_output).expanduser().resolve()
    if not output_path.is_file():
        raise FileNotFoundError(output_path)

    snapshot = vault.metadata_snapshot
    if snapshot is None:
        # Backward compatibility with vaults created before metadata support.
        return 0
    if not isinstance(snapshot, dict) or snapshot.get("schema_version") != 1:
        raise MetadataSanitizationError(
            "Unsupported metadata snapshot version"
        )

    expected_format = _extension(output_path)[1:]
    if snapshot.get("format") != expected_format:
        raise MetadataSanitizationError(
            "Metadata snapshot format does not match restored document"
        )

    if expected_format == "pdf":
        return _restore_pdf_metadata(output_path, snapshot)
    return _restore_ooxml_metadata(output_path, snapshot)


def inspect_document_metadata(source: str | Path) -> dict[str, Any]:
    """Return inspectable standard metadata for verification and tests."""
    path = Path(source).expanduser().resolve()
    extension = _extension(path)

    if extension == ".pdf":
        document = None
        try:
            document = pymupdf.open(str(path))
            metadata = document.metadata or {}
            return {
                "format": "pdf",
                "properties": {
                    field: metadata.get(field)
                    for field in PDF_METADATA_FIELDS
                },
                "xmp": document.get_xml_metadata() or "",
            }
        finally:
            if document is not None:
                document.close()

    parts = _read_zip(path)
    properties: dict[str, dict[str, str]] = {}
    for part_name in OOXML_XML_METADATA_PARTS:
        raw = parts.get(part_name)
        if raw is None:
            properties[part_name] = {}
            continue
        try:
            root = ET.fromstring(raw)
        except ET.ParseError as exc:
            raise MetadataSanitizationError(
                f"Unable to parse metadata XML part: {part_name}"
            ) from exc
        values = {}
        for element in root.iter():
            if element is root:
                continue
            local_name = element.tag.rsplit("}", 1)[-1]
            if element.text and element.text.strip():
                values[local_name] = element.text
        properties[part_name] = values
    return {
        "format": extension[1:],
        "properties": properties,
    }
