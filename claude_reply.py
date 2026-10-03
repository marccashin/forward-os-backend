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

import logging

_log = logging.getLogger("forward-cc.claude")


class ClaudeReplyError(ValueError):
    """The API answered, but the reply holds no text to use."""


def claude_text(data, where: str = "") -> str:
    """Join every text block of a Messages API reply, in order.

    Raises ClaudeReplyError naming the stop_reason when there is no text at
    all, so the log says why (max_tokens spent on thinking, refusal, ...)
    instead of a bare KeyError.
    """
    blocks = data.get("content") if isinstance(data, dict) else None
    # One line per reply, success or not, so the log shows how much of the
    # reply limit was used. A reply cut off by the limit gets a warning: its
    # text is incomplete even when some text came back.
    try:
        usage = data.get("usage") or {}
        stop = data.get("stop_reason")
        line = "claude reply where=%s model=%s stop=%s output_tokens=%s blocks=%s"
        args = (where or "-", data.get("model"), stop, usage.get("output_tokens"),
                [b.get("type") for b in blocks or [] if isinstance(b, dict)])
        if stop == "max_tokens":
            _log.warning(line + " REPLY CUT OFF BY max_tokens", *args)
        else:
            _log.info(line, *args)
    except Exception:
        pass
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
