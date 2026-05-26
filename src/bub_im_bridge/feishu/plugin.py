"""Bub plugin entry point for Feishu (Lark) channel."""

from __future__ import annotations

from contextvars import ContextVar
from typing import Any

from bub.channels.message import ChannelMessage
from bub.envelope import field_of
from bub.framework import BubFramework
from bub.hookspecs import hookimpl
from bub.types import MessageHandler

from bub_im_bridge.feishu.channel import FeishuChannel
from bub_im_bridge.feishu import tools  # noqa: F401 - import to register tools

_CURRENT_FEISHU_REPLY_TO: ContextVar[str | None] = ContextVar(
    "bub_im_bridge_current_feishu_reply_to",
    default=None,
)


class FeishPlugin:
    """Plugin that provides Feishu channel for Bub framework."""

    def __init__(self, framework: BubFramework) -> None:
        self.framework = framework

    @hookimpl
    def provide_channels(self, message_handler: MessageHandler) -> list[Any]:
        """Provide Feishu channel to Bub."""
        return [FeishuChannel(on_receive=message_handler, framework=self.framework)]

    @hookimpl
    async def load_state(self, message: Any, session_id: str) -> dict[str, str | None]:
        reply_to = None
        if field_of(message, "channel") == "feishu":
            reply_to = _extract_reply_to_message_id(message)
        _CURRENT_FEISHU_REPLY_TO.set(reply_to)
        return {"feishu_reply_to_message_id": reply_to}

    @hookimpl(tryfirst=True)
    async def dispatch_outbound(self, message: Any) -> bool:
        channel_name = field_of(message, "output_channel", field_of(message, "channel"))
        if channel_name != "feishu":
            return False

        reply_to = _CURRENT_FEISHU_REPLY_TO.get()
        if not reply_to:
            return False

        context = field_of(message, "context")
        if isinstance(context, dict):
            context.setdefault("reply_to_message_id", reply_to)
        elif isinstance(message, ChannelMessage):
            message.context = {"reply_to_message_id": reply_to}
        elif isinstance(message, dict):
            message["context"] = {"reply_to_message_id": reply_to}
        return False


def _extract_reply_to_message_id(message: Any) -> str | None:
    context = field_of(message, "context", {})
    if not isinstance(context, dict):
        return None
    reply_to = context.get("reply_to_message_id")
    return reply_to if isinstance(reply_to, str) and reply_to else None
