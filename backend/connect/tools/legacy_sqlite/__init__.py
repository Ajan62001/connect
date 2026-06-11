"""DEAD REFERENCE CODE — the closed v0.1 SQLite lineage.

Moved out of connect/storage at port phase P2 so the live application has
zero sqlite3 imports. Kept verbatim ONLY as the reference the one-shot ETL
(P4, tools/etl_sqlite_to_pg.py) reads the v9/v10 source database with.
Nothing under connect/ (outside tools/) may import this package.
"""
