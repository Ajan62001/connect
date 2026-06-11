"""Every controlled vocabulary in the system lives here — single home for all
CHECK-constraint value sets (storage/schema.py builds its DDL from these) and
for the Literal types the frozen domain contracts use.

`domain` imports nothing internal. Tuples (not Enums) because they are
consumed both by DDL string-building and by Pydantic Literal annotations.
"""

from __future__ import annotations

from typing import Literal

# --- source ----------------------------------------------------------------

SOURCE_TYPES = ("rss", "scrape", "api", "search", "manual", "twitter", "telegram")
SourceType = Literal[
    "rss", "scrape", "api", "search", "manual", "twitter", "telegram"]

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
EDGE_RELATIONS = ("links_to", "follows")

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
DOSSIER_INPUT_TYPES = ("claim", "policy", "event", "document")
DOSSIER_STAGES = (
    "normalize", "provenance", "verify", "extract_link",
    "forces", "impacts", "loopholes", "assemble")
SECTION_STATUSES = ("pending", "running", "completed", "failed", "skipped")

# --- jobs ------------------------------------------------------------------

JOB_KINDS = (
    "poll_source", "ingest_url", "analysis",
    "enrich_t1_sync", "enrich_t1_batch", "enrich_t2", "reverify_claim",
    "brief_generate")
JOB_STATUSES = ("queued", "running", "done", "failed", "cancelled")

# --- watches / consumption -------------------------------------------------

WATCH_KINDS = ("entity", "topic", "thread", "claim", "search")
WatchKind = Literal["entity", "topic", "thread", "claim", "search"]

WATCH_HIT_OBJECT_TYPES = ("document", "event", "claim", "contradiction")

BRIEF_SECTIONS = (
    "watch_dev", "thread_move", "contradiction", "trending_claim", "suggestion")
BriefSection = Literal[
    "watch_dev", "thread_move", "contradiction", "trending_claim", "suggestion"]
BRIEF_OBJECT_TYPES = ("event", "document", "claim", "contradiction", "thread")
BriefObjectType = Literal["event", "document", "claim", "contradiction", "thread"]
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

# --- vector backends (health reporting) -------------------------------------

VECTOR_BACKENDS = ("sqlite-vec", "bruteforce", "disabled")
VectorBackend = Literal["sqlite-vec", "bruteforce", "disabled"]


def sql_in(values: tuple) -> str:
    """Render a vocabulary tuple as a SQL IN(...) list for CHECK constraints."""
    rendered = ", ".join(
        str(v) if isinstance(v, int) else "'" + v + "'" for v in values)
    return "(" + rendered + ")"
