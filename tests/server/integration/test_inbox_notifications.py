"""Integration tests for durable user-facing Inbox notifications."""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI

from omnigent.runtime.agent_cache import AgentCache
from omnigent.server.app import create_app
from omnigent.server.auth import LEVEL_EDIT, UnifiedAuthProvider
from omnigent.stores.agent_store.sqlalchemy_store import SqlAlchemyAgentStore
from omnigent.stores.artifact_store.local import LocalArtifactStore
from omnigent.stores.conversation_store.sqlalchemy_store import (
    SqlAlchemyConversationStore,
)
from omnigent.stores.file_store.sqlalchemy_store import SqlAlchemyFileStore
from omnigent.stores.inbox_notification_store.sqlalchemy_store import (
    SqlAlchemyInboxNotificationStore,
)
from omnigent.stores.permission_store.sqlalchemy_store import SqlAlchemyPermissionStore

pytestmark = pytest.mark.asyncio


@pytest.fixture()
def notification_auth_app(runtime_init: None, db_uri: str, tmp_path: Path) -> FastAPI:
    artifact_store = LocalArtifactStore(str(tmp_path / "artifacts"))
    return create_app(
        agent_store=SqlAlchemyAgentStore(db_uri),
        file_store=SqlAlchemyFileStore(db_uri),
        conversation_store=SqlAlchemyConversationStore(db_uri),
        artifact_store=artifact_store,
        agent_cache=AgentCache(
            artifact_store=artifact_store,
            cache_dir=tmp_path / "cache",
        ),
        inbox_notification_store=SqlAlchemyInboxNotificationStore(db_uri),
        permission_store=SqlAlchemyPermissionStore(db_uri),
        auth_provider=UnifiedAuthProvider(source="header"),
    )


@pytest_asyncio.fixture()
async def notification_auth_client(
    notification_auth_app: FastAPI,
) -> AsyncIterator[httpx.AsyncClient]:
    transport = httpx.ASGITransport(app=notification_auth_app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        yield client


async def test_create_list_link_and_acknowledge_notification(
    client: httpx.AsyncClient,
    db_uri: str,
) -> None:
    manager = SqlAlchemyConversationStore(db_uri).create_conversation()

    created_response = await client.post(
        f"/v1/sessions/{manager.id}/inbox-notifications",
        json={
            "title": "Human action required",
            "body": "Reconnect the deployment credential.",
            "severity": "critical",
        },
    )
    assert created_response.status_code == 200, created_response.text
    created = created_response.json()
    assert created["source_session_id"] == manager.id
    assert created["acknowledged_at"] is None

    listed_response = await client.get("/v1/inbox/notifications")
    assert listed_response.status_code == 200
    listed = listed_response.json()
    assert listed["outstanding_count"] == 1
    assert listed["data"] == [created]

    acknowledged_response = await client.patch(
        f"/v1/inbox/notifications/{created['id']}",
        json={"acknowledged": True},
    )
    assert acknowledged_response.status_code == 200
    assert acknowledged_response.json()["acknowledged_at"] is not None

    outstanding = (await client.get("/v1/inbox/notifications")).json()
    assert outstanding == {"object": "list", "data": [], "outstanding_count": 0}
    all_items = (
        await client.get("/v1/inbox/notifications", params={"include_acknowledged": True})
    ).json()
    assert all_items["outstanding_count"] == 0
    assert all_items["data"][0]["id"] == created["id"]


async def test_notification_source_must_be_a_real_session(client: httpx.AsyncClient) -> None:
    response = await client.post(
        f"/v1/sessions/{'f' * 32}/inbox-notifications",
        json={"title": "Blocked", "body": "Act now.", "severity": "attention"},
    )

    assert response.status_code == 404


async def test_acknowledging_unknown_notification_returns_not_found(
    client: httpx.AsyncClient,
) -> None:
    response = await client.patch(
        f"/v1/inbox/notifications/{'e' * 32}",
        json={"acknowledged": True},
    )

    assert response.status_code == 404


async def test_notifications_are_current_user_scoped_and_source_authorized(
    notification_auth_client: httpx.AsyncClient,
    db_uri: str,
) -> None:
    conversation = SqlAlchemyConversationStore(db_uri).create_conversation()
    permissions = SqlAlchemyPermissionStore(db_uri)
    for user in ("alice@example.com", "bob@example.com"):
        permissions.ensure_user(user)
    permissions.grant("alice@example.com", conversation.id, LEVEL_EDIT)

    created = await notification_auth_client.post(
        f"/v1/sessions/{conversation.id}/inbox-notifications",
        headers={"X-Forwarded-Email": "alice@example.com"},
        json={"title": "Alice action", "body": "Review the failure.", "severity": "attention"},
    )
    assert created.status_code == 200, created.text

    alice_items = await notification_auth_client.get(
        "/v1/inbox/notifications",
        headers={"X-Forwarded-Email": "alice@example.com"},
    )
    bob_items = await notification_auth_client.get(
        "/v1/inbox/notifications",
        headers={"X-Forwarded-Email": "bob@example.com"},
    )
    assert alice_items.json()["outstanding_count"] == 1
    assert bob_items.json()["outstanding_count"] == 0

    forbidden = await notification_auth_client.post(
        f"/v1/sessions/{conversation.id}/inbox-notifications",
        headers={"X-Forwarded-Email": "bob@example.com"},
        json={"title": "Spoof", "body": "Must fail.", "severity": "critical"},
    )
    assert forbidden.status_code == 404
