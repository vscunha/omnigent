"""Scheduled-session capability overlay tests."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from omnigent._session_labels import SCHEDULED_AGENT_SESSION_CONTROL_LABEL
from omnigent.runner import create_runner_app
from omnigent.runner.app import _apply_scheduled_session_control
from omnigent.runner.native.orchestration import ResolvedSpec
from omnigent.runner.resource_registry import SessionResourceRegistry
from omnigent.spec.types import AgentSpec
from omnigent.tools.manager import ToolManager
from tests.runner.conftest import (
    _FakeProcessManager,
    _runner_client,
    _ScriptedHarnessClient,
    _sse,
)
from tests.runner.helpers import NullServerClient

_SESSION_CONTROL_TOOLS = {
    "sys_session_message",
    "sys_session_retry",
    "sys_inbox_notify",
}

_SESSION_ID = "schedcontrol5f0f70bd0e9f4d92a51d8bd4a3a1c9"
_AGENT_ID = "agentcontrol1febd0be51c94b309a6f9f19c1f6f8a"


def _tool_names(tools: list[dict[str, Any]]) -> set[str]:
    """Return tool names from a harness body's mixed schema shapes."""
    names: set[str] = set()
    for tool in tools:
        function = tool.get("function")
        name = function.get("name") if isinstance(function, dict) else tool.get("name")
        if isinstance(name, str):
            names.add(name)
    return names


def test_enabled_schedule_overrides_agent_default_and_preserves_workdir() -> None:
    entry = ResolvedSpec(
        spec=AgentSpec(spec_version=1, agent_session_control=False),
        workdir=Path("/agent"),
    )

    result = _apply_scheduled_session_control(
        entry, {SCHEDULED_AGENT_SESSION_CONTROL_LABEL: "true"}
    )

    assert isinstance(result, ResolvedSpec)
    assert result.spec.agent_session_control is True
    assert result.workdir == Path("/agent")
    names = {schema["function"]["name"] for schema in ToolManager(result.spec).get_tool_schemas()}
    assert names >= _SESSION_CONTROL_TOOLS


def test_disabled_schedule_overrides_control_capable_agent() -> None:
    spec = AgentSpec(spec_version=1, agent_session_control=True)

    result = _apply_scheduled_session_control(
        spec, {SCHEDULED_AGENT_SESSION_CONTROL_LABEL: "false"}
    )

    assert isinstance(result, AgentSpec)
    assert result.agent_session_control is False
    names = {schema["function"]["name"] for schema in ToolManager(result).get_tool_schemas()}
    assert _SESSION_CONTROL_TOOLS.isdisjoint(names)


def test_non_scheduled_session_keeps_agent_setting() -> None:
    spec = AgentSpec(spec_version=1, agent_session_control=True)

    assert _apply_scheduled_session_control(spec, {}) is spec


class _LabeledSessionServerClient(NullServerClient):
    """Server stub whose session snapshot carries the scheduled control label."""

    def __init__(self, labels: dict[str, str]) -> None:
        """
        Initialize with the persisted session labels.

        :param labels: Session labels the snapshot should report.
        """
        self._labels = labels

    class _SnapshotResponse(NullServerClient._Response):
        """Stub 200 carrying the session binding plus its labels."""

        def __init__(self, labels: dict[str, str]) -> None:
            """
            Initialize with the labels to expose.

            :param labels: Session labels the snapshot should report.
            """
            self._labels = labels

        def json(self) -> dict[str, Any]:
            """Return the session snapshot body."""
            return {
                "id": _SESSION_ID,
                "agent_id": _AGENT_ID,
                "created_at": 1234,
                "workspace": None,
                "labels": self._labels,
            }

    async def get(self, url: str, **kwargs: Any) -> NullServerClient._Response:
        """Serve the labeled session snapshot; defer everything else.

        :param url: Request path, e.g. ``"/v1/sessions/<id>"``.
        :param kwargs: Extra keyword arguments (forwarded to the parent).
        :returns: Snapshot response for the session URL, empty 200 otherwise.
        """
        if url.split("?", 1)[0] == f"/v1/sessions/{_SESSION_ID}":
            return self._SnapshotResponse(self._labels)
        return await super().get(url, **kwargs)


async def _turn_tool_names(labels: dict[str, str]) -> set[str]:
    """Drive one scheduled turn and return the tools advertised to the harness.

    Session init caches the label-applied spec, then the first kickoff turn
    carries an ``agent_revision``; that revision invalidates the runner's
    agent-derived caches, so the turn resolves the bundle again. The tools the
    harness actually receives must still include the supervisor grant.

    :param labels: Session labels the server reports for the fired session.
    :returns: Tool names in the harness request body.
    """
    harness = _ScriptedHarnessClient(
        [
            _sse({"type": "response.created", "response": {"id": "resp_1"}}),
            _sse({"type": "response.completed", "response": {"id": "resp_1"}}),
        ]
    )

    async def _resolver(agent_id: str, session_id: str | None = None) -> AgentSpec:
        del agent_id, session_id
        return AgentSpec(spec_version=1, name="scheduled-supervisor")

    app = create_runner_app(
        process_manager=_FakeProcessManager(harness),  # type: ignore[arg-type]
        spec_resolver=_resolver,
        server_client=_LabeledSessionServerClient(labels),  # type: ignore[arg-type]
        resource_registry=SessionResourceRegistry(terminal_registry=None),
    )
    async with _runner_client(app) as client:
        init = await client.post(
            "/v1/sessions",
            json={"session_id": _SESSION_ID, "agent_id": _AGENT_ID},
        )
        assert init.status_code == 201, init.text
        turn = await client.post(
            f"/v1/sessions/{_SESSION_ID}/events",
            json={
                "type": "message",
                "role": "user",
                "agent_id": _AGENT_ID,
                # The fire path stamps the bundle revision on the kickoff turn.
                "agent_revision": "rev-2",
                "content": [{"type": "input_text", "text": "manager pass"}],
            },
        )
        assert turn.status_code == 202, turn.text
        for _ in range(200):
            if harness.posted_bodies:
                break
            await asyncio.sleep(0.01)

    assert harness.posted_bodies, "the scheduled turn never reached the harness"
    return _tool_names(harness.posted_bodies[0].get("tools") or [])


@pytest.mark.asyncio
async def test_scheduled_control_survives_first_turn_agent_revision_reset() -> None:
    """A scheduled Manager keeps its supervisor tools for its first real turn.

    Regression: the kickoff turn's ``agent_revision`` cleared the session spec
    cache that init had built with the scheduled label, so the turn resolved the
    raw bundle and advertised no ``sys_session_message``/``sys_session_retry``.
    The Manager then had only child-targeted ``sys_session_send`` available and
    its recovery attempt failed with ``session_out_of_tree``.
    """
    names = await _turn_tool_names({SCHEDULED_AGENT_SESSION_CONTROL_LABEL: "true"})

    assert names >= _SESSION_CONTROL_TOOLS


@pytest.mark.asyncio
async def test_unscheduled_turn_keeps_isolation_without_control_label() -> None:
    """A non-scheduled session gains no cross-session mutation tools."""
    names = await _turn_tool_names({})

    assert _SESSION_CONTROL_TOOLS.isdisjoint(names)


@pytest.mark.asyncio
async def test_scheduled_opt_out_removes_control_tools_after_cache_reset() -> None:
    """An explicit scheduled opt-out still wins after the cache rebuild."""
    names = await _turn_tool_names({SCHEDULED_AGENT_SESSION_CONTROL_LABEL: "false"})

    assert _SESSION_CONTROL_TOOLS.isdisjoint(names)
