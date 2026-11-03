"""SQLAlchemy-backed durable Inbox notification store."""

from __future__ import annotations

import uuid

from sqlalchemy import func, select

from omnigent.db.db_models import SqlInboxNotification, current_workspace_id
from omnigent.db.utils import get_or_create_engine, make_named_managed_session_maker, now_epoch
from omnigent.entities.inbox_notification import InboxNotification
from omnigent.stores.inbox_notification_store import InboxNotificationStore


def _to_entity(row: SqlInboxNotification) -> InboxNotification:
    return InboxNotification(
        id=row.id,
        user_id=row.user_id,
        source_session_id=row.source_session_id,
        title=row.title,
        body=row.body,
        severity=row.severity,
        created_at=row.created_at,
        acknowledged_at=row.acknowledged_at,
    )


class SqlAlchemyInboxNotificationStore(InboxNotificationStore):
    """Relational Inbox notification persistence scoped by workspace and user."""

    def __init__(self, storage_location: str) -> None:
        super().__init__(storage_location)
        self._engine = get_or_create_engine(storage_location)
        self._session = make_named_managed_session_maker(
            self._engine,
            query_name_prefix="omnigent.inbox_notification_store",
        )

    def create(
        self,
        *,
        user_id: str | None,
        source_session_id: str,
        title: str,
        body: str,
        severity: str,
    ) -> InboxNotification:
        row = SqlInboxNotification(
            id=uuid.uuid4().hex,
            user_id=user_id,
            source_session_id=source_session_id,
            title=title,
            body=body,
            severity=severity,
            created_at=now_epoch(),
        )
        with self._session("insert_notification") as session:
            session.add(row)
            return _to_entity(row)

    def list_for_user(
        self,
        *,
        user_id: str | None,
        outstanding_only: bool = True,
    ) -> list[InboxNotification]:
        stmt = select(SqlInboxNotification).where(
            SqlInboxNotification.workspace_id == current_workspace_id(),
            SqlInboxNotification.user_id == user_id,
        )
        if outstanding_only:
            stmt = stmt.where(SqlInboxNotification.acknowledged_at.is_(None))
        stmt = stmt.order_by(
            SqlInboxNotification.created_at.desc(),
            SqlInboxNotification.id.desc(),
        )
        with self._session("list_user_notifications") as session:
            return [_to_entity(row) for row in session.execute(stmt).scalars().all()]

    def acknowledge(
        self,
        notification_id: str,
        *,
        user_id: str | None,
    ) -> InboxNotification | None:
        with self._session("acknowledge_notification") as session:
            row = session.get(
                SqlInboxNotification,
                (current_workspace_id(), notification_id),
            )
            if row is None or row.user_id != user_id:
                return None
            if row.acknowledged_at is None:
                row.acknowledged_at = now_epoch()
            return _to_entity(row)

    def count_outstanding(self, *, user_id: str | None) -> int:
        stmt = select(func.count(SqlInboxNotification.id)).where(
            SqlInboxNotification.workspace_id == current_workspace_id(),
            SqlInboxNotification.user_id == user_id,
            SqlInboxNotification.acknowledged_at.is_(None),
        )
        with self._session("count_outstanding_notifications") as session:
            return int(session.execute(stmt).scalar_one())
