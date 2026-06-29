"""GitLab review-request webhook channel for Bub."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import threading
import time
from dataclasses import dataclass
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, ClassVar
from urllib.parse import urlparse

from bub.channels.base import Channel
from bub.channels.message import ChannelMessage
from bub.framework import BubFramework
from loguru import logger


@dataclass(frozen=True)
class GitLabWebhookConfig:
    enabled: bool
    host: str
    port: int
    path: str
    secret_token: str
    project_ids: frozenset[str]
    reviewer_name: str
    notify_channel: str
    notify_chat_id: str
    dedupe_ttl_seconds: int


class GitLabWebhookChannel(Channel):
    """Receive GitLab MR reviewer assignment events and inject review tasks."""

    name: ClassVar[str] = "gitlab"

    def __init__(self, *, framework: BubFramework | None = None) -> None:
        self._framework = framework
        self._config = load_gitlab_webhook_config()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._server: ThreadingHTTPServer | None = None
        self._server_thread: threading.Thread | None = None
        self._seen_events: dict[str, float] = {}

    @property
    def enabled(self) -> bool:
        return self._config.enabled

    async def start(self, stop_event: asyncio.Event) -> None:
        if not self._config.enabled:
            logger.info("gitlab_webhook.disabled")
            return
        if self._framework is None:
            raise RuntimeError("gitlab_webhook: framework is required")
        if not self._config.reviewer_name or not self._config.notify_chat_id:
            logger.warning(
                "gitlab_webhook.skip BUB_GITLAB_REVIEWER_NAME or BUB_GITLAB_NOTIFY_CHAT_ID not set"
            )
            return

        self._loop = asyncio.get_running_loop()
        self._server = ThreadingHTTPServer(
            (self._config.host, self._config.port),
            self._make_handler(),
        )
        self._server_thread = threading.Thread(
            target=self._server.serve_forever,
            name="gitlab-webhook-http",
            daemon=True,
        )
        self._server_thread.start()
        logger.info(
            "gitlab_webhook.start host={} port={} path={} reviewer={}",
            self._config.host,
            self._config.port,
            self._config.path,
            self._config.reviewer_name,
        )

    async def stop(self) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
            self._server = None
        if self._server_thread is not None:
            self._server_thread.join(timeout=2)
            self._server_thread = None
        logger.info("gitlab_webhook.stop complete")

    async def handle_webhook(
        self,
        payload: dict[str, Any],
        *,
        event_name: str,
        delivery_id: str | None = None,
        token: str | None = None,
    ) -> bool:
        """Validate, filter, and inject one GitLab review-request event (fire-and-forget)."""
        self._validate_token(token)

        event = normalize_gitlab_merge_request_event(payload, event_name=event_name)
        if not should_trigger_review(event, self._config):
            logger.info(
                "gitlab_webhook.ignored project_id={} event={} action={} reviewer={}",
                event["project_id"],
                event["event_type"],
                event["action"],
                self._config.reviewer_name,
            )
            return False

        event_key = delivery_id or event_fingerprint(event)
        if self._is_duplicate(event_key):
            logger.info("gitlab_webhook.duplicate event_key={}", event_key)
            return False

        if self._framework is None:
            raise RuntimeError("gitlab_webhook: no live framework available")
        message = build_review_message(event, self._config, event_key)
        asyncio.create_task(self._framework.process_inbound(message))
        return True

    def _make_handler(self) -> type[BaseHTTPRequestHandler]:
        channel = self
        config = self._config

        class GitLabWebhookRequestHandler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:  # noqa: N802 - stdlib hook name
                parsed = urlparse(self.path)
                if parsed.path != config.path:
                    self._write_json(HTTPStatus.NOT_FOUND, {"error": "not found"})
                    return

                try:
                    content_length = int(self.headers.get("Content-Length", "0"))
                    body = self.rfile.read(content_length)
                    payload = json.loads(body.decode("utf-8"))
                    event_name = self.headers.get("X-Gitlab-Event", "")
                    delivery_id = self.headers.get("X-Gitlab-Event-UUID")
                    token = self.headers.get("X-Gitlab-Token")
                except Exception:
                    logger.warning("gitlab_webhook.bad_request", exc_info=True)
                    self._write_json(HTTPStatus.BAD_REQUEST, {"error": "bad request"})
                    return

                future = asyncio.run_coroutine_threadsafe(
                    channel.handle_webhook(
                        payload,
                        event_name=event_name,
                        delivery_id=delivery_id,
                        token=token,
                    ),
                    channel._require_loop(),
                )
                try:
                    accepted = future.result(timeout=10)
                except PermissionError:
                    self._write_json(HTTPStatus.FORBIDDEN, {"error": "invalid token"})
                    return
                except TimeoutError:
                    logger.warning("gitlab_webhook.dispatch_timeout")
                    self._write_json(HTTPStatus.ACCEPTED, {"accepted": True})
                    return
                except Exception:
                    logger.exception("gitlab_webhook.dispatch_failed")
                    self._write_json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": "dispatch failed"})
                    return

                self._write_json(HTTPStatus.ACCEPTED, {"accepted": accepted})

            def log_message(self, format: str, *args: object) -> None:
                logger.debug("gitlab_webhook.http {}", format % args if args else format)

            def _write_json(self, status: HTTPStatus, payload: dict[str, Any]) -> None:
                body = json.dumps(payload).encode("utf-8")
                self.send_response(status.value)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        return GitLabWebhookRequestHandler

    def _require_loop(self) -> asyncio.AbstractEventLoop:
        if self._loop is None:
            raise RuntimeError("gitlab_webhook: event loop is not ready")
        return self._loop

    def _validate_token(self, token: str | None) -> None:
        expected = self._config.secret_token
        if expected and token != expected:
            raise PermissionError("invalid GitLab webhook token")

    def _is_duplicate(self, event_key: str) -> bool:
        now = time.time()
        ttl = self._config.dedupe_ttl_seconds
        expired = [key for key, seen_at in self._seen_events.items() if now - seen_at > ttl]
        for key in expired:
            self._seen_events.pop(key, None)
        if event_key in self._seen_events:
            return True
        self._seen_events[event_key] = now
        return False


def load_gitlab_webhook_config() -> GitLabWebhookConfig:
    return GitLabWebhookConfig(
        enabled=not _parse_bool(os.environ.get("BUB_GITLAB_WEBHOOK_DISABLED", "false")),
        host=os.environ.get("BUB_GITLAB_WEBHOOK_HOST", "127.0.0.1"),
        port=int(os.environ.get("BUB_GITLAB_WEBHOOK_PORT", "8765")),
        path=os.environ.get("BUB_GITLAB_WEBHOOK_PATH", "/gitlab/webhook"),
        secret_token=os.environ.get("BUB_GITLAB_WEBHOOK_TOKEN", ""),
        project_ids=frozenset(_string_list(os.environ.get("BUB_GITLAB_PROJECT_IDS", ""))),
        reviewer_name=os.environ.get("BUB_GITLAB_REVIEWER_NAME", "").strip(),
        notify_channel=os.environ.get("BUB_GITLAB_NOTIFY_CHANNEL", "feishu").strip() or "feishu",
        notify_chat_id=os.environ.get("BUB_GITLAB_NOTIFY_CHAT_ID", "").strip(),
        dedupe_ttl_seconds=int(os.environ.get("BUB_GITLAB_WEBHOOK_DEDUPE_TTL", "600")),
    )


def normalize_gitlab_merge_request_event(payload: dict[str, Any], *, event_name: str) -> dict[str, Any]:
    object_attributes = payload.get("object_attributes")
    if not isinstance(object_attributes, dict):
        object_attributes = {}

    project = payload.get("project")
    if not isinstance(project, dict):
        project = {}

    user = payload.get("user")
    if not isinstance(user, dict):
        user = {}

    reviewers = _people(payload.get("reviewers")) or _people(object_attributes.get("reviewers"))
    assignees = _people(payload.get("assignees")) or _people(object_attributes.get("assignees"))

    project_id = str(project.get("id") or payload.get("project_id") or "").strip()
    return {
        "event_type": _event_type(str(payload.get("object_kind") or ""), event_name),
        "event_name": event_name,
        "action": str(object_attributes.get("action") or "").strip(),
        "project_id": project_id,
        "project": {
            "id": project_id,
            "name": project.get("name") or "",
            "path_with_namespace": project.get("path_with_namespace") or "",
            "web_url": project.get("web_url") or "",
        },
        "merge_request": {
            "iid": str(object_attributes.get("iid") or "").strip(),
            "title": object_attributes.get("title") or "",
            "source_branch": object_attributes.get("source_branch") or "",
            "target_branch": object_attributes.get("target_branch") or "",
            "url": object_attributes.get("url") or object_attributes.get("web_url") or "",
            "state": object_attributes.get("state") or "",
        },
        "author": {
            "name": user.get("name") or payload.get("user_name") or "",
            "username": user.get("username") or payload.get("user_username") or "",
        },
        "reviewers": reviewers,
        "assignees": assignees,
        "raw": payload,
    }


def should_trigger_review(event: dict[str, Any], config: GitLabWebhookConfig) -> bool:
    if event["event_type"] != "merge_request":
        return False
    if config.project_ids and event["project_id"] not in config.project_ids:
        return False
    if event["action"] not in {"update", "open", "reopen"}:
        return False
    target = config.reviewer_name.strip().lower()
    if not target:
        return False
    return any(_person_matches(person, target) for person in event["reviewers"] + event["assignees"])


def build_review_message(
    event: dict[str, Any], config: GitLabWebhookConfig, event_key: str
) -> ChannelMessage:
    merge_request = event["merge_request"]
    session_id = f"gitlab:{event['project_id']}:merge_request:{merge_request['iid']}"
    content = json.dumps(
        {
            "gitlab_event": {key: value for key, value in event.items() if key != "raw"},
        },
        ensure_ascii=False,
    )
    return ChannelMessage(
        session_id=session_id,
        channel="gitlab",
        chat_id=config.notify_chat_id,
        content=content,
        is_active=True,
        context={
            "gitlab_event_key": event_key,
            "gitlab_event_type": event["event_type"],
            "gitlab_project_id": event["project_id"],
            "gitlab_action": event["action"],
            "gitlab_reviewer_name": config.reviewer_name,
            "notify_channel": config.notify_channel,
            "notify_chat_id": config.notify_chat_id,
        },
        output_channel=config.notify_channel,
    )


def event_fingerprint(event: dict[str, Any]) -> str:
    stable = json.dumps({key: value for key, value in event.items() if key != "raw"}, sort_keys=True)
    return hashlib.sha256(stable.encode("utf-8")).hexdigest()


def _event_type(object_kind: str, event_name: str) -> str:
    if object_kind:
        return object_kind
    return event_name.lower().replace(" hook", "").replace(" ", "_")


def _people(value: Any) -> list[dict[str, str]]:
    if not isinstance(value, list):
        return []
    people: list[dict[str, str]] = []
    for item in value:
        if not isinstance(item, dict):
            continue
        people.append(
            {
                "name": str(item.get("name") or ""),
                "username": str(item.get("username") or item.get("user_name") or ""),
            }
        )
    return people


def _person_matches(person: dict[str, str], target: str) -> bool:
    return person.get("name", "").strip().lower() == target or person.get("username", "").strip().lower() == target


def _string_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [item.strip() for item in value.split(",") if item.strip()]
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    raise ValueError("expected string or list")


def _parse_bool(value: str) -> bool:
    return value.strip().lower() in {"1", "true", "yes", "on"}
