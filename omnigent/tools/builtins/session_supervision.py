"""Agent-facing Inbox notification tool."""

from __future__ import annotations

from typing import Any

from omnigent.tools.base import Tool


class SysInboxNotifyTool(Tool):
    """Create a durable notification in the current user's Inbox."""

    @classmethod
    def name(cls) -> str:
        return "sys_inbox_notify"

    @classmethod
    def description(cls) -> str:
        return (
            "Post a durable notification to the current user's Omnigent Inbox. "
            "The source is automatically the calling session and cannot be overridden."
        )

    def get_schema(self) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.name(),
                "description": self.description(),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "title": {
                            "type": "string",
                            "description": "Short summary of the condition requiring attention.",
                        },
                        "body": {
                            "type": "string",
                            "description": (
                                "What is blocked, why autonomous recovery is insufficient, "
                                "and the exact human action required."
                            ),
                        },
                        "severity": {
                            "type": "string",
                            "enum": ["info", "attention", "critical"],
                            "description": "Urgency of the notification.",
                        },
                    },
                    "required": ["title", "body", "severity"],
                    "additionalProperties": False,
                },
            },
        }
