"""The scope stage — deterministic, zero-LLM pre-pass (design doc §1.2).

Anchors -> corpus slice (FTS + vec RRF) -> graph expansion -> thread
membership -> timeline -> calendar context -> the two §3.2 channels:
the causal-marker pre-scan (channel a) and the reaction-candidate pair
math (channel b, the closed candidate menu speculative findings must cite).

The pair math and the marker scan are pure functions over plain inputs so
the tests drive them table-style on synthetic events + embeddings.
"""

from __future__ import annotations

import json
import logging
import re
import sqlite3
from dataclasses import dataclass
from datetime import date
from typing import Iterable, Sequence

from connect.investigation.schema import (
    AnchorEntity,
    CalendarItem,
    CausalMarkerHit,
    InvestigationSeed,
    ReactionCandidate,
    ScopeDoc,
    ScopeEdge,
    ScopePack,
    StoryRef,
    TimelineItem,
)
from connect.knowledge.embedder import Embedder
from connect.knowledge.linking.event_clusterer import (
    cosine_similarity,
    entity_jaccard,
)
from connect.knowledge.vector import VectorIndex, _unpack
from connect.storage import fts as fts_dao
from connect.storage.edges import CAUSAL_RELATIONS

log = logging.getLogger(__name__)

# -- §3.2b thresholds (inferred-reaction candidate pairs) ----------------------------

JACCARD_MIN = 0.3
COSINE_MIN = 0.55
WINDOW_MULTIPLIER = 3
DEFAULT_WINDOW_DAYS = 7
MAX_CANDIDATES = 12

# -- corpus slice / coverage knobs ----------------------------------------------------

FTS_TOP_K = 50
VEC_TOP_K = 50
RRF_K = 60
ANCHOR_DOC_DAYS = 90
ANCHOR_DOC_CAP = 60
SLICE_CAP = 60
MAX_ANCHORS = 6
TIMELINE_CAP = 40
EDGE_CAP = 100
CALENDAR_WINDOW_DAYS = 120
MARKER_DOC_CAP = 30
MARKER_HIT_CAP = 20

THIN_MIN_DOCS = 8
THIN_MIN_DOMAINS = 2

# the causal-marker lexicon (also embedded in the loop's system prompt)
CAUSAL_MARKERS: tuple[str, ...] = (
    "in response to", "following", "in the wake of", "after backlash over",
    "prompted by", "to counter", "in retaliation for", "as a result of",
    "reacting to", "in light of", "amid criticism of", "after pressure from")

_SENTENCE_RE = re.compile(r"[^.!?\n]*[.!?\n]?")


# -- seed resolution -----------------------------------------------------------------


def resolve_seed(conn: sqlite3.Connection, seed: InvestigationSeed,
                 ) -> tuple[str, str, int | None]:
    """(input_text, input_type, parent_question_id) for a validated seed.
    Raises LookupError when a referenced row does not exist."""
    if seed.topic is not None:
        return seed.topic.strip(), "topic", None
    if seed.entity_id is not None:
        row = conn.execute("SELECT name FROM entity WHERE id = ?",
                           (seed.entity_id,)).fetchone()
        if row is None:
            raise LookupError(f"entity {seed.entity_id} not found")
        return row["name"], "entity", None
    if seed.event_id is not None:
        row = conn.execute("SELECT title FROM event WHERE id = ?",
                           (seed.event_id,)).fetchone()
        if row is None:
            raise LookupError(f"event {seed.event_id} not found")
        return row["title"], "event", None
    if seed.story_id is not None:
        row = conn.execute(
            "SELECT s.title, e.title AS root_title FROM story s"
            " LEFT JOIN event e ON e.id = s.root_event_id WHERE s.id = ?",
            (seed.story_id,)).fetchone()
        if row is None:
            raise LookupError(f"story {seed.story_id} not found")
        return (row["title"] or row["root_title"]
                or f"story {seed.story_id}"), "story", None
    assert seed.question_id is not None
    row = conn.execute("SELECT text FROM question WHERE id = ?",
                       (seed.question_id,)).fetchone()
    if row is None:
        raise LookupError(f"question {seed.question_id} not found")
    return row["text"], "topic", seed.question_id


# -- pure math: §3.2b reaction-candidate pairs -----------------------------------------


@dataclass(frozen=True)
class EventLite:
    """Everything the pair math needs about one timeline event."""
    event_id: int
    title: str
    occurred_on: str | None        # ISO date (first 10 chars used)
    window_days: int               # from its event_type taxonomy row
    entity_ids: frozenset[int]
    centroid: tuple[float, ...] | None


def _parse_date(value: str | None) -> date | None:
    if not value:
        return None
    try:
        return date.fromisoformat(value[:10])
    except ValueError:
        return None


def reaction_candidate_pairs(
        events: Sequence[EventLite],
        existing_causal: Iterable[tuple[int, int]] = (),
) -> list[ReactionCandidate]:
    """Ordered pairs (B strictly after A) within A.window_days x 3, with
    entity-Jaccard >= 0.3 AND centroid cosine >= 0.55, skipping pairs that
    already carry a causal edge (either direction). Best-first by
    jaccard + cosine; ids RC1.. assigned after the sort; capped."""
    blocked = set()
    for a, b in existing_causal:
        blocked.add((a, b))
        blocked.add((b, a))
    scored: list[tuple[float, ReactionCandidate]] = []
    for a in events:
        a_date = _parse_date(a.occurred_on)
        if a_date is None:
            continue
        window = max(1, a.window_days) * WINDOW_MULTIPLIER
        for b in events:
            if b.event_id == a.event_id:
                continue
            b_date = _parse_date(b.occurred_on)
            if b_date is None:
                continue
            days = (b_date - a_date).days
            if days <= 0 or days > window:
                continue
            if (b.event_id, a.event_id) in blocked:
                continue
            jac = entity_jaccard(set(a.entity_ids), set(b.entity_ids))
            if jac < JACCARD_MIN:
                continue
            cos = cosine_similarity(
                list(a.centroid) if a.centroid else None,
                list(b.centroid) if b.centroid else None)
            if cos < COSINE_MIN:
                continue
            scored.append((jac + cos, ReactionCandidate(
                candidate_id="",  # assigned after the sort
                src_event_id=b.event_id, dst_event_id=a.event_id,
                src_title=b.title, dst_title=a.title,
                days_apart=float(days), entity_jaccard=round(jac, 4),
                cosine=round(cos, 4))))
    scored.sort(key=lambda t: (-t[0], t[1].src_event_id, t[1].dst_event_id))
    return [c.model_copy(update={"candidate_id": f"RC{i + 1}"})
            for i, (_, c) in enumerate(scored[:MAX_CANDIDATES])]


# -- pure scan: §3.2a causal-marker pre-scan -------------------------------------------


def scan_causal_markers(
        docs: Sequence[tuple[int, str | None, str]],
        markers: Sequence[str] = CAUSAL_MARKERS,
        cap: int = MARKER_HIT_CAP) -> list[CausalMarkerHit]:
    """Substring scan for causal-marker phrases over (id, title, text)
    rows; returns the surrounding sentence per hit (one hit per marker per
    document), capped."""
    hits: list[CausalMarkerHit] = []
    for doc_id, title, text in docs:
        lowered = text.lower()
        for marker in markers:
            idx = lowered.find(marker)
            if idx < 0:
                continue
            start = max(lowered.rfind(".", 0, idx),
                        lowered.rfind("!", 0, idx),
                        lowered.rfind("?", 0, idx),
                        lowered.rfind("\n", 0, idx)) + 1
            match = _SENTENCE_RE.match(text, start)
            sentence = (match.group(0) if match else text[start:start + 240])
            hits.append(CausalMarkerHit(
                document_id=doc_id, title=title, marker=marker,
                sentence=sentence.strip()[:400]))
            if len(hits) >= cap:
                return hits
    return hits


# -- RRF fusion ------------------------------------------------------------------------


def rrf_fuse(rank_lists: Sequence[Sequence[int]], k: int = RRF_K,
             ) -> list[int]:
    """Reciprocal-rank fusion over id rank lists; best-first, ties by id."""
    scores: dict[int, float] = {}
    for ranks in rank_lists:
        for rank, item in enumerate(ranks):
            scores[item] = scores.get(item, 0.0) + 1.0 / (k + rank + 1)
    return sorted(scores, key=lambda i: (-scores[i], i))


# -- the builder -------------------------------------------------------------------------


def build_scope_pack(conn: sqlite3.Connection, *, embedder: Embedder,
                     vectors: VectorIndex | None,
                     input_text: str, input_type: str,
                     seed: InvestigationSeed) -> ScopePack:
    anchors = _resolve_anchors(conn, input_text, seed)
    doc_ids = _corpus_slice(conn, embedder, vectors, input_text,
                            [a.entity_id for a in anchors])
    documents = _load_docs(conn, doc_ids)
    event_ids, stories = _thread_membership(conn, doc_ids)
    edges = _graph_expansion(conn, [a.entity_id for a in anchors], event_ids)
    timeline = _timeline(conn, event_ids, documents)
    calendar = _calendar(conn, timeline)
    candidates = _reaction_candidates(conn, event_ids)
    markers = scan_causal_markers(_marker_docs(conn, doc_ids))
    coverage = _coverage(documents)
    return ScopePack(
        input_text=input_text, input_type=input_type, anchors=anchors,
        documents=documents, edges=edges, stories=stories,
        timeline=timeline, calendar=calendar,
        reaction_candidates=candidates, causal_markers=markers,
        coverage=coverage)


def _resolve_anchors(conn: sqlite3.Connection, text: str,
                     seed: InvestigationSeed) -> list[AnchorEntity]:
    """Alias-exact matches in the input text + top mentioned entities of
    the FTS doc hits; explicit entity/event/story seeds contribute their
    own entities. Capped at 6."""
    found: dict[int, AnchorEntity] = {}

    def _add(entity_id: int) -> None:
        if entity_id in found or len(found) >= MAX_ANCHORS:
            return
        row = conn.execute(
            "SELECT id, name, entity_type FROM entity WHERE id = ?",
            (entity_id,)).fetchone()
        if row is not None:
            found[entity_id] = AnchorEntity(
                entity_id=row["id"], name=row["name"],
                entity_type=row["entity_type"])

    if seed.entity_id is not None:
        _add(seed.entity_id)

    lowered = text.lower()
    for row in conn.execute("SELECT id, name, aliases FROM entity"):
        names = [row["name"]]
        try:
            names.extend(a for a in json.loads(row["aliases"] or "[]")
                         if isinstance(a, str))
        except ValueError:
            pass
        if any(n and n.lower() in lowered for n in names):
            _add(row["id"])

    seed_event_ids: list[int] = []
    if seed.event_id is not None:
        seed_event_ids.append(seed.event_id)
    if seed.story_id is not None:
        seed_event_ids.extend(r[0] for r in conn.execute(
            "SELECT id FROM event WHERE story_id = ?", (seed.story_id,)))
    for event_id in seed_event_ids:
        for r in conn.execute(
                "SELECT m.entity_id, COUNT(*) AS n FROM entity_mention m"
                " JOIN event_assignment ea ON ea.document_id = m.document_id"
                " WHERE ea.event_id = ? GROUP BY m.entity_id"
                " ORDER BY n DESC LIMIT 3", (event_id,)):
            _add(r["entity_id"])

    if len(found) < MAX_ANCHORS:
        try:
            items, _total = fts_dao.search_documents(
                conn, text, page=1, page_size=10)
        except sqlite3.Error:
            items = []
        for item in items:
            for r in conn.execute(
                    "SELECT entity_id, COUNT(*) AS n FROM entity_mention"
                    " WHERE document_id = ? GROUP BY entity_id"
                    " ORDER BY n DESC LIMIT 2", (item.id,)):
                _add(r["entity_id"])
    return list(found.values())


def _corpus_slice(conn: sqlite3.Connection, embedder: Embedder,
                  vectors: VectorIndex | None, text: str,
                  anchor_ids: list[int]) -> list[int]:
    fts_ids: list[int] = []
    try:
        items, _total = fts_dao.search_documents(
            conn, text, page=1, page_size=FTS_TOP_K)
        fts_ids = [i.id for i in items]
    except sqlite3.Error:
        log.exception("scope FTS failed")
    vec_ids: list[int] = []
    if vectors is not None:
        vecs = embedder.embed([text])
        if vecs:
            vec_ids = [doc_id for doc_id, _s in
                       vectors.search(vecs[0], k=VEC_TOP_K)]
    fused = rrf_fuse([fts_ids, vec_ids]) if vec_ids else fts_ids

    anchor_docs: list[int] = []
    if anchor_ids:
        marks = ",".join("?" * len(anchor_ids))
        anchor_docs = [r[0] for r in conn.execute(
            f"SELECT DISTINCT m.document_id FROM entity_mention m"
            f" JOIN document d ON d.id = m.document_id"
            f" WHERE m.entity_id IN ({marks})"
            f" AND COALESCE(d.published_at, d.fetched_at)"
            f"     >= date('now', '-{ANCHOR_DOC_DAYS} days')"
            f" ORDER BY COALESCE(d.published_at, d.fetched_at) DESC"
            f" LIMIT {ANCHOR_DOC_CAP}", anchor_ids)]

    ordered: list[int] = []
    seen: set[int] = set()
    for doc_id in [*fused, *anchor_docs]:
        canonical = conn.execute(
            "SELECT COALESCE(canonical_document_id, id) FROM document"
            " WHERE id = ?", (doc_id,)).fetchone()
        if canonical is None:
            continue
        cid = int(canonical[0])
        if cid not in seen:
            seen.add(cid)
            ordered.append(cid)
        if len(ordered) >= SLICE_CAP:
            break
    return ordered


def _load_docs(conn: sqlite3.Connection,
               doc_ids: list[int]) -> list[ScopeDoc]:
    docs: list[ScopeDoc] = []
    for doc_id in doc_ids:
        row = conn.execute(
            "SELECT d.id, d.title, d.published_at, d.url,"
            " s.name AS source_name, s.credibility_tier"
            " FROM document d LEFT JOIN source s ON s.id = d.source_id"
            " WHERE d.id = ?", (doc_id,)).fetchone()
        if row is not None:
            docs.append(ScopeDoc(
                document_id=row["id"], title=row["title"],
                source_name=row["source_name"],
                credibility_tier=row["credibility_tier"],
                published_at=row["published_at"], url=row["url"]))
    return docs


def _thread_membership(conn: sqlite3.Connection, doc_ids: list[int],
                       ) -> tuple[list[int], list[StoryRef]]:
    """Slice docs -> events -> stories -> FULL sibling chains."""
    if not doc_ids:
        return [], []
    marks = ",".join("?" * len(doc_ids))
    event_ids = [r[0] for r in conn.execute(
        f"SELECT DISTINCT event_id FROM event_assignment"
        f" WHERE document_id IN ({marks}) AND event_id IS NOT NULL",
        doc_ids)]
    stories: list[StoryRef] = []
    all_events: dict[int, None] = {e: None for e in event_ids}
    if event_ids:
        emarks = ",".join("?" * len(event_ids))
        story_rows = conn.execute(
            f"SELECT DISTINCT s.id, s.title FROM story s"
            f" JOIN event e ON e.story_id = s.id WHERE e.id IN ({emarks})",
            event_ids).fetchall()
        for srow in story_rows:
            stories.append(StoryRef(story_id=srow["id"], title=srow["title"]))
            for erow in conn.execute(
                    "SELECT id FROM event WHERE story_id = ?", (srow["id"],)):
                all_events.setdefault(erow[0], None)
    return list(all_events), stories


def _graph_expansion(conn: sqlite3.Connection, anchor_ids: list[int],
                     event_ids: list[int]) -> list[ScopeEdge]:
    """1-hop ACTIVE edges of anchors + slice events — all relations,
    including prior causal edges (the compounding read)."""
    clauses: list[str] = []
    params: list[int] = []
    for node_type, ids in (("entity", anchor_ids), ("event", event_ids)):
        if not ids:
            continue
        marks = ",".join("?" * len(ids))
        clauses.append(f"(src_type = '{node_type}' AND src_id IN ({marks}))")
        params.extend(ids)
        clauses.append(f"(dst_type = '{node_type}' AND dst_id IN ({marks}))")
        params.extend(ids)
    if not clauses:
        return []
    rows = conn.execute(
        "SELECT id, src_type, src_id, dst_type, dst_id, relation,"
        " properties, grade FROM edge"
        f" WHERE status = 'active' AND ({' OR '.join(clauses)})"
        f" ORDER BY id DESC LIMIT {EDGE_CAP}", params).fetchall()
    edges: list[ScopeEdge] = []
    for row in rows:
        speculation = False
        try:
            speculation = bool(
                json.loads(row["properties"] or "{}").get("speculation"))
        except ValueError:
            pass
        edges.append(ScopeEdge(
            edge_id=row["id"], src_type=row["src_type"],
            src_id=row["src_id"], dst_type=row["dst_type"],
            dst_id=row["dst_id"], relation=row["relation"],
            speculation=speculation, grade=row["grade"]))
    return edges


def _timeline(conn: sqlite3.Connection, event_ids: list[int],
              documents: list[ScopeDoc]) -> list[TimelineItem]:
    """Events + event-less docs, date-ascending (undated last)."""
    items: list[TimelineItem] = []
    for event_id in event_ids:
        row = conn.execute(
            "SELECT id, title, event_type, occurred_on, doc_count"
            " FROM event WHERE id = ?", (event_id,)).fetchone()
        if row is not None:
            items.append(TimelineItem(
                event_id=row["id"], date=row["occurred_on"],
                title=row["title"], event_type=row["event_type"],
                doc_count=row["doc_count"] or 0))
    assigned = {r[0] for r in conn.execute(
        "SELECT DISTINCT document_id FROM event_assignment"
        " WHERE event_id IS NOT NULL")}
    for doc in documents:
        if doc.document_id in assigned:
            continue
        items.append(TimelineItem(
            document_id=doc.document_id, date=doc.published_at,
            title=doc.title or f"document {doc.document_id}", doc_count=1))
    items.sort(key=lambda i: (i.date is None, i.date or "",
                              i.event_id or i.document_id or 0))
    return items[:TIMELINE_CAP]


def _calendar(conn: sqlite3.Connection,
              timeline: list[TimelineItem]) -> list[CalendarItem]:
    dates = [i.date for i in timeline if i.date]
    if not dates:
        return []
    lo, hi = min(dates)[:10], max(dates)[:10]
    rows = conn.execute(
        "SELECT id, kind, occurs_on, label FROM calendar_event"
        " WHERE julianday(occurs_on) >= julianday(?) - ?"
        " AND julianday(occurs_on) <= julianday(?) + ?"
        " ORDER BY occurs_on",
        (lo, CALENDAR_WINDOW_DAYS, hi, CALENDAR_WINDOW_DAYS)).fetchall()
    return [CalendarItem(calendar_event_id=r["id"], kind=r["kind"],
                         occurs_on=r["occurs_on"], label=r["label"])
            for r in rows]


def _event_lite(conn: sqlite3.Connection, event_id: int) -> EventLite | None:
    row = conn.execute(
        "SELECT e.id, e.title, e.occurred_on, e.event_type,"
        " COALESCE(et.window_days, ?) AS window_days"
        " FROM event e LEFT JOIN event_type et ON et.name = e.event_type"
        " WHERE e.id = ?", (DEFAULT_WINDOW_DAYS, event_id)).fetchone()
    if row is None:
        return None
    entity_ids = frozenset(r[0] for r in conn.execute(
        "SELECT DISTINCT m.entity_id FROM entity_mention m"
        " JOIN event_assignment ea ON ea.document_id = m.document_id"
        " WHERE ea.event_id = ?", (event_id,)))
    vec_row = conn.execute(
        "SELECT vector FROM event_embedding WHERE event_id = ?",
        (event_id,)).fetchone()
    centroid = tuple(_unpack(vec_row[0])) if vec_row is not None else None
    return EventLite(
        event_id=row["id"], title=row["title"],
        occurred_on=row["occurred_on"], window_days=row["window_days"],
        entity_ids=entity_ids, centroid=centroid)


def _reaction_candidates(conn: sqlite3.Connection,
                         event_ids: list[int]) -> list[ReactionCandidate]:
    events = [e for e in (_event_lite(conn, eid) for eid in event_ids)
              if e is not None]
    if not events:
        return []
    marks = ",".join("?" * len(CAUSAL_RELATIONS))
    existing = [(r[0], r[1]) for r in conn.execute(
        "SELECT src_id, dst_id FROM edge WHERE status = 'active'"
        " AND src_type = 'event' AND dst_type = 'event'"
        f" AND relation IN ({marks})", CAUSAL_RELATIONS)]
    return reaction_candidate_pairs(events, existing)


def _marker_docs(conn: sqlite3.Connection, doc_ids: list[int],
                 ) -> list[tuple[int, str | None, str]]:
    out: list[tuple[int, str | None, str]] = []
    for doc_id in doc_ids[:MARKER_DOC_CAP]:
        row = conn.execute(
            "SELECT id, title, content_text FROM document WHERE id = ?",
            (doc_id,)).fetchone()
        if row is not None:
            out.append((row["id"], row["title"], row["content_text"] or ""))
    return out


def _coverage(documents: list[ScopeDoc]) -> str:
    """'thin' when <8 non-dup docs, <2 publisher domains, or no tier<=2."""
    if len(documents) < THIN_MIN_DOCS:
        return "thin"
    domains = set()
    for doc in documents:
        if doc.url:
            netloc = doc.url.split("//", 1)[-1].split("/", 1)[0].lower()
            domains.add(netloc[4:] if netloc.startswith("www.") else netloc)
        elif doc.source_name:
            domains.add(f"source:{doc.source_name.lower()}")
    if len(domains) < THIN_MIN_DOMAINS:
        return "thin"
    if not any(d.credibility_tier is not None and d.credibility_tier <= 2
               for d in documents):
        return "thin"
    return "ok"


# -- prompt rendering ---------------------------------------------------------------------


def render_scope_pack(pack: ScopePack) -> str:
    """The loop's opening user message — everything the deterministic
    pre-pass knows, including the CLOSED reaction-candidate menu."""
    lines = [f"INVESTIGATION TARGET ({pack.input_type}): {pack.input_text}",
             ""]
    if pack.coverage == "thin":
        lines.append(
            "COVERAGE: THIN — the local corpus is sparse on this topic. "
            "Use web_search early to gather primary material before "
            "drawing conclusions.")
        lines.append("")
    if pack.anchors:
        lines.append("ANCHOR ENTITIES:")
        lines.extend(f"- entity #{a.entity_id} {a.name} ({a.entity_type})"
                     for a in pack.anchors)
        lines.append("")
    if pack.timeline:
        lines.append("TIMELINE (events and unclustered documents):")
        for item in pack.timeline:
            ref = (f"event #{item.event_id}" if item.event_id
                   else f"document #{item.document_id}")
            lines.append(f"- {item.date or 'undated'} | {ref} | {item.title}"
                         + (f" [{item.event_type}]" if item.event_type
                            else ""))
        lines.append("")
    if pack.documents:
        lines.append("CORPUS SLICE (top documents):")
        for doc in pack.documents[:20]:
            tier = (f"tier {doc.credibility_tier}"
                    if doc.credibility_tier else "tier ?")
            lines.append(f"- document #{doc.document_id}"
                         f" ({doc.source_name or 'unknown'}, {tier},"
                         f" {doc.published_at or 'undated'})"
                         f" {doc.title or '(untitled)'}")
        lines.append("")
    if pack.edges:
        lines.append("KNOWN GRAPH EDGES (1-hop, incl. prior causal edges):")
        for e in pack.edges[:30]:
            spec = " [speculative]" if e.speculation else ""
            lines.append(f"- edge #{e.edge_id} {e.src_type}#{e.src_id}"
                         f" -[{e.relation}]-> {e.dst_type}#{e.dst_id}{spec}")
        lines.append("")
    if pack.calendar:
        lines.append("CALENDAR CONTEXT:")
        lines.extend(f"- {c.occurs_on} {c.label} ({c.kind})"
                     f" [calendar #{c.calendar_event_id}]"
                     for c in pack.calendar)
        lines.append("")
    if pack.reaction_candidates:
        lines.append(
            "REACTION CANDIDATES (closed menu — a speculative reaction/"
            "trigger finding MUST cite one of these candidate_ids):")
        for c in pack.reaction_candidates:
            lines.append(
                f"- {c.candidate_id}: event #{c.src_event_id}"
                f" '{c.src_title}' may be a reaction to event"
                f" #{c.dst_event_id} '{c.dst_title}'"
                f" ({c.days_apart:.0f}d apart, entity overlap"
                f" {c.entity_jaccard:.2f}, similarity {c.cosine:.2f})")
        lines.append("")
    if pack.causal_markers:
        lines.append("CAUSAL-MARKER SENTENCES (pre-scanned; read the "
                     "document and ground the connection with a verbatim "
                     "quote before recording it):")
        for m in pack.causal_markers:
            lines.append(f"- document #{m.document_id}"
                         f" ('{m.title or 'untitled'}', marker"
                         f" '{m.marker}'): \"{m.sentence}\"")
        lines.append("")
    return "\n".join(lines)
