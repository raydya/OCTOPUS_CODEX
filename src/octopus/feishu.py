"""Feishu transport, isolated from the user's lark-cli configuration.

The official SDK's blocking WebSocket client owns a module-global event loop and
has no public close method. It therefore runs in a spawned, terminable process.
The parent only acknowledges a delivery after the supplied inbox handler returns;
those handlers MUST durably enqueue work, never await a complete Codex turn.
"""
from __future__ import annotations

import asyncio
import json
import logging
import multiprocessing
import re
import time
import uuid
from dataclasses import asdict
from typing import Any, Callable

from .contracts import ActionHandler, CardAction, IncomingMessage, MessageHandler, View
from .images import MAX_IMAGE_BYTES, MAX_IMAGES, ImageInputError

CARD_BUDGET = 28_000
DELIVERY_TIMEOUT = 2.0
ENQUEUE_TIMEOUT = 1.7
HEARTBEAT_INTERVAL = 5.0
HEARTBEAT_TIMEOUT = 30.0
CONNECTION_TIMEOUT = 120.0
TRUNCATION = "\n\n…内容较长，已截断显示。"
# Cleanup retries use IDs returned by this app's successful create call. Feishu
# reports these codes when that reaction, its message, or its chat is gone:
# https://open.feishu.cn/document/server-docs/im-v1/message-reaction/delete
REACTION_GONE_CODES = frozenset({231011, 231003, 230110, 231005, 232009})
IMAGE_DEFAULT_PROMPT = "请结合当前对话，分析这张图片中的内容并回答相关业务或代码问题。"
IMAGE_INVALID = "无法读取这条消息中的图片，请重新发送图片。"
IMAGE_LIMIT = f"每条消息最多支持 {MAX_IMAGES} 张不同图片，请分开发送。"
POST_UNSUPPORTED = "这条图文消息包含暂不支持的附件，请改用文字和图片发送。"
IMAGE_DOWNLOAD_FAILED = "图片下载失败，请稍后重新发送图片；如果仍然失败，请联系管理员。"
IMAGE_TOO_LARGE = "图片超过 5 MiB，请压缩后重新发送。"
IMAGE_DOWNLOAD_TIMEOUT = 45.0


def _valid_image_key(value: Any) -> bool:
    return isinstance(value, str) and re.fullmatch(r"img_[A-Za-z0-9_-]{1,252}", value) is not None


def _valid_image_message_id(value: Any) -> bool:
    return isinstance(value, str) and re.fullmatch(r"om_[A-Za-z0-9_-]{1,253}", value) is not None


def _object(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _string(value: Any) -> str:
    return value if isinstance(value, str) else ""


def _parse_post(content: dict[str, Any]) -> tuple[str, tuple[str, ...], str]:
    """Preserve paragraph and image order without treating URLs as resources."""
    if "content" not in content:
        languages = [content.get("zh_cn"), content.get("en_us"), *content.values()]
        content = next((item for item in languages if isinstance(item, dict)), {})
    rows = content.get("content")
    if not isinstance(rows, list):
        return "", (), POST_UNSUPPORTED
    lines = [_string(content.get("title"))]
    keys: list[str] = []
    error = ""
    for row in rows:
        if not isinstance(row, list):
            return "", (), POST_UNSUPPORTED
        parts: list[str] = []
        for raw in row:
            item = _object(raw)
            tag = item.get("tag")
            if tag in ("text", "md", "code_block"):
                if not isinstance(item.get("text"), str):
                    error = error or POST_UNSUPPORTED
                    continue
                text = item["text"]
                # Receive-event `content` expands Markdown images into `img`
                # nodes. Do not download anything embedded in arbitrary text.
                if tag == "md" and re.search(r"!\[[^\]\n]*\]\(\s*img_", text):
                    error = error or POST_UNSUPPORTED
                if tag == "code_block":
                    text = "```\n" + text + "\n```"
                parts.append(text)
            elif tag == "a":
                label, href = _string(item.get("text")), _string(item.get("href"))
                parts.append(f"{label} ({href})" if label and href and label != href else label or href)
            elif tag == "img":
                key = item.get("image_key")
                if not _valid_image_key(key):
                    error = error or IMAGE_INVALID
                elif key not in keys and len(keys) >= MAX_IMAGES:
                    error = error or IMAGE_LIMIT
                else:
                    if key not in keys:
                        keys.append(key)
                    parts.append(f"[图片 {keys.index(key) + 1}]")
            elif tag == "at":
                # As with text messages, routing mentions are not part of the prompt.
                continue
            elif tag == "hr":
                parts.append("\n---\n")
            elif tag == "emotion":
                parts.append("[表情 " + _string(item.get("emoji_type")) + "]")
            else:
                # A video's cover image is not the video. Never silently answer
                # a partial message after dropping an unsupported attachment.
                error = error or POST_UNSUPPORTED
        lines.append("".join(parts))
    text = "\n".join(lines).strip()
    if keys and not any(re.sub(r"\[图片 \d+\]", "", line).strip() for line in lines):
        text = "请结合当前对话，分析所附图片中的内容并回答相关业务或代码问题。\n" + text
    return text, tuple(keys), error


def parse_message(value: Any, *, require_group_mention: bool = True,
                  bot_open_id: str | None = None) -> IncomingMessage | None:
    root = _object(value)
    event = _object(root.get("event", root))
    sender, message = _object(event.get("sender")), _object(event.get("message"))
    message_type = message.get("message_type")
    if sender.get("sender_type") != "user" or message_type not in ("text", "image", "post"):
        return None
    sender_id = _string(_object(sender.get("sender_id")).get("open_id"))
    message_id, chat_id = _string(message.get("message_id")), _string(message.get("chat_id"))
    chat_type = message.get("chat_type")
    if not sender_id or not message_id or not chat_id or chat_type not in ("p2p", "group"):
        return None
    mentions = message.get("mentions")
    mentions = [_object(item) for item in mentions] if isinstance(mentions, list) else []
    if chat_type == "group" and require_group_mention:
        if not bot_open_id or not any(_string(item.get("key")) and
            _object(item.get("id")).get("open_id") == bot_open_id for item in mentions):
            return None
    try:
        created_at = int(message.get("create_time", ""))
    except (ValueError, TypeError):
        return None
    if created_at <= 0:
        return None
    try:
        content = _object(json.loads(message.get("content", "")))
    except (ValueError, TypeError):
        if message_type == "text":
            return None
        content = {}
    image_keys: tuple[str, ...] = ()
    error = ""
    if message_type == "text":
        text = content.get("text")
        if not isinstance(text, str):
            return None
    elif message_type == "image":
        text = IMAGE_DEFAULT_PROMPT
        if _valid_image_key(content.get("image_key")):
            image_keys = (content["image_key"],)
        else:
            error = IMAGE_INVALID
    else:
        text, image_keys, error = _parse_post(content)
    for mention in mentions:
        key = _string(mention.get("key"))
        if key:
            text = text.replace(key, "")
    text = text.strip()
    if not text and not error:
        return None
    return IncomingMessage(message_id, chat_id, sender_id, text or "请分析这条消息。",
                           chat_type, created_at, image_keys=image_keys, attachment_error=error)


def parse_action(value: Any) -> CardAction | None:
    root = _object(value)
    event = _object(root.get("event", root))
    action, context = _object(event.get("action")), _object(event.get("context"))
    data = _object(action.get("value"))
    sender_id = _string(_object(event.get("operator")).get("open_id"))
    ticket_id, decision = _string(data.get("ticketId")), data.get("decision")
    chat_id = _string(context.get("open_chat_id"))
    message_id = _string(context.get("open_message_id"))
    if (action.get("tag") != "button" or not sender_id or not chat_id or not message_id
            or not ticket_id or len(ticket_id) > 128 or decision not in ("accept", "decline")):
        return None
    return CardAction(ticket_id, decision, sender_id, chat_id, message_id)


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def build_card(view: View) -> dict[str, Any]:
    colors = {"progress": "blue", "result": "green", "error": "red",
              "approval": "orange", "question": "purple", "info": "blue"}
    columns = []
    for index, action in enumerate(view.actions[:8]):
        if not action.ticket_id or len(action.ticket_id) > 128:
            raise ValueError("Invalid approval ticket ID")
        columns.append({
            "tag": "column", "width": "weighted", "weight": 1,
            "elements": [{
                "tag": "button", "text": {"tag": "plain_text", "content": action.label[:80]},
                "type": "danger" if action.decision == "decline" else "primary_filled" if index == 0 else "default",
                "width": "fill", "behaviors": [{"type": "callback", "value": {
                    "ticketId": action.ticket_id, "decision": action.decision,
                }}],
            }],
        })
    card: dict[str, Any] = {
        "schema": "2.0",
        "config": {"update_multi": True, "width_mode": "default", "enable_forward": False},
    }
    if view.title:
        card["header"] = {"title": {"tag": "plain_text", "content": view.title[:80]},
                          "template": colors.get(view.kind, "blue")}

    def set_text(text: str) -> None:
        elements: list[dict[str, Any]] = [{"tag": "markdown", "content": text or "…"}]
        if columns:
            elements.append({"tag": "column_set", "flex_mode": "none", "columns": columns})
        card["body"] = {"elements": elements}

    def fits() -> bool:
        # Account for the nested JSON string, not just the visible UTF-8 text.
        return len(_json({"content": _json(card)}).encode("utf-8")) <= CARD_BUDGET

    text = view.text[:CARD_BUDGET]
    set_text(text + (TRUNCATION if len(text) < len(view.text) else ""))
    if fits():
        return card
    low, high = 0, len(text)
    while low < high:
        middle = (low + high + 1) // 2
        set_text(text[:middle] + TRUNCATION)
        if fits():
            low = middle
        else:
            high = middle - 1
    set_text(text[:low] + TRUNCATION)
    if not fits():
        raise ValueError("Feishu card metadata exceeds the message size limit")
    return card


def _roundtrip(connection: Any, kind: str, payload: dict[str, Any],
               timeout: float = DELIVERY_TIMEOUT) -> None:
    """A callback only succeeds after the parent confirms durable acceptance."""
    delivery_id = uuid.uuid4().hex
    connection.send({"type": kind, "delivery_id": delivery_id, "payload": payload})
    deadline = time.monotonic() + timeout
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0 or not connection.poll(remaining):
            raise RuntimeError("Feishu inbox acceptance timed out")
        reply = connection.recv()
        if reply.get("delivery_id") != delivery_id:
            continue  # A previous timed-out delivery may have a late ACK.
        if reply.get("accepted") is True:
            return
        raise RuntimeError("Feishu inbox did not accept the delivery")


def _post_with_timeout(post: Callable[..., Any]) -> Callable[..., Any]:
    """Bound SDK endpoint discovery, which has no timeout in pinned SDK 1.7.5."""
    def bounded(*args: Any, **kwargs: Any) -> Any:
        if kwargs.get("timeout") is None:
            kwargs["timeout"] = (10, 20)
        return post(*args, **kwargs)
    return bounded


async def _heartbeat(connection: Any, client: Any) -> None:
    while True:
        try:
            # Inspect only connection presence; connection URLs contain credentials.
            connection.send({"type": "heartbeat", "connected": client._conn is not None})
        except (EOFError, OSError):
            return
        await asyncio.sleep(HEARTBEAT_INTERVAL)


def _listener_process(connection: Any, app_id: str, app_secret: str,
                      require_group_mention: bool, bot_open_id: str | None) -> None:
    """Spawn target: SDK-global event loop and raw SDK logs stay in this process."""
    try:
        asyncio.set_event_loop(asyncio.new_event_loop())
        import lark_oapi as lark
        import requests
        from lark_oapi.event.callback.model.p2_card_action_trigger import P2CardActionTriggerResponse
        from lark_oapi.ws.client import loop as sdk_loop
        # This module mutation is confined to the spawned listener, not the parent API client.
        requests.post = _post_with_timeout(requests.post)
        sdk_logger = logging.getLogger("Lark")
        sdk_logger.disabled = True  # SDK errors/connection URLs can include secrets.

        def on_message(data: Any) -> None:
            message = parse_message(json.loads(lark.JSON.marshal(data)),
                                    require_group_mention=require_group_mention, bot_open_id=bot_open_id)
            if message is not None:
                _roundtrip(connection, "message", asdict(message))

        def on_action(data: Any) -> Any:
            action = parse_action(json.loads(lark.JSON.marshal(data)))
            if action is not None:
                _roundtrip(connection, "action", asdict(action))
            return P2CardActionTriggerResponse({"toast": {
                "type": "info" if action is not None else "warning",
                "content": "请求已收到，处理中" if action is not None else "无法识别此操作",
            }})

        handler = (lark.EventDispatcherHandler.builder("", "")
                   .register_p2_im_message_receive_v1(on_message)
                   .register_p2_card_action_trigger(on_action).build())
        class ObservedClient(lark.ws.Client):
            async def _connect(self) -> None:
                await super()._connect()
                # The public SDK hooks only report reconnects, not the initial connection.
                connection.send({"type": "status", "state": "connected"})

        client = ObservedClient(app_id, app_secret, event_handler=handler, auto_reconnect=True)
        client.on_reconnecting = lambda: connection.send({"type": "status", "state": "reconnecting"})
        client.on_reconnected = lambda: connection.send({"type": "status", "state": "connected"})
        connection.send({"type": "status", "state": "starting"})
        sdk_loop.create_task(_heartbeat(connection, client))
        client.start()
    except Exception:
        try:
            connection.send({"type": "status", "state": "failed"})
        except (EOFError, OSError):
            pass
    finally:
        connection.close()


class _LarkApi:
    def __init__(self, app_id: str, app_secret: str) -> None:
        import lark_oapi as lark
        self.client = (lark.Client.builder().app_id(app_id).app_secret(app_secret)
                       .timeout(15).build())
        logging.getLogger("Lark").disabled = True

    def send(self, chat_id: str, content: str, reply_to: str | None, request_id: str) -> Any:
        from lark_oapi.api.im.v1 import (CreateMessageRequest, CreateMessageRequestBody,
                                        ReplyMessageRequest, ReplyMessageRequestBody)
        if reply_to:
            body = (ReplyMessageRequestBody.builder().msg_type("interactive")
                    .content(content).uuid(request_id).build())
            request = ReplyMessageRequest.builder().message_id(reply_to).request_body(body).build()
            return self.client.im.v1.message.reply(request)
        body = (CreateMessageRequestBody.builder().receive_id(chat_id).msg_type("interactive")
                .content(content).uuid(request_id).build())
        request = (CreateMessageRequest.builder().receive_id_type("chat_id")
                   .request_body(body).build())
        return self.client.im.v1.message.create(request)

    def send_user(self, open_id: str, content: str, request_id: str) -> Any:
        from lark_oapi.api.im.v1 import CreateMessageRequest, CreateMessageRequestBody
        body = (CreateMessageRequestBody.builder().receive_id(open_id).msg_type("interactive")
                .content(content).uuid(request_id).build())
        request = (CreateMessageRequest.builder().receive_id_type("open_id")
                   .request_body(body).build())
        return self.client.im.v1.message.create(request)

    def update(self, message_id: str, content: str) -> Any:
        from lark_oapi.api.im.v1 import PatchMessageRequest, PatchMessageRequestBody
        body = PatchMessageRequestBody.builder().content(content).build()
        request = PatchMessageRequest.builder().message_id(message_id).request_body(body).build()
        return self.client.im.v1.message.patch(request)

    def add_reaction(self, message_id: str, emoji_type: str) -> Any:
        from lark_oapi.api.im.v1 import (CreateMessageReactionRequest,
                                        CreateMessageReactionRequestBody, Emoji)
        emoji = Emoji.builder().emoji_type(emoji_type).build()
        body = CreateMessageReactionRequestBody.builder().reaction_type(emoji).build()
        request = (CreateMessageReactionRequest.builder().message_id(message_id)
                   .request_body(body).build())
        return self.client.im.v1.message_reaction.create(request)

    def remove_reaction(self, message_id: str, reaction_id: str) -> Any:
        from lark_oapi.api.im.v1 import DeleteMessageReactionRequest
        request = (DeleteMessageReactionRequest.builder().message_id(message_id)
                   .reaction_id(reaction_id).build())
        return self.client.im.v1.message_reaction.delete(request)

    def download_image(self, message_id: str, image_key: str) -> bytes:
        """Download only a resource belonging to the triggering message.

        The SDK's resource helper buffers response.content before returning.
        Reuse its per-app tenant-token cache, but bound the actual download.
        """
        import requests
        from lark_oapi.core.token import TokenManager

        if not _valid_image_message_id(message_id) or not _valid_image_key(image_key):
            raise ImageInputError(IMAGE_INVALID)
        deadline = time.monotonic() + IMAGE_DOWNLOAD_TIMEOUT
        try:
            token = TokenManager.get_self_tenant_token(self.client.config)
            if not isinstance(token, str) or not token or time.monotonic() >= deadline:
                raise ImageInputError(IMAGE_DOWNLOAD_FAILED)
            # Both components are restricted path segments; never use a
            # sender-supplied URL, SDK domain override or redirect target.
            url = f"https://open.feishu.cn/open-apis/im/v1/messages/{message_id}/resources/{image_key}"
            with requests.get(url, params={"type": "image"},
                              headers={"Authorization": f"Bearer {token}", "Accept-Encoding": "identity"},
                              auth=lambda request: request, stream=True, allow_redirects=False,
                              timeout=(5, 10)) as response:
                if response.status_code != 200:
                    # Inspect only a bounded code field, never return API text.
                    error_body = response.raw.read1(4096, decode_content=False)
                    try:
                        error_code = _object(json.loads(error_body)).get("code")
                    except (ValueError, TypeError, UnicodeError):
                        error_code = None
                    if error_code == 99991672:
                        raise ImageInputError(
                            "图片读取权限尚未开通，请管理员为机器人启用 im:message:readonly 权限并发布应用版本。")
                    raise ImageInputError(IMAGE_DOWNLOAD_FAILED)
                if response.headers.get("Content-Encoding", "").lower() not in ("", "identity"):
                    raise ImageInputError(IMAGE_DOWNLOAD_FAILED)
                content_type = response.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
                if content_type and not (content_type.startswith("image/")
                                         or content_type == "application/octet-stream"):
                    raise ImageInputError(IMAGE_DOWNLOAD_FAILED)
                declared_size = response.headers.get("Content-Length")
                if declared_size is not None:
                    if not declared_size.isascii() or not declared_size.isdecimal():
                        raise ImageInputError(IMAGE_DOWNLOAD_FAILED)
                    if int(declared_size) > MAX_IMAGE_BYTES:
                        raise ImageInputError(IMAGE_TOO_LARGE)
                data = bytearray()
                while True:
                    if time.monotonic() >= deadline:
                        raise ImageInputError(IMAGE_DOWNLOAD_FAILED)
                    # read1 returns after at most one underlying read, unlike
                    # iter_content which may wait forever to fill a large block
                    # from a peer sending a few bytes before every read timeout.
                    chunk = response.raw.read1(min(64 * 1024, MAX_IMAGE_BYTES - len(data) + 1),
                                               decode_content=False)
                    if time.monotonic() >= deadline:
                        raise ImageInputError(IMAGE_DOWNLOAD_FAILED)
                    if not chunk:
                        break
                    if len(data) + len(chunk) > MAX_IMAGE_BYTES:
                        raise ImageInputError(IMAGE_TOO_LARGE)
                    data.extend(chunk)
                if not data:
                    raise ImageInputError(IMAGE_INVALID)
                return bytes(data)
        except ImageInputError:
            raise
        except Exception:
            raise ImageInputError(IMAGE_DOWNLOAD_FAILED) from None


class FeishuMessenger:
    def __init__(self, app_id: str, app_secret: str, logger: Callable[[str], None] | None = None,
                 *, require_group_mention: bool = True, bot_open_id: str | None = None,
                 api: Any = None, process_context: Any = None) -> None:
        if not app_id or not app_secret:
            raise ValueError("Independent Feishu application credentials are required")
        self._app_id, self._app_secret = app_id, app_secret
        self._logger = logger
        self._require_group_mention, self._bot_open_id = require_group_mention, bot_open_id
        self._api = api
        self._api_init_lock: asyncio.Lock | None = None
        self._context = process_context
        self._connection: Any = None
        self._process: Any = None
        self._reader: asyncio.Task[Any] | None = None
        self._closed = False
        self._started_at: float | None = None
        self._disconnected_at: float | None = None
        self._last_heartbeat: float | None = None
        self._connected = False
        self._listener_failed = False
        self._on_message: MessageHandler | None = None
        self._on_action: ActionHandler | None = None

    @property
    def is_alive(self) -> bool:
        return bool(not self._closed and self._process is not None and self._process.is_alive()
                    and self._reader is not None and not self._reader.done())

    @property
    def is_connected(self) -> bool:
        return bool(self.is_alive and self._connected and self._last_heartbeat is not None
                    and time.monotonic() - self._last_heartbeat <= HEARTBEAT_TIMEOUT)

    @property
    def connection_stalled(self) -> bool:
        if self._closed or self._started_at is None:
            return False
        if self._listener_failed or not self.is_alive:
            return True
        now = time.monotonic()
        if self._connected:
            return self._last_heartbeat is None or now - self._last_heartbeat > HEARTBEAT_TIMEOUT
        disconnected_at = self._disconnected_at if self._disconnected_at is not None else self._started_at
        return now - disconnected_at > CONNECTION_TIMEOUT

    def _record_connection(self, connected: bool) -> None:
        now = time.monotonic()
        if connected:
            self._connected = True
            self._disconnected_at = None
            self._last_heartbeat = now
        else:
            self._connected = False
            if self._disconnected_at is None:
                self._disconnected_at = now

    def _record_status(self, state: str) -> None:
        if state == "connected":
            self._record_connection(True)
        elif state in ("starting", "reconnecting", "failed"):
            self._record_connection(False)
            if state == "failed":
                self._listener_failed = True

    async def start(self, on_message: MessageHandler, on_action: ActionHandler) -> None:
        if self._process is not None or self._closed:
            raise RuntimeError("Feishu messenger cannot be started twice")
        self._on_message, self._on_action = on_message, on_action
        context = self._context or multiprocessing.get_context("spawn")
        parent, child = context.Pipe(duplex=True)
        process = context.Process(target=_listener_process,
                                  args=(child, self._app_id, self._app_secret,
                                        self._require_group_mention, self._bot_open_id), daemon=True)
        try:
            process.start()
        except Exception:
            parent.close()
            child.close()
            raise RuntimeError("Feishu listener could not start") from None
        child.close()
        self._connection, self._process = parent, process
        self._started_at = time.monotonic()
        self._disconnected_at = self._started_at
        self._reader = asyncio.create_task(self._read_events(), name="feishu-inbox")

    async def _read_events(self) -> None:
        try:
            while not self._closed:
                if not await asyncio.to_thread(self._connection.poll, 0.25):
                    if not self._process.is_alive():
                        break
                    continue
                item = await asyncio.to_thread(self._connection.recv)
                if item.get("type") == "status":
                    state = item.get("state")
                    if state in ("starting", "connected", "reconnecting", "failed"):
                        self._record_status(state)
                        self._log(f"Feishu listener {state}.")
                    continue
                if item.get("type") == "heartbeat":
                    if isinstance(item.get("connected"), bool):
                        self._last_heartbeat = time.monotonic()
                        self._record_connection(item["connected"])
                    continue
                await self._deliver(item)
        except (EOFError, OSError):
            pass
        except asyncio.CancelledError:
            raise
        except Exception:
            self._log("Feishu inbox failed.")
        finally:
            if not self._closed:
                self._log("Feishu listener stopped; restart after checking application configuration.")

    async def _deliver(self, item: dict[str, Any]) -> None:
        accepted = False
        try:
            kind, payload = item.get("type"), item.get("payload", {})
            if kind == "message" and self._on_message is not None:
                work = self._on_message(IncomingMessage(**payload))
            elif kind == "action" and self._on_action is not None:
                work = self._on_action(CardAction(**payload))
            else:
                raise ValueError("Unsupported inbox delivery")
            await asyncio.wait_for(work, timeout=ENQUEUE_TIMEOUT)
            accepted = True
        except asyncio.CancelledError:
            raise
        except Exception:
            self._log("Feishu delivery not accepted; requesting retry.")
        await asyncio.to_thread(self._connection.send, {
            "delivery_id": item.get("delivery_id"), "accepted": accepted,
        })

    async def send(self, chat_id: str, view: View, reply_to: str | None = None) -> str:
        content = _json(build_card(view))
        response = await self._request("send", chat_id, content, reply_to, uuid.uuid4().hex)
        message_id = getattr(getattr(response, "data", None), "message_id", None)
        if not isinstance(message_id, str) or not message_id:
            raise RuntimeError("Feishu send returned no message ID")
        return message_id

    async def send_user(self, open_id: str, view: View, *, request_id: str | None = None) -> str:
        """Send as this application; callers persist request_id across outbox retries."""
        if not isinstance(open_id, str) or re.fullmatch(r"ou_[A-Za-z0-9_-]{1,125}", open_id) is None:
            raise ValueError("A valid Feishu open ID is required")
        if request_id is None:
            request_id = uuid.uuid4().hex
        elif not isinstance(request_id, str) or re.fullmatch(r"[A-Za-z0-9_-]{1,50}", request_id) is None:
            raise ValueError("A valid Feishu message request ID is required")
        response = await self._request("send_user", open_id, _json(build_card(view)), request_id)
        message_id = getattr(getattr(response, "data", None), "message_id", None)
        if not isinstance(message_id, str) or not message_id.strip():
            raise RuntimeError("Feishu send_user returned no message ID")
        return message_id

    async def update(self, message_id: str, view: View) -> None:
        await self._request("update", message_id, _json(build_card(view)))

    async def add_reaction(self, message_id: str, emoji_type: str = "Typing") -> str:
        if not isinstance(message_id, str) or not message_id.strip():
            raise ValueError("A message ID is required for a Feishu reaction")
        if not isinstance(emoji_type, str) or not emoji_type.strip():
            raise ValueError("An emoji type is required for a Feishu reaction")
        response = await self._request("add_reaction", message_id, emoji_type)
        reaction_id = getattr(getattr(response, "data", None), "reaction_id", None)
        if not isinstance(reaction_id, str) or not reaction_id.strip():
            raise RuntimeError("Feishu add_reaction returned no reaction ID")
        return reaction_id

    async def remove_reaction(self, message_id: str, reaction_id: str) -> None:
        if not isinstance(message_id, str) or not message_id.strip():
            raise ValueError("A message ID is required for a Feishu reaction")
        if not isinstance(reaction_id, str) or not reaction_id.strip():
            raise ValueError("A reaction ID is required to remove a Feishu reaction")
        await self._request("remove_reaction", message_id, reaction_id)

    async def download_image(self, message_id: str, image_key: str) -> bytes:
        if not _valid_image_message_id(message_id) or not _valid_image_key(image_key):
            raise ImageInputError(IMAGE_INVALID)
        if self._closed:
            raise ImageInputError(IMAGE_DOWNLOAD_FAILED)
        try:
            data = await self._invoke("download_image", message_id, image_key)
        except ImageInputError:
            raise
        except Exception:
            raise ImageInputError(IMAGE_DOWNLOAD_FAILED) from None
        if not isinstance(data, bytes) or not data:
            raise ImageInputError(IMAGE_INVALID)
        if len(data) > MAX_IMAGE_BYTES:
            raise ImageInputError(IMAGE_TOO_LARGE)
        return data

    async def _invoke(self, operation: str, *args: Any) -> Any:
        if self._api is None:
            if self._api_init_lock is None:
                self._api_init_lock = asyncio.Lock()
            async with self._api_init_lock:
                if self._api is None:
                    self._api = await asyncio.to_thread(_LarkApi, self._app_id, self._app_secret)
        # SDK authentication performs synchronous I/O even in its async API.
        # Keep all SDK HTTP work off the parent inbox event loop.
        return await asyncio.to_thread(getattr(self._api, operation), *args)

    async def _request(self, operation: str, *args: Any) -> Any:
        if self._closed:
            raise RuntimeError("Feishu messenger is closed")
        try:
            response = await self._invoke(operation, *args)
        except Exception:
            raise RuntimeError(f"Feishu {operation} failed; check connectivity and application permissions") from None
        code = getattr(response, "code", None)
        if operation == "remove_reaction" and isinstance(code, int) and code in REACTION_GONE_CODES:
            # DELETE may have succeeded before persisting cleanup (or before a
            # lost HTTP response). Absence is already the desired final state.
            return response
        if code != 0:
            suffix = f" (code {code})" if isinstance(code, int) else ""
            raise RuntimeError(f"Feishu {operation} failed{suffix}")
        return response

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._connected = False
        if self._reader is not None:
            self._reader.cancel()
            await asyncio.gather(self._reader, return_exceptions=True)
        if self._process is not None:
            if self._process.is_alive():
                self._process.terminate()
            await asyncio.to_thread(self._process.join, 2)
            if self._process.is_alive():
                self._process.kill()
                await asyncio.to_thread(self._process.join, 2)
            self._process.close()
        if self._connection is not None:
            self._connection.close()

    def _log(self, message: str) -> None:
        if self._logger is not None:
            try:
                self._logger(message)
            except Exception:
                pass
