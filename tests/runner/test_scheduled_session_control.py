"""Scheduled-session capability overlay tests."""

from pathlib import Path

from omnigent._session_labels import SCHEDULED_AGENT_SESSION_CONTROL_LABEL
from omnigent.runner.app import _apply_scheduled_session_control
from omnigent.runner.native.orchestration import ResolvedSpec
from omnigent.spec.types import AgentSpec
from omnigent.tools.manager import ToolManager

_SESSION_CONTROL_TOOLS = {
    "sys_session_message",
    "sys_session_retry",
    "sys_inbox_notify",
}


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
