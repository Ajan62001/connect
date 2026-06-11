"""The record_finding grounding gate (design §3.4) — table-driven
rejections (span, alternative discipline, speculative candidate menu,
causal vocabulary, node existence) and the success paths (finding +
evidence rows, grade-2 edge writes, question open -> partial)."""

from __future__ import annotations

import json

import pytest
from kb_factories import insert_doc

from connect.investigation.schema import ReactionCandidate, ScopePack
from connect.investigation.writeback import (
    FindingValidationError,
    record_finding,
)
from connect.storage import db as db_mod
from connect.storage import edges as edge_dao
from connect.storage.db import utc_now

DOC_TEXT = ("The ministry withdrew the draft rules in the wake of "
            "objections from the parliamentary standing committee. "
            "Officials briefed the press on Friday.")
QUOTE = ("The ministry withdrew the draft rules in the wake of objections "
         "from the parliamentary standing committee.")


@pytest.fixture()
def conn(tmp_path):
    c = db_mod.connect(tmp_path / "t.db")
    db_mod.init_db(c)
    yield c
    c.close()


@pytest.fixture()
def env(conn):
    """One dossier, one doc, two events, one open question, an RC menu."""
    doc_id = insert_doc(conn, title="Withdrawal report", text=DOC_TEXT)
    with conn:
        conn.execute(
            "INSERT INTO dossier (kind, input_text, status, created_at)"
            " VALUES ('investigation', 'draft rules', 'running', ?)",
            (utc_now(),))
        conn.execute("INSERT INTO event (title, occurred_on, created_at)"
                     " VALUES ('Committee objections', '2026-05-20', ?)",
                     (utc_now(),))
        conn.execute("INSERT INTO event (title, occurred_on, created_at)"
                     " VALUES ('Rules withdrawn', '2026-05-28', ?)",
                     (utc_now(),))
        conn.execute(
            "INSERT INTO question (dossier_id, qtype, text, status,"
            " created_at) VALUES (1, 'what_triggered',"
            " 'What triggered the withdrawal?', 'open', ?)", (utc_now(),))
    pack = ScopePack(
        input_text="draft rules", input_type="topic",
        reaction_candidates=[ReactionCandidate(
            candidate_id="RC1", src_event_id=2, dst_event_id=1,
            src_title="Rules withdrawn", dst_title="Committee objections",
            days_apart=8.0, entity_jaccard=0.5, cosine=0.7)])
    return {"conn": conn, "doc_id": doc_id, "dossier_id": 1, "pack": pack,
            "question_id": 1}


def attempt(env, **overrides):
    args = {"kind": "context", "text": "a finding",
            "evidence": [{"document_id": env["doc_id"], "quote": QUOTE}],
            **overrides}
    return record_finding(env["conn"], dossier_id=env["dossier_id"],
                          scope_pack=env["pack"], args=args)


# --- rejection table -------------------------------------------------------------

REJECTIONS = [
    ("span_not_verbatim",
     {"evidence": [{"document_id": None, "quote":
                    "The ministry happily withdrew the rules"}]},
     "not a verbatim substring"),
    ("unknown_document",
     {"evidence": [{"document_id": 999, "quote": QUOTE}]},
     "document 999 not found"),
    ("empty_quote",
     {"evidence": [{"document_id": None, "quote": "  "}]},
     "non-empty string"),
    ("alternative_without_evidence",
     {"kind": "alternative", "evidence": [], "speculation": True},
     "requires non-empty evidence"),
    ("alternative_with_speculation",
     {"kind": "alternative", "speculation": True},
     "forbids speculation"),
    ("evidence_free_without_speculation",
     {"evidence": [], "speculation": False},
     "evidence is required when speculation=false"),
    ("speculative_reaction_without_candidate",
     {"kind": "reaction", "evidence": [], "speculation": True},
     "require a candidate_id"),
    ("speculative_trigger_without_candidate",
     {"kind": "trigger", "evidence": [], "speculation": True},
     "require a candidate_id"),
    ("candidate_off_menu",
     {"kind": "reaction", "evidence": [], "speculation": True,
      "candidate_id": "RC99"},
     "not on the reaction-candidate menu"),
    ("off_vocabulary_relation",
     {"link": {"src_type": "event", "src_id": 2, "relation": "causes",
               "dst_type": "event", "dst_id": 1}},
     "not in the causal vocabulary"),
    ("links_to_is_not_causal",
     {"link": {"src_type": "event", "src_id": 2, "relation": "links_to",
               "dst_type": "event", "dst_id": 1}},
     "not in the causal vocabulary"),
    ("bad_node_id",
     {"link": {"src_type": "event", "src_id": 999,
               "relation": "reaction_to", "dst_type": "event",
               "dst_id": 1}},
     "event 999 does not exist"),
    ("signature_violation",
     {"link": {"src_type": "entity", "src_id": 1,
               "relation": "reaction_to", "dst_type": "event",
               "dst_id": 1}},
     "does not accept"),
    ("alternative_to_mixed_kinds",
     {"link": {"src_type": "entity", "src_id": 1,
               "relation": "alternative_to", "dst_type": "event",
               "dst_id": 1}},
     "same-kind"),
    ("bad_kind", {"kind": "hunch"}, "kind must be one of"),
    ("empty_text", {"text": "  "}, "non-empty string"),
    ("foreign_question", {"question_id": 999}, "question 999 not found"),
]


@pytest.mark.parametrize("name,overrides,reason",
                         REJECTIONS, ids=[r[0] for r in REJECTIONS])
def test_rejections(env, name, overrides, reason):
    # patch real doc id into evidence overrides built before the fixture
    if "evidence" in overrides:
        for item in overrides["evidence"]:
            if isinstance(item, dict) and item.get("document_id") is None:
                item["document_id"] = env["doc_id"]
    with pytest.raises(FindingValidationError, match=reason):
        attempt(env, **overrides)
    # nothing persisted on rejection
    assert env["conn"].execute(
        "SELECT COUNT(*) FROM finding").fetchone()[0] == 0
    assert env["conn"].execute(
        "SELECT COUNT(*) FROM finding_evidence").fetchone()[0] == 0


# --- success paths ----------------------------------------------------------------


def test_grounded_finding_with_link_and_question(env):
    conn = env["conn"]
    result = attempt(
        env, kind="reaction", question_id=env["question_id"],
        confidence=0.9,
        link={"src_type": "event", "src_id": 2, "relation": "reaction_to",
              "dst_type": "event", "dst_id": 1})
    assert result["finding_id"] == 1
    assert result["edge_id"] is not None
    assert result["speculation"] is False

    finding = conn.execute("SELECT * FROM finding WHERE id=1").fetchone()
    assert (finding["kind"], finding["speculation"],
            finding["question_id"]) == ("reaction", 0, env["question_id"])
    assert finding["edge_id"] == result["edge_id"]

    ev = conn.execute("SELECT * FROM finding_evidence"
                      " WHERE finding_id=1").fetchone()
    assert ev["document_id"] == env["doc_id"]
    assert ev["quote"] == QUOTE
    assert ev["quote_start"] is not None and ev["quote_end"] is not None
    assert DOC_TEXT[ev["quote_start"]:ev["quote_end"]] == QUOTE

    edge = conn.execute("SELECT * FROM edge WHERE id = ?",
                        (result["edge_id"],)).fetchone()
    assert (edge["src_type"], edge["src_id"], edge["relation"],
            edge["dst_type"], edge["dst_id"]) == (
                "event", 2, "reaction_to", "event", 1)
    assert edge["grade"] == 2
    assert edge["provenance_dossier_id"] == 1
    assert edge["provenance_document_id"] == env["doc_id"]
    props = json.loads(edge["properties"])
    assert props["quote"] == QUOTE
    assert props["speculation"] is False
    assert props["finding_id"] == 1

    # question flipped open -> partial and accumulated the finding id
    q = conn.execute("SELECT status, answer_finding_ids FROM question"
                     " WHERE id = ?", (env["question_id"],)).fetchone()
    assert q["status"] == "partial"
    assert json.loads(q["answer_finding_ids"]) == [1]


def test_speculative_candidate_finding_derives_styled_edge(env):
    conn = env["conn"]
    result = attempt(env, kind="reaction", evidence=[], speculation=True,
                     candidate_id="RC1", confidence=0.4)
    assert result["speculation"] is True
    edge = conn.execute("SELECT * FROM edge WHERE id = ?",
                        (result["edge_id"],)).fetchone()
    # derived from the candidate pair: B(2) -[reaction_to]-> A(1)
    assert (edge["src_id"], edge["relation"], edge["dst_id"]) == (
        2, "reaction_to", 1)
    props = json.loads(edge["properties"])
    assert props["speculation"] is True
    assert props["score_components"] == {
        "days_apart": 8.0, "entity_jaccard": 0.5, "cosine": 0.7}
    finding = conn.execute("SELECT payload FROM finding"
                           " WHERE id=1").fetchone()
    payload = json.loads(finding["payload"])
    assert payload["candidate_id"] == "RC1"


def test_edge_insert_is_idempotent(env):
    first = attempt(env, kind="reaction",
                    link={"src_type": "event", "src_id": 2,
                          "relation": "reaction_to", "dst_type": "event",
                          "dst_id": 1})
    second = attempt(env, kind="reaction",
                     link={"src_type": "event", "src_id": 2,
                           "relation": "reaction_to", "dst_type": "event",
                           "dst_id": 1})
    assert first["edge_id"] == second["edge_id"]
    assert env["conn"].execute(
        "SELECT COUNT(*) FROM edge WHERE relation='reaction_to'"
        " AND status='active'").fetchone()[0] == 1
    # but both findings persisted
    assert env["conn"].execute(
        "SELECT COUNT(*) FROM finding").fetchone()[0] == 2


def test_insert_causal_validates_directly(env):
    conn = env["conn"]
    with pytest.raises(ValueError, match="unknown causal relation"):
        edge_dao.insert_causal(conn, src_type="event", src_id=2,
                               dst_type="event", dst_id=1,
                               relation="follows")
    with pytest.raises(ValueError, match="does not accept"):
        edge_dao.insert_causal(conn, src_type="document", src_id=1,
                               dst_type="event", dst_id=1,
                               relation="enables")
