"""Bub plugin entry point for GitLab webhook events."""

from __future__ import annotations

import json
from typing import Any

from bub.envelope import content_of, field_of
from bub.framework import BubFramework
from bub.hookspecs import hookimpl
from bub.types import MessageHandler, State

from bub_im_bridge.gitlab.channel import (
    GitLabWebhookChannel,
    load_gitlab_webhook_config,
)


GITLAB_REVIEW_PROMPT = """你被指派为这个 GitLab Merge Request 的 reviewer。

请基于 GitLab 事件上下文完成工程化 review，并将结果提交为 MR comment：

1. 用 glab 获取 MR diff 和已有 discussions：`glab api projects/<project_id>/merge_requests/<iid>/changes`、`glab api projects/<project_id>/merge_requests/<iid>/discussions`。
2. 分析 diff，识别安全风险、逻辑缺陷、代码风格问题，给出具体行号和修改建议。
3. 必须使用 `glab mr note` 命令提交 review comment，不要用 `glab api` 或其他方式。命令格式：`glab mr note <iid> -R <path_with_namespace> -m "<review 内容>"`。多行内容直接作为 -m 的字符串值传入（支持换行）；内容太长时先写入临时文件，再用 `glab mr note <iid> -R <path_with_namespace> -m "$(cat /tmp/review.md)"`。
4. review comment 格式：先给结论（LGTM / 需要修改 / 阻塞），再列具体问题（严重程度 + 行号 + 建议），最后给总结。
5. 提交 comment 后，输出简短中文摘要作为飞书群通知内容（包含 MR 链接和 review 结论）。
6. 不要尝试用 feishu CLI 或 lark-cli 发送消息——通知由框架自动完成，你只需输出摘要文本。
7. 全程使用中文输出。不要输出英文思考过程、进度说明或 "Let me..." 之类过渡语。直接给出结果。"""


class GitLabWebhookPlugin:
    """Plugin that provides GitLab webhook event ingestion and review prompts."""

    def __init__(self, framework: BubFramework) -> None:
        self.framework = framework
        self.config = load_gitlab_webhook_config()

    @hookimpl
    def provide_channels(self, message_handler: MessageHandler) -> list[Any]:
        return [GitLabWebhookChannel(framework=self.framework)]

    @hookimpl(tryfirst=True)
    def build_prompt(self, message: Any, session_id: str, state: State) -> str | None:
        if field_of(message, "channel") != "gitlab":
            return None
        content = content_of(message)
        try:
            payload = json.loads(content)
        except json.JSONDecodeError:
            return None
        event = payload.get("gitlab_event")
        if not isinstance(event, dict):
            return None
        return (
            f"{GITLAB_REVIEW_PROMPT}\n\n"
            "GitLab merge request event context:\n"
            f"{json.dumps(event, ensure_ascii=False, indent=2)}"
        )

    @hookimpl(tryfirst=True)
    async def dispatch_outbound(self, message: Any) -> bool:
        if field_of(message, "channel") != "gitlab":
            return False
        context = field_of(message, "context", {})
        if not isinstance(context, dict):
            return False
        notify_channel = context.get("notify_channel")
        notify_chat_id = context.get("notify_chat_id")
        if not notify_channel or not notify_chat_id:
            return False
        if isinstance(message, dict):
            message["output_channel"] = notify_channel
            message["chat_id"] = notify_chat_id
        else:
            message.output_channel = str(notify_channel)
            message.chat_id = str(notify_chat_id)
        return False
