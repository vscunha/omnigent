"""The resolve-URL path (UI Approve) must wake a parked harness elicitation via
its ``resolved_elsewhere`` event, not only its Future.

Before the fix, ``_resolve_elicitation`` set the Future but never signalled
``_harness_parked_elicitations``, so an ASK-gated tool call whose long-poll had
severed/re-parked never woke on UI Approve → indefinite hang. Only the
``/events`` path signalled it. This locks the resolve-URL path in.
"""

from __future__ import annotations

import asyncio

import pytest

from omnigent.server.routes import sessions as S


@pytest.mark.asyncio
async def test_resolve_elicitation_signals_parked_harness_elicitation():
    sid = "conv_resolveurl_test"
    eid = "elicit_evaluate_deadbeefdeadbeefdeadbeefdeadbeef"
    parked = S._ParkedHarnessElicitation(
        session_id=sid,
        tool_name="mcp_example__apply_change",
        tool_input={},
        resolved_elsewhere=asyncio.Event(),
    )
    S._harness_parked_elicitations[eid] = parked
    S._harness_elicitation_owners[eid] = sid
    try:
        assert not parked.resolved_elsewhere.is_set()
        # runner_router=None → the runner forward is skipped; we only assert the
        # server-side parked-elicitation wake.
        await S._resolve_elicitation(sid, {"elicitation_id": eid, "action": "accept"}, None)
        assert parked.resolved_elsewhere.is_set(), (
            "resolve-URL must signal the parked harness elicitation (resolved_elsewhere), "
            "otherwise an ASK-gated tool hangs on UI Approve"
        )
    finally:
        S._harness_parked_elicitations.pop(eid, None)
        S._harness_elicitation_owners.pop(eid, None)


@pytest.mark.asyncio
async def test_not_parked_resolve_keeps_verdict_tombstone():
    # Regression (#62 review): when nothing is parked (severed long-poll, before
    # the retry re-parks), resolve-URL must store a pre-resolved tombstone
    # carrying the ACTUAL verdict, so the re-park returns it. The resolved_elsewhere
    # wake must NOT clobber it with a verdict-less tombstone.
    sid = "conv_tombstone"
    eid = "elicit_evaluate_22222222222222222222222222222222"
    S._harness_parked_elicitations.pop(eid, None)  # ensure NOT parked
    S._harness_pre_resolved_elicitations.pop(eid, None)
    try:
        await S._resolve_elicitation(sid, {"elicitation_id": eid, "action": "accept"}, None)
        tomb = S._harness_pre_resolved_elicitations.get(eid)
        assert tomb is not None, "a not-parked resolve must leave a pre-resolved tombstone"
        assert tomb.result is not None, "the tombstone must carry the verdict (not clobbered)"
    finally:
        S._harness_pre_resolved_elicitations.pop(eid, None)


@pytest.mark.asyncio
async def test_resolve_elicitation_wrong_session_does_not_wake():
    # Ownership guard: a resolve for a DIFFERENT session must not wake this park.
    sid = "conv_owner"
    eid = "elicit_evaluate_11111111111111111111111111111111"
    parked = S._ParkedHarnessElicitation(
        session_id=sid, tool_name="t", tool_input={}, resolved_elsewhere=asyncio.Event()
    )
    S._harness_parked_elicitations[eid] = parked
    S._harness_elicitation_owners[eid] = sid
    try:
        await S._resolve_elicitation(
            "conv_other", {"elicitation_id": eid, "action": "accept"}, None
        )
        assert not parked.resolved_elsewhere.is_set()
    finally:
        S._harness_parked_elicitations.pop(eid, None)
        S._harness_elicitation_owners.pop(eid, None)


@pytest.mark.asyncio
async def test_resolve_url_drains_pending_elicitation_index():
    """An accepted verdict must remove the prompt from the pending index.

    ``GET /v1/sessions/{id}`` replays the index into the UI, so an entry left
    behind is an approval card that reappears on every refresh even though the
    verdict was accepted.
    """
    from omnigent.runtime import pending_elicitations, session_stream

    sid = "conv_drain_test"
    eid = "elicit_evaluate_33333333333333333333333333333333"
    session_stream.publish(
        sid,
        {
            "type": "response.elicitation_request",
            "elicitation_id": eid,
            "params": {
                "mode": "form",
                "message": "Approve?",
                "requestedSchema": {},
                "phase": "tool_call",
                "policy_name": "__ask_on_add_policy",
                "content_preview": "{}",
            },
        },
    )
    assert pending_elicitations.count_for(sid) == 1

    await S._resolve_elicitation(sid, {"elicitation_id": eid, "action": "accept"}, None)

    assert pending_elicitations.count_for(sid) == 0, (
        "an accepted verdict must drain the pending index; a leftover entry "
        "re-renders the approval card on every page refresh"
    )


class _NeverDisconnectingRequest:
    """A parked long-poll whose client stays connected.

    ``_poll_request_disconnect`` loops on ``request.receive()`` until it sees
    ``http.disconnect``; blocking forever here keeps the verdict path — not
    disconnect detection — as the signal that ends the wait.
    """

    async def receive(self) -> dict[str, object]:
        """Block until cancelled; a connected client never disconnects."""
        await asyncio.Event().wait()
        raise AssertionError("unreachable")


async def _wait_until(predicate, *, timeout: float = 5.0) -> None:
    """Spin the loop until *predicate* holds, or fail the test."""
    deadline = asyncio.get_running_loop().time() + timeout
    while not predicate():
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError("condition never became true")
        await asyncio.sleep(0.01)


@pytest.mark.asyncio
async def test_hook_retry_reattaches_instead_of_orphaning_the_first_park(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A retry reusing a live elicitation id must share that park's verdict.

    A native hook whose long-poll is severed retries with the same stable id
    while its earlier wait can still be parked. Registering the retry by
    overwriting the per-id registry orphans the earlier waiter's Future: only
    one of the two ever observes the verdict, the other holds its connection
    until its own timeout, and the retry re-publishes a card that is already on
    screen — so an approval looks like it did not stick.

    Both waiters must return the verdict, and the card must be published once.
    """
    from omnigent.runtime import session_stream
    from omnigent.server.schemas import ElicitationRequestParams

    sid = "conv_reattach_test"
    eid = "elicit_evaluate_44444444444444444444444444444444"
    params = ElicitationRequestParams(
        mode="form",
        message="Approve?",
        requestedSchema={},
        phase="tool_call",
        policy_name="__ask_on_add_policy",
        content_preview="{}",
    )

    published: list[dict] = []
    real_publish = session_stream.publish

    def _recording_publish(conversation_id: str, event: dict) -> None:
        if event.get("type") == "response.elicitation_request":
            published.append(event)
        real_publish(conversation_id, event)

    monkeypatch.setattr(session_stream, "publish", _recording_publish)

    request = _NeverDisconnectingRequest()
    try:
        first = asyncio.create_task(
            S._publish_and_wait_for_harness_elicitation(
                request,
                session_id=sid,
                params=params,
                timeout_s=30.0,
                elicitation_id=eid,
            )
        )
        await _wait_until(lambda: eid in S._harness_parked_elicitations)
        owner_future = S._harness_elicitation_registry[eid]
        assert len(published) == 1, "the first park publishes the card once"

        # The hook retry: same id, same question, first park still live.
        retry = asyncio.create_task(
            S._publish_and_wait_for_harness_elicitation(
                request,
                session_id=sid,
                params=params,
                timeout_s=30.0,
                elicitation_id=eid,
            )
        )
        await asyncio.sleep(0.05)
        assert S._harness_elicitation_registry[eid] is owner_future, (
            "a retry must re-attach to the live park, not replace its future"
        )
        assert len(published) == 1, "a re-attached retry must not re-publish the approval card"

        await S._resolve_elicitation(sid, {"elicitation_id": eid, "action": "accept"}, None)

        verdicts = await asyncio.wait_for(asyncio.gather(first, retry), timeout=10.0)
        assert [v.action if v is not None else None for v in verdicts] == [
            "accept",
            "accept",
        ], "every waiter attached to the park must observe the verdict"
    finally:
        S._harness_elicitation_registry.pop(eid, None)
        S._harness_elicitation_owners.pop(eid, None)
        S._harness_parked_elicitations.pop(eid, None)
        S._harness_pre_resolved_elicitations.pop(eid, None)


@pytest.mark.asyncio
async def test_orphan_approval_leaves_a_trace(caplog: pytest.LogCaptureFixture) -> None:
    """An approval that lands with nothing parked must not vanish silently.

    When no waiter is registered the verdict is only tombstoned for a possible
    re-park — no side effect is applied. If the hook never re-parks, the user's
    approval is simply gone. It has to leave a log record so the drop is
    diagnosable rather than invisible.
    """
    import logging

    from omnigent.runtime import pending_elicitations, session_stream

    sid = "conv_orphan_trace"
    eid = "elicit_evaluate_55555555555555555555555555555555"
    session_stream.publish(
        sid,
        {
            "type": "response.elicitation_request",
            "elicitation_id": eid,
            "params": {
                "mode": "form",
                "message": "Approve?",
                "requestedSchema": {},
                "phase": "tool_call",
                "policy_name": "__ask_on_add_policy",
                "content_preview": "{}",
            },
        },
    )
    S._harness_elicitation_registry.pop(eid, None)
    S._harness_elicitation_owners.pop(eid, None)
    try:
        assert pending_elicitations.count_for(sid) == 1
        with caplog.at_level(logging.INFO):
            await S._resolve_elicitation(sid, {"elicitation_id": eid, "action": "accept"}, None)
        assert any("nothing parked" in record.getMessage() for record in caplog.records), (
            "an orphaned approval must leave a log record"
        )
    finally:
        S._harness_pre_resolved_elicitations.pop(eid, None)
        pending_elicitations.resolve(sid, eid)
