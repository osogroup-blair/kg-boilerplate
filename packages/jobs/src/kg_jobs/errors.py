"""Typed job failures. A handler raises JobError(category, code); the dispatcher maps the category to an outcome (policy.decide).

`code` is a stable machine-readable token. It is stored and logged, so it never carries free text from content or from exception messages."""

from __future__ import annotations

import re
from enum import StrEnum

_CODE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")


class Category(StrEnum):
    """Same semantics as kg_modelgw.errors.Category (ADR-012); duplicated here so kg_jobs stays free of the model gateway."""

    RETRYABLE = "retryable"
    POLICY_TERMINAL = "policy_terminal"  # stage -> skipped_by_policy, never retried
    CONFIG_TERMINAL = "config_terminal"  # dead letter + alert, never retried
    BUDGET = "budget"  # pause the job with a long backoff, without consuming an attempt


def check_code(code: str) -> str:
    if not _CODE.match(code):
        raise ValueError(f"invalid error code {code!r}: use a lower_snake token")
    return code


class JobError(Exception):
    def __init__(self, category: Category, code: str, *, retry_after: float | None = None):
        super().__init__(f"{category.value}:{code}")  # the text is the code, never content
        self.category = category
        self.code = check_code(code)
        self.retry_after = retry_after


class Skip(Exception):  # noqa: N818 (a control-flow signal, not an error)
    """The handler decided there is nothing to do for a policy reason; the job completes as `skipped`."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = check_code(code)
