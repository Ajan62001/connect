"""P4 ETL round-trip: synthetic SQLite v9 fixture -> Postgres -> assertions.

Builds a REAL v9 SQLite database (legacy schema create_all, then the job
table stripped back to its v9 shape and the version stamp set to 9) with at
least one row in every copied table plus the deliberate edge cases the
design calls out: circular dossier<->question FKs, forward-pointing
edge.superseded_by_edge_id, a document self-FK, negative simhash, malformed
aliases JSON, a wrong-dimension vector (must be skipped + logged), and all
four origin-backfill classes. The ETL's own verification report (counts,
checksums, FTS/vec smoke) is the primary assertion; targeted SQL spot-checks
cover the tenancy bootstrap rules of v02-tenancy-auth.md §7.
"""

from __future__ import annotations

import sqlite3
import struct

import psycopg
import pytest

from connect.tools.etl_sqlite_to_pg import (
    DEFAULT_VEC_MODEL,
    EtlError,
    main,
    run_etl,
)
from connect.tools.legacy_sqlite import schema as legacy_schema

T = "2026-06-10T08:00:00.000Z"
T2 = "2026-06-11T09:30:00.000Z"
ADMIN = "admin@example.com"
FTS_QUERIES = ["policy", "inflation", "zzznothing"]


def _vec(seed: float) -> list[float]:
    return [((seed * (i + 1)) % 7) / 7.0 for i in range(384)]


def _pack(vec: list[float]) -> bytes:
    return struct.pack(f"{len(vec)}f", *vec)


def _build_v9(path) -> None:
    conn = sqlite3.connect(str(path))
    legacy_schema.create_all(conn)
    with conn:
        # back to the honest v9 shape: cancel_requested arrived at v10.
        conn.execute("ALTER TABLE job DROP COLUMN cancel_requested")
        conn.execute(
            "UPDATE meta SET value = '9' WHERE key = 'schema_version'")

        conn.execute(
            "INSERT INTO source (id, name, type, config, credibility_tier,"
            " enabled, t1_exempt, created_at) VALUES"
            " (1, 'Feed A', 'rss', '{\"url\": \"https://e.x/f\"}', 2, 1, 0, ?),"
            " (2, 'Web (investigation)', 'search', '{}', 3, 1, 1, ?)", (T, T))

        docs = [
            # (id, source_id, url, title, media, content, hash, simhash,
            #  canonical, watch_hit)
            (1, 1, "https://e.x/a1", "Reserve bank policy decision",
             "html", "The reserve bank announced a policy decision targeting"
             " inflation in the economy.", "h1", 1234567890123, None, 0),
            (2, 1, "https://e.x/a2", "Policy reaction roundup",
             "html", "Markets reacted to the policy decision with caution"
             " amid inflation worries.", "h2", -987654321, 1, 1),
            (3, 2, "https://web.x/bg", "Investigation policy backgrounder",
             "html", "Background memo on inflation policy fetched during an"
             " investigation.", "h3", None, None, 0),
            (4, None, "https://gov.x/circ.pdf", "Official circular",
             "pdf", "Circular text body about reserve requirements.",
             "h4", 42, None, 0),
            (5, None, "https://blog.x/p", "Reader submitted article",
             "html", "User submitted url content about elections.",
             "h5", None, None, 0),
            (6, None, None, "Pasted note",
             "text", "A pasted note for private analysis.", "h6", None,
             None, 0),
        ]
        for (i, sid, url, title, media, text, h, sim, canon, wh) in docs:
            conn.execute(
                "INSERT INTO document (id, source_id, url, title,"
                " published_at, fetched_at, media_type, content_text,"
                " content_hash, raw_blob_path, enrichment_tier,"
                " enrichment_status, simhash, canonical_document_id,"
                " watch_hit) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (i, sid, url, title, T, T2, media, text, h,
                 f"blobs/ab/{h}", 1, "done", sim, canon, wh))

        conn.execute(
            "INSERT INTO document_link (id, document_id, url, anchor_text,"
            " is_file, is_official, status, resolved_document_id, created_at)"
            " VALUES (1, 1, 'https://gov.x/circ.pdf', 'circular', 1, 1,"
            " 'fetched', 4, ?), (2, 1, 'https://e.x/o', 'other', 0, 0,"
            " 'not_followed', NULL, ?)", (T, T))
        conn.execute(
            "INSERT INTO document_topic (document_id, topic, source)"
            " VALUES (1, 'monetary-policy', 't1'), (1, 'banking', 'rule')")
        conn.execute(
            "INSERT INTO document_enrichment (document_id, summary,"
            " event_type, model, prompt_version, created_at)"
            " VALUES (1, 'RBI cut rates.', 'policy_change', 'mock', 't1-v1',"
            " ?)", (T,))

        conn.execute(
            "INSERT INTO entity (id, name, entity_type, aliases, attrs,"
            " grade, created_at) VALUES"
            " (1, 'Reserve Bank of India', 'organization',"
            "  '[\"RBI\", \"central bank\"]', '{\"k\": 1}', 2, ?),"
            " (2, 'Broken Alias Corp', 'company', 'oops[not-json', '{}',"
            "  1, ?)", (T, T))
        conn.execute(
            "INSERT INTO entity_mention (id, document_id, entity_id,"
            " surface, span_start, span_end, method, grade, created_at)"
            " VALUES (1, 1, 1, 'RBI', 4, 7, 'alias', 1, ?)", (T,))

        conn.execute(
            "INSERT INTO event_type (id, name, lifecycle_group, window_days)"
            " VALUES (1, 'policy_change', 'policy', 14)")
        conn.execute(
            "INSERT INTO story (id, title, root_event_id, status, doc_count,"
            " summary_stale, created_at) VALUES"
            " (1, 'Rate cut saga', 1, 'active', 2, 1, ?)", (T,))
        conn.execute(
            "INSERT INTO event (id, title, event_type, story_id, occurred_on,"
            " date_precision, doc_count, window_start, window_end,"
            " last_seen_at, grade, created_at) VALUES"
            " (1, 'RBI cuts repo rate', 'policy_change', 1, '2026-06-10',"
            " 'day', 2, '2026-06-10', '2026-06-17', '2026-06-11', 1, ?)",
            (T,))
        conn.execute(
            "INSERT INTO event_assignment (id, document_id, event_id, method,"
            " score, created_at) VALUES (1, 1, 1, 'attach', 0.91, ?)", (T,))

        # circular pair: dossier 2's parent question belongs to dossier 1.
        conn.execute(
            "INSERT INTO dossier (id, kind, title, input_text, input_type,"
            " input_document_id, parent_question_id, budget_usd, status,"
            " model_usage, created_at) VALUES"
            " (1, 'analysis', 'A1', 'claim text', 'claim', 1, NULL, 2.0,"
            "  'completed', '{\"haiku\": 3}', ?),"
            " (2, 'investigation', 'I1', 'why now', 'topic', NULL, 1, 10.0,"
            "  'completed', '{}', ?)", (T, T))
        conn.execute(
            "INSERT INTO dossier_section (id, dossier_id, stage, status,"
            " content, created_at) VALUES"
            " (1, 1, 'verify', 'completed', '{\"claims\": []}', ?)", (T,))
        conn.execute(
            "INSERT INTO question (id, dossier_id, qtype, text, about_type,"
            " about_id, status, priority, answer_finding_ids,"
            " spawned_dossier_id, created_at) VALUES"
            " (1, 1, 'why_now', 'Why now?', 'event', 1, 'answered', 0.9,"
            " '[1]', 2, ?)", (T,))

        # edge 2 superseded by the NEWER edge 3 (forward self-FK).
        conn.execute(
            "INSERT INTO edge (id, src_type, src_id, dst_type, dst_id,"
            " relation, properties, provenance_document_id, confidence,"
            " valid_from, status, superseded_by_edge_id, grade, created_at)"
            " VALUES"
            " (1, 'document', 1, 'document', 4, 'links_to', '{}', 1, NULL,"
            "  NULL, 'active', NULL, 1, ?),"
            " (2, 'event', 1, 'event', 1, 'follows', '{\"v\": 1}', NULL, 0.4,"
            "  '2026-06-01', 'superseded', 3, 2, ?),"
            " (3, 'event', 1, 'event', 1, 'follows', '{\"v\": 2}', NULL, 0.8,"
            "  '2026-06-10', 'active', NULL, 2, ?)", (T, T, T))

        conn.execute(
            "INSERT INTO claim (id, text, claim_type, first_document_id,"
            " verdict, confidence, check_worthiness, created_at) VALUES"
            " (1, 'Repo rate cut by 50 bps.', 'factual', 1, 'supported',"
            "  0.9, 0.8, ?),"
            " (2, 'Inflation will fall next year.', 'prediction', 2,"
            "  'unverified', NULL, 0.5, ?)", (T, T))
        conn.execute(
            "INSERT INTO evidence (id, claim_id, document_id, stance,"
            " confidence, rationale, quote, method, grade, created_at)"
            " VALUES (1, 1, 1, 'supports', 0.95, 'states it', 'cut by 50',"
            " 'llm', 2, ?)", (T,))
        conn.execute(
            "INSERT INTO claim_sighting (id, claim_id, document_id, quote,"
            " stance, grade, created_at)"
            " VALUES (1, 1, 1, 'cut by 50', 'asserts', 1, ?),"
            " (2, 2, 2, 'will fall', 'reports', 1, ?)", (T, T))
        conn.execute(
            "INSERT INTO verdict_history (id, claim_id, verdict, computed_at,"
            " \"trigger\", evidence_snapshot)"
            " VALUES (1, 1, 'supported', ?, 'new_evidence', '[1]')", (T,))
        conn.execute(
            "INSERT INTO contradiction (id, claim_id, n_support, n_refute,"
            " status, detected_at) VALUES (1, 2, 1, 1, 'open', ?)", (T,))

        conn.execute(
            "INSERT INTO finding (id, dossier_id, kind, text, speculation,"
            " confidence, question_id, edge_id, payload, created_at)"
            " VALUES (1, 2, 'trigger', 'Triggered by X', 0, 0.7, 1, 3,"
            " '{\"link\": {}}', ?)", (T,))
        conn.execute(
            "INSERT INTO finding_evidence (id, finding_id, document_id,"
            " quote, quote_start) VALUES (1, 1, 3, 'memo says', 5)")

        conn.execute(
            "INSERT INTO statement (id, document_id, entity_id, quote,"
            " topics, position_summary, stated_at, grade, created_at) VALUES"
            " (1, 1, 1, 'We will act.', '[\"monetary-policy\"]', 'hawkish',"
            "  ?, 1, ?),"
            " (2, 2, 1, 'We are patient.', '[\"monetary-policy\"]',"
            "  'dovish', ?, 1, ?)", (T, T, T2, T2))
        conn.execute(
            "INSERT INTO position_shift (id, entity_id, topic,"
            " from_statement_id, to_statement_id, kind, detected_at, status)"
            " VALUES (1, 1, 'monetary-policy', 1, 2, 'shifted', ?, 'open')",
            (T2,))
        conn.execute(
            "INSERT INTO view_summary (entity_id, topic, text, citations,"
            " statement_count_at_gen, generated_at)"
            " VALUES (1, 'monetary-policy', 'evolved', '[1, 2]', 2, ?)",
            (T2,))

        conn.execute(
            "INSERT INTO job (id, kind, payload, dossier_id, status,"
            " attempts, error, created_at, started_at, finished_at) VALUES"
            " (1, 'poll_source', '{\"source_id\": 1}', NULL, 'done', 1,"
            "  NULL, ?, ?, ?),"
            " (2, 'analysis', '{\"dossier_id\": 1}', 1, 'queued', 0, NULL,"
            "  ?, NULL, NULL)", (T, T, T2, T2))
        conn.execute(
            "INSERT INTO job_event (seq, job_id, ts, type, data) VALUES"
            " (1, 1, ?, 'stage', '{\"s\": \"fetch\"}'),"
            " (2, 1, ?, 'done', '{}')", (T, T2))

        conn.execute(
            "INSERT INTO watch (id, kind, label, entity_id, query_fts,"
            " promote, muted, created_at)"
            " VALUES (1, 'entity', 'RBI watch', 1, NULL, 1, 0, ?)", (T,))
        conn.execute(
            "INSERT INTO watch_hit (watch_id, object_type, object_id,"
            " created_at) VALUES (1, 'document', 1, ?)", (T,))
        conn.execute(
            "INSERT INTO brief (id, brief_date, generated_at, gloss_text)"
            " VALUES (1, '2026-06-11', ?, 'Morning brief')", (T2,))
        conn.execute(
            "INSERT INTO brief_item (id, brief_id, section, rank,"
            " object_type, object_id, reason_json, payload, seen) VALUES"
            " (1, 1, 'watch_dev', 1, 'document', 1, '{\"w\": 1}',"
            " '{\"title\": \"x\"}', 0)")
        conn.execute(
            "INSERT INTO view_cursor (surface, ref_id, last_seen_at)"
            " VALUES ('feed', 0, ?)", (T2,))
        conn.execute(
            "INSERT INTO calendar_event (id, kind, scope, occurs_on, ends_on,"
            " label) VALUES (1, 'rbi_mpc', 'national', '2026-08-04',"
            " '2026-08-06', 'MPC meet')")
        conn.execute(
            "INSERT INTO llm_call (id, purpose, model, input_tokens,"
            " output_tokens, cost_estimate, created_at)"
            " VALUES (1, 't1', 'mock-model', 1000, 200, 0.0042, ?)", (T,))
        conn.execute(
            "INSERT INTO source_stats (source_id, day, items, dups,"
            " quality_score) VALUES (1, '2026-06-10', 7, 2, 0.8)")

        # vectors: two good document vectors, one WRONG-DIM row (skipped),
        # one event + one claim vector.
        conn.execute(
            "INSERT INTO document_embedding (document_id, model, dim, vector)"
            " VALUES (1, 'bge-test', 384, ?), (2, 'bge-test', 384, ?),"
            " (5, 'bge-test', 8, ?)",
            (_pack(_vec(1.7)), _pack(_vec(2.3)),
             _pack([0.5] * 8)))
        conn.execute(
            "INSERT INTO event_embedding (event_id, model, dim, vector)"
            " VALUES (1, 'centroid', 384, ?)", (_pack(_vec(3.1)),))
        conn.execute(
            "INSERT INTO claim_embedding (claim_id, model, dim, vector)"
            " VALUES (1, 'bge-test', 384, ?)", (_pack(_vec(4.9)),))
    conn.close()


@pytest.fixture()
def v9_path(tmp_path):
    path = tmp_path / "v9.db"
    _build_v9(path)
    return str(path)


def _pg(dsn: str) -> psycopg.Connection:
    return psycopg.connect(dsn, autocommit=True)


def _one(conn, sql, *params):
    return conn.execute(sql, params or None).fetchone()


# --- refusals -----------------------------------------------------------------


def test_refuses_unsupported_source_version(v9_path, pg_database):
    sq = sqlite3.connect(v9_path)
    with sq:
        sq.execute("UPDATE meta SET value = '8' WHERE key='schema_version'")
    sq.close()
    with pytest.raises(EtlError, match="schema_version 8"):
        run_etl(v9_path, pg_database)


def test_refuses_non_empty_target_without_force_wipe(v9_path, pg_database):
    report = run_etl(v9_path, pg_database, owner_email=ADMIN,
                     fts_queries=FTS_QUERIES)
    assert report.ok
    with pytest.raises(EtlError, match="--force-wipe"):
        run_etl(v9_path, pg_database, owner_email=ADMIN)
    # CLI surfaces the refusal as a non-zero exit, not a traceback.
    assert main([v9_path, "--pg", pg_database, "--owner-email", ADMIN]) == 2
    # idempotent re-run: wipe and reload reproduces a passing report.
    report2 = run_etl(v9_path, pg_database, owner_email=ADMIN,
                      force_wipe=True, fts_queries=FTS_QUERIES)
    assert report2.ok
    with _pg(pg_database) as pg:
        assert _one(pg, "SELECT count(*) FROM app_user")[0] == 1
        assert _one(pg, "SELECT count(*) FROM document")[0] == 6


# --- the round trip -------------------------------------------------------------


def test_round_trip_report_passes(v9_path, pg_database):
    report = run_etl(v9_path, pg_database, owner_email=ADMIN,
                     fts_queries=FTS_QUERIES)
    assert report.source_version == 9
    # the verification IS the contract: counts, checksums, FTS, vectors.
    assert report.ok, report.render()
    # every copied table accounted for in the report (35 specs + 3 embedding)
    assert len(report.table_counts) == 38
    src, copied, pg_n = report.table_counts["document"]
    assert (src, copied, pg_n) == (6, 6, 6)
    # wrong-dim vector skipped + logged, good ones copied
    assert report.skipped_vectors == [("document_embedding", 5, 8)]
    assert report.table_counts["document_embedding"] == (2, 2, 2)
    assert report.table_counts["event_embedding"] == (1, 1, 1)
    assert report.table_counts["claim_embedding"] == (1, 1, 1)
    # malformed aliases fell back to [] with a warning, not a crash
    assert any("entity.aliases" in w for w in report.warnings)
    # vec smoke ran against the fixed probe
    assert report.vec_checked == 2
    assert report.vec_max_delta <= 1e-6
    assert report.active_edges == (2, 2, True)
    # FTS smoke: both corpus words found, junk query empty on both sides
    assert [q for (q, *_rest) in report.fts] == FTS_QUERIES
    assert all(ok for *_x, ok in report.fts)


def test_bootstrap_rules_and_value_transforms(v9_path, pg_database):
    report = run_etl(v9_path, pg_database, owner_email=ADMIN,
                     fts_queries=FTS_QUERIES)
    assert report.ok, report.render()
    admin_id = report.admin_user_id
    with _pg(pg_database) as pg:
        # §7 rule 1 — admin pre-created, google_sub NULL, role admin
        row = _one(pg, "SELECT id, google_sub, role, disabled FROM app_user"
                       " WHERE email = %s", ADMIN)
        assert row == (admin_id, None, "admin", False)

        # §7 rule 4 — origin backfill + visibility + ownership
        origins = dict(pg.execute(
            "SELECT id, origin FROM document ORDER BY id").fetchall())
        assert origins == {1: "polled", 2: "polled",
                           3: "investigation_fetch", 4: "link_follow",
                           5: "user_url", 6: "user_text"}
        assert report.origin_counts == {
            "polled": 2, "investigation_fetch": 1, "link_follow": 1,
            "user_url": 1, "user_text": 1}
        owners = dict(pg.execute(
            "SELECT id, owner_id FROM document ORDER BY id").fetchall())
        assert owners == {1: None, 2: None, 3: None, 4: None, 5: None,
                          6: admin_id}
        assert _one(pg, "SELECT count(*) FROM document"
                        " WHERE visibility = 'shared'")[0] == 6

        # §7 rules 2/3 — watches/briefs/cursors and dossiers go to the admin
        assert _one(pg, "SELECT user_id FROM watch")[0] == admin_id
        assert _one(pg, "SELECT user_id FROM brief")[0] == admin_id
        assert _one(pg, "SELECT user_id FROM view_cursor")[0] == admin_id
        assert pg.execute("SELECT owner_id, visibility FROM dossier"
                          ).fetchall() == [(admin_id, "shared")] * 2

        # §7 rule 5 — history stays system
        assert _one(pg, "SELECT user_id FROM llm_call")[0] is None
        assert _one(pg, "SELECT count(*) FROM job"
                        " WHERE owner_id IS NOT NULL")[0] == 0

        # blob paths verbatim
        assert _one(pg, "SELECT raw_blob_path FROM document WHERE id = 1"
                    )[0] == "blobs/ab/h1"

        # int -> bool, signed simhash, deferred self-FK
        assert _one(pg, "SELECT watch_hit, simhash, canonical_document_id"
                        " FROM document WHERE id = 2"
                    ) == (True, -987654321, 1)

        # JSON text -> jsonb (parsed objects out), malformed -> []
        assert _one(pg, "SELECT aliases FROM entity WHERE id = 1"
                    )[0] == ["RBI", "central bank"]
        assert _one(pg, "SELECT aliases FROM entity WHERE id = 2")[0] == []
        assert _one(pg, "SELECT model_usage FROM dossier WHERE id = 1"
                    )[0] == {"haiku": 3}
        assert _one(pg, "SELECT topics FROM statement WHERE id = 1"
                    )[0] == ["monetary-policy"]

        # circular + forward FKs landed intact
        assert _one(pg, "SELECT parent_question_id FROM dossier"
                        " WHERE id = 2")[0] == 1
        assert _one(pg, "SELECT superseded_by_edge_id FROM edge"
                        " WHERE id = 2")[0] == 3

        # date columns are real dates now, value-preserved
        assert _one(pg, "SELECT occurred_on::text, window_end::text"
                        " FROM event")[0:1] != (None,)
        assert _one(pg, "SELECT occurred_on::text FROM event"
                    )[0] == "2026-06-10"
        assert _one(pg, "SELECT brief_date::text FROM brief"
                    )[0] == "2026-06-11"
        assert _one(pg, "SELECT day::text FROM source_stats"
                    )[0] == "2026-06-10"

        # v2 queue columns: run_at backfilled from created_at, per-kind
        # priority policy, v9 cancel_requested default false
        assert _one(pg, "SELECT priority, max_attempts, cancel_requested,"
                        " run_at = created_at FROM job WHERE id = 1"
                    ) == (90, 1, False, True)
        assert _one(pg, "SELECT priority FROM job WHERE id = 2")[0] == 10

        # generated tsvector rebuilt itself (FTS desync impossible)
        assert _one(pg, "SELECT count(*) FROM document WHERE search_tsv @@"
                        " websearch_to_tsquery('english', 'inflation')"
                    )[0] >= 2
        assert _one(pg, "SELECT count(*) FROM claim WHERE search_tsv @@"
                        " websearch_to_tsquery('english', 'repo rate')"
                    )[0] == 1

        # vectors copied (not re-embedded): models preserved / attributed
        assert _one(pg, "SELECT model FROM document_embedding"
                        " WHERE document_id = 1")[0] == "bge-test"
        assert _one(pg, "SELECT model FROM event_embedding")[0] == "centroid"
        # pgvector KNN over the copied vectors works
        assert _one(pg, "SELECT document_id FROM document_embedding"
                        " ORDER BY embedding <=> (SELECT embedding FROM"
                        " document_embedding WHERE document_id = 2) LIMIT 1"
                    )[0] == 2


def test_identity_sequences_realigned(v9_path, pg_database):
    report = run_etl(v9_path, pg_database, owner_email=ADMIN,
                     fts_queries=FTS_QUERIES)
    assert report.ok, report.render()
    with _pg(pg_database) as pg:
        # new rows allocate ABOVE the copied ids — setval ran per table
        new_doc = _one(pg, "INSERT INTO document (fetched_at, content_text,"
                           " content_hash) VALUES (now(), 'x', 'h-new')"
                           " RETURNING id")[0]
        assert new_doc == 7
        new_seq = _one(pg, "INSERT INTO job_event (job_id, ts, type)"
                           " VALUES (1, now(), 'ping') RETURNING seq")[0]
        assert new_seq == 3
        new_user = _one(pg, "INSERT INTO app_user (email, created_at)"
                            " VALUES ('m@e.x', now()) RETURNING id")[0]
        assert new_user == report.admin_user_id + 1


def test_runs_without_owner_email_single_user_nulls(v9_path, pg_database):
    """Without --owner-email the baseline stays single-user-safe: user/owner
    columns NULL, visibility/origin still backfilled."""
    report = run_etl(v9_path, pg_database, fts_queries=FTS_QUERIES)
    assert report.ok, report.render()
    assert report.admin_user_id is None
    with _pg(pg_database) as pg:
        assert _one(pg, "SELECT count(*) FROM app_user")[0] == 0
        assert _one(pg, "SELECT user_id FROM watch")[0] is None
        assert _one(pg, "SELECT owner_id, visibility FROM dossier"
                        " WHERE id = 1") == (None, "shared")
        assert _one(pg, "SELECT owner_id, origin FROM document WHERE id = 6"
                    ) == (None, "user_text")
