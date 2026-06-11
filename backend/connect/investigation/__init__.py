"""Investigation mode (v8) — topic -> scope -> tool loop -> synthesis.

An investigation IS a dossier row (kind='investigation') that runs three
stages: scope (deterministic, code-only), investigate (the first real tool
loop, BALANCED tier) and synthesize (one DEEP call + code-rendered
sections). Findings and questions persist incrementally; causal edges land
at grade 2 so every investigation makes the next one smarter.
"""
