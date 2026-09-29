"""Turn 1: read-only OPC (Open Packaging Conventions) reader for ``.docx``.

``zipfile`` + ``lxml`` only (design D1: no python-docx in the library). Everything
downstream walks the parts this module hands it; nothing downstream touches the zip.

Three producer-variance traps the reader exists to absorb. All three were found by
running the Turn 0b spike over real packages (``docs/design/opc-spike.md`` sites 1-5);
the first two fail **silently** if unhandled, which is why they are load-bearing:

1. **Parts are selected by relationship type and content type, never by path.**
   ``word/commentsExtended.xml`` may be renamed (``word/ext/commentsExt.xml``) or
   absent, and ``word/document.xml`` itself is found through the ``officeDocument``
   relationship. A path-based reader needs a fallback list and still misses.
2. **ISO-strict packages use different URIs.** Strict writes relationship *types*
   under ``http://purl.oclc.org/ooxml/...`` and strict ``w``/``r`` namespaces differ
   from transitional ones for elements **and attributes**. A strictly transitional
   reader returns an empty document here, not an error. Canonicalization happens once,
   in this module, so no downstream comparison ever sees a raw strict URI.
3. **Relative targets.** ``../customXml/item1.xml`` is a legal target; a naive
   ``base + target`` join drops the part with no diagnostic.

Addressability (design D2/D3): a part's :attr:`Part.part_id` is stable and
path-independent -- its relationship type plus an ordinal -- so a renamed part keeps
its id and a span address never encodes a zip path.
"""
from __future__ import annotations

import posixpath
import zipfile
from dataclasses import dataclass, field
from pathlib import Path

from lxml import etree

# --- the one internal namespace map ------------------------------------------
# Every part's namespaces are normalized to these. Strict URIs are never allowed to
# leak past canonical_namespace(): the walker, the term matcher and the codec all
# assume one map.

W_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
R_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
A_NS = "http://schemas.openxmlformats.org/drawingml/2006/main"
WP_NS = "http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing"
MC_NS = "http://schemas.openxmlformats.org/markup-compatibility/2006"
W14_NS = "http://schemas.microsoft.com/office/word/2010/wordml"
W15_NS = "http://schemas.microsoft.com/office/word/2012/wordml"
WPS_NS = "http://schemas.microsoft.com/office/word/2010/wordprocessingShape"
VML_NS = "urn:schemas-microsoft-com:vml"

#: Prefix -> canonical (transitional) URI. The internal map.
NS = {
    "w": W_NS,
    "r": R_NS,
    "a": A_NS,
    "wp": WP_NS,
    "mc": MC_NS,
    "w14": W14_NS,
    "w15": W15_NS,
    "wps": WPS_NS,
    "v": VML_NS,
}

STRICT_W_NS = "http://purl.oclc.org/ooxml/wordprocessingml/main"
STRICT_R_NS = "http://purl.oclc.org/ooxml/officeDocument/relationships"

#: The ``w`` namespaces a reader must accept, strict and transitional.
W_URIS = frozenset({W_NS, STRICT_W_NS})

#: ISO/IEC 29500 strict URI -> the canonical transitional URI above. Only suites
#: that have a strict form are listed; anything else is left untouched, because
#: guessing a rewrite silently corrupts an extension namespace.
STRICT_NAMESPACES = {
    STRICT_W_NS: W_NS,
    STRICT_R_NS: R_NS,
    "http://purl.oclc.org/ooxml/drawingml/main": A_NS,
    "http://purl.oclc.org/ooxml/drawingml/wordprocessingDrawing": WP_NS,
}

REL_PREFIX = "http://schemas.openxmlformats.org/officeDocument/2006/relationships/"
STRICT_REL_PREFIX = "http://purl.oclc.org/ooxml/officeDocument/relationships/"

# --- relationship types and content types we resolve -------------------------
# Wordprocessing parts are reached through document-level relationships; the
# Microsoft ones (commentsExtended, stylesWithEffects) are not in the OOXML suite.
RT_OFFICE_DOCUMENT = REL_PREFIX + "officeDocument"
RT_STYLES = REL_PREFIX + "styles"
RT_NUMBERING = REL_PREFIX + "numbering"
RT_SETTINGS = REL_PREFIX + "settings"
RT_COMMENTS = REL_PREFIX + "comments"
RT_FOOTNOTES = REL_PREFIX + "footnotes"
RT_ENDNOTES = REL_PREFIX + "endnotes"
RT_HEADER = REL_PREFIX + "header"
RT_FOOTER = REL_PREFIX + "footer"
RT_HYPERLINK = REL_PREFIX + "hyperlink"
RT_CUSTOM_XML = REL_PREFIX + "customXml"
#: Microsoft extension rel types, used by Word but not in the OOXML suite.
RT_COMMENTS_EXTENDED = "http://schemas.microsoft.com/office/2011/relationships/commentsExtended"

CT_DOCUMENT = "application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"
CT_COMMENTS_EXTENDED = "application/vnd.openxmlformats-officedocument.wordprocessingml.commentsExtended+xml"

CONTENT_TYPES_PART = "[Content_Types].xml"
PACKAGE_RELS_PART = "_rels/.rels"

XML_SUFFIXES = (".xml", ".rels")


class OpcError(Exception):
    """A package that cannot be read as OPC. Never a silently empty document."""


# --- untrusted-input hardening -------------------------------------------------
# Documents arrive from outside senders. OOXML never legitimately uses a DTD, so a
# ``<!DOCTYPE`` is rejected outright, and the one parser below is used for every
# parse in the package. Safety must not depend on library defaults.

#: Ceilings on what one package may expand to. Generous for real Word documents,
#: small enough that a zip bomb is refused before it is read into memory.
MAX_TOTAL_BYTES = 256 * 1024 * 1024
MAX_MEMBER_BYTES = 64 * 1024 * 1024

_SAFE_PARSER = etree.XMLParser(
    resolve_entities=False,
    no_network=True,
    huge_tree=False,
    load_dtd=False,
    dtd_validation=False,
    remove_comments=False,
)


def parse_xml(data: bytes, *, name: str = "<xml>") -> etree._Element:
    """Parse one XML part with the hardened parser; malformed or DTD input is an OpcError."""
    if b"<!DOCTYPE" in data or b"<!ENTITY" in data:
        raise OpcError(f"{name}: a DOCTYPE/ENTITY declaration is not allowed in an OOXML part")
    try:
        return etree.fromstring(data, parser=_SAFE_PARSER)
    except etree.XMLSyntaxError as exc:
        raise OpcError(f"{name}: not well-formed XML ({exc})") from exc


# --- namespace helpers -------------------------------------------------------


def canonical_namespace(uri: str | None) -> str | None:
    """Map a strict namespace URI to its transitional equivalent, else unchanged."""
    if uri is None:
        return None
    return STRICT_NAMESPACES.get(uri, uri)


def canonical_rel_type(rtype: str) -> str:
    """Canonicalize a relationship type, so strict and transitional agree."""
    if rtype.startswith(STRICT_REL_PREFIX):
        return REL_PREFIX + rtype[len(STRICT_REL_PREFIX) :]
    return rtype


def normalize_nsmap(root: etree._Element) -> dict[str, str]:
    """A part root's namespace declarations, canonicalized to :data:`NS` values."""
    return {prefix or "": canonical_namespace(uri) for prefix, uri in root.nsmap.items()}


def local_name(element: etree._Element) -> str:
    """The local name of an element, namespace-independent."""
    return etree.QName(element).localname


def is_w(element: etree._Element, local: str) -> bool:
    """True for a ``w:``-family element with this local name, strict or transitional."""
    if not isinstance(element.tag, str):
        return False
    qname = etree.QName(element)
    return qname.localname == local and qname.namespace in W_URIS


def wattr(element: etree._Element, local: str) -> str | None:
    """A ``w:``-family attribute value by local name.

    Strict and transitional packages namespace *attributes* differently too, so a
    caller must never match ``"{...}val"`` literally. A bare local-name match is the
    last resort for an attribute written without a namespace.
    """
    bare = None
    for key, value in element.attrib.items():
        qname = etree.QName(key)
        if qname.localname != local:
            continue
        if qname.namespace in W_URIS:
            return value
        if qname.namespace is None:
            bare = value
    return bare


# --- OPC records -------------------------------------------------------------


@dataclass(frozen=True)
class Relationship:
    """One entry of a ``.rels`` part. ``type`` is canonicalized on read."""

    id: str
    type: str
    target: str
    target_mode: str | None = None
    part_name: str | None = None

    @property
    def external(self) -> bool:
        return self.part_name is None


@dataclass(frozen=True)
class Part:
    """One reachable package part.

    ``part_id`` is ``<relationship type local name>:<ordinal>`` -- stable across a
    rename and independent of the zip path (design D3). ``tree`` is the parsed root
    for an XML part and ``None`` for a binary part (a thumbnail is enumerated and
    must never be parsed as XML). ``empty`` means "exists but has no content".
    """

    part_id: str
    name: str
    content_type: str
    rel_type: str
    data: bytes = field(repr=False, compare=False)
    tree: etree._Element | None = field(default=None, repr=False, compare=False)

    @property
    def is_xml(self) -> bool:
        return self.tree is not None

    @property
    def empty(self) -> bool:
        return self.tree is not None and len(self.tree) == 0

    @property
    def has_text(self) -> bool:
        """True when the part carries any non-whitespace text.

        "Exists" and "has content" are different: a separator-only footnotes part is
        present, non-``empty`` (it has child elements) and has no text.
        """
        return self.tree is not None and bool("".join(self.tree.itertext()).strip())

    @property
    def nsmap(self) -> dict[str, str]:
        return normalize_nsmap(self.tree) if self.tree is not None else {}

    @property
    def root_namespace(self) -> str | None:
        if self.tree is None:
            return None
        return canonical_namespace(etree.QName(self.tree).namespace)


@dataclass(frozen=True)
class ContentTypes:
    """``[Content_Types].xml``: ``Default`` by extension, ``Override`` by part name."""

    defaults: dict[str, str]
    overrides: dict[str, str]

    def for_part(self, name: str) -> str:
        if name in self.overrides:
            return self.overrides[name]
        ext = name.rsplit(".", 1)[-1].lower() if "." in name else ""
        return self.defaults.get(ext, "")


def _rels_part_for(part_name: str) -> str:
    """The ``.rels`` part belonging to ``part_name`` (OPC source-relative naming)."""
    directory, base = posixpath.split(part_name)
    return posixpath.join(directory, "_rels", base + ".rels")


def resolve_target(source_part: str | None, target: str) -> str:
    """Normalize an internal relationship target to a package part name.

    Handles a leading ``/`` (package-absolute) and ``.``/``..`` segments; the
    spike's ``../customXml/item1.xml`` from ``word/document.xml`` resolves to
    ``customXml/item1.xml`` only through this (opc-spike.md site 5).
    """
    if target.startswith("/"):
        return posixpath.normpath(target.lstrip("/"))
    base = posixpath.dirname(source_part) if source_part else ""
    return posixpath.normpath(posixpath.join(base, target))


class Package:
    """A ``.docx`` read as OPC: reachable parts, their rels and their content types.

    Parts are the *reachable* graph (root rels, then each reached part's rels, in the
    order the package lists them). ``names`` is every non-directory zip member, so a
    caller can tell "not part of the graph" from "absent".
    """

    def __init__(
        self,
        path: str | Path,
        *,
        max_total_bytes: int = MAX_TOTAL_BYTES,
        max_member_bytes: int = MAX_MEMBER_BYTES,
    ):
        self.path = Path(path)
        try:
            archive = zipfile.ZipFile(self.path)
        except zipfile.BadZipFile as exc:
            raise OpcError(f"{self.path}: not a zip package ({exc})") from exc
        with archive:
            infos = [i for i in archive.infolist() if not i.filename.endswith("/")]
            self.names: tuple[str, ...] = tuple(i.filename for i in infos)
            if sum(i.file_size for i in infos) > max_total_bytes:
                raise OpcError(f"{self.path}: package expands past {max_total_bytes} bytes")
            self._members: dict[str, bytes] = {}
            total = 0
            for info in infos:
                # read with a hard limit, and surface a corrupt member as an OpcError
                try:
                    with archive.open(info) as handle:
                        data = handle.read(max_member_bytes + 1)
                except (zipfile.BadZipFile, NotImplementedError, RuntimeError) as exc:
                    raise OpcError(f"{self.path}: cannot read member {info.filename} ({exc})") from exc
                if len(data) > max_member_bytes:
                    raise OpcError(f"{self.path}: member {info.filename} exceeds {max_member_bytes} bytes")
                total += len(data)
                if total > max_total_bytes:
                    raise OpcError(f"{self.path}: package expands past {max_total_bytes} bytes")
                self._members[info.filename] = data
        self.content_types = self._read_content_types()
        self._rels: dict[str | None, tuple[Relationship, ...]] = {}
        self.parts: dict[str, Part] = {}
        self._document: Part | None = None
        self._build()

    # -- loading --------------------------------------------------------------

    def _read_content_types(self) -> ContentTypes:
        raw = self._members.get(CONTENT_TYPES_PART)
        if raw is None:
            raise OpcError(f"{self.path}: no {CONTENT_TYPES_PART}")
        root = parse_xml(raw, name=CONTENT_TYPES_PART)
        defaults: dict[str, str] = {}
        overrides: dict[str, str] = {}
        for element in root.iter():
            if not isinstance(element.tag, str):
                continue
            local = etree.QName(element).localname
            if local == "Default":
                defaults[element.get("Extension", "").lower()] = element.get("ContentType", "")
            elif local == "Override":
                overrides[element.get("PartName", "").lstrip("/")] = element.get("ContentType", "")
        return ContentTypes(defaults=defaults, overrides=overrides)

    def relationships(self, source: str | None = None) -> tuple[Relationship, ...]:
        """The relationships of ``source``, or of the package root when ``None``."""
        if source not in self._rels:
            rels_part = PACKAGE_RELS_PART if source is None else _rels_part_for(source)
            raw = self._members.get(rels_part)
            self._rels[source] = () if raw is None else self._parse_rels(source, raw)
        return self._rels[source]

    def _parse_rels(self, source: str | None, raw: bytes) -> tuple[Relationship, ...]:
        root = parse_xml(raw, name=f"rels of {source or 'package'}")
        out: list[Relationship] = []
        for element in root.iter():
            if not isinstance(element.tag, str):
                continue
            if etree.QName(element).localname != "Relationship":
                continue
            target = element.get("Target", "")
            mode = element.get("TargetMode")
            external = mode == "External" or "://" in target
            out.append(
                Relationship(
                    id=element.get("Id", ""),
                    type=canonical_rel_type(element.get("Type", "")),
                    target=target,
                    target_mode=mode,
                    part_name=None if external else resolve_target(source, target),
                )
            )
        return tuple(out)

    def _build(self) -> None:
        """Walk the relationship graph, assigning a stable ``part_id`` per part.

        The ordinal counts **distinct part names** of a type in resolution order, so
        two relationships to one header yield one part with one id.
        """
        part_id_of: dict[str, str] = {}
        rel_type_of: dict[str, str] = {}
        counters: dict[str, int] = {}

        def assign(rels: tuple[Relationship, ...]) -> list[str]:
            reached = []
            for rel in rels:
                name = rel.part_name
                if name is None or name in rel_type_of or name not in self._members:
                    continue
                local = rel.type.rsplit("/", 1)[-1] or rel.type
                ordinal = counters.get(local, 0)
                counters[local] = ordinal + 1
                rel_type_of[name] = rel.type
                part_id_of[name] = f"{local}:{ordinal}"
                reached.append(name)
            return reached

        frontier = assign(self.relationships(None))
        while frontier:
            following: list[str] = []
            for name in frontier:
                following.extend(assign(self.relationships(name)))
            frontier = following

        for name, part_id in part_id_of.items():
            data = self._members[name]
            tree = parse_xml(data, name=name) if self._is_xml_name(name) else None
            self.parts[name] = Part(
                part_id=part_id,
                name=name,
                content_type=self.content_types.for_part(name),
                rel_type=rel_type_of[name],
                data=data,
                tree=tree,
            )

    @staticmethod
    def _is_xml_name(name: str) -> bool:
        return name.endswith(XML_SUFFIXES)

    # -- lookup ---------------------------------------------------------------

    def part(self, name: str) -> Part | None:
        """A reachable part by package name, or ``None`` if it is not in the graph."""
        return self.parts.get(name)

    def related(self, rel_type: str, source: str | None = None, ordinal: int = 0) -> Part | None:
        """The ``ordinal``-th internal part of ``rel_type``, or ``None`` if absent.

        An absent part is ``None``, never an exception (a document legitimately has no
        comments, no ``commentsExtended``, no footnotes).
        """
        matches = [
            rel.part_name
            for rel in self.relationships(source)
            if rel.type == rel_type and rel.part_name is not None
        ]
        if ordinal >= len(matches):
            return None
        return self.parts.get(matches[ordinal])

    def related_all(self, rel_type: str, source: str | None = None) -> list[Part]:
        """Every distinct internal part of ``rel_type``, in resolution order."""
        found: list[Part] = []
        seen: set[str] = set()
        for rel in self.relationships(source):
            if rel.type != rel_type or rel.part_name is None or rel.part_name in seen:
                continue
            seen.add(rel.part_name)
            part = self.parts.get(rel.part_name)
            if part is not None:
                found.append(part)
        return found

    @property
    def document(self) -> Part:
        """The main document part, found via the ``officeDocument`` relationship.

        A package whose ``officeDocument`` relationship is unresolvable is an error,
        never an empty document (opc-spike.md site 2).
        """
        if self._document is None:
            part = self.related(RT_OFFICE_DOCUMENT, None)
            if part is None:
                raise OpcError(f"{self.path}: no officeDocument relationship")
            self._document = part
        return self._document

    def document_related(self, rel_type: str, ordinal: int = 0) -> Part | None:
        return self.related(rel_type, self.document.name, ordinal)

    def document_related_all(self, rel_type: str) -> list[Part]:
        return self.related_all(rel_type, self.document.name)

    @property
    def styles(self) -> Part | None:
        return self.document_related(RT_STYLES)

    @property
    def numbering(self) -> Part | None:
        return self.document_related(RT_NUMBERING)

    @property
    def settings(self) -> Part | None:
        return self.document_related(RT_SETTINGS)

    @property
    def comments(self) -> Part | None:
        return self.document_related(RT_COMMENTS)

    @property
    def comments_extended(self) -> Part | None:
        return self.document_related(RT_COMMENTS_EXTENDED)

    @property
    def footnotes(self) -> Part | None:
        return self.document_related(RT_FOOTNOTES)

    @property
    def endnotes(self) -> Part | None:
        return self.document_related(RT_ENDNOTES)

    def headers(self) -> list[Part]:
        return self.document_related_all(RT_HEADER)

    def footers(self) -> list[Part]:
        return self.document_related_all(RT_FOOTER)
