"""Token colouring for the read-only devices.yaml view.

Deliberately a line-scanner rather than a real YAML lexer: the view exists so a bad edit
is legible, and it must therefore colour a file that does *not* parse. Anything a proper
parser would reject is exactly the case we still have to render.
"""

from __future__ import annotations

import html
import re
from dataclasses import dataclass

from markupsafe import Markup

_BOOLS = {"true", "false", "yes", "no", "on", "off", "null", "~"}
_NUM_RE = re.compile(r"^-?\d+(\.\d+)?$")
_MAP_RE = re.compile(r"^(\s*)(-\s+)?([A-Za-z_][\w.\-]*)(\s*:)(\s*)(.*)$")
_ITEM_RE = re.compile(r"^(\s*)(-\s*)(.*)$")


@dataclass(frozen=True)
class Line:
    number: int
    html: Markup
    bad: bool


def _value(text: str) -> str:
    """Colour a scalar, an inline list, or a trailing comment."""
    if not text:
        return ""

    comment = ""
    # Only split on ` #` so a `#` inside a quoted value survives.
    if " #" in text and not text.strip().startswith("#"):
        head, _, tail = text.partition(" #")
        if head.count('"') % 2 == 0 and head.count("'") % 2 == 0:
            text, comment = head, f'<span class="y-comment"> #{html.escape(tail)}</span>'

    stripped = text.strip()
    if not stripped:
        return comment

    if stripped.startswith("[") and stripped.endswith("]"):
        inner = stripped[1:-1]
        parts = [_scalar(p.strip()) for p in inner.split(",")] if inner.strip() else []
        joined = '<span class="y-punct">, </span>'.join(parts)
        return (
            f'<span class="y-punct">[</span>{joined}<span class="y-punct">]</span>'
            + comment
        )

    return _scalar(stripped) + comment


def _scalar(text: str) -> str:
    if not text:
        return ""
    escaped = html.escape(text)
    lowered = text.lower()
    if lowered in _BOOLS:
        return f'<span class="y-bool">{escaped}</span>'
    if _NUM_RE.match(text):
        return f'<span class="y-num">{escaped}</span>'
    # A quoted value where a bare one was expected is the classic devices.yaml mistake
    # (`port: "8022 "`), so quotes stay visible rather than being styled away.
    return f'<span class="y-str">{escaped}</span>'


def highlight(text: str, bad_lines: set[int] | None = None) -> list[Line]:
    bad_lines = bad_lines or set()
    out: list[Line] = []
    for number, raw in enumerate(text.splitlines(), start=1):
        out.append(Line(number, Markup(_line_html(raw)), number in bad_lines))
    return out


def _line_html(raw: str) -> str:
    stripped = raw.strip()
    if not stripped:
        return "&nbsp;"
    if stripped.startswith("#"):
        return f'<span class="y-comment">{html.escape(raw)}</span>'

    match = _MAP_RE.match(raw)
    if match:
        indent, dash, key, colon, gap, value = match.groups()
        parts = [html.escape(indent)]
        if dash:
            parts.append(f'<span class="y-punct">{html.escape(dash)}</span>')
        parts.append(f'<span class="y-key">{html.escape(key)}</span>')
        parts.append(f'<span class="y-punct">{html.escape(colon)}</span>')
        parts.append(html.escape(gap))
        parts.append(_value(value))
        return "".join(parts)

    item = _ITEM_RE.match(raw)
    if item:
        indent, dash, value = item.groups()
        return (
            html.escape(indent)
            + f'<span class="y-punct">{html.escape(dash)}</span>'
            + _value(value)
        )

    return html.escape(raw)


__all__ = ["Line", "highlight"]
