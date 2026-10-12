"""kg_extraction: the `graph_ingest` stage (P4). Graphiti's extraction of an interaction is a PROPOSAL; this package turns it into evidence-backed,
time-aware platform knowledge without letting a model overwrite verified truth (ADR-026/027/028).

handler.py     GraphIngestJob: decrypt, egress, workspace lock, pre-created episode, add_episode (or skip on a re-run), reconcile, restore
reconcile.py   Reconciler: Graphiti's nodes and edges -> entities, mentions, facts, claims, reviews; detects what Graphiti changed that it may not
identity.py    deterministic links (subject refs, anchored name compatibility) and identity candidates (trigram + E5 cosine)
conflicts.py   the authority-aware conflict policy (pure; lives in kg_ontology so the review decisions in kg_knowledge use the same code)
errors.py      extraction error classification -> job categories
"""

__version__ = "0.1.0"
