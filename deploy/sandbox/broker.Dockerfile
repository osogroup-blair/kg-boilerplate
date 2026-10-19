# The sandbox broker: the platform's own Python image plus the broker and the example kit it reuses. Build context: repository root.
# It needs the owner database URL because `kg workspaces create` (the governed way to make a workspace) talks to the database; it is as privileged as the worker.
ARG BASE=kg-python:p9
FROM ${BASE}
USER root
COPY apps/sandbox /app/apps/sandbox
COPY examples /app/examples
RUN chown -R kg /app/apps/sandbox /app/examples
USER kg
ENV PYTHONPATH=/app:/app/apps/sandbox/src SANDBOX_DB=/data/sandbox.sqlite
CMD ["python", "-m", "kg_sandbox"]
