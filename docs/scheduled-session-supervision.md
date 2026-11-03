# Scheduled session supervision

An automation can reach an existing session owned by the same user through
Omnigent's native peer-message path: the fired run calls `sys_session_message`
with that session's ID. The message lands as ordinary input, so the target keeps
its own tree, workers and history; its completion is not routed back into the
automation's session. Messaging a stopped session resumes it on its connected
host, so no separate recovery call is required.

`sys_session_send` by session ID stays child-only: use `sys_session_message` for
a session outside the caller's subtree. A queued response confirms acceptance,
not completion; inspect the target's status and history before deciding whether
further action is necessary.

## Inbox notifications

Set `agent_session_control` on a scheduled task to advertise `sys_inbox_notify`
to its fired sessions. The tool writes a durable notification to the owner's
Inbox, with the calling session as the source; it grants no cross-session
authority. Ordinary child creation, reads and peer messaging keep their native
authorization whether or not the opt-in is set.

## Migration and deployment

The merged migration head joins the previously deployed private schema and the
upstream schema. Both lineages introduced `scheduled_tasks.project_id`; the
migrations reconcile the existing column and preserve its values. A later
revision drops the retired `supervised_session_id` binding, so tasks carry no
supervision target. Upgrade and downgrade reconciliation require an online
database connection. Do not stamp or edit `alembic_version` by hand.

Before applying an update, review the pending code and perform the live
installation's `./update.sh` workflow from its existing checkout. The script
creates a SQLite-aware backup when a migration is pending, rebuilds the web UI,
restarts the services, and restores live sessions. Verify service health, the
migration head, and the existing automation states without firing paused tasks.
