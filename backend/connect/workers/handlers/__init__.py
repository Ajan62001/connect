"""Job handlers — importing this package registers every handler.

The composition root imports it once, so HANDLERS is complete before the
first enqueue. One module per job family, mirroring the enqueue sites.
"""

from connect.workers.handlers import (  # noqa: F401 — registration imports
    analysis,
    backfill,
    brief,
    enrichment,
    investigation,
    poll,
    story,
    workspace_task,
)
