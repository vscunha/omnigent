"""End-to-end topology checks for independent Manager supervision."""

from __future__ import annotations

import asyncio
import json
from typing import Any
from uuid import uuid4

import httpx
import pytest

from omnigent.runner.tool_dispatch import execute_tool
from omnigent.server.routes.sessions import routes_events
from omnigent.spec.types import AgentSpec
from omnigent.stores.conversation_store.sqlalchemy_store import (
    SqlAlchemyConversationStore,
)
from tests.server.helpers import create_test_agent
from tests.server.integration.test_managed_startup_inputs import auth_app as auth_app
from tests.server.integration.test_managed_startup_inputs import (
    startup_permissions as startup_permissions,
)
from tests.server.integration.test_session_orchestration import peers as peers

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


async def _create_top_level_session(
    client: httpx.AsyncClient,
    *,
    agent_id: str,
    title: str,
) -> str:
    """Create a top-level session and return its id.

    :param client: Test HTTP client bound to the app.
    :param agent_id: Bundled agent id the session binds, e.g. ``"ag_..."``.
    :param title: Session title, e.g. ``"Orchestrator"``.
    :returns: The created session id.
    """
    response = await client.post("/v1/sessions", json={"agent_id": agent_id, "title": title})
    assert response.status_code == 201, response.text
    return response.json()["id"]


async def test_manager_runner_send_wakes_idle_orchestrator_across_trees(
    auth_client: httpx.AsyncClient,
    auth_app: Any,
    peers: Any,
    db_uri: str,
) -> None:
    """The real runner dispatch wakes an idle cross-tree Orchestrator in place.

    A scheduled Manager calls ``sys_session_message`` against a separately
    rooted Orchestrator. The message starts the Orchestrator's next turn on the
    same session without changing its tree or forwarding a child completion.
    """
    from omnigent.server.scheduled.fire import FireDeps, build_on_fire
    from omnigent.stores.permission_store.sqlalchemy_store import SqlAlchemyPermissionStore
    from omnigent.stores.scheduled_task_store.sqlalchemy_store import SqlAlchemyScheduledTaskStore
    from tests._helpers.polling import poll_until_async
    from tests.server.integration.test_sessions_attribution import _bind_runner

    source, target, forwarded = peers
    orchestrator_id = target["id"]
    conversations = SqlAlchemyConversationStore(db_uri)
    worker = conversations.create_conversation(
        kind="sub_agent",
        title="worker:implementation",
        parent_conversation_id=orchestrator_id,
        agent_id=target["agent_id"],
        sub_agent_name="implementation",
    )
    schedules = SqlAlchemyScheduledTaskStore(db_uri)
    auth_app.state.scheduled_task_store = schedules
    task = schedules.create(
        scheduled_task_id=uuid4().hex,
        name="Manager",
        prompt="Supervise the existing Orchestrator",
        rrule="FREQ=HOURLY",
        user_id="alice",
        agent_id=source["agent_id"],
        timezone="UTC",
        host_id=uuid4().hex,
        workspace="/tmp",
    )

    async def launch(session: Any, scheduled_task: Any) -> None:
        pass

    on_fire = build_on_fire(
        FireDeps(
            scheduled_task_store=schedules,
            agent_store=auth_app.state.agent_store,
            conversation_store=conversations,
            permission_store=SqlAlchemyPermissionStore(db_uri),
            host_store=auth_app.state.host_store,
            host_registry=auth_app.state.host_registry,
        ),
        launch_dispatch=launch,
    )
    assert await on_fire(0, task.id)
    await poll_until_async(lambda: schedules.get(task.id).last_run_conversation_id, timeout_s=5)
    manager_id = schedules.get(task.id).last_run_conversation_id
    auth_client.headers.update(_bind_runner(db_uri, manager_id))
    forwarded.clear()
    manager_inbox: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
    # A Manager points at its Orchestrator with the native peer-message tool.
    output = await execute_tool(
        tool_name="sys_session_message",
        arguments=json.dumps(
            {"session_id": orchestrator_id, "message": "Manager: refill the writer lanes."}
        ),
        server_client=auth_client,
        conversation_id=manager_id,
        agent_spec=AgentSpec(spec_version=1, spawn=True),
        session_inbox=manager_inbox,
    )
    assert json.loads(output).get("queued") is True, output
    assert forwarded.get("posts"), "the idle Orchestrator never received a turn"
    sent_event = forwarded["posts"][-1][1]
    sent_text = sent_event["content"][0]["text"]
    assert "Manager: refill the writer lanes." in sent_text
    assert f"[Message from session {manager_id}]" in sent_text
    assert manager_inbox.empty()

    orchestrator = conversations.get_conversation(orchestrator_id)
    manager = conversations.get_conversation(manager_id)
    same_worker = conversations.get_conversation(worker.id)
    assert orchestrator is not None and orchestrator.parent_conversation_id is None
    assert manager is not None and manager.parent_conversation_id is None
    assert same_worker is not None and same_worker.parent_conversation_id == orchestrator_id
    children = (await auth_client.get(f"/v1/sessions/{orchestrator_id}/child_sessions")).json()[
        "data"
    ]
    assert [row["id"] for row in children] == [worker.id]
    history = (await auth_client.get(f"/v1/sessions/{orchestrator_id}/items")).json()
    assert "Manager: refill the writer lanes." in json.dumps(history)


@pytest.mark.parametrize("notification_opt_in", [False, True])
async def test_unauthorized_cross_tree_send_still_refused(
    client: httpx.AsyncClient,
    db_uri: str,
    monkeypatch: pytest.MonkeyPatch,
    notification_opt_in: bool,
) -> None:
    """Inbox notification opt-in never permits sending to a non-child."""
    agent = await create_test_agent(client, name="supervision-no-grant")
    orchestrator_id = await _create_top_level_session(
        client, agent_id=agent["id"], title="Orchestrator"
    )
    manager_id = await _create_top_level_session(client, agent_id=agent["id"], title="Manager")

    async def _never_forward(
        session_id: str,
        runner_router: object,
    ) -> None:
        del session_id, runner_router
        raise AssertionError("an unauthorized cross-tree send must not reach a runner")

    monkeypatch.setattr(routes_events, "_get_runner_client", _never_forward)
    before = (await client.get(f"/v1/sessions/{orchestrator_id}")).json()["items"]
    output = await execute_tool(
        tool_name="sys_session_send",
        arguments=json.dumps({"session_id": orchestrator_id, "args": "resume"}),
        server_client=client,
        conversation_id=manager_id,
        agent_spec=AgentSpec(
            spec_version=1, spawn=True, agent_session_control=notification_opt_in
        ),
        session_inbox=asyncio.Queue(),
    )

    assert json.loads(output)["error"] == "session_out_of_tree"
    after = (await client.get(f"/v1/sessions/{orchestrator_id}")).json()["items"]
    assert after == before
    orchestrator = SqlAlchemyConversationStore(db_uri).get_conversation(orchestrator_id)
    assert orchestrator is not None and orchestrator.parent_conversation_id is None
