"""Durable user-facing Inbox notification entity."""

from __future__ import annotations

import dataclasses


@dataclasses.dataclass
class InboxNotification:
    """A notification posted by an agent session for its owning user."""

    id: str
    user_id: str | None
    source_session_id: str
    title: str
    body: str
    severity: str
    created_at: int
    acknowledged_at: int | None = None
