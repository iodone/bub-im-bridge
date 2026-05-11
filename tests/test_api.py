"""Unit tests for feishu API helpers."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

from bub_im_bridge.feishu.api import _normalize_text, fetch_message_content, fetch_user_info


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

    mock_item = MagicMock()
    mock_item.msg_type = "interactive"
    mock_item.body.content = json.dumps({"schema": "2.0", "body": {}})
    mock_item.mentions = []

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
