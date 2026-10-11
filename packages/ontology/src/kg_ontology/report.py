"""Validation report types shared by the validator, the rule-test runner and the API (ONT-01). Issues carry a code and a JSON path, never values."""

from __future__ import annotations

from pydantic import BaseModel


class Issue(BaseModel):
    code: str
    path: str
    message: str


class RuleTestResult(BaseModel):
    path: str
    name: str
    expected: str
    actual: str
    passed: bool
    codes: list[str] = []


class ValidationReport(BaseModel):
    errors: list[Issue] = []
    warnings: list[Issue] = []
    rule_tests: list[RuleTestResult] = []
    content_hash: str | None = None

    @property
    def ok(self) -> bool:
        return not self.errors
