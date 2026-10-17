"""kg-platform-sdk: a thin typed Python client for the Knowledge & Memory Platform HTTP API.

    async with KGClient(base_url="https://kg.example.com", api_key=os.environ["KG_API_KEY"]) as kg:
        ctx = await kg.build_context("Help Sarah assess starting a business", subject_ids=[sarah], profile="planning", max_tokens=600)
        prompt = f"<retrieved_context>\\n{ctx.as_prompt_block()}\\n</retrieved_context>\\n\\n{user_question}"

The context text is DATA retrieved for the task, not instructions: quote it (as above), verify before acting, and keep your own instructions outside the block.
"""

from kg_platform_sdk import models
from kg_platform_sdk.client import KGClient, KGClientSync
from kg_platform_sdk.errors import (
    AuthError,
    Conflict,
    ContextIncomplete,
    Forbidden,
    InvalidEvidence,
    KGError,
    NotFound,
    QuotaExceeded,
    RateLimited,
    RuleViolation,
    ServerError,
    ValidationError,
)
from kg_platform_sdk.results import Accepted, ContextPackage, InteractionStatus, MemoryRecord, SearchResult, Usage, WriteResult

__all__ = [
    "Accepted",
    "AuthError",
    "Conflict",
    "ContextIncomplete",
    "ContextPackage",
    "Forbidden",
    "InteractionStatus",
    "InvalidEvidence",
    "KGClient",
    "KGClientSync",
    "KGError",
    "MemoryRecord",
    "NotFound",
    "QuotaExceeded",
    "RateLimited",
    "RuleViolation",
    "SearchResult",
    "ServerError",
    "Usage",
    "ValidationError",
    "WriteResult",
    "models",
]
__version__ = "0.1.0"
