"""Helpers for embedding untrusted text (issues, repository content) in prompts."""

from __future__ import annotations

import re

_TAG_ESCAPE = re.compile(r"<\s*/\s*(untrusted[a-z_]*)", re.IGNORECASE)


def wrap_untrusted(tag: str, text: str, *, max_chars: int = 8000) -> str:
    """Wrap ``text`` in ``<tag>`` delimiters so the model treats it as data, not instructions.

    Any closing-tag lookalike inside the text is defanged so it cannot break out.
    """
    cleaned = _TAG_ESCAPE.sub(r"<\/\1", text or "")
    if len(cleaned) > max_chars:
        cleaned = cleaned[:max_chars] + "\n... (truncated)"
    return f"<{tag}>\n{cleaned}\n</{tag}>"


def neutralize_mentions(text: str) -> str:
    """Stop AI-written text from pinging users or teams when posted to GitHub."""
    return re.sub(r"(?<![A-Za-z0-9_.])@(?=[A-Za-z0-9_])", "@​", text)
