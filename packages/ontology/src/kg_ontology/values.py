"""Attribute value coercion: one place that decides whether a JSON value fits an attribute spec and what its stored form is.

Stored forms are JSON-native: dates are 'YYYY-MM-DD', datetimes are UTC ISO-8601 with a trailing 'Z'. The same strings go to the graph (PJ-01: native
Neo4j temporal types would not round-trip through the drift comparison)."""

from __future__ import annotations

import datetime as dt
import math
import re
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from kg_ontology.spec import AttributeSpec

MIN_YEAR = 1900


def iso_utc(value: dt.datetime) -> str:
    aware = value if value.tzinfo else value.replace(tzinfo=dt.UTC)
    return aware.astimezone(dt.UTC).isoformat().replace("+00:00", "Z")


def parse_instant(value: object) -> dt.datetime | None:
    """A date, a datetime or an ISO-8601 string -> an aware UTC datetime (a bare date means 00:00 UTC). None when it is not a valid instant."""
    if isinstance(value, dt.datetime):
        return value if value.tzinfo else value.replace(tzinfo=dt.UTC)
    if isinstance(value, dt.date):
        return dt.datetime(value.year, value.month, value.day, tzinfo=dt.UTC)
    if isinstance(value, str):
        try:
            if len(value) == 10:
                d = dt.date.fromisoformat(value)
                return dt.datetime(d.year, d.month, d.day, tzinfo=dt.UTC)
            parsed = dt.datetime.fromisoformat(value)
        except ValueError:
            return None
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=dt.UTC)
    return None


def coerce_value(attr: AttributeSpec, raw: Any) -> tuple[Any, str | None]:
    """Returns (normalised value, error code). The error code is one of: wrong_type, pattern_mismatch, below_min, above_max, too_long."""
    t = attr.type
    if t == "string":
        if not isinstance(raw, str):
            return None, "wrong_type"
        if attr.max_length is not None and len(raw) > attr.max_length:
            return None, "too_long"
        if attr.pattern is not None and re.search(attr.pattern, raw) is None:
            return None, "pattern_mismatch"
        return raw, None
    if t == "boolean":
        return (raw, None) if isinstance(raw, bool) else (None, "wrong_type")
    if t in ("integer", "number"):
        if isinstance(raw, bool) or not isinstance(raw, int | float):
            return None, "wrong_type"
        if t == "integer" and not isinstance(raw, int):
            return None, "wrong_type"
        if isinstance(raw, float) and not math.isfinite(raw):
            return None, "wrong_type"
        if attr.min is not None and raw < attr.min:
            return None, "below_min"
        if attr.max is not None and raw > attr.max:
            return None, "above_max"
        return raw, None
    if t == "date":
        if isinstance(raw, dt.datetime):
            return None, "wrong_type"
        if isinstance(raw, dt.date):
            return raw.isoformat(), None
        if isinstance(raw, str) and len(raw) == 10:
            try:
                return dt.date.fromisoformat(raw).isoformat(), None
            except ValueError:
                return None, "wrong_type"
        return None, "wrong_type"
    if t == "datetime":
        if isinstance(raw, str) and len(raw) == 10:
            return None, "wrong_type"  # a bare date is not a datetime
        parsed = parse_instant(raw)
        return (iso_utc(parsed), None) if parsed is not None else (None, "wrong_type")
    return None, "wrong_type"


def default_fits(attr: AttributeSpec) -> bool:
    return attr.default is None or coerce_value(attr, attr.default)[1] is None
