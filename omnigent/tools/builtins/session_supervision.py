"""Agent-facing tools for supervising independent sessions."""

from __future__ import annotations

from typing import Any

from omnigent.tools.base import Tool


class SysSessionChildrenTool(Tool):
    """List the direct children of any readable session."""

    @classmethod
    def name(cls) -> str:
        return "sys_session_children"

    @classmethod
    def description(cls) -> str:
        return (
            "List the direct child sessions of another session you can read. "
            "Returns compact lifecycle, identity, activity, and failure state "
            "from Omnigent's persisted parent_session_id relationship."
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
                        "session_id": {
                            "type": "string",
                            "description": "Parent session whose direct children to list.",
                        }
                    },
                    "required": ["session_id"],
                    "additionalProperties": False,
                },
            },
        }


class SysSessionMessageTool(Tool):
    """Deliver ordinary input to an independently owned session."""

    @classmethod
    def name(cls) -> str:
        return "sys_session_message"

    @classmethod
    def description(cls) -> str:
        return (
            "Deliver a normal user-input message to another session you may edit. "
            "This does not create or change any parent/child relationship and does "
            "not route the target's completion into your agent inbox."
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
                        "session_id": {
                            "type": "string",
                            "description": "Target session to receive the message.",
                        },
                        "message": {
                            "type": "string",
                            "description": "Non-empty input to deliver to the target session.",
                        },
                    },
                    "required": ["session_id", "message"],
                    "additionalProperties": False,
                },
            },
        }


class SysSessionRetryTool(Tool):
    """Recover the existing runner for an independently owned session."""

    @classmethod
    def name(cls) -> str:
        return "sys_session_retry"

    @classmethod
    def description(cls) -> str:
        return (
            "Reconnect or relaunch an existing session's runner in place. The "
            "session id, history, agent configuration, and workspace are preserved."
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
                        "session_id": {
                            "type": "string",
                            "description": "Existing session to recover in place.",
                        }
                    },
                    "required": ["session_id"],
                    "additionalProperties": False,
                },
            },
        }


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
