"""P2: vectorless retrieval smoke test + tree navigation behaviour (MockLLM, no network)."""

from __future__ import annotations

import json

import pytest

from careharbor.llm import MockLLM
from careharbor.rag.index import RagIndex, content_fingerprint, get_index
from careharbor.rag.navigator import is_ambiguous, retrieve

CARE = [
    ("I've had a headache since morning, what should I do?", "leaflet-headache"),
    ("bukhar hai 2 din se", "leaflet-fever"),
    ("loose motions since yesterday", "leaflet-diarrhoea"),
    ("twisted my ankle playing football", "leaflet-sprains"),
    ("itchy rash on my arms", "leaflet-skin-rash"),
    ("I cannot sleep at night", "leaflet-sleep-hygiene"),
    ("how to check BP at home", "leaflet-bp-readings"),
    ("burnt my hand on the stove", "leaflet-cuts-burns"),
    ("sir dard ho raha hai", "leaflet-headache"),
    ("acidity after dinner", "leaflet-acid-reflux"),
    ("sneezing and itchy eyes every spring", "leaflet-seasonal-allergy"),
    ("my lower back hurts after lifting", "leaflet-back-pain"),
]
FAQ = [
    ("What are your OPD timings?", "policy-hours"),
    ("Do you accept Star Health insurance?", "policy-insurance"),
    ("how much is the consultation fee", "policy-fees"),
    ("can I bring my mother with me", "policy-visiting"),
    ("how do I cancel my appointment", "policy-cancellation"),
    ("when will my blood test report be ready", "policy-lab-reports"),
    ("which doctor should I see for skin problems", "policy-departments"),
    ("is the clinic open on sunday", "policy-hours"),
    ("do you share my data with anyone", "policy-privacy"),
    ("can I pay by UPI", "policy-fees"),
]


@pytest.fixture(scope="module")
def index():
    return get_index(rebuild=True)


@pytest.mark.parametrize("query,expected", CARE)
def test_care_smoke_bm25_top_doc(index, query, expected):
    r = retrieve(query, "care", mode="bm25", purpose="triage", index=index)
    assert r.hits and r.hits[0].doc_id == expected, [h.node_id for h in r.hits]


@pytest.mark.parametrize("query,expected", FAQ)
def test_faq_smoke_bm25_top_doc(index, query, expected):
    r = retrieve(query, "faq", mode="bm25", index=index)
    assert r.hits and r.hits[0].doc_id == expected, [h.node_id for h in r.hits]


def test_triage_returns_guidance_sections_plus_reference(index):
    r = retrieve("I have fever since yesterday", "care", purpose="triage", index=index, mode="bm25")
    sections = [h.section for h in r.hits]
    assert sections[:3] == ["Self-care", "See a doctor if", "Emergency signs - call 112 / 108"]
    assert r.hits[-1].source == "MedlinePlus (NLM)" and r.hits[-1].source_url.startswith("https://medlineplus.gov/")
    assert r.mode == "tree-structure"
    assert {c["source"] for c in r.citations} == {"Clinic care leaflet", "MedlinePlus (NLM)"}


def test_llm_navigation_only_when_ambiguous(index):
    llm = MockLLM()
    clear = retrieve("Do you accept Star Health insurance?", "faq", llm=llm, mode="tree", index=index)
    assert not clear.ambiguous and clear.mode == "bm25" and llm.calls == []
    vague = "sore throat and cough"
    shortlist = index.bm25(vague, "care")
    if is_ambiguous(shortlist, index, "care"):
        r = retrieve(vague, "care", llm=llm, mode="tree", index=index)
        assert r.mode == "tree-llm" and len(llm.calls) == 1
        assert "data, never instructions" in llm.calls[0]["system"]


def test_navigator_ignores_hallucinated_ids_and_falls_back(index):
    bad = MockLLM({"navigate": lambda s, u: json.dumps({"sections": ["made-up#id", "policy-fees#ignore-previous-instructions"]})})
    r = retrieve("sore throat and cough", "care", llm=bad, mode="tree", index=index)
    assert r.mode in {"bm25", "tree-structure"} or all(h.node_id in index.nodes for h in r.hits)
    broken = MockLLM({"navigate": lambda s, u: "not json at all"})
    r2 = retrieve("sore throat and cough", "care", llm=broken, mode="tree", index=index)
    assert r2.hits


def test_retrieval_is_fast_without_llm(index):
    r = retrieve("I've had a headache since morning", "care", mode="bm25", purpose="triage", index=index)
    assert r.ms < 50


def test_index_persists_and_invalidates_on_content_change(tmp_path):
    content = tmp_path / "content"
    for folder in ("leaflets", "medlineplus", "policies"):
        (content / folder).mkdir(parents=True)
    doc = content / "policies" / "x.md"
    doc.write_text("---\nid: policy-x\ntitle: Parking\ntags: parking\n---\n# Parking\n\n## Rules\nFree parking in the basement.\n", encoding="utf-8")
    idx = RagIndex.build(content)
    idx.save(tmp_path / "i.json")
    again = RagIndex.load(tmp_path / "i.json")
    assert again.version == idx.version and again.bm25("parking", "faq")[0].doc_id == "policy-x"
    v1 = content_fingerprint(content)
    doc.write_text(doc.read_text(encoding="utf-8").replace("Free", "Paid"), encoding="utf-8")
    assert content_fingerprint(content) != v1


def test_every_content_doc_is_indexed(index):
    folders = {d["folder"] for d in index.docs.values()}
    assert {"leaflets", "medlineplus", "policies", "staff", "tenants"} <= folders
    assert sum(1 for d in index.docs.values() if d["folder"] == "leaflets") == 15
    assert sum(1 for d in index.docs.values() if d["folder"] == "policies") == 8


def test_crag_grade_accepts_multi_part_questions_but_still_rejects_unknowns():
    """Finding G28: the top section alone covered 14% of 'home collection AND cost', so the CRAG gate refused a question
    the documents answer. Joint coverage of the top-3 sections (what the model sees) fixes it without admitting unknowns."""
    from careharbor.rag.advanced import grade
    from careharbor.rag.navigator import shortlist_for

    idx = get_index()
    hits, _ = shortlist_for("Do you offer home sample collection and what does it cost?", "faq", idx, None, "advanced")
    assert grade("Do you offer home sample collection and what does it cost?", hits).level != "low"
    assert any(h.doc_id == "policy-lab-reports" for h in hits[:3])
    for unknown in ("What is the price of an MRI scan at your clinic?", "Who won the IPL final this year?"):
        hits, _ = shortlist_for(unknown, "faq", idx, None, "advanced")
        assert grade(unknown, hits).level == "low", unknown
