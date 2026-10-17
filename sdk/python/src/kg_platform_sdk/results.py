"""What the client returns: the generated response models, each carrying the `request_id` of the answer (the `x-request-id` header). `request_id` is excluded from
serialisation, so a result round-trips to exactly the API's own JSON."""

from __future__ import annotations

from pydantic import BaseModel, Field

from kg_platform_sdk import models as m


class _WithRequestId(BaseModel):
    request_id: str | None = Field(default=None, exclude=True, description="x-request-id of the response that produced this result")


class ContextPackage(m.ContextOut, _WithRequestId):
    """A context for a task. `context_text` is DATA for the consuming agent (the envelope says so); put it in the prompt as a quoted block, never as an instruction."""

    def as_prompt_block(self) -> str:
        """The context text, unchanged: its own header already marks it as retrieved data to verify, and every record ends in its citations."""
        return self.context_text or ""


class Accepted(m.AcceptedOut, _WithRequestId):
    pass


class InteractionStatus(m.InteractionDetail, _WithRequestId):
    pass


class MemoryRecord(m.MemoryOut, _WithRequestId):
    pass


class WriteResult(m.WriteOut, _WithRequestId):
    pass


class SearchResult(m.SearchOut, _WithRequestId):
    pass


class Usage(m.UsageOut, _WithRequestId):
    pass
