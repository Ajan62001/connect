"""Every controlled vocabulary in the system lives here — single home for all
CHECK-constraint value sets (storage/schema.py builds its DDL from these) and
for the Literal types the frozen domain contracts use.

`domain` imports nothing internal. Tuples (not Enums) because they are
consumed both by DDL string-building and by Pydantic Literal annotations.
"""

from __future__ import annotations

from typing import Literal

# --- source ----------------------------------------------------------------

SOURCE_TYPES = ("rss", "web_news", "scrape", "api", "search", "manual",
                "twitter", "telegram")
SourceType = Literal[
    "rss", "web_news", "scrape", "api", "search", "manual",
    "twitter", "telegram"]

CREDIBILITY_TIERS = (1, 2, 3, 4)  # 1 official/primary, 2 national media, 3 regional/aggregator, 4 unverified

# --- document --------------------------------------------------------------

# 'xlsx' covers legacy .xls too (one spreadsheet bucket); 'docx' is Word.
MEDIA_TYPES = ("html", "pdf", "text", "tweet", "telegram", "xlsx", "docx")
MediaType = Literal[
    "html", "pdf", "text", "tweet", "telegram", "xlsx", "docx"]

ENRICHMENT_STATUSES = (
    "pending", "queued", "done", "failed", "skipped_dup", "skipped_aged")
EnrichmentStatus = Literal[
    "pending", "queued", "done", "failed", "skipped_dup", "skipped_aged"]

# --- claims / evidence -----------------------------------------------------

CLAIM_TYPES = ("factual", "policy_provision", "prediction", "opinion")
VERDICTS = ("supported", "refuted", "mixed", "unverified")
EVIDENCE_STANCES = ("supports", "refutes", "mixed", "unrelated")
EVIDENCE_METHODS = ("llm", "manual")
SIGHTING_STANCES = ("asserts", "disputes", "reports", "quotes")

# --- entities / events / edges (the graph) ---------------------------------

ENTITY_TYPES = (
    "person", "organization", "ministry", "company", "political_party",
    "law", "scheme", "policy_instrument", "place", "other")
DATE_PRECISIONS = ("day", "month", "year")
NODE_TYPES = ("entity", "event", "claim", "document", "dossier")
EDGE_STATUSES = ("active", "superseded", "retracted")

# edge.relation is free TEXT in the DDL (the closed menu would force a table
# rebuild every time a phase adds a relation); the vocabulary is enforced in
# Python by storage/edges.py. Grows phase by phase.
# 'follows' (Phase 2): event -> event story threading.
# v8 (investigation mode): the causal vocabulary — reaction_to/triggered_by
# (event -> event), enables/blocks (event|entity -> event|entity),
# alternative_to (same-kind pairs). Signatures enforced in storage/edges.py.
EDGE_RELATIONS = ("links_to", "follows", "reaction_to", "triggered_by",
                  "enables", "blocks", "alternative_to")

# --- document links (Phase 0.5: in-content link extraction & follow) --------

LINK_STATUSES = ("not_followed", "pending", "fetched", "failed", "skipped")
LinkStatus = Literal["not_followed", "pending", "fetched", "failed", "skipped"]

# Provenance grade lattice: curated(3) > analyzed(2) > enriched(1).
GRADES = (1, 2, 3)

MENTION_METHODS = ("alias", "llm", "manual")
ASSIGNMENT_METHODS = ("attach", "new", "adjudicated", "manual")
STORY_STATUSES = ("active", "archived")

# --- dossiers (Pillar A; tables ship in v1, pipeline is a later phase) ------

DOSSIER_STATUSES = ("pending", "running", "completed", "failed", "cancelled")
# v8 appends topic/entity/story (investigation seeds).
DOSSIER_INPUT_TYPES = ("claim", "policy", "event", "document",
                       "topic", "entity", "story")
# v8 appends the investigation stages + its section-row names.
DOSSIER_STAGES = (
    "normalize", "provenance", "verify", "extract_link",
    "forces", "impacts", "loopholes", "assemble",
    "scope", "investigate", "synthesize", "timeline", "causal_narrative",
    "actors", "alternatives", "open_questions", "watch_next")
SECTION_STATUSES = ("pending", "running", "completed", "failed", "skipped")

# v8: a dossier is either an analysis (Phase 3) or an investigation.
DOSSIER_KINDS = ("analysis", "investigation")

# --- investigations (v8) -----------------------------------------------------

QUESTION_TYPES = (
    "why_now", "why_this_design", "why_not_alternative", "who_pushed",
    "what_triggered", "why_silent", "who_benefits", "what_next")
QUESTION_STATUSES = ("open", "partial", "answered", "dropped")
FINDING_KINDS = (
    "reaction", "trigger", "alternative", "actor_motive", "timing",
    "context", "consequence")

# --- jobs ------------------------------------------------------------------

JOB_KINDS = (
    "poll_source", "ingest_url", "analysis",
    "enrich_t1_sync", "enrich_t1_batch", "enrich_t2", "reverify_claim",
    "brief_generate", "investigation")
JOB_STATUSES = ("queued", "running", "done", "failed", "cancelled")

# --- watches / consumption -------------------------------------------------

WATCH_KINDS = ("entity", "topic", "thread", "claim", "search")
WatchKind = Literal["entity", "topic", "thread", "claim", "search"]

WATCH_HIT_OBJECT_TYPES = ("document", "event", "claim", "contradiction")

BRIEF_SECTIONS = (
    "watch_dev", "thread_move", "contradiction", "trending_claim",
    "suggestion", "position_shift")
BriefSection = Literal[
    "watch_dev", "thread_move", "contradiction", "trending_claim",
    "suggestion", "position_shift"]
BRIEF_OBJECT_TYPES = ("event", "document", "claim", "contradiction", "thread",
                      "position_shift")
BriefObjectType = Literal["event", "document", "claim", "contradiction",
                          "thread", "position_shift"]
CONTRADICTION_STATUSES = ("open", "dismissed", "resolved")
CALENDAR_KINDS = ("election", "budget", "parliament_session", "rbi_mpc", "other")
TOPIC_SOURCES = ("rule", "t1")
VIEW_SURFACES = ("entity", "thread", "contradictions", "brief", "feed")

# --- T1 enrichment ------------------------------------------------------------

# Controlled topic vocabulary for T1 tagging (document_topic.topic carries no
# CHECK — enforcement is in Python at persist time; the T1 system prompt embeds
# this list verbatim). 25 topics; 'other' is the catch-all.
T1_TOPICS = (
    "monetary-policy", "taxation", "securities-regulation", "elections",
    "judiciary", "trade", "agriculture", "welfare-schemes", "infrastructure",
    "defence", "foreign-policy", "energy", "telecom", "data-privacy",
    "labour", "education", "health", "environment", "federalism",
    "parliament", "budget", "banking", "markets", "misinformation", "other")

# --- statements / position tracking (v9) -------------------------------------

# Entity types allowed to SPEAK — a statement's resolved speaker must carry
# one of these; anything else (place, law, scheme, ...) is dropped at persist.
STATEMENT_SPEAKER_TYPES = ("person", "organization", "ministry",
                           "political_party")

POSITION_SHIFT_KINDS = ("shifted", "reversed")
PositionShiftKind = Literal["shifted", "reversed"]
POSITION_SHIFT_STATUSES = ("open", "dismissed")
PositionShiftStatus = Literal["open", "dismissed"]

# --- tenancy (v0.2: tables ship in the PG baseline schema; auth/tenancy CODE
# is the next workstream — design v02-tenancy-auth.md §2) ---------------------

VISIBILITIES = ("private", "shared")
Visibility = Literal["private", "shared"]

ROLES = ("admin", "member")
Role = Literal["admin", "member"]

DOCUMENT_ORIGINS = ("polled", "link_follow", "investigation_fetch",
                    "user_url", "user_upload", "user_text")
DocumentOrigin = Literal["polled", "link_follow", "investigation_fetch",
                         "user_url", "user_upload", "user_text"]

# --- vector backends (health reporting) -------------------------------------

# v0.2: pgvector replaces sqlite-vec/bruteforce (storage is Postgres).
VECTOR_BACKENDS = ("pgvector", "disabled")
VectorBackend = Literal["pgvector", "disabled"]


def sql_in(values: tuple) -> str:
    """Render a vocabulary tuple as a SQL IN(...) list for CHECK constraints."""
    rendered = ", ".join(
        str(v) if isinstance(v, int) else "'" + v + "'" for v in values)
    return "(" + rendered + ")"
