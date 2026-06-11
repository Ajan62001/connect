# Design Document — Investigation Mode ("connect" v0.1, migration slot v8)

> Commissioned from the user directive: "I want to do a wholistic understanding of a topic. grounded on
> articles. connecting one thing to another. as it is most of the time its natural, one can be reaction
> to other. Agent should be why mindset — like why this happened, why they did not do that way. kind of
> investigative journalist."
>
> USER DECISIONS (override anything below that conflicts):
> - Investigations get a SEPARATE daily budget: `investigation_daily_budget_usd` default **$10.00**
>   (own governor over ledger purposes investigation*, independent of the $2/day general governor).
> - Investigation-fetched web docs land at **tier 4** ("Web (investigation)" seed source) until manually promoted.
> - Question recursion is **manual-click only** (no auto-spawn).
> - Speculative causal edges are **first-class** in the shared graph with distinct hypothesis styling.
> - Per-run default cap $1.00 with $0.20 synthesis reserve.

**Spec restated:** given a topic, build a wholistic, article-grounded understanding by connecting things —
most connections are natural ("one thing is a reaction to another") — driven by an investigative-journalist
"why" mindset. Every connection is either quote-grounded or explicitly speculative; what cannot be answered
becomes a first-class open question.

**Shape:** an investigation is a `dossier` row with `kind='investigation'` that runs three stages —
`scope` (deterministic, code-only) → `investigate` (the first real `tool_loop`, BALANCED tier) →
`synthesize` (one DEEP call + code-rendered sections). The loop persists **findings** and **questions**
incrementally (crash loses nothing already found), writes **causal edges at grade 2** so every
investigation makes the next one smarter, and streams per-iteration progress over the existing
`job_event` SSE machinery exactly like the Phase-3 `/api/analyses/{id}/events` contract.

---

## 1. Input & scoping

### 1.1 Input contract

```python
# investigation/schema.py (frozen)
class InvestigationSeed(BaseModel):
    topic: str | None = None          # free text
    entity_id: int | None = None
    event_id: int | None = None
    story_id: int | None = None
    question_id: int | None = None    # recursion: spawned from an open question
    # exactly one must be set (validator)

class InvestigationOptions(BaseModel):
    budget_usd: float = 1.00
    max_iterations: int = 14
    max_web_fetches: int = 8
```

`POST /api/investigations` creates `dossier(kind='investigation', input_type='topic'|'entity'|'event'|'story')`
plus a `job(kind='investigation')`, returns 202.

### 1.2 The scope stage (`investigation/scoping.py`, zero LLM)

Deterministic pre-pass producing a frozen `ScopePack` persisted as the `scope` dossier_section:

1. **Anchor resolution.** Topic → entity alias-exact match + FTS over entity/document_fts + vec top-k → anchor entities (≤6).
2. **Corpus slice.** FTS top-50 + vec top-50 RRF-fused; union with entity_mention docs of anchors (90d, cap 60); near-dups collapse to canonical.
3. **Graph expansion.** 1-hop edges of anchors (all relations incl. follows, links_to, prior causal edges — the compounding read).
4. **Thread membership.** Slice events → story → full sibling chains.
5. **Timeline assembly.** Events + undated docs ordered by date.
6. **Calendar context.** calendar_event within ±120 days of timeline span.
7. **Reaction candidates** (§3.2b) and **causal-marker pre-scan** (§3.2a) included in the pack.

**"Need more material?"** — code heuristic at scope (<8 non-dup docs, <2 domains, or no tier≤2 → pack stamped
`coverage: "thin"`, loop prompted to web-search first) + in-loop rule (web_search when corpus returns <3
relevant hits or single-domain). Every fetched article goes through `IngestionPipeline.ingest_url`
(permanent doc: dedup, blob, FTS, embedding, watch-match, links) + synchronous T1, attached to seeded
source `("Web (investigation)", type='search', tier=4)`.

## 2. The investigation loop — implementing `LLMProvider.tool_loop`

### 2.1 Provider surface (`llm/provider.py` + `llm/anthropic_provider.py`)

Transport primitive (abstract) + loop template (concrete on the ABC):

```python
class ToolDef(BaseModel):  name: str; description: str; input_schema: dict
class ToolCall(BaseModel): id: str; name: str; input: dict
class ToolTurn(BaseModel):
    text: str; tool_calls: list[ToolCall]; stop_reason: str
    raw_content: list[dict]; model: str; usage: Usage

class LLMProvider(ABC):
    @abstractmethod
    async def complete_with_tools(self, *, system, messages, tools, tier,
                                  max_tokens, cache: bool = True) -> ToolTurn: ...
    async def tool_loop(self, *, system, messages, tools, executor, tier,
                        max_iterations, on_turn=None,
                        terminal_tools=frozenset({"conclude"})) -> ToolLoopResult: ...
```

AnthropicProvider: one `messages.create` per turn with `cache_control={"type":"ephemeral"}` on the system
block — each iteration re-reads system + tools + prior turns at ~0.1×; system prompt padded past sonnet's
2048-token cache minimum.

### 2.2 Tools (`investigation/tools.py` — frozen ToolDefs, executor closures over (conn, pipeline, search_client, scope_pack, state))

| Tool | input (required*) | Executor |
|---|---|---|
| `search_corpus` | query*, top_k=8, published_before/after | FTS+vec RRF; returns [{document_id,title,source,tier,published_at,snippet}]; touched docs get T2-promotion flag |
| `web_search` | query*, max_results=5 | SearchClient (Tavily; Null → is_error "web search unavailable; corpus only") |
| `fetch_and_ingest` | url* | ingest_url → permanent doc + sync T1; counts vs max_web_fetches; dup → existing id free |
| `read_document` | document_id*, offset=0 | meta + content_text[offset:offset+6000] + has_more |
| `graph_neighborhood` | node_type*, node_id*, relations? | 1-hop active edges grouped by relation with provenance/speculation/grade |
| `entity_timeline` | entity_id or name*, window_days=365 | events + claims by/about with verdicts |
| `calendar_context` | date*, window_days=120 | calendar rows near date |
| `record_finding` | kind* ∈ reaction,trigger,alternative,actor_motive,timing,context,consequence; text*; evidence*: [{document_id, quote}] ([] only when speculation+kind allows); speculation=false; confidence?; question_id?; link?: {src_type,src_id,relation,dst_type,dst_id}; candidate_id? | **THE GROUNDING GATE** §3.4 — persists finding+evidence, writes causal edge grade 2, SSE; is_error with exact reason on validation failure (one retry) |
| `raise_question` | qtype* ∈ taxonomy; text*; about?; priority=0.5 | insert question(open) |
| `conclude` | summary*; resolutions*: [{question_id, status answered|partial|open, answer_summary, finding_ids}]; confidence* | TERMINAL: marks questions, stores summary, ends loop |

### 2.3 Loop driver (`investigation/runner.py`)

Manual loop (per-call ledger, budget metering, truncation, own SSE):
- per turn: complete_with_tools → record_call(purpose='investigation') → budget.debit → emit `iteration` SSE
- budget exhausted / last iteration / end_turn without tools → forced conclude via tool_choice
- parallel tool calls allowed within one iteration; results returned as tool_result blocks (is_error on validation failures)
- durable state = finding/question/edge rows + job_event log (loop not checkpoint-resumable in v1; synthesize re-runnable from rows alone)
- SSE events: iteration{n,tools,cost_so_far}, finding_recorded, question_raised, question_resolved, doc_ingested, section_completed, done, error — job_event.seq ids, ?after= replay
- caps: max_iterations=14 turns, max_web_fetches=8, BudgetExceeded → forced conclude

### 2.4 Tiers & prompt

Loop = BALANCED (sonnet); synthesis = DEEP (opus); question gen = BALANCED. System prompt
(`investigation/prompts.py`, frozen, >2048 tokens): investigative-journalist method — work the open
questions; corpus before web; follow citation chains to primary sources; verbatim quotes only; tag
inference as speculation; never invent alternatives; question taxonomy + causal vocabulary + causal-marker
lexicon ("in response to", "following", "in the wake of", "after backlash over", "prompted by", "to counter"…);
stop criteria.

## 3. The "why mindset", operationalized

### 3.1 Typed why-questions — first-class persisted objects

`QUESTION_TYPES = ("why_now","why_this_design","why_not_alternative","who_pushed","what_triggered","why_silent","who_benefits","what_next")`.

After scope, ONE BALANCED structured call generates ≤10 typed questions bound to timeline items
(why_now for dated decisions near calendar events; what_triggered for thread-opening events; why_silent
for central-but-silent actors; why_not_alternative for policy-design events). Persisted as
question rows (status open) = the loop's work queue, rendered live. Agent may raise_question mid-loop.
conclude.resolutions flips statuses; remaining open questions are A PRODUCT FEATURE (open-questions
section + recursion button), not a failure.

### 3.2 Reaction detection — two epistemic channels

**(a) Explicit textual causality → grounded edge.** Scope-time FTS pre-scan for causal-marker phrases
intersected with anchor-entity docs → candidate sentences in the pack. Agent reads doc, records finding
with verbatim quote → span verified in code → edge with provenance_document_id, properties.quote,
speculation:false, grade 2.

**(b) Inferred (timing + actor overlap + topical similarity) → speculation-tagged hypothesis.** Computed
IN CODE at scope: ordered pairs (B after A) within A.event_type.window_days×3, entity-Jaccard ≥ 0.3,
centroid cosine ≥ 0.55, no existing causal edge → candidate_id menu. A speculative reaction/trigger
finding MUST reference a candidate_id from this closed menu (validator rejects free-floating speculation).
Edge carries speculation:true + confidence + score components in properties.

### 3.3 Alternatives — "why they did not do that way"

Mined, never invented: PRS/committee analyses, consultation coverage, opposition statements, expert
op-eds, earlier drafts (links_to chains from official docs). `record_finding(kind='alternative')` REQUIRES
non-empty evidence and speculation=false. No sources → the question stays open (that IS the answer surface).

### 3.4 The record_finding validator (`investigation/writeback.py`)

1. every quote verbatim-verifies (shared span verifier; one retry then drop+count)
2. alternative → evidence required, speculation forbidden
3. speculation+reaction/trigger → candidate_id required, must be in menu
4. evidence=[] only when speculation=true
5. link node ids exist; relation in causal vocabulary; src/dst types match relation signature
6. success: finding + finding_evidence rows; edge insert (idempotent); question→partial

## 4. Causal graph writeback

`EDGE_RELATIONS += ('reaction_to','triggered_by','enables','blocks','alternative_to')` (Python-enforced;
edge.relation is free TEXT). Signatures: reaction_to/triggered_by event→event; enables/blocks
event|entity→event|entity; alternative_to policy-entity|event→same.

`storage/edges.py::insert_causal(...)` — extends insert() with properties JSON
({quote, speculation, finding_id, score_components?}), provenance_dossier_id, confidence, grade=2.
Grade lattice protects from enrichment overwrites; bitemporal columns give SCD2 free.
Compounding: graph_neighborhood + scope pre-pass surface these to later investigations; entity pages
render them (speculation styled).

## 5. Output — the investigation dossier

Stages: scope, investigate, synthesize. Section rows: timeline, causal_narrative, actors, alternatives,
open_questions, watch_next.

| section | producer | content |
|---|---|---|
| timeline | code | {items:[{event_id?, document_id?, date, title, event_type?, doc_count, causal:[{edge_id, relation, other_id, other_title, speculation}]}]} |
| causal_narrative | DEEP | {narrative_md with [[f#]] markers, chains:[{steps:[{node_type,node_id,title,relation_to_next?,speculation,finding_id?}]}]} |
| actors | DEEP (same call) | {actors:[{entity_id,name,role,motive_md,finding_ids,speculation}]} |
| alternatives | code | {items:[{title,description,finding_ids,by_whom_entity_id?}], unanswered_question_ids} |
| open_questions | code | {items:[{question_id,qtype,text,status,priority,spawned_dossier_id?}]} |
| watch_next | DEEP (same call) | {items:[{text, question_id?, calendar_event_id?, watch_suggestion?}]} — each item must anchor to a question or calendar id (validator) |

**Synthesis grounding:** closed evidence menu = the findings table (f<id> → kind/text/quote/document_id/
speculation). One DEEP structured call sees ONLY menu + timeline + questions. Post-hoc: every [[f#]]
resolves (unknown → one regeneration then strip+flag in grounding_report); finding_ids exist; chains citing
speculative findings inherit speculation. UI: [[f#]] → CitationChip → DocumentSheet with quote highlighted.

## 6. Cost & caps

Per-run budget default $1.00; **separate daily governor: investigation_daily_budget_usd = $10.00** over
purposes investigation/investigation_t1/investigation_synthesis (general $2/day governor untouched by
investigations). Typical run ≈ $0.56 (question gen $0.04, loop ~13 cached turns $0.34, T1 on fetches $0.03,
DEEP synthesis $0.15). Degrade ladder: 60% → corpus-only; 85% → forced conclude; $0.20 synthesis reserve;
over-reserve → BALANCED synthesis fallback (config investigation_synthesis_tier).

## 7. API + frontend

Endpoints (`api/routers/investigations.py`): POST /api/investigations (202 {investigation_id, job_id};
429 on governor) · GET /api/investigations?page= (Page with counts{findings,questions_open,questions_answered,
docs_added}) · GET /api/investigations/{id} (snapshot: stages, sections, questions, findings w/ evidence,
cost_usd, last_seq) · GET /api/investigations/{id}/events (SSE ?after=) · POST .../cancel ·
POST /api/questions/{id}/investigate (202 — manual recursion; sets spawned_dossier_id + parent_question_id).

Frontend: /investigations (input card + EntityAutocomplete + recent list) · /investigation/[id] one route
live+finished: StageTimeline (scope→investigate→synthesize) + ActivityLog (iteration lines) + Cancel;
sections as they complete: InvestigationTimeline (causal chips under items — solid grounded, dashed+
"hypothesis" badge speculative), CausalChainList (narrative_md [[f#]] → CitationChip → DocumentSheet),
ActorsPanel, AlternativesPanel, OpenQuestionsPanel ([Investigate this] recursion button), WatchNextPanel
(+Watch button → watch CRUD). Entity/event/thread pages gain an "Investigate" button.
useInvestigation(id) clones the useAnalysis snapshot+SSE reducer.

## 8. Schema deltas — migration v8

```sql
CREATE TABLE question (
    id INTEGER PRIMARY KEY,
    dossier_id INTEGER NOT NULL REFERENCES dossier(id) ON DELETE CASCADE,
    qtype TEXT NOT NULL CHECK (qtype IN ('why_now','why_this_design','why_not_alternative',
            'who_pushed','what_triggered','why_silent','who_benefits','what_next')),
    text TEXT NOT NULL,
    about_type TEXT CHECK (about_type IN ('entity','event','claim','document','dossier')),
    about_id INTEGER,
    status TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('open','partial','answered','dropped')),
    priority REAL NOT NULL DEFAULT 0.5,
    answer_summary TEXT,
    answer_finding_ids TEXT NOT NULL DEFAULT '[]',
    spawned_dossier_id INTEGER REFERENCES dossier(id),
    created_at TEXT NOT NULL, updated_at TEXT
);
CREATE INDEX idx_question_dossier ON question(dossier_id);
CREATE INDEX idx_question_about ON question(about_type, about_id);

CREATE TABLE finding (
    id INTEGER PRIMARY KEY,
    dossier_id INTEGER NOT NULL REFERENCES dossier(id) ON DELETE CASCADE,
    kind TEXT NOT NULL CHECK (kind IN ('reaction','trigger','alternative','actor_motive',
            'timing','context','consequence')),
    text TEXT NOT NULL,
    speculation INTEGER NOT NULL DEFAULT 0,
    confidence REAL,
    question_id INTEGER REFERENCES question(id),
    edge_id INTEGER REFERENCES edge(id),
    payload TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL
);
CREATE INDEX idx_finding_dossier ON finding(dossier_id);

CREATE TABLE finding_evidence (
    id INTEGER PRIMARY KEY,
    finding_id INTEGER NOT NULL REFERENCES finding(id) ON DELETE CASCADE,
    document_id INTEGER NOT NULL REFERENCES document(id),
    quote TEXT NOT NULL,
    quote_start INTEGER, quote_end INTEGER
);
CREATE INDEX idx_finding_evidence_finding ON finding_evidence(finding_id);
```

Rebuilds (exact-DDL pattern): dossier + kind CHECK ('analysis','investigation') + parent_question_id +
budget_usd; DOSSIER_INPUT_TYPES += ('topic','entity','story'); dossier_section stages += ('scope',
'investigate','synthesize','timeline','causal_narrative','actors','alternatives','open_questions',
'watch_next'); job kinds += ('investigation',). Python-only: EDGE_RELATIONS/QUESTION_TYPES/FINDING_KINDS/
QUESTION_STATUSES. Seed: source("Web (investigation)", type='search', credibility_tier=4).

## 9. Phasing

**v1 slice (one build):** complete_with_tools + tool_loop template; investigation/ package (scoping,
questions, tools, runner, reactions, writeback, synthesize, prompts); v8 migration; all 10 tools;
both reaction channels; alternatives discipline; causal writeback; sections + one-DEEP synthesis;
API incl. SSE + manual recursion; both pages list-first. Tests: validator table (span rejection,
alternative-without-evidence, speculative-without-candidate), candidate-pair math, loop driver with
MockProvider scripted calls (budget degrade, forced conclude, cancel), citation checker, migration drill,
SSE replay.

**Follow-ups:** domain→registry source resolution; re-investigation deltas; cytoscape causal graph;
overnight batch investigations from brief suggestions; adaptive thinking; questions on entity pages.

**Plan impact:** provenance backtracking = same tool_loop + report_origin terminal tool + goal prompt
(old Phase 4 shrinks to prompt+schema+PRS/Kanoon adapters+UI); forces/cui-bono absorbed into
who_benefits/who_pushed questions + actors section; Phase 5 retains impacts + full reconciliation.
