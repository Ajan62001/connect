"""Social posting — turn a document's enrichment into an Instagram post.

``card`` renders the shareable image; ``instagram`` publishes it via the
Graph API when the deployment is connected. Generation (the caption/card
content) is a governed LLM call in the API router.
"""
