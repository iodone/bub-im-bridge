"""Tests for Feishu image message MediaItem creation.

Validates that:
- _parse_event extracts image_key from image messages
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
# _parse_event: image_key extraction
# ---------------------------------------------------------------------------


class TestParseEventImageKey:
    """Verify _parse_event extracts image_key from image messages."""

    def test_image_message_extracts_image_key(self):
        raw = _make_raw_image_event(image_key="img_v3_test_key")
        msg = _parse_event(raw)
        assert msg is not None
        assert msg.image_key == "img_v3_test_key"
        assert msg.message_type == "image"

    def test_text_message_has_no_image_key(self):
        raw = _make_raw_text_event()
        msg = _parse_event(raw)
        assert msg is not None
        assert msg.image_key is None

    def test_image_message_with_malformed_content_has_no_image_key(self):
        """Malformed JSON content should not crash, just leave image_key as None."""
        raw = _make_raw_image_event()
        raw["event"]["message"]["content"] = "not-json"
        msg = _parse_event(raw)
        assert msg is not None
        assert msg.image_key is None

    def test_image_message_with_empty_content_has_no_image_key(self):
        raw = _make_raw_image_event()
        raw["event"]["message"]["content"] = ""
        msg = _parse_event(raw)
        assert msg is not None
        assert msg.image_key is None


# ---------------------------------------------------------------------------
# _build_channel_message: MediaItem creation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
class TestBuildChannelMessageMedia:
    """Verify _build_channel_message creates MediaItem for image messages."""

    async def test_image_message_produces_media_item(self, tmp_path: Path):
        """Image messages should produce a MediaItem with data_fetcher."""
        channel = _make_channel(tmp_path)
        channel._api_client = MagicMock()  # simulate initialized client

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
        assert item.url is None  # lazy download, no direct URL
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

        # Create a mock API client
        mock_client = MagicMock()
        channel._api_client = mock_client

        # Mock the image download response
        fake_image_bytes = b"\xff\xd8\xff\xe0" + b"\x00" * 100  # fake JPEG header
        mock_file = io.BytesIO(fake_image_bytes)

        mock_response = MagicMock()
        mock_response.success.return_value = True
        mock_response.file = mock_file
        mock_response.file_name = "test.jpg"

        mock_client.im.v1.image.aget = AsyncMock(return_value=mock_response)

        # Build message with image
        raw = _make_raw_image_event(image_key="img_v3_fetch_test")
        msg = _parse_event(raw)
        assert msg is not None

        channel_msg = await channel._build_channel_message(
            msg, msg.text, "ou_sender", "feishu:chat_001"
        )

        assert len(channel_msg.media) == 1
        item = channel_msg.media[0]

        # Call get_url — this should trigger the lazy download
        url = await item.get_url()

        assert url is not None
        assert url.startswith("data:image/jpeg;base64,")
        mock_client.im.v1.image.aget.assert_called_once()

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
