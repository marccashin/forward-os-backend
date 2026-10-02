"""Read the text out of an Anthropic Messages API reply.

The first content block of a reply is not always a text block. Opus 5 and
Sonnet 5 can return a thinking block first, and a thinking block has no
"text" key. Every reader in this backend used
resp.json()["content"][0]["text"], which raises KeyError: 'text' on such a
reply. On Oct 2, 2026 that took down the CMA Builder's MLS import: Railway
logged HTTP 200 from the API followed by KeyError: 'text' at the parse line,
and the agent saw "Could not read this sheet" for a perfectly good PDF.

The front end had the same bug and fixed it the same way (claudeText, PR
#163). Never index content[0] directly. Use claude_text().
"""
from __future__ import annotations


class ClaudeReplyError(ValueError):
    """The API answered, but the reply holds no text to use."""


def claude_text(data) -> str:
    """Join every text block of a Messages API reply, in order.

    Raises ClaudeReplyError naming the stop_reason when there is no text at
    all, so the log says why (max_tokens spent on thinking, refusal, ...)
    instead of a bare KeyError.
    """
    blocks = data.get("content") if isinstance(data, dict) else None
    parts = []
    for b in blocks or []:
        if isinstance(b, dict) and b.get("type") == "text" and isinstance(b.get("text"), str):
            parts.append(b["text"])
    text = "".join(parts)
    if not text.strip():
        stop = data.get("stop_reason") if isinstance(data, dict) else None
        kinds = [b.get("type") for b in blocks or [] if isinstance(b, dict)]
        raise ClaudeReplyError(
            f"Claude returned no text (stop reason: {stop}, blocks: {kinds})"
        )
    return text
