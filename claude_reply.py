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


def raise_for_claude(resp, where: str = "") -> None:
    """Use in place of resp.raise_for_status() on a Messages API call.

    On Oct 3, 2026 the API answered 400 to /api/buyer-report/regenerate and
    /api/chat-offers. The code called raise_for_status(), which discards the
    response body, so the log said only "400 Bad Request" and not why. The
    unhandled error also became a bare 500 with no CORS headers, which the
    browser reports as "Failed to fetch".

    This logs the API's own error message and raises an HTTPException, which
    FastAPI returns as JSON with CORS headers, so the agent sees a reason.
    """
    status = getattr(resp, "status_code", 200)
    if not isinstance(status, int) or status < 400:
        return
    msg = ""
    try:
        body = resp.json()
        msg = ((body.get("error") or {}).get("message") or "") if isinstance(body, dict) else ""
    except Exception:
        pass
    if not msg:
        try:
            msg = (resp.text or "")[:300]
        except Exception:
            msg = ""
    _log.error("claude api error where=%s status=%s message=%s", where or "-", status, msg[:600])
    from fastapi import HTTPException
    raise HTTPException(
        status_code=502,
        detail=f"The AI service returned an error ({status}): {msg[:300] or 'no detail given'}",
    )


# Models that answer 400 to any non-default sampling parameter. Live evidence
# for claude-opus-5 (Oct 3, 2026: /api/buyer-report/regenerate and
# /api/chat-offers both got 400 with "temperature": 0); Anthropic's docs state
# it for claude-sonnet-5.
NO_SAMPLING_PARAMS_PREFIXES = ("claude-opus-5", "claude-sonnet-5")
_SAMPLING_PARAMS = ("temperature", "top_p", "top_k")

CLAUDE_MESSAGES_URL = "https://api.anthropic.com/v1/messages"


async def claude_post(headers: dict, body: dict, timeout: float = 120, where: str = ""):
    """THE one place this backend calls the Messages API.

    Until Oct 3, 2026 there were nine hand-written copies of this request. A
    change that had to reach all of them (the Opus 5 switch) reached some and
    broke the rest, and nobody knew for days. Every reader now comes through
    here, so the next such change is made once.

    - Drops sampling parameters the model would reject, with a warning, so a
      stray "temperature" can never take a tool down again.
    - Logs and raises a readable error on a non-2xx answer (raise_for_claude).
    - Returns the httpx response; callers keep using resp.json().
    """
    import httpx
    model = str((body or {}).get("model") or "")
    if model.startswith(NO_SAMPLING_PARAMS_PREFIXES):
        dropped = [k for k in _SAMPLING_PARAMS if k in body]
        if dropped:
            body = {k: v for k, v in body.items() if k not in _SAMPLING_PARAMS}
            _log.warning("claude_post where=%s dropped %s: %s rejects them",
                         where or "-", dropped, model)
    async with httpx.AsyncClient(timeout=timeout) as client:
        resp = await client.post(CLAUDE_MESSAGES_URL, headers=headers, json=body)
    raise_for_claude(resp, where)
    return resp

