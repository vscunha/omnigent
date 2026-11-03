"""Persistence tests for durable user-facing Inbox notifications."""

from __future__ import annotations

from omnigent.stores.inbox_notification_store.sqlalchemy_store import (
    SqlAlchemyInboxNotificationStore,
)


def test_notification_survives_store_reconstruction_and_acknowledgement(db_uri: str) -> None:
    source_id = "a" * 32
    created = SqlAlchemyInboxNotificationStore(db_uri).create(
        user_id="alice@example.com",
        source_session_id=source_id,
        title="Human action required",
        body="Reconnect the deployment credential.",
        severity="critical",
    )

    reloaded = SqlAlchemyInboxNotificationStore(db_uri)
    assert reloaded.count_outstanding(user_id="alice@example.com") == 1
    assert reloaded.list_for_user(user_id="alice@example.com") == [created]

    acknowledged = reloaded.acknowledge(created.id, user_id="alice@example.com")
    assert acknowledged is not None
    assert acknowledged.source_session_id == source_id
    assert acknowledged.acknowledged_at is not None
    assert reloaded.count_outstanding(user_id="alice@example.com") == 0
    assert reloaded.list_for_user(user_id="alice@example.com") == []
    assert reloaded.list_for_user(
        user_id="alice@example.com",
        outstanding_only=False,
    ) == [acknowledged]


def test_notifications_are_scoped_to_owner(db_uri: str) -> None:
    store = SqlAlchemyInboxNotificationStore(db_uri)
    created = store.create(
        user_id="alice@example.com",
        source_session_id="b" * 32,
        title="Alice only",
        body="Private owner-scoped notification.",
        severity="attention",
    )

    assert store.list_for_user(user_id="bob@example.com") == []
    assert store.count_outstanding(user_id="bob@example.com") == 0
    assert store.acknowledge(created.id, user_id="bob@example.com") is None
    assert store.count_outstanding(user_id="alice@example.com") == 1


def test_healthy_manager_without_notify_call_creates_no_inbox_item(db_uri: str) -> None:
    store = SqlAlchemyInboxNotificationStore(db_uri)

    assert store.list_for_user(user_id=None) == []
    assert store.count_outstanding(user_id=None) == 0
