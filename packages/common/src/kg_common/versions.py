"""Platform-wide version tags that are pinned onto records (ONT-03: every interaction and job records the pipeline and ontology version it ran under)."""

PIPELINE_VERSION = "p4.v1"  # p3.v1 = normalize only; p4.v1 enables graph_ingest. Interactions keep the version they were accepted under.
