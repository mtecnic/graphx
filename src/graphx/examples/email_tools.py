"""Helpers for the email_triage workflow.

Kept dependency-free and importable as `graphx.examples.email_tools:...`
so the workflow's function nodes resolve without extra setup.
"""

from __future__ import annotations

from typing import Any


def compile_triage(emails: list[dict], triaged: list[dict]) -> dict[str, Any]:
    """Zip each email with its classification (same order, from the map),
    build a human-readable summary and the list of drafts to create."""
    rows: list[str] = []
    drafts: list[dict] = []
    counts: dict[str, int] = {}

    for email, verdict in zip(emails, triaged):
        category = str(verdict.get("category", "other"))
        priority = str(verdict.get("priority", "normal"))
        needs_reply = bool(verdict.get("needs_reply", False))
        counts[category] = counts.get(category, 0) + 1

        flag = "✏️  reply drafted" if needs_reply else "—"
        rows.append(f"[{priority:^6}] {category:<9} {flag}  "
                    f"{email.get('from', '?')}: {email.get('subject', '(no subject)')}")

        if needs_reply and verdict.get("draft_reply", "").strip():
            drafts.append({
                "message_id": email.get("id", ""),
                "to": email.get("from", ""),
                "subject": "Re: " + email.get("subject", ""),
                "body": verdict["draft_reply"].strip(),
            })

    breakdown = ", ".join(f"{n} {cat}" for cat, n in sorted(counts.items()))
    summary = (f"Triaged {len(emails)} email(s): {breakdown}.\n"
               f"{len(drafts)} reply/replies drafted.\n\n" + "\n".join(rows))
    return {"summary": summary, "drafts": drafts, "draft_count": len(drafts)}
