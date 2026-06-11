"""The political calendar — seeded fixture (idempotent) + the upcoming query.

India 2026-27 cycle. Dates are APPROXIMATE where the official notification
has not happened (poll-body announcements move them); every approximate row
says so in its label. scope is 'national' or a state code. This is fixture
content, not architecture — edit freely.

Used by GET /api/calendar and (Phase 3) the suggestion scorer's
``w_calendar`` component (event within 90d before a calendar_event).
"""

from __future__ import annotations

import sqlite3

# (kind, scope, occurs_on, ends_on, label)
CALENDAR_SEED: tuple[tuple[str, str, str, str | None, str], ...] = (
    # -- state assembly elections, mid-2026 cycle (terms end Apr-Jun 2026) ----
    ("election", "AS", "2026-04-06", "2026-05-02",
     "Assam assembly election (approx. window)"),
    ("election", "WB", "2026-04-10", "2026-05-08",
     "West Bengal assembly election (approx. window)"),
    ("election", "TN", "2026-05-02", None,
     "Tamil Nadu assembly election (approx.)"),
    ("election", "KL", "2026-05-06", None,
     "Kerala assembly election (approx.)"),
    ("election", "PY", "2026-05-02", None,
     "Puducherry assembly election (approx.)"),
    # -- early-2027 cycle ------------------------------------------------------
    ("election", "UP", "2027-02-10", "2027-03-10",
     "Uttar Pradesh assembly election (approx. window)"),
    ("election", "PB", "2027-02-20", None,
     "Punjab assembly election (approx.)"),
    ("election", "UK", "2027-02-14", None,
     "Uttarakhand assembly election (approx.)"),
    ("election", "GA", "2027-02-14", None,
     "Goa assembly election (approx.)"),
    ("election", "MN", "2027-02-28", "2027-03-05",
     "Manipur assembly election (approx. window)"),
    # -- union budgets (Feb 1 by convention) -----------------------------------
    ("budget", "national", "2026-02-01", None, "Union Budget 2026-27"),
    ("budget", "national", "2027-02-01", None, "Union Budget 2027-28"),
    # -- parliament session windows (approx., by convention) --------------------
    ("parliament_session", "national", "2026-07-20", "2026-08-14",
     "Parliament monsoon session 2026 (approx. window)"),
    ("parliament_session", "national", "2026-11-25", "2026-12-19",
     "Parliament winter session 2026 (approx. window)"),
    ("parliament_session", "national", "2027-01-29", "2027-04-08",
     "Parliament budget session 2027 (approx. window)"),
    ("parliament_session", "national", "2027-07-19", "2027-08-13",
     "Parliament monsoon session 2027 (approx. window)"),
    # -- RBI MPC bi-monthly meetings (3-day meetings, approx. after 2026-06) ----
    ("rbi_mpc", "national", "2026-06-03", "2026-06-05",
     "RBI MPC meeting (Jun 2026)"),
    ("rbi_mpc", "national", "2026-08-04", "2026-08-06",
     "RBI MPC meeting (Aug 2026, approx.)"),
    ("rbi_mpc", "national", "2026-09-29", "2026-10-01",
     "RBI MPC meeting (Oct 2026, approx.)"),
    ("rbi_mpc", "national", "2026-12-02", "2026-12-04",
     "RBI MPC meeting (Dec 2026, approx.)"),
    ("rbi_mpc", "national", "2027-02-03", "2027-02-05",
     "RBI MPC meeting (Feb 2027, approx.)"),
    ("rbi_mpc", "national", "2027-04-05", "2027-04-07",
     "RBI MPC meeting (Apr 2027, approx.)"),
)


def seed_calendar_events(conn: sqlite3.Connection) -> int:
    """Insert missing fixture rows (matched on kind+label+occurs_on);
    returns how many were added. Idempotent."""
    added = 0
    with conn:
        for kind, scope, occurs_on, ends_on, label in CALENDAR_SEED:
            exists = conn.execute(
                "SELECT 1 FROM calendar_event WHERE kind = ? AND label = ?"
                " AND occurs_on = ?", (kind, label, occurs_on)).fetchone()
            if exists:
                continue
            conn.execute(
                "INSERT INTO calendar_event (kind, scope, occurs_on,"
                " ends_on, label) VALUES (?,?,?,?,?)",
                (kind, scope, occurs_on, ends_on, label))
            added += 1
    return added


def upcoming(conn: sqlite3.Connection, days: int = 120) -> list[sqlite3.Row]:
    """Calendar rows still in progress or starting within ``days`` from
    today (UTC), soonest first."""
    return conn.execute(
        "SELECT id, kind, scope, occurs_on, ends_on, label"
        " FROM calendar_event"
        " WHERE COALESCE(ends_on, occurs_on) >= date('now')"
        "   AND occurs_on <= date('now', ?)"
        " ORDER BY occurs_on ASC, id ASC", (f"+{days} days",)).fetchall()
