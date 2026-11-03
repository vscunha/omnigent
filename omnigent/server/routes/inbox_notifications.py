"""Durable user-facing Inbox notification routes."""

from __future__ import annotations

import asyncio
from dataclasses import asdict
from typing import Any, Literal

from fastapi import APIRouter, Request
from pydantic import BaseModel, ConfigDict, Field

from omnigent.errors import ErrorCode, OmnigentError
from omnigent.server.auth import LEVEL_EDIT, AuthProvider
from omnigent.server.routes._auth_helpers import attribution_user, require_access, require_user
from omnigent.server.routes._errors import session_not_found
from omnigent.stores import ConversationStore
from omnigent.stores.inbox_notification_store import InboxNotificationStore
from omnigent.stores.permission_store import PermissionStore


class CreateInboxNotificationRequest(BaseModel):
    """Payload accepted from the source agent session."""

    model_config = ConfigDict(extra="forbid")

    title: str = Field(min_length=1, max_length=256)
    body: str = Field(min_length=1, max_length=20_000)
    severity: Literal["info", "attention", "critical"]


class AcknowledgeInboxNotificationRequest(BaseModel):
    """Explicit acknowledgement payload used by the Inbox UI."""

    model_config = ConfigDict(extra="forbid")

    acknowledged: Literal[True] = True


def create_inbox_notifications_router(
    store: InboxNotificationStore,
    *,
    conversation_store: ConversationStore,
    auth_provider: AuthProvider | None = None,
    permission_store: PermissionStore | None = None,
) -> APIRouter:
    """Build routes for durable, owner-scoped Inbox notifications."""
    router = APIRouter()

    def _identities(request: Request) -> tuple[str | None, str | None]:
        auth_user_id = require_user(request, auth_provider)
        return auth_user_id, attribution_user(auth_user_id)

    @router.post("/sessions/{source_session_id}/inbox-notifications")
    async def create_notification(
        request: Request,
        source_session_id: str,
        body: CreateInboxNotificationRequest,
    ) -> dict[str, Any]:
        """Create a notification sourced from a session the caller can edit."""
        auth_user_id, owner_user_id = _identities(request)
        if permission_store is not None:
            await require_access(
                auth_user_id,
                source_session_id,
                LEVEL_EDIT,
                permission_store,
                conversation_store,
            )
        conversation = await asyncio.to_thread(
            conversation_store.get_conversation,
            source_session_id,
        )
        if conversation is None:
            raise session_not_found()
        notification = await asyncio.to_thread(
            store.create,
            user_id=owner_user_id,
            source_session_id=source_session_id,
            title=body.title.strip(),
            body=body.body.strip(),
            severity=body.severity,
        )
        return asdict(notification)

    @router.get("/inbox/notifications")
    async def list_notifications(
        request: Request,
        include_acknowledged: bool = False,
    ) -> dict[str, Any]:
        """List the current user's notifications and outstanding count."""
        _, owner_user_id = _identities(request)
        notifications, outstanding_count = await asyncio.gather(
            asyncio.to_thread(
                store.list_for_user,
                user_id=owner_user_id,
                outstanding_only=not include_acknowledged,
            ),
            asyncio.to_thread(store.count_outstanding, user_id=owner_user_id),
        )
        return {
            "object": "list",
            "data": [asdict(notification) for notification in notifications],
            "outstanding_count": outstanding_count,
        }

    @router.patch("/inbox/notifications/{notification_id}")
    async def acknowledge_notification(
        request: Request,
        notification_id: str,
        body: AcknowledgeInboxNotificationRequest,
    ) -> dict[str, Any]:
        """Acknowledge one notification owned by the current user."""
        del body
        _, owner_user_id = _identities(request)
        notification = await asyncio.to_thread(
            store.acknowledge,
            notification_id,
            user_id=owner_user_id,
        )
        if notification is None:
            raise OmnigentError("Inbox notification not found", code=ErrorCode.NOT_FOUND)
        return asdict(notification)

    return router
