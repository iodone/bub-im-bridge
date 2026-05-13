"""Tests for Feishu image message MediaItem creation.

Validates that:
- _parse_event extracts image_key from image messages
- _parse_event extracts embedded image_keys from post messages
- _build_channel_message creates MediaItem with data_fetcher for image messages
- text-only messages produce empty media list
- MediaItem.get_url() lazily downloads via Feishu API
"""

from __future__ import annotations

import io
import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from bub.channels.message import MediaItem

from bub_im_bridge.feishu.channel import (
    FeishuChannel,
    FeishuInboundMessage,
    _parse_event,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_raw_image_event(
    *,
    image_key: str = "img_v3_abc123",
    sender_open_id: str = "ou_sender",
    sender_name: str = "张三",
    chat_type: str = "p2p",
) -> dict:
    """Build a minimal raw Feishu event dict for an image message."""
    content = json.dumps({"image_key": image_key})

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
                "message_id": "msg_img_001",
                "chat_id": "chat_001",
                "chat_type": chat_type,
                "message_type": "image",
                "content": content,
                "create_time": "1714000000000",
            },
        }
    }


def _make_raw_post_event(
    *,
    elements: list[list[dict]] | None = None,
    sender_open_id: str = "ou_sender",
    sender_name: str = "张三",
    chat_type: str = "p2p",
) -> dict:
    """Build a minimal raw Feishu event dict for a post (rich text) message."""
    if elements is None:
        elements = [[
            {"tag": "text", "text": "看看这张图 "},
            {"tag": "img", "image_key": "img_v3_post_001"},
        ]]

    content = json.dumps({
        "title": "",
        "content": elements,
    })

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
                "message_id": "msg_post_001",
                "chat_id": "chat_001",
                "chat_type": chat_type,
                "message_type": "post",
                "content": content,
                "create_time": "1714000000000",
            },
        }
    }


def _make_raw_text_event(
    *,
    text: str = "hello",
    sender_open_id: str = "ou_sender",
    sender_name: str = "张三",
    chat_type: str = "p2p",
) -> dict:
    """Build a minimal raw Feishu event dict for a text message."""
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
                "message_id": "msg_text_001",
                "chat_id": "chat_001",
                "chat_type": chat_type,
                "message_type": "text",
                "content": content,
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
# _parse_event: image_key extraction (image messages)
# ---------------------------------------------------------------------------


class TestParseEventImageKey:
    """Verify _parse_event extracts image_key from image messages."""

    def test_image_message_extracts_image_key(self):
        raw = _make_raw_image_event(image_key="img_v3_test_key")
        msg = _parse_event(raw)
        assert msg is not None
        assert msg.image_keys == ["img_v3_test_key"]
        assert msg.message_type == "image"

    def test_text_message_has_no_image_keys(self):
        raw = _make_raw_text_event()
        msg = _parse_event(raw)
        assert msg is not None
        assert msg.image_keys == []

    def test_image_message_with_malformed_content_has_no_image_keys(self):
        """Malformed JSON content should not crash, just leave image_keys empty."""
        raw = _make_raw_image_event()
        raw["event"]["message"]["content"] = "not-json"
        msg = _parse_event(raw)
        assert msg is not None
        assert msg.image_keys == []

    def test_image_message_with_empty_content_has_no_image_keys(self):
        raw = _make_raw_image_event()
        raw["event"]["message"]["content"] = ""
        msg = _parse_event(raw)
        assert msg is not None
        assert msg.image_keys == []


# ---------------------------------------------------------------------------
# _parse_event: image_key extraction (post messages)
# ---------------------------------------------------------------------------


class TestParseEventPostImageKey:
    """Verify _parse_event extracts embedded image_keys from post messages."""

    def test_post_message_extracts_single_image(self):
        raw = _make_raw_post_event(elements=[[
            {"tag": "text", "text": "看看 "},
            {"tag": "img", "image_key": "img_v3_post_single"},
        ]])
        msg = _parse_event(raw)
        assert msg is not None
        assert msg.image_keys == ["img_v3_post_single"]
        assert msg.message_type == "post"

    def test_post_message_extracts_multiple_images(self):
        raw = _make_raw_post_event(elements=[
            [
                {"tag": "text", "text": "第一张 "},
                {"tag": "img", "image_key": "img_v3_post_001"},
            ],
            [
                {"tag": "text", "text": "第二张 "},
                {"tag": "img", "image_key": "img_v3_post_002"},
            ],
        ])
        msg = _parse_event(raw)
        assert msg is not None
        assert msg.image_keys == ["img_v3_post_001", "img_v3_post_002"]

    def test_post_message_with_no_images_has_empty_keys(self):
        raw = _make_raw_post_event(elements=[[
            {"tag": "text", "text": "纯文字，没有图片"},
        ]])
        msg = _parse_event(raw)
        assert msg is not None
        assert msg.image_keys == []

    def test_post_message_with_mixed_elements(self):
        """Post with text, at, and img tags in a single paragraph."""
        raw = _make_raw_post_event(elements=[[
            {"tag": "text", "text": "分析 "},
            {"tag": "at", "user_id": "ou_123"},
            {"tag": "text", "text": " 这张截图 "},
            {"tag": "img", "image_key": "img_v3_mixed"},
        ]])
        msg = _parse_event(raw)
        assert msg is not None
        assert msg.image_keys == ["img_v3_mixed"]

    def test_post_message_with_malformed_content_is_safe(self):
        raw = _make_raw_post_event()
        raw["event"]["message"]["content"] = "not-json"
        msg = _parse_event(raw)
        assert msg is not None
        assert msg.image_keys == []


# ---------------------------------------------------------------------------
# _build_channel_message: MediaItem creation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
class TestBuildChannelMessageMedia:
    """Verify _build_channel_message creates MediaItem for image messages."""

    async def test_image_message_produces_media_item(self, tmp_path: Path):
        """Image messages should produce a MediaItem with data_fetcher."""
        channel = _make_channel(tmp_path)
        channel._api_client = MagicMock()

        raw = _make_raw_image_event(image_key="img_v3_xyz")
        msg = _parse_event(raw)
        assert msg is not None

        channel_msg = await channel._build_channel_message(
            msg, msg.text, "ou_sender", "feishu:chat_001"
        )

        assert len(channel_msg.media) == 1
        item = channel_msg.media[0]
        assert item.type == "image"
        assert item.mime_type == "image/jpeg"
        assert item.filename == "img_v3_xyz.jpg"
        assert item.url is None
        assert item.data_fetcher is not None

    async def test_post_message_produces_media_items(self, tmp_path: Path):
        """Post messages with embedded images should produce MediaItems."""
        channel = _make_channel(tmp_path)
        channel._api_client = MagicMock()

        raw = _make_raw_post_event(elements=[
            [
                {"tag": "text", "text": "第一张 "},
                {"tag": "img", "image_key": "img_v3_aaa"},
            ],
            [
                {"tag": "img", "image_key": "img_v3_bbb"},
            ],
        ])
        msg = _parse_event(raw)
        assert msg is not None

        channel_msg = await channel._build_channel_message(
            msg, msg.text, "ou_sender", "feishu:chat_001"
        )

        assert len(channel_msg.media) == 2
        assert channel_msg.media[0].filename == "img_v3_aaa.jpg"
        assert channel_msg.media[1].filename == "img_v3_bbb.jpg"
        for item in channel_msg.media:
            assert item.type == "image"
            assert item.data_fetcher is not None

    async def test_text_message_has_empty_media(self, tmp_path: Path):
        """Text-only messages should have an empty media list."""
        channel = _make_channel(tmp_path)

        raw = _make_raw_text_event()
        msg = _parse_event(raw)
        assert msg is not None

        channel_msg = await channel._build_channel_message(
            msg, msg.text, "ou_sender", "feishu:chat_001"
        )

        assert channel_msg.media == []

    async def test_image_message_without_api_client_has_empty_media(
        self, tmp_path: Path
    ):
        """When API client is None, image message should have empty media (no crash)."""
        channel = _make_channel(tmp_path)
        channel._api_client = None

        raw = _make_raw_image_event(image_key="img_v3_no_client")
        msg = _parse_event(raw)
        assert msg is not None

        channel_msg = await channel._build_channel_message(
            msg, msg.text, "ou_sender", "feishu:chat_001"
        )

        assert channel_msg.media == []


# ---------------------------------------------------------------------------
# MediaItem.get_url() lazy download
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
class TestMediaItemGetUrl:
    """Verify MediaItem.get_url() lazily downloads via Feishu API."""

    async def test_get_url_calls_data_fetcher_and_returns_data_uri(self, tmp_path: Path):
        """MediaItem.get_url() should call data_fetcher and return a data: URI."""
        channel = _make_channel(tmp_path)

        mock_client = MagicMock()
        channel._api_client = mock_client

        fake_image_bytes = b"\xff\xd8\xff\xe0" + b"\x00" * 100  # fake JPEG bytes
        mock_file = io.BytesIO(fake_image_bytes)

        mock_response = MagicMock()
        mock_response.success.return_value = True
        mock_response.file = mock_file
        mock_response.file_name = "test.jpg"

        mock_client.im.v1.image.aget = AsyncMock(return_value=mock_response)

        raw = _make_raw_image_event(image_key="img_v3_fetch_test")
        msg = _parse_event(raw)
        assert msg is not None

        channel_msg = await channel._build_channel_message(
            msg, msg.text, "ou_sender", "feishu:chat_001"
        )

        assert len(channel_msg.media) == 1
        item = channel_msg.media[0]

        url = await item.get_url()

        assert url is not None
        assert url.startswith("data:image/jpeg;base64,")
        assert item.mime_type == "image/jpeg"
        mock_client.im.v1.image.aget.assert_called_once()

    async def test_png_response_produces_correct_data_uri(self, tmp_path: Path):
        """Regression: a PNG response must NOT produce data:image/jpeg prefix."""
        channel = _make_channel(tmp_path)

        mock_client = MagicMock()
        channel._api_client = mock_client

        png_header = b"\x89PNG\r\n\x1a\n" + b"\x00" * 50
        mock_file = io.BytesIO(png_header)

        mock_response = MagicMock()
        mock_response.success.return_value = True
        mock_response.file = mock_file
        mock_response.file_name = "screenshot.png"

        mock_client.im.v1.image.aget = AsyncMock(return_value=mock_response)

        raw = _make_raw_image_event(image_key="img_v3_png")
        msg = _parse_event(raw)
        assert msg is not None

        channel_msg = await channel._build_channel_message(
            msg, msg.text, "ou_sender", "feishu:chat_001"
        )

        item = channel_msg.media[0]
        url = await item.get_url()

        assert url.startswith("data:image/png;base64,")
        assert item.mime_type == "image/png"

    async def test_webp_response_produces_correct_data_uri(self, tmp_path: Path):
        """WebP response should produce data:image/webp prefix."""
        channel = _make_channel(tmp_path)

        mock_client = MagicMock()
        channel._api_client = mock_client

        mock_file = io.BytesIO(b"RIFF\x00\x00\x00\x00WEBP" + b"\x00" * 20)

        mock_response = MagicMock()
        mock_response.success.return_value = True
        mock_response.file = mock_file
        mock_response.file_name = "photo.webp"

        mock_client.im.v1.image.aget = AsyncMock(return_value=mock_response)

        raw = _make_raw_image_event(image_key="img_v3_webp")
        msg = _parse_event(raw)
        assert msg is not None

        channel_msg = await channel._build_channel_message(
            msg, msg.text, "ou_sender", "feishu:chat_001"
        )

        item = channel_msg.media[0]
        url = await item.get_url()

        assert url.startswith("data:image/webp;base64,")
        assert item.mime_type == "image/webp"

    async def test_unknown_extension_falls_back_to_jpeg(self, tmp_path: Path):
        """Unknown extension should fall back to image/jpeg."""
        channel = _make_channel(tmp_path)

        mock_client = MagicMock()
        channel._api_client = mock_client

        mock_file = io.BytesIO(b"\x00" * 10)

        mock_response = MagicMock()
        mock_response.success.return_value = True
        mock_response.file = mock_file
        mock_response.file_name = "image.xyz"

        mock_client.im.v1.image.aget = AsyncMock(return_value=mock_response)

        raw = _make_raw_image_event(image_key="img_v3_unknown")
        msg = _parse_event(raw)
        assert msg is not None

        channel_msg = await channel._build_channel_message(
            msg, msg.text, "ou_sender", "feishu:chat_001"
        )

        item = channel_msg.media[0]
        url = await item.get_url()

        assert url.startswith("data:image/jpeg;base64,")

    async def test_get_url_propagates_download_error(self, tmp_path: Path):
        """When download fails, get_url() should propagate the error."""
        channel = _make_channel(tmp_path)

        mock_client = MagicMock()
        channel._api_client = mock_client

        mock_response = MagicMock()
        mock_response.success.return_value = False
        mock_response.code = 230001
        mock_response.msg = "image not found"
        mock_response.get_log_id.return_value = "log_abc"

        mock_client.im.v1.image.aget = AsyncMock(return_value=mock_response)

        raw = _make_raw_image_event(image_key="img_v3_bad")
        msg = _parse_event(raw)
        assert msg is not None

        channel_msg = await channel._build_channel_message(
            msg, msg.text, "ou_sender", "feishu:chat_001"
        )

        item = channel_msg.media[0]

        with pytest.raises(RuntimeError, match="image not found"):
            await item.get_url()
