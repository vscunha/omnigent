"""Persistence interface for durable Inbox notifications."""

from __future__ import annotations

from abc import ABC, abstractmethod

from omnigent.entities.inbox_notification import InboxNotification


class InboxNotificationStore(ABC):
    """Owner-scoped persistence for agent-created Inbox items."""

    def __init__(self, storage_location: str) -> None:
        self.storage_location = storage_location

    @abstractmethod
    def create(
        self,
        *,
        user_id: str | None,
        source_session_id: str,
        title: str,
        body: str,
        severity: str,
    ) -> InboxNotification: ...

    @abstractmethod
    def list_for_user(
        self,
        *,
        user_id: str | None,
        outstanding_only: bool = True,
    ) -> list[InboxNotification]: ...

    @abstractmethod
    def acknowledge(
        self,
        notification_id: str,
        *,
        user_id: str | None,
    ) -> InboxNotification | None: ...

    @abstractmethod
    def count_outstanding(self, *, user_id: str | None) -> int: ...


__all__ = ["InboxNotificationStore"]
