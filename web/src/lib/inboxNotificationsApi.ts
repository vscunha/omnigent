import { authenticatedFetch } from "./identity";

export type InboxNotificationSeverity = "info" | "attention" | "critical";

export interface InboxNotification {
  id: string;
  user_id: string | null;
  source_session_id: string;
  title: string;
  body: string;
  severity: InboxNotificationSeverity;
  created_at: number;
  acknowledged_at: number | null;
}

export interface InboxNotificationsResult {
  object: "list";
  data: InboxNotification[];
  outstanding_count: number;
}

async function readJson<T>(response: Response): Promise<T> {
  if (!response.ok) throw new Error(`Inbox request failed (${response.status})`);
  return (await response.json()) as T;
}

export async function listInboxNotifications(): Promise<InboxNotificationsResult> {
  const response = await authenticatedFetch("/v1/inbox/notifications");
  return readJson<InboxNotificationsResult>(response);
}

export async function acknowledgeInboxNotification(id: string): Promise<InboxNotification> {
  const response = await authenticatedFetch(`/v1/inbox/notifications/${encodeURIComponent(id)}`, {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ acknowledged: true }),
  });
  return readJson<InboxNotification>(response);
}
