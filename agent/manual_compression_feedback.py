"""User-facing summaries for manual compression commands."""

from __future__ import annotations

from typing import Any, Sequence

from agent.i18n import t


def summarize_manual_compression(
    before_messages: Sequence[dict[str, Any]],
    after_messages: Sequence[dict[str, Any]],
    before_tokens: int,
    after_tokens: int,
) -> dict[str, Any]:
    """Return consistent user-facing feedback for manual compression."""
    before_count = len(before_messages)
    after_count = len(after_messages)
    before_tokens_s = f"{before_tokens:,}"
    after_tokens_s = f"{after_tokens:,}"
    noop = list(after_messages) == list(before_messages)

    if noop:
        headline = t("gateway.compress.noop_headline", count=before_count)
        if after_tokens == before_tokens:
            token_line = t("gateway.compress.size_unchanged", tokens=before_tokens_s)
        else:
            token_line = t(
                "gateway.compress.size_changed",
                before=before_tokens_s,
                after=after_tokens_s,
            )
    else:
        headline = t(
            "gateway.compress.done_headline",
            before=before_count,
            after=after_count,
        )
        token_line = t(
            "gateway.compress.size_changed",
            before=before_tokens_s,
            after=after_tokens_s,
        )

    note = None
    if not noop and after_count < before_count and after_tokens > before_tokens:
        note = t("gateway.compress.denser_note")

    return {
        "noop": noop,
        "headline": headline,
        "token_line": token_line,
        "note": note,
    }
