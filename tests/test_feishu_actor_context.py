"""Tests for Feishu structured actor context (phase 4) and prompt rules (phase 5).

These tests validate that:
- Phase 4: sender, mentions[], and reply_target are passed as structured data
- Phase 5: prompt rules prevent wrong-person replies and "I don't know your name" hallucinations
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from bub.channels.message import ChannelMessage
from bub_im_bridge.feishu.channel import (
    FeishuChannel,
    FeishuInboundMessage,
    FeishuMention,
    _parse_event,
)
from bub_im_bridge.feishu.feishu_prompts import build_user_context_hint
from bub_im_bridge.profiles import ProfileStore, UserProfile


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_raw_event(
    *,
    sender_open_id: str = "ou_sender",
    sender_name: str = "张三",
    mentions: list[dict] | None = None,
    text: str = "hello",
    chat_type: str = "group",
    parent_id: str | None = None,
) -> dict:
    """Build a minimal raw Feishu event dict for testing."""
    mention_list = []
    for m in (mentions or []):
        mention_list.append({
            "key": m.get("key", "@_user"),
            "name": m.get("name", ""),
            "id": {"open_id": m.get("open_id", "")},
        })

    content = json.dumps({"text": text})

    return {
        "event": {
            "sender": {
                "sender_id": {
                    "open_id": sender_open_id,
                    "union_id": "on_sender",
                    "user_id": "sender_uid",
                },
                "sender_type": "user",
                "name": sender_name,
                "tenant_key": "tenant",
            },
            "message": {
                "message_id": "msg_001",
                "chat_id": "chat_001",
                "chat_type": chat_type,
                "message_type": "text",
                "content": content,
                "mentions": mention_list,
                "parent_id": parent_id,
                "create_time": "1714000000000",
            },
        }
    }


def _make_channel(tmp_path: Path) -> FeishuChannel:
    """Create a minimal FeishuChannel for testing (no real WS connection)."""
    framework = MagicMock()
    framework.workspace = tmp_path
    channel = FeishuChannel(on_receive=AsyncMock(), framework=framework)
    return channel


# ---------------------------------------------------------------------------
# Phase 4: Structured Actor Context Tests
# ---------------------------------------------------------------------------


class TestParseEventStructuredSender:
    """Verify _parse_event extracts structured sender data."""

    def test_sender_fields_extracted(self):
        raw = _make_raw_event(sender_open_id="ou_alice", sender_name="Alice")
        msg = _parse_event(raw)
        assert msg is not None
        assert msg.sender_open_id == "ou_alice"
        assert msg.sender_name == "Alice"
        assert msg.sender_union_id == "on_sender"
        assert msg.sender_user_id == "sender_uid"

    def test_mentions_extracted_as_structured_objects(self):
        raw = _make_raw_event(
            mentions=[
                {"open_id": "ou_bob", "name": "Bob", "key": "@_user1"},
                {"open_id": "ou_charlie", "name": "Charlie", "key": "@_user2"},
            ]
        )
        msg = _parse_event(raw)
        assert msg is not None
        assert len(msg.mentions) == 2
        assert msg.mentions[0].open_id == "ou_bob"
        assert msg.mentions[0].name == "Bob"
        assert msg.mentions[1].open_id == "ou_charlie"
        assert msg.mentions[1].name == "Charlie"

    def test_mentions_in_text_are_replaced_with_display_names(self):
        raw = _make_raw_event(
            text="@_user1 你好",
            mentions=[{"open_id": "ou_bob", "name": "Bob", "key": "@_user1"}],
        )
        msg = _parse_event(raw)
        assert msg is not None
        assert "@Bob" in msg.text

    def test_no_mentions_results_in_empty_tuple(self):
        raw = _make_raw_event(mentions=[])
        msg = _parse_event(raw)
        assert msg is not None
        assert msg.mentions == ()


@pytest.mark.asyncio
class TestStructuredPayloadInChannelMessage:
    """Verify that the payload sent to the model includes structured actor data."""

    async def test_payload_includes_sender_as_structured_object(self, tmp_path: Path):
        """Payload should include sender as {open_id, name, user_id, union_id}."""
        channel = _make_channel(tmp_path)
        raw = _make_raw_event(sender_open_id="ou_alice", sender_name="Alice")
        msg = _parse_event(raw)
        assert msg is not None

        channel_msg = await channel._build_channel_message(
            msg, msg.text.strip(), "ou_alice", "feishu:chat_001"
        )
        payload = json.loads(channel_msg.content)

        assert "sender" in payload
        assert payload["sender"]["open_id"] == "ou_alice"
        assert payload["sender"]["name"] == "Alice"
        assert payload["sender"]["user_id"] == "sender_uid"
        assert payload["sender"]["union_id"] == "on_sender"

    async def test_payload_includes_mentions_as_structured_list(self, tmp_path: Path):
        """Payload should include mentions[] with {open_id, name} per mention."""
        channel = _make_channel(tmp_path)
        raw = _make_raw_event(
            mentions=[
                {"open_id": "ou_bob", "name": "Bob", "key": "@_user1"},
                {"open_id": "ou_charlie", "name": "Charlie", "key": "@_user2"},
            ]
        )
        msg = _parse_event(raw)
        assert msg is not None

        channel_msg = await channel._build_channel_message(
            msg, msg.text.strip(), "ou_sender", "feishu:chat_001"
        )
        payload = json.loads(channel_msg.content)

        assert "mentions" in payload
        assert len(payload["mentions"]) == 2
        assert payload["mentions"][0] == {"open_id": "ou_bob", "name": "Bob"}
        assert payload["mentions"][1] == {"open_id": "ou_charlie", "name": "Charlie"}

    async def test_reply_target_defaults_to_sender(self, tmp_path: Path):
        """When no parent_id, reply_target should default to current sender as structured object."""
        channel = _make_channel(tmp_path)
        raw = _make_raw_event(sender_open_id="ou_alice", sender_name="Alice")
        msg = _parse_event(raw)
        assert msg is not None

        channel_msg = await channel._build_channel_message(
            msg, msg.text.strip(), "ou_alice", "feishu:chat_001"
        )
        payload = json.loads(channel_msg.content)

        assert "reply_target" in payload
        assert payload["reply_target"] == {
            "kind": "sender",
            "open_id": "ou_alice",
            "name": "Alice",
        }

    async def test_reply_target_still_sender_when_quoting(self, tmp_path: Path):
        """Even when quoting, reply_target defaults to current sender."""
        channel = _make_channel(tmp_path)
        raw = _make_raw_event(
            sender_open_id="ou_alice",
            sender_name="Alice",
            parent_id="msg_parent",
        )
        msg = _parse_event(raw)
        assert msg is not None

        channel_msg = await channel._build_channel_message(
            msg, msg.text.strip(), "ou_alice", "feishu:chat_001"
        )
        payload = json.loads(channel_msg.content)

        # reply_target is the current sender, not the quoted message author
        assert payload["reply_target"]["kind"] == "sender"
        assert payload["reply_target"]["open_id"] == "ou_alice"
        assert payload["reply_target"]["name"] == "Alice"

    async def test_payload_preserves_flat_sender_fields(self, tmp_path: Path):
        """Flat sender_id and sender_name should still be present for backward compat."""
        channel = _make_channel(tmp_path)
        raw = _make_raw_event(sender_open_id="ou_alice", sender_name="Alice")
        msg = _parse_event(raw)

        channel_msg = await channel._build_channel_message(
            msg, msg.text.strip(), "ou_alice", "feishu:chat_001"
        )
        payload = json.loads(channel_msg.content)

        # Backward compat: flat fields still present
        assert payload["sender_id"] == "ou_alice"
        assert payload["sender_name"] == "Alice"

    async def test_empty_mentions_produces_empty_list(self, tmp_path: Path):
        """No mentions should produce an empty list in payload."""
        channel = _make_channel(tmp_path)
        raw = _make_raw_event(mentions=[])
        msg = _parse_event(raw)

        channel_msg = await channel._build_channel_message(
            msg, msg.text.strip(), "ou_sender", "feishu:chat_001"
        )
        payload = json.loads(channel_msg.content)

        assert payload["mentions"] == []

    async def test_channel_message_context_includes_reply_target(self, tmp_path: Path):
        channel = _make_channel(tmp_path)
        raw = _make_raw_event()
        msg = _parse_event(raw)
        assert msg is not None

        channel_msg = await channel._build_channel_message(
            msg, msg.text.strip(), "ou_sender", "feishu:chat_001"
        )

        assert channel_msg.context["reply_to_message_id"] == "msg_001"


@pytest.mark.asyncio
class TestFeishuOutboundReplyRouting:
    async def test_send_replies_to_explicit_message_id(self, tmp_path: Path):
        channel = _make_channel(tmp_path)
        channel._api_client = object()
        channel._message_start_time["msg_001"] = 100.0
        reply_calls: list[tuple[str, str, str]] = []
        create_calls: list[tuple[str, str, str]] = []

        channel._reply_message = lambda mid, msg_type, content: reply_calls.append((mid, msg_type, content))
        channel._create_message = lambda cid, msg_type, content: create_calls.append((cid, msg_type, content))

        with patch("bub_im_bridge.feishu.channel.time.time", return_value=103.0):
            await channel.send(
                ChannelMessage(
                    session_id="feishu:chat_001",
                    channel="feishu",
                    chat_id="chat_001",
                    content="hello",
                    context={"reply_to_message_id": "msg_001"},
                )
            )

        assert [call[0] for call in reply_calls] == ["msg_001"]
        assert create_calls == []
        assert "msg_001" not in channel._message_start_time

    async def test_send_creates_new_message_without_reply_target(self, tmp_path: Path):
        channel = _make_channel(tmp_path)
        channel._api_client = object()
        reply_calls: list[tuple[str, str, str]] = []
        create_calls: list[tuple[str, str, str]] = []

        channel._reply_message = lambda mid, msg_type, content: reply_calls.append((mid, msg_type, content))
        channel._create_message = lambda cid, msg_type, content: create_calls.append((cid, msg_type, content))

        await channel.send(
            ChannelMessage(
                session_id="feishu:chat_001",
                channel="feishu",
                chat_id="chat_001",
                content="hello",
            )
        )

        assert reply_calls == []
        assert [call[0] for call in create_calls] == ["chat_001"]


# ---------------------------------------------------------------------------
# Phase 5: Prompt Rules Tests
# ---------------------------------------------------------------------------


class TestPromptRulesForGroupChat:
    """Verify prompt rules prevent wrong-person replies and name hallucinations."""

    def test_prompt_includes_sender_name_when_available(self, tmp_path: Path):
        """When sender profile exists with a name, prompt should include it."""
        store = ProfileStore(tmp_path / "profiles")
        store.load()

        profile = store.upsert(
            platform="feishu",
            id_field="open_id",
            id_value="ou_alice",
            name="Alice Wang",
            department="Engineering",
        )

        hint = build_user_context_hint(profile)
        assert "Alice Wang" in hint

    def test_prompt_includes_reply_rule_default_to_sender(self, tmp_path: Path):
        """Prompt should include rule: default reply to sender."""
        store = ProfileStore(tmp_path / "profiles")
        store.load()

        profile = store.upsert(
            platform="feishu",
            id_field="open_id",
            id_value="ou_alice",
            name="Alice",
        )

        hint = build_user_context_hint(profile)
        assert "默认回复" in hint or "默认" in hint
        assert "发送者" in hint

    def test_prompt_includes_rule_not_switch_addressee(self, tmp_path: Path):
        """Prompt should include rule: don't switch addressee because of multiple names."""
        store = ProfileStore(tmp_path / "profiles")
        store.load()

        profile = store.upsert(
            platform="feishu",
            id_field="open_id",
            id_value="ou_alice",
            name="Alice",
        )

        hint = build_user_context_hint(profile)
        assert "不要擅自切换" in hint or "切换" in hint

    def test_prompt_includes_rule_not_say_unknown_name(self, tmp_path: Path):
        """Prompt should include rule: don't say 'I don't know your name' when name is available."""
        store = ProfileStore(tmp_path / "profiles")
        store.load()

        profile = store.upsert(
            platform="feishu",
            id_field="open_id",
            id_value="ou_alice",
            name="Alice Wang",
        )

        hint = build_user_context_hint(profile)
        assert "不知道" in hint or "你的名字" in hint or "是谁" in hint

    def test_prompt_includes_rule_not_emit_at_name(self, tmp_path: Path):
        """Prompt should include rule: don't output @name unless needed."""
        store = ProfileStore(tmp_path / "profiles")
        store.load()

        profile = store.upsert(
            platform="feishu",
            id_field="open_id",
            id_value="ou_alice",
            name="Alice",
        )

        hint = build_user_context_hint(profile)
        assert "@" in hint  # mentions the @ symbol in the rule

    def test_prompt_rules_present_even_without_profile(self):
        """Reply rules should be present even when no sender profile exists."""
        hint = build_user_context_hint(None)
        assert "回复规则" in hint or "默认回复" in hint

    def test_prompt_includes_rule_direct_reply_without_skill_or_tool(self):
        """Ordinary chat replies should not require loading skills or tools."""
        hint = build_user_context_hint(None)
        assert "普通聊天回复直接回答即可" in hint
        assert "不要为了回复当前这条 Feishu 消息去调用任何 skill 或 tool" in hint

    def test_prompt_includes_rule_not_to_use_feishu_or_opencli_for_sending_reply(self):
        """Sending the current reply is handled by the channel, not by extra send skills."""
        hint = build_user_context_hint(None)
        assert "Feishu plugin / channel" in hint
        assert "Feishu、opencli 等发送类 skill" in hint


# ---------------------------------------------------------------------------
# Integration: parse -> payload -> prompt chain
# ---------------------------------------------------------------------------


class TestActorContextIntegration:
    """End-to-end tests for the parse -> payload -> prompt chain."""

    def test_parse_preserves_multiple_mentions_ordering(self):
        """Mentions should preserve their order from the raw event."""
        raw = _make_raw_event(
            mentions=[
                {"open_id": "ou_first", "name": "First", "key": "@_u1"},
                {"open_id": "ou_second", "name": "Second", "key": "@_u2"},
                {"open_id": "ou_third", "name": "Third", "key": "@_u3"},
            ]
        )
        msg = _parse_event(raw)
        assert msg is not None
        assert len(msg.mentions) == 3
        assert msg.mentions[0].name == "First"
        assert msg.mentions[1].name == "Second"
        assert msg.mentions[2].name == "Third"

    def test_parse_handles_missing_sender_gracefully(self):
        """Event with missing sender should return None."""
        raw = {"event": {"message": {"message_id": "m1", "chat_id": "c1"}}}
        msg = _parse_event(raw)
        assert msg is None

    def test_parse_handles_empty_mentions(self):
        """Event with no mentions should have empty tuple."""
        raw = _make_raw_event(mentions=None)
        msg = _parse_event(raw)
        assert msg is not None
        assert msg.mentions == ()

    def test_sender_display_fallback_to_open_id(self):
        """When sender_name is empty, sender_display should fallback to open_id."""
        msg = FeishuInboundMessage(
            message_id="m1",
            chat_id="c1",
            chat_type="group",
            message_type="text",
            text="hello",
            sender_open_id="ou_fallback",
            sender_name="",
        )
        assert msg.sender_display == "ou_fallback"

    def test_sender_display_prefers_name(self):
        """When sender_name is available, sender_display should use it."""
        msg = FeishuInboundMessage(
            message_id="m1",
            chat_id="c1",
            chat_type="group",
            message_type="text",
            text="hello",
            sender_open_id="ou_alice",
            sender_name="Alice",
        )
        assert msg.sender_display == "Alice"


class TestGroupCommandActivation:
    """Group-chat activation rules around slash/comma commands."""

    def test_group_slash_command_requires_admin(self, tmp_path: Path):
        channel = _make_channel(tmp_path)
        msg = FeishuInboundMessage(
            message_id="m1",
            chat_id="c1",
            chat_type="group",
            message_type="text",
            text="/openapi/resource/mysql/list",
            sender_open_id="ou_non_admin",
            sender_name="User",
        )

        with patch("bub_im_bridge.feishu.channel.is_admin_sender", return_value=False):
            is_active, reason = channel._check_active(msg)

        assert is_active is False
        assert reason == "group_command_not_admin"

    def test_group_slash_command_allows_admin(self, tmp_path: Path):
        channel = _make_channel(tmp_path)
        msg = FeishuInboundMessage(
            message_id="m1",
            chat_id="c1",
            chat_type="group",
            message_type="text",
            text="/openapi/resource/mysql/list",
            sender_open_id="ou_admin",
            sender_name="Admin",
        )

        with patch("bub_im_bridge.feishu.channel.is_admin_sender", return_value=True):
            is_active, reason = channel._check_active(msg)

        assert is_active is True
        assert reason == "group_command_admin"

    def test_group_bot_mention_still_works_for_non_admin(self, tmp_path: Path):
        channel = _make_channel(tmp_path)
        channel._bot_open_id = "ou_bot"
        msg = FeishuInboundMessage(
            message_id="m1",
            chat_id="c1",
            chat_type="group",
            message_type="text",
            text="@Philip 帮我看看",
            sender_open_id="ou_non_admin",
            sender_name="User",
            mentions=(FeishuMention(open_id="ou_bot", name="Philip", key="@_u1"),),
        )

        with patch("bub_im_bridge.feishu.channel.is_admin_sender", return_value=False):
            is_active, reason = channel._check_active(msg)

        assert is_active is True
        assert reason == "bot_mentioned"
