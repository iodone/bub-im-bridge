from __future__ import annotations

import asyncio
from unittest.mock import MagicMock

import pytest

from bub.channels.message import ChannelMessage

from bub_im_bridge.feishu.plugin import FeishPlugin


@pytest.mark.asyncio
async def test_dispatch_outbound_injects_reply_target_for_current_feishu_turn() -> None:
    plugin = FeishPlugin(framework=MagicMock())
    inbound = ChannelMessage(
        session_id="feishu:chat-1",
        channel="feishu",
        chat_id="chat-1",
        content="hello",
        context={"reply_to_message_id": "msg-1"},
    )
    outbound = ChannelMessage(
        session_id="feishu:chat-1",
        channel="feishu",
        chat_id="chat-1",
        content="reply",
    )

    await plugin.load_state(inbound, "feishu:chat-1")
    result = await plugin.dispatch_outbound(outbound)

    assert result is False
    assert outbound.context["reply_to_message_id"] == "msg-1"


@pytest.mark.asyncio
async def test_dispatch_outbound_does_not_override_explicit_reply_target() -> None:
    plugin = FeishPlugin(framework=MagicMock())
    inbound = ChannelMessage(
        session_id="feishu:chat-1",
        channel="feishu",
        chat_id="chat-1",
        content="hello",
        context={"reply_to_message_id": "msg-1"},
    )
    outbound = ChannelMessage(
        session_id="feishu:chat-1",
        channel="feishu",
        chat_id="chat-1",
        content="reply",
        context={"reply_to_message_id": "msg-explicit"},
    )

    await plugin.load_state(inbound, "feishu:chat-1")
    await plugin.dispatch_outbound(outbound)

    assert outbound.context["reply_to_message_id"] == "msg-explicit"


@pytest.mark.asyncio
async def test_schedule_like_feishu_turn_without_reply_target_stays_unreplied() -> None:
    plugin = FeishPlugin(framework=MagicMock())
    inbound = ChannelMessage(
        session_id="feishu:chat-1",
        channel="feishu",
        chat_id="chat-1",
        content="scheduled reminder",
    )
    outbound = ChannelMessage(
        session_id="feishu:chat-1",
        channel="feishu",
        chat_id="chat-1",
        content="reply",
    )

    await plugin.load_state(inbound, "feishu:chat-1")
    await plugin.dispatch_outbound(outbound)

    assert "reply_to_message_id" not in outbound.context


@pytest.mark.asyncio
async def test_dispatch_outbound_ignores_non_feishu_channels() -> None:
    plugin = FeishPlugin(framework=MagicMock())
    inbound = ChannelMessage(
        session_id="jsonrpc:chat-1",
        channel="jsonrpc",
        chat_id="chat-1",
        content="hello",
        context={"reply_to_message_id": "msg-1"},
    )
    outbound = ChannelMessage(
        session_id="jsonrpc:chat-1",
        channel="jsonrpc",
        chat_id="chat-1",
        content="reply",
    )

    await plugin.load_state(inbound, "jsonrpc:chat-1")
    await plugin.dispatch_outbound(outbound)

    assert "reply_to_message_id" not in outbound.context


@pytest.mark.asyncio
async def test_reply_target_is_isolated_per_concurrent_turn() -> None:
    plugin = FeishPlugin(framework=MagicMock())

    async def run_turn(message_id: str, delay: float) -> str | None:
        inbound = ChannelMessage(
            session_id=f"feishu:{message_id}",
            channel="feishu",
            chat_id=message_id,
            content="hello",
            context={"reply_to_message_id": message_id},
        )
        outbound = ChannelMessage(
            session_id=f"feishu:{message_id}",
            channel="feishu",
            chat_id=message_id,
            content="reply",
        )
        await plugin.load_state(inbound, inbound.session_id)
        await asyncio.sleep(delay)
        await plugin.dispatch_outbound(outbound)
        return outbound.context.get("reply_to_message_id")

    result_a, result_b = await asyncio.gather(
        run_turn("msg-a", 0.02),
        run_turn("msg-b", 0.0),
    )

    assert result_a == "msg-a"
    assert result_b == "msg-b"
