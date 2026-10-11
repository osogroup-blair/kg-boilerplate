"""Fact templates: deterministic text for a graph fact, no LLM (ADR-016).

Grammar: `{source}` and `{target}` are the endpoint names; `{attr}` is a relationship attribute; `{attr?: text with {attr}}` renders its text only
when `attr` has a value. Everything else is literal. Parsing is strict so a bad template is a validation error at publish, never a runtime surprise."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

BUILTIN_NAMES = ("source", "target")


class TemplateError(ValueError):
    pass


@dataclass
class _Text:
    text: str


@dataclass
class _Field:
    name: str


@dataclass
class _Optional:
    name: str
    children: list[_Node] = field(default_factory=list)


_Node = _Text | _Field | _Optional


def _parse(src: str, pos: int, closing: bool) -> tuple[list[_Node], int]:
    nodes: list[_Node] = []
    buf: list[str] = []

    def flush() -> None:
        if buf:
            nodes.append(_Text("".join(buf)))
            buf.clear()

    while pos < len(src):
        ch = src[pos]
        if ch == "{":
            flush()
            end = pos + 1
            while end < len(src) and src[end] not in "{}:?":
                end += 1
            name = src[pos + 1 : end].strip()
            if not name.replace("_", "a").isalnum():
                raise TemplateError(f"invalid placeholder name at position {pos}")
            if end >= len(src):
                raise TemplateError("unterminated placeholder")
            if src[end] == "}":
                nodes.append(_Field(name))
                pos = end + 1
            elif src[end : end + 2] == "?:":
                children, pos = _parse(src, end + 2, True)
                nodes.append(_Optional(name, children))
            else:
                raise TemplateError(f"unexpected '{src[end]}' in placeholder")
        elif ch == "}":
            if not closing:
                raise TemplateError(f"unmatched '}}' at position {pos}")
            flush()
            return nodes, pos + 1
        else:
            buf.append(ch)
            pos += 1
    if closing:
        raise TemplateError("unterminated optional segment")
    flush()
    return nodes, pos


def parse_template(src: str) -> list[_Node]:
    nodes, _ = _parse(src, 0, False)
    return nodes


def placeholders(src: str) -> set[str]:
    """Every name the template refers to (raises TemplateError on a malformed template)."""
    out: set[str] = set()

    def walk(nodes: list[_Node]) -> None:
        for n in nodes:
            if isinstance(n, _Field):
                out.add(n.name)
            elif isinstance(n, _Optional):
                out.add(n.name)
                walk(n.children)

    walk(parse_template(src))
    return out


def _present(value: Any) -> bool:
    return value is not None and value != ""


def render_fact(template: str | None, rel_type: str, source_name: str, target_name: str, attributes: dict[str, Any]) -> str:
    """Render the fact text. Without a template the default is '<source> <relationship in words> <target>'."""
    if not template:
        return f"{source_name} {rel_type.lower().replace('_', ' ')} {target_name}"
    values: dict[str, Any] = {"source": source_name, "target": target_name, **attributes}

    def render(nodes: list[_Node]) -> str:
        parts: list[str] = []
        for n in nodes:
            if isinstance(n, _Text):
                parts.append(n.text)
            elif isinstance(n, _Field):
                parts.append("" if values.get(n.name) is None else str(values[n.name]))
            elif _present(values.get(n.name)):
                parts.append(render(n.children))
        return "".join(parts)

    return " ".join(render(parse_template(template)).split())
