"""Social content pipeline — turn ONE corpus subject into a batch of grounded,
multi-format social posts that flow through a review queue to publishing.

A *campaign* (on-demand) resolves a CLOSED fact-set for its subject (a
story-mode narrative, a story thread, an investigation, a workspace, or a
topic — the same fact-set currency as story mode), then generates one
*content_item* per requested format (Instagram card / carousel, an X thread,
a LinkedIn post). Every format is written over the closed evidence menu with
the SAME citation discipline as story synthesis: [[E#]] markers are verified
in code, regenerated once, then stripped+flagged — and the resolved source
list rides on the item so trust survives even where the platform text cannot
render a citation.

Items are reviewed (draft -> approved -> scheduled), and beat publishes the
scheduled ones via the platform adapters. Auto-curation and video are future
seams over the same queue.
"""
