"""Regression coverage for Bub 0.6 plugin loading and tool collection."""

from __future__ import annotations

from unittest.mock import MagicMock

from bub_im_bridge.feishu.plugin import FeishPlugin
from bub_im_bridge.gitlab.plugin import GitLabWebhookPlugin
from bub_im_bridge.tool_stats import install_sink, pop, register
from bub_im_bridge.weixin.plugin import WeixinPlugin


def test_feishu_provides_tools_through_bub_06_hook(monkeypatch):
    monkeypatch.setenv("BUB_FEISHU_ENABLED", "false")
    plugin = FeishPlugin(framework=MagicMock())

    tools = plugin.provide_tools()

    assert {tool.name for tool in tools} == {
        "feishu.history",
        "user.create",
        "user.lookup",
        "user.search",
        "user.update",
    }


def test_all_channel_plugins_import_with_bub_06(monkeypatch):
    monkeypatch.delenv("GITLAB_WEBHOOK_TOKEN", raising=False)
    assert FeishPlugin(framework=MagicMock())
    assert GitLabWebhookPlugin(framework=MagicMock())
    assert WeixinPlugin(framework=MagicMock())


def test_tool_stats_reads_bub_06_core_logger():
    import bub.core.tools  # noqa: F401
    from loguru import logger

    install_sink()
    stats = register("bub-06")
    logger.patch(lambda record: record.update(name="bub.core.tools")).info(
        "tool.call.start name=bash"
    )

    assert stats.snapshot() == (1, {"bash": 1}, 0)
    assert pop("bub-06") is stats
