"""Turn 1: the read-only OPC reader.

Every fixture is opened, including the 0a model package (and a machine-local
Word-style sample when one is present). The tests that matter pin the producer-variance sites the spike found
(``docs/design/opc-spike.md``): a renamed part, strict namespaces, a relative
``..`` target, and an absent optional part returning ``None`` rather than raising.
"""
from __future__ import annotations

import zipfile
from pathlib import Path

import pytest

from real_sample import REAL_SAMPLE, requires_real_sample
from lxml import etree

from wordextract import opc
from wordextract.opc import OpcError, Package
from wordextract.evals import labels

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
MODEL = FIXTURES / "model"

ALL_DOCX = sorted(p for p in FIXTURES.rglob("*.docx"))
MODEL_NAMES = sorted(p.name[: -len(".expected.json")] for p in MODEL.glob("*.expected.json"))


def _sidecar(name: str) -> labels.Sidecar:
    for base in (FIXTURES, MODEL):
        path = base / f"{name}.expected.json"
        if path.exists():
            return labels.load_sidecar(path)
    raise AssertionError(f"no sidecar for {name}")


# --- every fixture opens -----------------------------------------------------


@pytest.mark.parametrize("path", ALL_DOCX, ids=lambda p: p.name)
def test_every_fixture_opens_and_has_a_document(path):
    package = Package(path)
    document = package.document
    assert document.name in package.names
    assert document.is_xml
    assert document.rel_type == opc.RT_OFFICE_DOCUMENT
    assert package.parts, "no parts reached"


@pytest.mark.parametrize("path", ALL_DOCX, ids=lambda p: p.name)
def test_every_part_is_actually_in_the_zip(path):
    package = Package(path)
    for name, part in package.parts.items():
        assert name in package.names, name
        assert part.data == _member(path, name)


def _member(docx: Path, name: str) -> bytes:
    with zipfile.ZipFile(docx) as archive:
        return archive.read(name)


# --- resolution is by relationship type and content type, never by path ------


def test_document_is_found_via_the_officedocument_relationship():
    package = Package(MODEL / "strict_namespaces.docx")
    assert package.document.name == "word/document.xml"
    assert package.document.part_id == "officeDocument:0"


def test_strict_namespaces_are_canonicalized():
    """opc-spike site 2/3: a transitional-only reader returns an empty document."""
    sidecar = _sidecar("strict_namespaces")
    package = Package(MODEL / "strict_namespaces.docx")
    document = package.document
    # element namespaces normalized to the transitional map
    assert document.root_namespace == opc.W_NS
    assert document.nsmap["w"] == opc.W_NS
    assert document.nsmap["r"] == opc.R_NS
    assert opc.is_w(document.tree[0], "body")
    # the rel type canonicalized too, so it compares equal to the transitional constant
    assert document.rel_type == opc.RT_OFFICE_DOCUMENT
    assert sidecar.annotations["parts"][0]["content_type"] == document.content_type


def test_strict_wattr_reads_a_namespaced_attribute():
    package = Package(MODEL / "strict_namespaces.docx")
    body = package.document.tree[0]
    paragraph = body[1]
    deleted = next(e for e in paragraph if opc.is_w(e, "del"))
    assert opc.wattr(deleted, "id") == "2"
    assert opc.wattr(deleted, "author") == "A. Ito"
    assert opc.wattr(deleted, "date") == "2026-01-01T00:00:00Z"
    assert opc.wattr(deleted, "missing") is None


def test_renamed_comments_extended_is_resolved_by_relationship_type():
    """opc-spike site 4: the part is at ext/commentsExt.xml, not commentsExtended.xml."""
    sidecar = _sidecar("renamed_comments_extended")
    package = Package(MODEL / "renamed_comments_extended.docx")
    part = package.comments_extended
    assert part is not None
    assert part.name == "word/ext/commentsExt.xml"
    annotated = {p["path"]: p for p in sidecar.annotations["parts"]}
    assert part.content_type == opc.CT_COMMENTS_EXTENDED
    assert part.content_type == annotated[part.name]["content_type"]
    assert part.rel_type == annotated[part.name]["rel_type"]
    # the same part is reached under its canonical id anywhere else
    assert part.part_id == Package(FIXTURES / "spec_threaded.docx").comments_extended.part_id


def test_part_id_is_path_independent():
    """A renamed part keeps its id; distinct parts of a type get distinct ordinals."""
    renamed = Package(MODEL / "renamed_comments_extended.docx")
    threaded = Package(FIXTURES / "spec_threaded.docx")
    assert renamed.comments_extended.part_id == threaded.comments_extended.part_id
    assert renamed.comments.part_id == threaded.comments.part_id
    assert renamed.comments_extended.part_id != renamed.comments.part_id
    assert renamed.document.part_id == threaded.document.part_id == "officeDocument:0"


def test_two_relationships_to_one_part_yield_one_part():
    for path in ALL_DOCX:
        package = Package(path)
        ids = [part.part_id for part in package.parts.values()]
        assert len(ids) == len(set(ids)), path
        assert len(set(package.parts)) == len(package.parts)


# --- absent and empty optional parts ----------------------------------------


def test_absent_part_is_none_not_an_exception():
    package = Package(MODEL / "breaks_and_specials.docx")
    assert package.comments is None
    assert package.comments_extended is None
    assert package.footnotes is None
    assert package.endnotes is None
    assert package.numbering is None
    assert package.related(opc.RT_STYLES) is None
    assert package.document_related(opc.RT_HEADER) is None
    assert package.part("word/nope.xml") is None


def test_empty_part_is_present_but_empty():
    """opc-spike site 11: "part exists" must not mean "has content"."""
    sidecar = _sidecar("empty_parts")
    package = Package(MODEL / "empty_parts.docx")
    for expected in sidecar.annotations["parts"]:
        part = package.part(expected["path"])
        assert part is not None, expected["path"]
        assert part.empty is expected["empty"]
        assert part.content_type == expected["content_type"]
        assert part.rel_type == expected["rel_type"]


@requires_real_sample
def test_real_sample_has_separator_only_note_parts():
    package = Package(REAL_SAMPLE)
    # the sample's footnotes/endnotes carry separator runs, so they are non-empty
    assert package.footnotes is not None and not package.footnotes.empty
    assert package.endnotes is not None and not package.endnotes.empty
    assert package.comments is not None and not package.comments.empty
    # ... and it reaches the sample's distinct styles part
    assert package.styles is not None
    assert package.styles.name == "word/styles.xml"


# --- targets, directory entries, non-XML parts -------------------------------


def test_relative_target_with_parent_segment_is_resolved():
    """opc-spike site 5: ../customXml/item1.xml resolves to customXml/item1.xml."""
    package = Package(FIXTURES / "program_review_v3.docx")
    rel = next(r for r in package.relationships(package.document.name) if r.type == opc.RT_CUSTOM_XML)
    assert rel.target == "../customXml/item1.xml"
    assert rel.part_name == "customXml/item1.xml"
    assert package.part("customXml/item1.xml") is not None


@pytest.mark.parametrize(
    ("source", "target", "expected"),
    [
        ("word/document.xml", "../customXml/item1.xml", "customXml/item1.xml"),
        ("word/document.xml", "header1.xml", "word/header1.xml"),
        ("word/document.xml", "/word/styles.xml", "word/styles.xml"),
        ("word/ext/x.xml", "../comments.xml", "word/comments.xml"),
        (None, "word/document.xml", "word/document.xml"),
        ("word/document.xml", "./styles.xml", "word/styles.xml"),
    ],
)
def test_resolve_target(source, target, expected):
    assert opc.resolve_target(source, target) == expected


def test_external_relationship_has_no_part():
    package = Package(MODEL / "hyperlink_and_fields.docx")
    sidecar = _sidecar("hyperlink_and_fields")
    rel = next(r for r in package.relationships(package.document.name) if r.type == opc.RT_HYPERLINK)
    expected = sidecar.annotations["doc_rels"][0]
    assert rel.id == expected["id"]
    assert rel.target == expected["target"]
    assert rel.target_mode == expected["mode"]
    assert rel.external and rel.part_name is None
    assert package.part(rel.target) is None


@requires_real_sample
def test_directory_entries_are_tolerated():
    document = REAL_SAMPLE
    with zipfile.ZipFile(document) as archive:
        assert any(name.endswith("/") for name in archive.namelist())
    package = Package(document)
    assert not any(name.endswith("/") for name in package.names)
    assert not any(name.endswith("/") for name in package.parts)


def test_non_xml_part_is_enumerated_but_not_parsed():
    package = Package(FIXTURES / "program_review_v3.docx")
    thumbnail = package.part("docProps/thumbnail.jpeg")
    assert thumbnail is not None
    assert thumbnail.tree is None and not thumbnail.is_xml
    assert not thumbnail.empty


def test_content_types_default_and_override():
    package = Package(FIXTURES / "program_review_v3.docx")
    types = package.content_types
    assert types.for_part("docProps/thumbnail.jpeg") == "image/jpeg"
    assert types.for_part("word/document.xml") == opc.CT_DOCUMENT
    assert types.for_part("word/nothing.zzz") == ""


def test_every_xml_part_parses_and_declares_a_root_namespace():
    for path in ALL_DOCX:
        package = Package(path)
        for name, part in package.parts.items():
            if name.endswith((".xml", ".rels")):
                assert part.tree is not None, (path, name)
                assert part.is_xml
            else:
                assert part.tree is None
                assert not part.is_xml


def test_headers_and_footers_are_all_reachable():
    package = Package(FIXTURES / "program_review_v3.docx")
    assert [p.name for p in package.headers()] == ["word/header1.xml"]
    assert [p.name for p in package.footers()] == ["word/footer1.xml"]


# --- damaged packages are errors, never a silently empty document ------------


def _write_zip(path: Path, members: dict[str, bytes]) -> Path:
    with zipfile.ZipFile(path, "w") as archive:
        for name, data in members.items():
            archive.writestr(name, data)
    return path


def _broken(tmp_path: Path, **overrides) -> Path:
    members = {
        "[Content_Types].xml": (
            b'<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
            b'<Default Extension="xml" ContentType="application/xml"/>'
            b'<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
            b'</Types>'
        ),
        "_rels/.rels": (
            b'<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            b'<Relationship Id="rId1" '
            b'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" '
            b'Target="word/document.xml"/></Relationships>'
        ),
        "word/document.xml": b'<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"/>',
    }
    members.update(overrides)
    return _write_zip(tmp_path / "broken.docx", members)


def test_missing_officedocument_relationship_is_an_error(tmp_path):
    path = _broken(
        tmp_path,
        **{
            "_rels/.rels": (
                b'<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"/>'
            )
        },
    )
    with pytest.raises(OpcError):
        Package(path).document


def test_missing_content_types_is_an_error(tmp_path):
    members = {
        n: _member(FIXTURES / "spec_threaded.docx", n)
        for n in zipfile.ZipFile(FIXTURES / "spec_threaded.docx").namelist()
    }
    del members["[Content_Types].xml"]
    with pytest.raises(OpcError):
        Package(_write_zip(tmp_path / "noct.docx", members))


def test_office_document_targeting_a_missing_part_is_an_error(tmp_path):
    """opc-spike site 2 shape: a resolvable rel whose part is not in the zip."""
    members = {
        "[Content_Types].xml": _member(FIXTURES / "spec_threaded.docx", "[Content_Types].xml"),
        "_rels/.rels": _member(FIXTURES / "spec_threaded.docx", "_rels/.rels"),
    }
    with pytest.raises(OpcError):
        Package(_write_zip(tmp_path / "dangling.docx", members)).document


# --- the canonical namespace map is the only one a caller sees --------------


def test_normalize_nsmap_canonicalizes_strict_uris():
    strict = {
        "w": opc.STRICT_W_NS,
        "r": opc.STRICT_R_NS,
        "w14": opc.W14_NS,
    }
    root = etree.fromstring(
        b'<w:document xmlns:w="%s" xmlns:r="%s" xmlns:w14="%s"/>'
        % (opc.STRICT_W_NS.encode(), opc.STRICT_R_NS.encode(), opc.W14_NS.encode())
    )
    assert root.nsmap["w"] == opc.STRICT_W_NS  # raw lxml still sees strict
    assert opc.normalize_nsmap(root) == {"w": opc.W_NS, "r": opc.R_NS, "w14": opc.W14_NS}
    assert strict["w"] == opc.STRICT_W_NS


def test_canonical_namespace_leaves_unknown_uris_alone():
    assert opc.canonical_namespace(None) is None
    assert opc.canonical_namespace(opc.W14_NS) == opc.W14_NS
    assert opc.canonical_namespace(opc.STRICT_W_NS) == opc.W_NS


def test_canonical_rel_type_maps_strict_to_transitional():
    strict = "http://purl.oclc.org/ooxml/officeDocument/relationships/officeDocument"
    assert opc.canonical_rel_type(strict) == opc.RT_OFFICE_DOCUMENT
    assert opc.canonical_rel_type(opc.RT_COMMENTS) == opc.RT_COMMENTS
    assert opc.canonical_rel_type("urn:example:custom") == "urn:example:custom"


def test_no_raw_strict_uri_leaks_into_a_part():
    package = Package(MODEL / "strict_namespaces.docx")
    for part in package.parts.values():
        assert "purl.oclc.org" not in part.rel_type
        if part.nsmap:
            assert all("purl.oclc.org" not in uri for uri in part.nsmap.values())


def test_local_name_and_is_w_consume_the_canonical_map():
    package = Package(MODEL / "strict_namespaces.docx")
    body = package.document.tree[0]
    assert opc.local_name(body) == "body"
    assert opc.is_w(body, "body")
    assert not opc.is_w(body, "notBody")


# --- untrusted-input hardening ------------------------------------------------


def _with_document(tmp_path, document_xml: bytes, name="evil.docx"):
    src = FIXTURES / "spec_threaded.docx"
    members = {n: _member(src, n) for n in zipfile.ZipFile(src).namelist()}
    members["word/document.xml"] = document_xml
    return _write_zip(tmp_path / name, members)


_W = b'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"'


def test_external_entity_does_not_leak_a_local_file(tmp_path):
    secret = tmp_path / "secret.txt"
    secret.write_text("TOPSECRET-LOCAL-FILE", encoding="utf-8")
    doc = (
        f'<?xml version="1.0"?><!DOCTYPE d [<!ENTITY xxe SYSTEM "{secret.as_uri()}">]>'
        f"<w:document {_W.decode()}><w:body><w:p><w:r><w:t>&xxe;</w:t></w:r></w:p></w:body></w:document>"
    ).encode()
    with pytest.raises(OpcError, match="DOCTYPE"):
        Package(_with_document(tmp_path, doc))


def test_entity_expansion_bomb_is_rejected(tmp_path):
    ents = '<!ENTITY a0 "aaaaaaaaaa">' + "".join(
        f'<!ENTITY a{i} "' + f"&a{i - 1};" * 10 + '">' for i in range(1, 8)
    )
    doc = (
        f"<?xml version=\"1.0\"?><!DOCTYPE d [{ents}]>"
        f"<w:document {_W.decode()}><w:body><w:p><w:r><w:t>&a7;</w:t></w:r></w:p></w:body></w:document>"
    ).encode()
    with pytest.raises(OpcError):
        Package(_with_document(tmp_path, doc))


def test_doctype_is_rejected_in_any_part(tmp_path):
    src = FIXTURES / "spec_threaded.docx"
    members = {n: _member(src, n) for n in zipfile.ZipFile(src).namelist()}
    members["[Content_Types].xml"] = b"<!DOCTYPE x []>" + members["[Content_Types].xml"]
    with pytest.raises(OpcError, match="DOCTYPE"):
        Package(_write_zip(tmp_path / "ct.docx", members))


def test_malformed_xml_is_an_opc_error_not_a_raw_lxml_error(tmp_path):
    with pytest.raises(OpcError, match="not well-formed"):
        Package(_with_document(tmp_path, b"<w:document"))


def test_not_a_zip_is_an_opc_error(tmp_path):
    bogus = tmp_path / "bogus.docx"
    bogus.write_bytes(b"this is not a zip")
    with pytest.raises(OpcError, match="not a zip"):
        Package(bogus)


def test_parse_xml_uses_one_hardened_parser():
    tree = opc.parse_xml(b"<a><b/></a>")
    assert tree.tag == "a"
    with pytest.raises(OpcError):
        opc.parse_xml(b'<!DOCTYPE a [<!ENTITY e "x">]><a>&e;</a>')


def test_oversized_package_is_refused(tmp_path):
    src = FIXTURES / "spec_threaded.docx"
    with pytest.raises(OpcError, match="expands past"):
        Package(src, max_total_bytes=100)


def test_oversized_member_is_refused(tmp_path):
    src = FIXTURES / "spec_threaded.docx"
    with pytest.raises(OpcError, match="exceeds"):
        Package(src, max_member_bytes=100)


def test_corrupt_member_is_an_opc_error(tmp_path):
    """A header that lies about the uncompressed size makes the member unreadable;
    that is an OpcError, never a raw zipfile exception."""
    payload = b"<a>" + b"x" * 5000 + b"</a>"
    path = tmp_path / "lie.zip"
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("big.xml", payload)
    raw = bytearray(path.read_bytes())
    # patch the declared uncompressed size (central directory + local header) to 10
    import struct

    size = struct.pack("<I", len(payload))
    fake = struct.pack("<I", 10)
    while size in raw:
        raw[raw.index(size) : raw.index(size) + 4] = fake
    path.write_bytes(bytes(raw))
    with pytest.raises(OpcError, match="cannot read member"):
        Package(path, max_member_bytes=1000, max_total_bytes=10_000)


# --- has_text vs exists --------------------------------------------------------


@requires_real_sample
def test_separator_only_note_parts_have_no_text():
    package = Package(REAL_SAMPLE)
    assert package.footnotes is not None and not package.footnotes.empty
    assert package.footnotes.has_text is False
    assert package.endnotes is not None and package.endnotes.has_text is False
    assert package.comments is not None and package.comments.has_text is True
    assert package.document.has_text is True


def test_binary_part_has_no_text():
    package = Package(FIXTURES / "program_review_v3.docx")
    binaries = [p for p in package.parts.values() if not p.is_xml]
    for part in binaries:
        assert part.has_text is False
