import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  acknowledgeInboxNotification,
  listInboxNotifications,
  type InboxNotificationsResult,
} from "@/lib/inboxNotificationsApi";

export const INBOX_NOTIFICATIONS_KEY = ["inbox-notifications"] as const;

export function useInboxNotifications() {
  const queryClient = useQueryClient();
  const query = useQuery({
    queryKey: INBOX_NOTIFICATIONS_KEY,
    queryFn: listInboxNotifications,
    staleTime: 5_000,
    refetchInterval: 30_000,
  });
  const acknowledge = useMutation({
    mutationFn: acknowledgeInboxNotification,
    onSuccess: (notification) => {
      queryClient.setQueryData<InboxNotificationsResult>(INBOX_NOTIFICATIONS_KEY, (current) => {
        if (!current) return current;
        return {
          ...current,
          data: current.data.filter((item) => item.id !== notification.id),
          outstanding_count: Math.max(0, current.outstanding_count - 1),
        };
      });
    },
  });
  return { ...query, acknowledge };
}
