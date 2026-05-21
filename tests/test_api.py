"""Unit tests for feishu API helpers."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

from bub_im_bridge.feishu.api import _normalize_text, fetch_chat_history, fetch_message_content, fetch_quoted_message, fetch_user_info


def test_fetch_user_info_returns_dict():
    """fetch_user_info returns a dict with name, department, title, avatar_url."""
    mock_user = MagicMock()
    mock_user.name = "Alice"
    mock_user.department_id = "dept_001"
    mock_user.job_title = "Engineer"
    mock_user.avatar = MagicMock()
    mock_user.avatar.avatar_72 = "https://avatar.url"

    mock_data = MagicMock()
    mock_data.user = mock_user

    mock_resp = MagicMock()
    mock_resp.success.return_value = True
    mock_resp.data = mock_data

    mock_client = MagicMock()
    mock_client.contact.v3.user.get.return_value = mock_resp

    info = fetch_user_info(mock_client, "ou_aaa")
    assert info["name"] == "Alice"
    assert info["job_title"] == "Engineer"
    assert info["avatar_url"] == "https://avatar.url"


def test_fetch_user_info_fallback_on_failure():
    mock_resp = MagicMock()
    mock_resp.success.return_value = False
    mock_resp.code = 41050
    mock_resp.msg = "no permission"

    mock_client = MagicMock()
    mock_client.contact.v3.user.get.return_value = mock_resp

    info = fetch_user_info(mock_client, "ou_bbb")
    assert info["name"] == "ou_bbb"


def test_normalize_text_keeps_interactive_card_as_raw_json_context():
    content = json.dumps(
        {
            "schema": "2.0",
            "body": {
                "elements": [
                    {
                        "tag": "column_set",
                        "columns": [
                            {
                                "elements": [
                                    {
                                        "tag": "div",
                                        "text": {
                                            "tag": "lark_md",
                                            "content": "指标\\n**<font color='blue'>1,200</font>**",
                                        },
                                    }
                                ]
                            }
                        ],
                    }
                ]
            },
        },
        ensure_ascii=False,
    )

    text = _normalize_text("interactive", content)

    assert text.startswith("[interactive message] ")
    assert '"column_set"' in text
    assert '"lark_md"' in text


async def test_fetch_message_content_sets_card_msg_content_type_user_card_content():
    """fetch_message_content always requests original card JSON via
    ``add_query`` on the built request object, bypassing the builder."""

    mock_req = MagicMock()

    mock_builder = MagicMock()
    mock_builder.message_id.return_value = mock_builder
    mock_builder.build.return_value = mock_req

    mock_sender = MagicMock()
    mock_sender.id = "ou_bot123"
    mock_sender.sender_type = "bot"

    mock_item = MagicMock()
    mock_item.msg_type = "interactive"
    mock_item.body.content = json.dumps({"schema": "2.0", "body": {}})
    mock_item.mentions = []
    mock_item.sender = mock_sender

    mock_data = MagicMock()
    mock_data.items = [mock_item]

    mock_resp = MagicMock()
    mock_resp.success.return_value = True
    mock_resp.data = mock_data

    mock_client = MagicMock()
    mock_client.im.v1.message.get.return_value = mock_resp

    with patch(
        "lark_oapi.api.im.v1.GetMessageRequest"
    ) as MockReq:
        MockReq.builder.return_value = mock_builder
        result = await fetch_message_content(mock_client, "om_test")

    assert "[interactive message]" in result
    mock_req.add_query.assert_called_once_with("card_msg_content_type", "user_card_content")


async def test_fetch_quoted_message_returns_sender_info():
    """fetch_quoted_message returns structured dict with content, sender_id,
    sender_type, and msg_type."""

    mock_req = MagicMock()

    mock_builder = MagicMock()
    mock_builder.message_id.return_value = mock_builder
    mock_builder.build.return_value = mock_req

    mock_sender = MagicMock()
    mock_sender.id = "ou_sender456"
    mock_sender.sender_type = "user"

    mock_item = MagicMock()
    mock_item.msg_type = "text"
    mock_item.body.content = json.dumps({"text": "hello world"})
    mock_item.mentions = []
    mock_item.sender = mock_sender

    mock_data = MagicMock()
    mock_data.items = [mock_item]

    mock_resp = MagicMock()
    mock_resp.success.return_value = True
    mock_resp.data = mock_data

    mock_client = MagicMock()
    mock_client.im.v1.message.get.return_value = mock_resp

    with patch(
        "lark_oapi.api.im.v1.GetMessageRequest"
    ) as MockReq:
        MockReq.builder.return_value = mock_builder
        result = await fetch_quoted_message(mock_client, "om_quoted")

    assert result is not None
    assert result["content"] == "hello world"
    assert result["sender_id"] == "ou_sender456"
    assert result["sender_type"] == "user"
    assert result["msg_type"] == "text"


async def test_fetch_quoted_message_bot_sender():
    """fetch_quoted_message correctly identifies bot senders."""

    mock_req = MagicMock()

    mock_builder = MagicMock()
    mock_builder.message_id.return_value = mock_builder
    mock_builder.build.return_value = mock_req

    mock_sender = MagicMock()
    mock_sender.id = "ou_96f06d22c01e55dd7b8706fe2e314508"
    mock_sender.sender_type = "app"

    mock_item = MagicMock()
    mock_item.msg_type = "interactive"
    mock_item.body.content = json.dumps({"schema": "2.0", "body": {"elements": []}})
    mock_item.mentions = []
    mock_item.sender = mock_sender

    mock_data = MagicMock()
    mock_data.items = [mock_item]

    mock_resp = MagicMock()
    mock_resp.success.return_value = True
    mock_resp.data = mock_data

    mock_client = MagicMock()
    mock_client.im.v1.message.get.return_value = mock_resp

    with patch(
        "lark_oapi.api.im.v1.GetMessageRequest"
    ) as MockReq:
        MockReq.builder.return_value = mock_builder
        result = await fetch_quoted_message(mock_client, "om_card")

    assert result is not None
    assert "[interactive message]" in result["content"]
    assert result["sender_id"] == "ou_96f06d22c01e55dd7b8706fe2e314508"
    assert result["sender_type"] == "app"
    assert result["msg_type"] == "interactive"


async def test_fetch_chat_history_sets_card_msg_content_type():
    """fetch_chat_history requests user_card_content so interactive card
    messages return parsed text instead of raw card JSON."""

    mock_req = MagicMock()

    mock_builder = MagicMock()
    mock_builder.container_id_type.return_value = mock_builder
    mock_builder.container_id.return_value = mock_builder
    mock_builder.page_size.return_value = mock_builder
    mock_builder.build.return_value = mock_req

    mock_sender = MagicMock()
    mock_sender.id = "ou_user123"

    mock_item = MagicMock()
    mock_item.msg_type = "text"
    mock_item.body.content = json.dumps({"text": "hello"})
    mock_item.mentions = []
    mock_item.sender = mock_sender
    mock_item.message_id = "om_test"
    mock_item.create_time = "1716288000000"

    mock_data = MagicMock()
    mock_data.items = [mock_item]
    mock_data.has_more = False

    mock_resp = MagicMock()
    mock_resp.success.return_value = True
    mock_resp.data = mock_data

    mock_client = MagicMock()
    mock_client.im.v1.message.list.return_value = mock_resp

    with patch(
        "lark_oapi.api.im.v1.ListMessageRequest"
    ) as MockReq:
        MockReq.builder.return_value = mock_builder
        result = await fetch_chat_history(
            mock_client, "oc_test_chat", resolve_names=False
        )

    assert len(result) == 1
    assert result[0]["content"] == "hello"
    mock_req.add_query.assert_called_once_with(
        "card_msg_content_type", "user_card_content"
    )
