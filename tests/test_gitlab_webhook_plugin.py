from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from bub.channels.message import ChannelMessage
from bub_im_bridge.gitlab.channel import (
    GitLabWebhookChannel,
    GitLabWebhookConfig,
    build_review_message,
    normalize_gitlab_merge_request_event,
    should_trigger_review,
)
from bub_im_bridge.gitlab.plugin import GitLabWebhookPlugin


@pytest.fixture
def config() -> GitLabWebhookConfig:
    return GitLabWebhookConfig(
        enabled=True,
        host="127.0.0.1",
        port=8765,
        path="/gitlab/webhook",
        secret_token="secret",
        project_ids=frozenset({"123"}),
        reviewer_name="Meta42",
        notify_channel="feishu",
        notify_chat_id="oc_123",
        dedupe_ttl_seconds=600,
    )


@pytest.fixture
def merge_request_payload() -> dict:
    return {
        "object_kind": "merge_request",
        "project": {
            "id": 123,
            "name": "demo",
            "path_with_namespace": "group/demo",
            "web_url": "https://gitlab.example/group/demo",
        },
        "user": {"name": "Alice", "username": "alice"},
        "object_attributes": {
            "iid": 7,
            "action": "update",
            "title": "Add webhook support",
            "source_branch": "feat/webhook",
            "target_branch": "main",
            "url": "https://gitlab.example/group/demo/-/merge_requests/7",
            "state": "opened",
        },
        "reviewers": [{"name": "Meta42", "username": "meta42"}],
        "assignees": [],
    }


def test_normalize_merge_request_review_event(merge_request_payload: dict) -> None:
    event = normalize_gitlab_merge_request_event(
        merge_request_payload, event_name="Merge Request Hook"
    )

    assert event["event_type"] == "merge_request"
    assert event["project_id"] == "123"
    assert event["action"] == "update"
    assert event["merge_request"]["iid"] == "7"
    assert event["merge_request"]["target_branch"] == "main"
    assert event["reviewers"] == [{"name": "Meta42", "username": "meta42"}]


def test_triggers_only_for_configured_reviewer(
    merge_request_payload: dict, config: GitLabWebhookConfig
) -> None:
    event = normalize_gitlab_merge_request_event(
        merge_request_payload, event_name="Merge Request Hook"
    )

    assert should_trigger_review(event, config) is True

    other_config = GitLabWebhookConfig(
        **{**config.__dict__, "reviewer_name": "Someone Else"}
    )
    assert should_trigger_review(event, other_config) is False


def test_triggers_for_configured_assignee(
    merge_request_payload: dict, config: GitLabWebhookConfig
) -> None:
    merge_request_payload["reviewers"] = []
    merge_request_payload["assignees"] = [{"name": "Meta42", "username": "meta42"}]
    event = normalize_gitlab_merge_request_event(
        merge_request_payload, event_name="Merge Request Hook"
    )

    assert should_trigger_review(event, config) is True


def test_build_review_message_keeps_gitlab_source_and_feishu_sink(
    merge_request_payload: dict, config: GitLabWebhookConfig
) -> None:
    event = normalize_gitlab_merge_request_event(
        merge_request_payload, event_name="Merge Request Hook"
    )

    message = build_review_message(event, config, "event-uuid")
    content = json.loads(message.content)

    assert message.channel == "gitlab"
    assert message.session_id == "gitlab:123:merge_request:7"
    assert message.chat_id == "oc_123"
    assert message.output_channel == "feishu"
    assert message.context["notify_channel"] == "feishu"
    assert message.context["notify_chat_id"] == "oc_123"
    assert "message" not in content
    assert content["gitlab_event"]["project"]["path_with_namespace"] == "group/demo"


def test_plugin_build_prompt_injects_review_prompt(
    merge_request_payload: dict, config: GitLabWebhookConfig
) -> None:
    event = normalize_gitlab_merge_request_event(
        merge_request_payload, event_name="Merge Request Hook"
    )
    message = build_review_message(event, config, "event-uuid")
    plugin = GitLabWebhookPlugin(framework=MagicMock())
    plugin.config = config

    prompt = plugin.build_prompt(message, message.session_id, {})

    assert prompt is not None
    assert prompt.startswith("你被指派为这个 GitLab Merge Request 的 reviewer。")
    assert "GitLab merge request event context" in prompt
    assert "Add webhook support" in prompt


@pytest.mark.asyncio
async def test_handle_webhook_injects_review_request_into_framework(
    merge_request_payload: dict, config: GitLabWebhookConfig
) -> None:
    framework = MagicMock()
    framework.process_inbound = AsyncMock()
    channel = GitLabWebhookChannel(framework=framework)
    channel._config = config

    accepted = await channel.handle_webhook(
        merge_request_payload,
        event_name="Merge Request Hook",
        delivery_id="uuid-1",
        token="secret",
    )

    assert accepted is True
    await asyncio.sleep(0)
    framework.process_inbound.assert_awaited_once()
    message = framework.process_inbound.await_args.args[0]
    assert isinstance(message, ChannelMessage)
    assert message.channel == "gitlab"
    assert message.output_channel == "feishu"


@pytest.mark.asyncio
async def test_handle_webhook_ignores_non_matching_reviewer(
    merge_request_payload: dict, config: GitLabWebhookConfig
) -> None:
    merge_request_payload["reviewers"] = [{"name": "Other", "username": "other"}]
    framework = MagicMock()
    framework.process_inbound = AsyncMock()
    channel = GitLabWebhookChannel(framework=framework)
    channel._config = config

    accepted = await channel.handle_webhook(
        merge_request_payload,
        event_name="Merge Request Hook",
        delivery_id="uuid-1",
        token="secret",
    )

    assert accepted is False
    framework.process_inbound.assert_not_awaited()


@pytest.mark.asyncio
async def test_handle_webhook_deduplicates_delivery_id(
    merge_request_payload: dict, config: GitLabWebhookConfig
) -> None:
    framework = MagicMock()
    framework.process_inbound = AsyncMock()
    channel = GitLabWebhookChannel(framework=framework)
    channel._config = GitLabWebhookConfig(**{**config.__dict__, "secret_token": ""})

    first = await channel.handle_webhook(
        merge_request_payload,
        event_name="Merge Request Hook",
        delivery_id="uuid-1",
    )
    second = await channel.handle_webhook(
        merge_request_payload,
        event_name="Merge Request Hook",
        delivery_id="uuid-1",
    )

    assert first is True
    assert second is False
    await asyncio.sleep(0)
    framework.process_inbound.assert_awaited_once()


@pytest.mark.asyncio
async def test_plugin_dispatch_outbound_sets_notify_sink() -> None:
    plugin = GitLabWebhookPlugin(framework=MagicMock())
    outbound = ChannelMessage(
        session_id="gitlab:123:merge_request:7",
        channel="gitlab",
        chat_id="123",
        content="review result",
        context={"notify_channel": "feishu", "notify_chat_id": "oc_123"},
    )

    result = await plugin.dispatch_outbound(outbound)

    assert result is False
    assert outbound.output_channel == "feishu"
    assert outbound.chat_id == "oc_123"
