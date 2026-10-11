"""End-to-end topology checks for independent Manager supervision."""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from omnigent.server.routes.sessions import routes_events
from omnigent.stores.conversation_store.sqlalchemy_store import (
    SqlAlchemyConversationStore,
)
from tests.server.helpers import create_test_agent

pytestmark = pytest.mark.asyncio


async def test_manager_message_is_normal_input_and_preserves_orchestrator_tree(
    client: httpx.AsyncClient,
    db_uri: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    agent = await create_test_agent(client, name="supervision-agent")
    orchestrator_response = await client.post(
        "/v1/sessions",
        json={
            "agent_id": agent["id"],
            "title": "Orchestrator",
            "initial_items": [
                {
                    "type": "message",
                    "data": {
                        "role": "user",
                        "content": [{"type": "input_text", "text": "Original objective"}],
                    },
                }
            ],
        },
    )
    manager_response = await client.post(
        "/v1/sessions",
        json={"agent_id": agent["id"], "title": "Manager"},
    )
    assert orchestrator_response.status_code == 201
    assert manager_response.status_code == 201
    orchestrator_id = orchestrator_response.json()["id"]
    manager_id = manager_response.json()["id"]

    conversations = SqlAlchemyConversationStore(db_uri)
    worker = conversations.create_conversation(
        kind="sub_agent",
        title="worker:implementation",
        parent_conversation_id=orchestrator_id,
        agent_id=agent["id"],
        sub_agent_name="implementation",
    )
    before_history = (await client.get(f"/v1/sessions/{orchestrator_id}")).json()["items"]

    forwarded: list[dict[str, Any]] = []

    def _runner_handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST" and request.url.path.endswith("/events"):
            forwarded.append(json.loads(request.content))
            return httpx.Response(202, json={"queued": True})
        return httpx.Response(200, json={"data": []})

    fake_runner = httpx.AsyncClient(
        transport=httpx.MockTransport(_runner_handler),
        base_url="http://runner",
    )

    async def _get_runner_client(
        session_id: str,
        runner_router: object,
    ) -> httpx.AsyncClient:
        del runner_router
        assert session_id == orchestrator_id
        return fake_runner

    monkeypatch.setattr(routes_events, "_get_runner_client", _get_runner_client)
    try:
        message_response = await client.post(
            f"/v1/sessions/{orchestrator_id}/events",
            json={
                "type": "message",
                "data": {
                    "role": "user",
                    "content": [
                        {
                            "type": "input_text",
                            "text": "Manager guidance: inspect the failed worker.",
                        }
                    ],
                },
            },
        )
    finally:
        await fake_runner.aclose()

    assert message_response.status_code == 202, message_response.text
    assert forwarded
    forwarded_message = forwarded[-1]
    assert forwarded_message["type"] == "message"
    assert forwarded_message["content"][0]["text"] == (
        "Manager guidance: inspect the failed worker."
    )

    orchestrator = conversations.get_conversation(orchestrator_id)
    manager = conversations.get_conversation(manager_id)
    same_worker = conversations.get_conversation(worker.id)
    assert orchestrator is not None and orchestrator.parent_conversation_id is None
    assert manager is not None and manager.parent_conversation_id is None
    assert same_worker is not None and same_worker.parent_conversation_id == orchestrator_id

    child_rows = (await client.get(f"/v1/sessions/{orchestrator_id}/child_sessions")).json()[
        "data"
    ]
    assert [row["id"] for row in child_rows] == [worker.id]

    after_history = (await client.get(f"/v1/sessions/{orchestrator_id}")).json()["items"]
    assert after_history[: len(before_history)] == before_history
    assert any(
        item.get("type") == "message"
        and item.get("data", {}).get("content", [{}])[0].get("text")
        == "Manager guidance: inspect the failed worker."
        for item in after_history
    )
    # A Manager run is silent unless it explicitly calls sys_inbox_notify.
    inbox = (await client.get("/v1/inbox/notifications")).json()
    assert inbox == {"object": "list", "data": [], "outstanding_count": 0}
