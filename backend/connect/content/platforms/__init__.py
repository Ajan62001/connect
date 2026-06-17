"""Platform publish adapters for the content pipeline.

Instagram is functional (Graph API, via connect/social/instagram.py). X and
LinkedIn are scaffolded: ``is_configured`` returns False until credentials are
set, and publishing raises ``NotConnected`` — the pipeline, queue, scheduling
and rendering all work; only the final hop to those networks awaits creds.
"""


class NotConnected(Exception):
    """The target platform is not configured for direct publishing."""
