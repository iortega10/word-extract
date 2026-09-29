"""Turn 3: fixture sidecars (``<name>.expected.json``) as generator ground truth.

The sidecars are re-derived here straight from the OOXML with ``zipfile`` + ``lxml``,
never python-docx -- python-docx drops text inside ``w:ins`` (and ``w:delText``), so it
would silently agree with a wrong sidecar.
"""
import hashlib
import json
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest
from lxml import etree

from wordextract.evals import labels

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
import make_fixtures  # noqa: E402


def _w(tag):
    return f"{{{W}}}{tag}"


def _part(docx, name):
    with zipfile.ZipFile(docx) as z:
        return z.read(name)


def _body(docx):
    return etree.fromstring(_part(docx, "word/document.xml")).find(_w("body"))


def _sidecar(name):
    return json.loads((FIXTURES / f"{name}.expected.json").read_text(encoding="utf-8"))


SYNTHETIC = ["program_review_v3", "binder_summary", "edge_cases"]
ALL = SYNTHETIC + ["spec_threaded"]


@pytest.mark.parametrize("name", ALL)
def test_sidecar_is_deterministic_and_current(name):
    path = FIXTURES / f"{name}.docx"
    assert path.exists(), f"missing fixture {path}"
    committed = (FIXTURES / f"{name}.expected.json").read_text(encoding="utf-8")
    provenance = _sidecar(name)["labels_provenance"]
    recomputed = json.dumps(
        make_fixtures.sidecar_for(path, provenance=provenance),
        indent=2,
        sort_keys=True,
        ensure_ascii=False,
    )
    assert committed == recomputed


@pytest.mark.parametrize("name", ALL)
def test_sidecar_schema_and_provenance(name):
    sc = _sidecar(name)
    assert set(sc) == {
        "fixture",
        "labels_provenance",
        "comments",
        "revisions",
        "sections",
        "tables",
    }
    assert sc["fixture"] == f"{name}.docx"
    assert sc["labels_provenance"] in {"generator", "spec"}


MODEL = FIXTURES / "model"
MODEL_NAMES = sorted(p.name[: -len(".expected.json")] for p in MODEL.glob("*.expected.json"))
MODEL_KEYS = {
    "fixture",
    "labels_provenance",
    "clauses",
    "span_rule",
    "terminator",
    "known_gaps",
    "paragraphs",
    "comments",
    "revisions",
}


@pytest.mark.spec_derived
@pytest.mark.parametrize("name", MODEL_NAMES)
def test_model_sidecar_schema_and_provenance(name):
    """The hand-typed fixtures/model sidecars: schema, provenance, strict loading."""
    path = MODEL / f"{name}.expected.json"
    assert (MODEL / f"{name}.docx").exists(), f"missing fixture {name}.docx"
    raw = json.loads(path.read_text(encoding="utf-8"))
    sidecar = labels.load_sidecar(path)
    assert sidecar.fixture == f"{name}.docx"
    assert sidecar.labels_provenance == "spec"
    # every key is either modelled or a declared annotation
    assert set(raw) <= MODEL_KEYS | set(labels.ANNOTATION_KEYS), name
    assert sidecar.paragraphs and all(p.spans and p.union for p in sidecar.paragraphs), name
    # strict: an unknown key is an error, never silently dropped
    with pytest.raises(ValueError):
        labels.sidecar_from_dict({**raw, "not_a_key": 1})


@pytest.mark.parametrize("name", SYNTHETIC)
def test_generated_fixtures_are_labelled_generator(name):
    assert _sidecar(name)["labels_provenance"] == "generator"


@pytest.mark.spec_derived
def test_spec_fixture_is_labelled_spec():
    assert _sidecar("spec_threaded")["labels_provenance"] == "spec"


# --- independent re-derivation from the raw OOXML ----------------------------


def test_comment_counts_match_raw_xml():
    for name in ALL:
        root = etree.fromstring(_part(FIXTURES / f"{name}.docx", "word/comments.xml"))
        raw = root.findall(_w("comment"))
        sc = _sidecar(name)
        assert len(sc["comments"]) == len(raw), name
        assert [c["id"] for c in sc["comments"]] == [c.get(_w("id")) for c in raw], name
        assert [c["author"] for c in sc["comments"]] == [c.get(_w("author")) for c in raw], name


def test_comment_ids_and_anchors_have_a_matching_range_in_document_xml():
    for name in ALL:
        body = _body(FIXTURES / f"{name}.docx")
        starts = {e.get(_w("id")) for e in body.iter(_w("commentRangeStart"))}
        ends = {e.get(_w("id")) for e in body.iter(_w("commentRangeEnd"))}
        for c in _sidecar(name)["comments"]:
            assert c["id"] in starts and c["id"] in ends, (name, c["id"])
            assert isinstance(c["anchor_text"], str)


def test_section_outline_matches_raw_xml():
    body = _body(FIXTURES / "program_review_v3.docx")
    raw = []
    for p in body.findall(_w("p")):
        style = p.find(f"{_w('pPr')}/{_w('pStyle')}")
        if style is not None:
            raw.append(style.get(_w("val")))
    headings = [s for s in raw if s == "Title" or s.startswith("Heading")]
    assert len(_sidecar("program_review_v3")["sections"]) == len(headings)


def test_program_review_ground_truth():
    sc = _sidecar("program_review_v3")

    assert [(c["author"], c["initials"]) for c in sc["comments"]] == [
        ("R. Alvarez", "RA"),
        ("M. Chen", "MC"),
        ("R. Alvarez", "RA"),
        ("Legal", "LG"),
        ("M. Chen", "MC"),
    ]
    # python-docx does not set w14:paraId in these fixtures -> null, never invented.
    assert all(c["para_id"] is None for c in sc["comments"])
    assert sc["comments"][0]["anchor_text"] == (
        "The policy provides a Limit of Liability of $1,000,000 per occurrence "
        "and $2,000,000 aggregate. "
    )
    assert sc["comments"][2]["anchor_text"].startswith("Additional insured status")
    assert sc["comments"][4]["anchor_text"] == "Pollution: "

    assert sc["revisions"] == [
        {"author": "R. Alvarez", "kind": "del", "text": "excluded in all cases"},
        {
            "author": "R. Alvarez",
            "kind": "ins",
            "text": "excluded except for hostile-fire release",
        },
    ]
    assert [(s["text"], s["level"], s["order"]) for s in sc["sections"]] == [
        ("Program Review: Contractors General Liability", 0, 0),
        ("1. Coverage Terms", 1, 1),
        ("2. Exclusions", 1, 2),
        ("3. Fee and Rate Schedule", 1, 3),
        ("4. Notes", 1, 4),
    ]
    assert sc["tables"] == [{"rows": 4, "columns": 3, "merges": [[3, 0, 3, 2]]}]


@pytest.mark.parametrize("name", ["program_review_v3", "spec_threaded"])
def test_revision_spans_match_raw_xml(name):
    body = _body(FIXTURES / f"{name}.docx")
    raw = [(etree.QName(e).localname, e.get(_w("author"))) for e in body.iter()
           if etree.QName(e).localname in {"ins", "del", "moveFrom", "moveTo"}]
    assert [(r["kind"], r["author"]) for r in _sidecar(name)["revisions"]] == raw


def test_edge_cases_ground_truth():
    sc = _sidecar("edge_cases")
    assert [c["author"] for c in sc["comments"]] == ["A", "B"]
    # anchored across a range of runs incl. a bold one in the middle
    assert sc["comments"][0]["anchor_text"] == "Alpha bravo charlie."
    assert sc["tables"] == [{"rows": 2, "columns": 2, "merges": []}]


def test_binder_summary_ground_truth():
    sc = _sidecar("binder_summary")
    assert [c["author"] for c in sc["comments"]] == ["M. Chen"]
    assert [s["text"] for s in sc["sections"]] == [
        "Binder Summary",
        "Insuring Agreement",
        "Conditions",
        "Not Covered",
    ]
    assert sc["revisions"] == [] and sc["tables"] == []


# --- why lxml and not python-docx --------------------------------------------


def test_python_docx_would_miss_inserted_text():
    docx = FIXTURES / "program_review_v3.docx"
    paragraphs = [p.text for p in make_fixtures.Document(docx).paragraphs]
    assert any(t.startswith("Pollution: ") for t in paragraphs)
    assert not any("hostile-fire" in t for t in paragraphs)

    sc = _sidecar("program_review_v3")
    assert "excluded except for hostile-fire release" in [r["text"] for r in sc["revisions"]]


# --- threaded comments (spec fixture) ----------------------------------------


@pytest.mark.spec_derived
def test_spec_fixture_has_threaded_comment_metadata():
    docx = FIXTURES / "spec_threaded.docx"
    with zipfile.ZipFile(docx) as z:
        assert "word/commentsExtended.xml" in z.namelist()
        assert "word/comments.xml" in z.namelist()
        ext = etree.fromstring(z.read("word/commentsExtended.xml"))
        rels = z.read("word/_rels/document.xml.rels").decode("utf-8")
        types = z.read("[Content_Types].xml").decode("utf-8")
    W15 = "http://schemas.microsoft.com/office/word/2012/wordml"
    exts = ext.findall(f"{{{W15}}}commentEx")
    assert len(exts) == 2
    # reply linked to its parent, and the reply is marked resolved
    assert exts[0].get(f"{{{W15}}}paraIdParent") is None
    assert exts[1].get(f"{{{W15}}}paraIdParent") == exts[0].get(f"{{{W15}}}paraId")
    assert exts[1].get(f"{{{W15}}}done") == "1"
    # wired through the package
    assert "commentsExtended.xml" in rels and "commentsExtended" in types


@pytest.mark.spec_derived
def test_spec_fixture_ground_truth():
    sc = _sidecar("spec_threaded")
    assert [(c["id"], c["author"], c["initials"]) for c in sc["comments"]] == [
        ("1", "D. Okafor", "DO"),
        ("2", "S. Ruiz", "SR"),
    ]
    assert [c["anchor_text"] for c in sc["comments"]] == ["The endorsement ", "Post-completion tail"]
    assert [c["para_id"] for c in sc["comments"]] == ["00000001", "00000002"]
    assert sc["revisions"] == [
        {"author": "D. Okafor", "kind": "del", "text": "three years"},
        {"author": "D. Okafor", "kind": "ins", "text": "thirty-six months"},
    ]
    assert [(s["text"], s["level"], s["order"]) for s in sc["sections"]] == [
        ("Spec-Annotated Threaded Comments", 0, 0),
        ("Threaded Review", 1, 1),
    ]
    assert sc["tables"] == []


def test_regeneration_is_byte_reproducible(tmp_path):
    root = Path(__file__).resolve().parents[1]

    def build(dest):
        subprocess.run(
            [sys.executable, "tools/make_fixtures.py", str(dest)],
            cwd=root, check=True, capture_output=True,
        )
        subprocess.run(
            [sys.executable, "tools/make_spec_fixtures.py", str(dest)],
            cwd=root, check=True, capture_output=True,
        )
        return {
            p.name: hashlib.sha256(p.read_bytes()).hexdigest()
            for p in Path(dest).iterdir() if p.is_file()
        }

    first, second = build(tmp_path / "a"), build(tmp_path / "b")
    assert first == second, "fixture generation is not reproducible"

    committed = {
        p.name: hashlib.sha256(p.read_bytes()).hexdigest()
        for p in FIXTURES.iterdir() if p.suffix in {".docx", ".json"}
    }
    assert committed == first, "committed fixtures are stale vs the generator"


def test_real_fixtures_dir_documents_its_convention():
    readme = FIXTURES / "real" / "README.md"
    assert readme.exists()
    text = readme.read_text(encoding="utf-8")
    assert "blocked on the user" in text.lower()
    assert "LabelsProvenance" in text and "tracked change" in text
    assert not list((FIXTURES / "real").glob("*.docx")), "real docs must not be committed"
