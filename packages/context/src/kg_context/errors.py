"""Context errors. Codes are stable; messages and details carry names of fields and sources, never request values or content."""

from __future__ import annotations

from typing import Any

from kg_common.errors import PlatformError, ValidationFailed


class ContextIncomplete(PlatformError):
    """`allow_partial=false` and a source is unavailable or stale: no package is returned (CTX-07, fail closed)."""

    code, http_status, default_message = "context_incomplete", 503, "A source the context needs is unavailable"

    def __init__(self, unavailable: list[dict[str, Any]]):
        super().__init__(details={"unavailable_sources": unavailable}, retryable=True)


class FeatureDisabled(PlatformError):
    code, http_status, default_message = "feature_disabled", 503, "The context API is disabled"


class RateLimited(PlatformError):
    code, http_status, default_message = "rate_limited", 429, "Too many context builds"

    def __init__(self, retry_after: int):
        super().__init__(retryable=True, retry_after=max(1, retry_after))


class ExceedsProfile(ValidationFailed):
    """A request tried to WIDEN a profile (a larger budget, a section it does not include, a compression it does not allow). Only the field names are returned."""

    code, default_message = "exceeds_profile", "The request asks for more than the profile allows"

    def __init__(self, fields: list[str]):
        super().__init__(details={"fields": sorted(fields)})


class ProfileNotAllowed(ValidationFailed):
    """The principal's registration lists the profiles it may build, and this is not one of them (P7). Applies to a Lab build too: the TARGET's registration is judged."""

    code, default_message = "profile_not_allowed", "This principal is not allowed to build that context profile"


class UnknownProfile(ValidationFailed):
    code, default_message = "unknown_profile", "No such context profile"
