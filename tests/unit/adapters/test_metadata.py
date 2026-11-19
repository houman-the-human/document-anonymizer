from __future__ import annotations

import datetime as dt
import shutil
import zipfile
from pathlib import Path

import pymupdf
import pytest
from docx import Document
from openpyxl import Workbook
from openpyxl.comments import Comment

from document_anonymizer.adapters.metadata import (
    MetadataSanitizationError,
    assert_metadata_surfaces_supported,
    inspect_document_metadata,
    mask_document_metadata,
    restore_document_metadata,
)
from document_anonymizer.core.rules import RuleSet
from document_anonymizer.core.vault import MaskingVault
from document_anonymizer.core.exceptions import VaultAuthenticationError
from document_anonymizer.storage.file import FileVaultStore


def _make_vault() -> MaskingVault:
    return MaskingVault(RuleSet(()), vault_id="metadata-test-vault")


def _create_docx(path: Path, *, persian: bool = False) -> None:
    document = Document()
    document.add_paragraph("Ordinary document content")
    properties = document.core_properties
    properties.author = "علی رضایی" if persian else "John Smith"
    properties.last_modified_by = "Editor Name"
    properties.title = "قرارداد محرمانه" if persian else "Confidential Contract"
    properties.subject = "Internal review"
    properties.keywords = "private, customer"
    properties.comments = "Created by internal team"
    properties.category = "HR"
    properties.identifier = "DOC-12345"
    properties.language = "fa-IR"
    properties.revision = 9
    properties.created = dt.datetime(2024, 2, 3, 4, 5, 6)
    properties.modified = dt.datetime(2025, 3, 4, 5, 6, 7)
    properties.last_printed = dt.datetime(2025, 4, 5, 6, 7, 8)
    document.save(path)


def _create_xlsx(path: Path, *, persian: bool = False) -> None:
    workbook = Workbook()
    workbook.active["A1"] = "Ordinary document content"
    properties = workbook.properties
    properties.creator = "علی رضایی" if persian else "John Smith"
    properties.lastModifiedBy = "Editor Name"
    properties.title = "قرارداد محرمانه" if persian else "Confidential Contract"
    properties.subject = "Internal review"
    properties.description = "Private customer data"
    properties.keywords = "private, customer"
    properties.category = "HR"
    properties.identifier = "XLS-12345"
    properties.language = "fa-IR"
    properties.version = "1.0"
    properties.revision = "9"
    properties.created = dt.datetime(2024, 2, 3, 4, 5, 6)
    properties.modified = dt.datetime(2025, 3, 4, 5, 6, 7)
    properties.lastPrinted = dt.datetime(2025, 4, 5, 6, 7, 8)
    workbook.save(path)
    workbook.close()


def _create_pdf(path: Path, *, persian: bool = False) -> None:
    document = pymupdf.open()
    page = document.new_page()
    page.insert_text((50, 70), "Ordinary document content")
    document.set_metadata({
        "title": "قرارداد محرمانه" if persian else "Confidential Contract",
        "author": "علی رضایی" if persian else "John Smith",
        "subject": "Internal review",
        "keywords": "private, customer",
        "creator": "Editor Name",
        "producer": "Private PDF Generator",
        "creationDate": "D:20240203040506",
        "modDate": "D:20250304050607",
        "trapped": "False",
    })
    document.set_xml_metadata("""<?xpacket begin="﻿" id="W5M0MpCehiHzreSzNTczkc9d"?>
<x:xmpmeta xmlns:x="adobe:ns:meta/">
 <rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#">
  <rdf:Description rdf:about="" xmlns:dc="http://purl.org/dc/elements/1.1/">
   <dc:title><rdf:Alt><rdf:li xml:lang="x-default">Secret PDF XMP Title</rdf:li></rdf:Alt></dc:title>
  </rdf:Description>
 </rdf:RDF>
</x:xmpmeta>
<?xpacket end="w"?>""")
    document.save(path)
    document.close()


@pytest.mark.parametrize(
    ("extension", "creator_field"),
    [
        (".docx", "creator"),
        (".xlsx", "creator"),
        (".pdf", "author"),
    ],
)
@pytest.mark.parametrize("persian", [False, True])
def test_metadata_is_removed_and_restored_exactly(
    tmp_path: Path,
    extension: str,
    creator_field: str,
    persian: bool,
):
    source = tmp_path / f"Original{extension}"
    masked = tmp_path / f"Masked{extension}"
    restored = tmp_path / f"Restored{extension}"

    if extension == ".docx":
        _create_docx(source, persian=persian)
    elif extension == ".xlsx":
        _create_xlsx(source, persian=persian)
    else:
        _create_pdf(source, persian=persian)

    original_bytes = source.read_bytes()
    shutil.copy2(source, masked)
    vault = _make_vault()

    removed = mask_document_metadata(source, masked, vault)
    assert removed > 0
    assert source.read_bytes() == original_bytes

    masked_metadata = inspect_document_metadata(masked)
    if extension == ".pdf":
        assert all(
            value in (None, "")
            for value in masked_metadata["properties"].values()
        )
        assert masked_metadata["xmp"] == ""
    else:
        assert masked_metadata["properties"]["docProps/core.xml"] == {}
        assert masked_metadata["properties"]["docProps/app.xml"] == {}

    # Metadata is persisted as part of the encrypted vault, not a plaintext sidecar.
    store = FileVaultStore(tmp_path / "vaults")
    password = "metadata-password"
    store.save(vault, password)
    vault_file = next((tmp_path / "vaults").glob("*.vault"))
    encrypted_bytes = vault_file.read_bytes()
    assert b"John Smith" not in encrypted_bytes
    assert "علی رضایی".encode("utf-8") not in encrypted_bytes

    loaded_vault = store.load(vault.vault_id, password)
    assert loaded_vault.metadata_snapshot is not None

    with pytest.raises(VaultAuthenticationError):
        store.load(vault.vault_id, "wrong-password")

    shutil.copy2(masked, restored)
    restored_count = restore_document_metadata(restored, loaded_vault)
    assert restored_count > 0

    # The metadata parts for OOXML are byte-for-byte restored. PDF values are
    # restored through PyMuPDF's document-info and XMP APIs.
    restored_metadata = inspect_document_metadata(restored)
    if extension == ".pdf":
        expected_author = "علی رضایی" if persian else "John Smith"
        assert restored_metadata["properties"]["author"] == expected_author
        assert restored_metadata["properties"]["title"] == (
            "قرارداد محرمانه" if persian else "Confidential Contract"
        )
        assert "Secret PDF XMP Title" in restored_metadata["xmp"]
    else:
        core = restored_metadata["properties"]["docProps/core.xml"]
        expected_author = "علی رضایی" if persian else "John Smith"
        assert core[creator_field] == expected_author
        assert core["title"] == (
            "قرارداد محرمانه" if persian else "Confidential Contract"
        )
        with zipfile.ZipFile(source, "r") as source_archive:
            with zipfile.ZipFile(restored, "r") as restored_archive:
                for part_name in (
                    "docProps/core.xml",
                    "docProps/app.xml",
                    "docProps/thumbnail.jpeg",
                ):
                    if part_name in source_archive.namelist():
                        assert source_archive.read(part_name) == restored_archive.read(part_name)
        with zipfile.ZipFile(source, "r") as source_archive:
            with zipfile.ZipFile(masked, "r") as masked_archive:
                if "docProps/thumbnail.jpeg" in source_archive.namelist():
                    assert len(masked_archive.read("docProps/thumbnail.jpeg")) < 500


def test_metadata_snapshot_is_defensive_and_only_set_once():
    vault = _make_vault()
    snapshot = {"schema_version": 1, "format": "docx", "source_sha256": "a" * 64, "masked_sha256": "b" * 64, "parts": {
        "docProps/core.xml": None,
        "docProps/app.xml": None,
        "docProps/thumbnail.jpeg": None,
    }}
    vault.set_metadata_snapshot(snapshot)
    snapshot["format"] = "xlsx"
    assert vault.metadata_snapshot["format"] == "docx"

    with pytest.raises(ValueError, match="already attached"):
        vault.set_metadata_snapshot({"schema_version": 1})


def test_metadata_snapshot_survives_vault_serialization():
    from document_anonymizer.storage.serialization import (
        deserialize_vault,
        serialize_vault,
    )

    vault = _make_vault()
    vault.set_metadata_snapshot({
        "schema_version": 1,
        "format": "docx",
        "source_sha256": "a" * 64,
        "masked_sha256": "b" * 64,
        "parts": {
            "docProps/core.xml": "PHhtbD7wn5mE",
            "docProps/app.xml": None,
            "docProps/thumbnail.jpeg": None,
        },
    })
    restored = deserialize_vault(serialize_vault(vault))
    assert restored.metadata_snapshot == vault.metadata_snapshot


def test_legacy_vault_without_metadata_snapshot_remains_supported():
    from document_anonymizer.storage.serialization import (
        deserialize_vault,
        serialize_vault,
    )

    vault = _make_vault()
    payload = serialize_vault(vault)
    import json
    data = json.loads(payload)
    data["schema_version"] = 2
    data.pop("metadata_snapshot")
    legacy = deserialize_vault(json.dumps(data).encode("utf-8"))
    assert legacy.metadata_snapshot is None


def _append_zip_part(path: Path, part_name: str, data: bytes) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    with zipfile.ZipFile(path, "r") as source:
        with zipfile.ZipFile(temporary, "w", zipfile.ZIP_DEFLATED) as destination:
            for item in source.infolist():
                destination.writestr(item, source.read(item.filename))
            destination.writestr(part_name, data)
    temporary.replace(path)


def test_docx_custom_properties_are_explicitly_rejected(tmp_path: Path):
    source = tmp_path / "custom.docx"
    _create_docx(source)
    _append_zip_part(
        source,
        "docProps/custom.xml",
        b'<Properties xmlns="http://schemas.openxmlformats.org/officeDocument/2006/custom-properties"/>',
    )
    with pytest.raises(MetadataSanitizationError, match="custom document properties"):
        assert_metadata_surfaces_supported(source)


def test_docx_comments_are_explicitly_rejected(tmp_path: Path):
    source = tmp_path / "comments.docx"
    _create_docx(source)
    _append_zip_part(source, "word/comments.xml", b"<w:comments/>")
    with pytest.raises(MetadataSanitizationError, match="Word comments"):
        assert_metadata_surfaces_supported(source)


def test_docx_tracked_changes_are_explicitly_rejected(tmp_path: Path):
    source = tmp_path / "tracked.docx"
    _create_docx(source)
    with zipfile.ZipFile(source, "r") as archive:
        parts = {name: archive.read(name) for name in archive.namelist()}
    document_xml = parts["word/document.xml"]
    document_xml = document_xml.replace(
        b"<w:p>",
        b'<w:p><w:ins w:id="1" w:author="Secret Reviewer" w:date="2025-01-01T00:00:00Z">',
        1,
    )
    document_xml = document_xml.replace(b"</w:p>", b"</w:ins></w:p>", 1)
    parts["word/document.xml"] = document_xml
    temp = source.with_suffix(".tmp")
    with zipfile.ZipFile(temp, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, data in parts.items():
            archive.writestr(name, data)
    temp.replace(source)
    with pytest.raises(MetadataSanitizationError, match="tracked changes"):
        assert_metadata_surfaces_supported(source)


def test_xlsx_comments_are_explicitly_rejected(tmp_path: Path):
    source = tmp_path / "comments.xlsx"
    workbook = Workbook()
    workbook.active["A1"] = "data"
    workbook.active["A1"].comment = Comment("Private note", "Secret Author")
    workbook.save(source)
    workbook.close()
    with pytest.raises(MetadataSanitizationError, match="Excel cell comments"):
        assert_metadata_surfaces_supported(source)


def test_xlsx_hyperlinks_are_explicitly_rejected(tmp_path: Path):
    source = tmp_path / "links.xlsx"
    workbook = Workbook()
    workbook.active["A1"] = "Profile"
    workbook.active["A1"].hyperlink = "https://example.test/private/john"
    workbook.save(source)
    workbook.close()
    with pytest.raises(MetadataSanitizationError, match="hyperlinks"):
        assert_metadata_surfaces_supported(source)


def test_pdf_annotations_are_explicitly_rejected(tmp_path: Path):
    source = tmp_path / "annotations.pdf"
    document = pymupdf.open()
    page = document.new_page()
    page.add_text_annot((50, 50), "Private reviewer comment")
    document.save(source)
    document.close()
    with pytest.raises(MetadataSanitizationError, match="annotations"):
        assert_metadata_surfaces_supported(source)


def test_pdf_embedded_attachments_are_explicitly_rejected(tmp_path: Path):
    source = tmp_path / "attachments.pdf"
    document = pymupdf.open()
    document.new_page()
    document.embfile_add("private.txt", b"Private attachment", filename="private.txt")
    document.save(source)
    document.close()
    with pytest.raises(MetadataSanitizationError, match="attachments"):
        assert_metadata_surfaces_supported(source)


def test_pdf_links_are_explicitly_rejected(tmp_path: Path):
    source = tmp_path / "links.pdf"
    document = pymupdf.open()
    page = document.new_page()
    page.insert_link({
        "kind": pymupdf.LINK_URI,
        "from": pymupdf.Rect(30, 30, 150, 60),
        "uri": "https://example.test/private/john",
    })
    document.save(source)
    document.close()
    with pytest.raises(MetadataSanitizationError, match="PDF links"):
        assert_metadata_surfaces_supported(source)


def test_xlsx_custom_properties_are_explicitly_rejected(tmp_path: Path):
    source = tmp_path / "custom.xlsx"
    workbook = Workbook()
    workbook.active["A1"] = "data"
    workbook.save(source)
    workbook.close()
    _append_zip_part(
        source,
        "docProps/custom.xml",
        b'<Properties xmlns="http://schemas.openxmlformats.org/officeDocument/2006/custom-properties"/>',
    )
    with pytest.raises(MetadataSanitizationError, match="custom document properties"):
        assert_metadata_surfaces_supported(source)


def test_metadata_snapshot_requires_valid_fingerprints():
    vault = _make_vault()
    snapshot = {
        "schema_version": 1,
        "format": "docx",
        "source_sha256": "a" * 64,
        "masked_sha256": "not-a-hash",
        "parts": {
            "docProps/core.xml": None,
            "docProps/app.xml": None,
            "docProps/thumbnail.jpeg": None,
        },
    }
    with pytest.raises(ValueError, match="masked_sha256"):
        vault.set_metadata_snapshot(snapshot)
