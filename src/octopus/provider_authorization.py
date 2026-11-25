"""Admin-only provider login, outside model tools and execution approvals."""
from __future__ import annotations

import asyncio
import logging
import re
import secrets
from dataclasses import dataclass, field
from time import monotonic
from typing import Awaitable, Callable
from urllib.parse import urlsplit

from .contracts import Button, CardAction, IncomingMessage, View

log = logging.getLogger(__name__)
AUTHORIZATION_WAIT_SECONDS = 300
authorization_wait_seconds = AUTHORIZATION_WAIT_SECONDS
POLL_SECONDS = 2
AUTH_PREFIX = "provider-auth:"
_SCOPE = "重新登录并更新本机共享 TLS 查询身份，所有已获准成员共用其现有权限。"
_TERMINAL = {
    "succeeded": "TLS 授权已完成，共享查询身份已通过验证。",
    "failed": "TLS 授权未完成。",
    "expired": "TLS 授权等待已到期。",
    "cancelled": "TLS 授权已取消。",
    "forbidden": "此授权请求已失效。",
    "busy": "已有 TLS 授权正在处理。",
    "no_device_prompt": "当前 TLS 查询身份已通过验证。本次授权卡片未显示新的设备链接，无法确认已重新授权。"
                        "请在私聊中发送“重新发起 TLS 授权”、/tls_auth 或 /tls-auth 重试。若仍无链接，请联系宿主机管理员。",
}


@dataclass
class _Flow:
    message: IncomingMessage
    allowed: Callable[[], Awaitable[bool]]
    done: asyncio.Future[bool]
    deadline: float
    manual: bool
    request_id: str = field(default_factory=lambda: secrets.token_hex(16))
    cards: dict[str, str] = field(default_factory=dict)
    actor: str = ""
    decision: str = ""
    cancelled: bool = False
    requested: bool = False
    finishing: bool = False
    wakeup: asyncio.Event = field(default_factory=asyncio.Event)
    task: asyncio.Task | None = None

    @property
    def ticket_id(self) -> str:
        return AUTH_PREFIX + self.request_id


class ProviderAuthorization:
    """Keep login links in administrator DMs, never in a tool result."""

    def __init__(self, store, messenger, queries):
        self.store, self.messenger, self.queries = store, messenger, queries
        self._flow: _Flow | None = None
        self._closed = False

    async def _admin(self, actor: str) -> bool:
        member = await self.store.member(actor)
        return bool(member and member.get("enabled") and member.get("role") == "admin")

    async def _valid(self, flow: _Flow) -> bool:
        if self._closed or self._flow is not flow or flow.cancelled or monotonic() >= flow.deadline:
            return False
        if not await flow.allowed():
            return False
        if flow.actor and not await self._admin(flow.actor):
            return False
        # The membership checks yield. A stop/cancel/close during them wins.
        return (not self._closed and self._flow is flow and not flow.cancelled
                and monotonic() < flow.deadline)

    @staticmethod
    def _invalid_state(flow: _Flow) -> str:
        return ("cancelled" if flow.cancelled else "expired"
                if monotonic() >= flow.deadline else "forbidden")

    async def recover(self, message: IncomingMessage, *, still_allowed: Callable[[], Awaitable[bool]]) -> bool:
        return await self._authorize(message, still_allowed, manual=False)

    async def request_manual(self, message: IncomingMessage) -> bool:
        async def permitted() -> bool:
            return message.chat_type == "p2p" and await self._admin(message.sender_id)

        return await self._authorize(message, permitted, manual=True)

    async def _authorize(self, message: IncomingMessage, still_allowed: Callable[[], Awaitable[bool]],
                         *, manual: bool) -> bool:
        if self._closed:
            return False
        allowed = await still_allowed()
        if self._closed:
            return False
        if not allowed:
            if manual:
                await self.messenger.send(message.chat_id,
                    View("info", "", "请由管理员在与机器人的私聊中发起 TLS 授权。"), message.id)
            return False
        # No await between checking and reserving: two callers cannot both win.
        if self._flow is not None:
            busy = _TERMINAL["busy"] if manual else "已有 TLS 授权正在处理，请等待管理员完成后再查询。"
            await self.messenger.send(message.chat_id, View("info", "", busy), message.id)
            return False
        flow = _Flow(message, still_allowed, asyncio.get_running_loop().create_future(),
                     monotonic() + AUTHORIZATION_WAIT_SECONDS, manual)
        self._flow = flow
        flow.task = asyncio.create_task(self._run(flow))
        try:
            result = await asyncio.shield(flow.done)
            if not result or not await still_allowed() or not await self._admin(flow.actor):
                return False
            return not self._closed
        except asyncio.CancelledError:
            flow.cancelled = True
            flow.wakeup.set()
            if not flow.finishing:
                flow.task.cancel()
            raise

    async def receive_action(self, action: CardAction) -> None:
        flow = self._flow
        if (self._closed or flow is None or action.ticket_id != flow.ticket_id
                or not action.chat_id or not action.message_id
                or (flow.manual and action.chat_id != flow.message.chat_id)
                or action.message_id != flow.cards.get(action.sender_id)
                or action.decision not in {"accept", "decline"}):
            return
        if not await self._admin(action.sender_id):
            return
        # Membership lookup yields: gate again before consuming the decision.
        if (self._closed or self._flow is not flow or flow.done.done() or flow.cancelled or flow.finishing
                or monotonic() >= flow.deadline):
            return
        if flow.decision:
            if flow.actor == action.sender_id and action.decision == "decline":
                flow.cancelled = True
                flow.wakeup.set()
            return
        flow.actor, flow.decision = action.sender_id, action.decision
        flow.cancelled = action.decision == "decline"
        flow.wakeup.set()

    async def _pause(self, flow: _Flow) -> None:
        flow.wakeup.clear()
        remaining = flow.deadline - monotonic()
        if remaining <= 0:
            raise TimeoutError()
        try:
            await asyncio.wait_for(flow.wakeup.wait(), min(POLL_SECONDS, remaining))
        except TimeoutError:
            pass

    async def _audit(self, flow: _Flow, decision: str) -> None:
        await self.store.audit(flow.actor or flow.message.sender_id,
                               "provider.auth", "tls", decision, flow.message.id)

    async def _call(self, flow: _Flow, operation: str) -> dict:
        remaining = flow.deadline - monotonic()
        if remaining <= 0:
            raise TimeoutError()
        result = await asyncio.wait_for(getattr(self.queries, "auth_" + operation)(
            "tls", flow.actor, flow.request_id), min(15, remaining))
        if (not isinstance(result, dict) or result.get("provider") != "tls"
                or result.get("request_id") != flow.request_id):
            raise ValueError("Invalid provider authorization response")
        return result

    @staticmethod
    def _login_text(result: dict) -> str | None:
        url = result.get("verification_url")
        if url is None:
            return None
        if not isinstance(url, str) or len(url) > 4096 or re.search(r"[\s<>\[\]()`]", url):
            raise ValueError("Invalid authorization URL")
        parsed = urlsplit(url)
        if (parsed.scheme != "https" or parsed.hostname != "signin.volcengine.com"
                or parsed.port not in {None, 443} or parsed.username or parsed.password or parsed.fragment):
            raise ValueError("Invalid authorization origin")
        code = result.get("user_code")
        if code is not None and (not isinstance(code, str) or not re.fullmatch(r"[A-Za-z0-9-]{1,64}", code)):
            raise ValueError("Invalid authorization code")
        text = _SCOPE + "\n\n[打开火山引擎官方授权页面](" + url + ")"
        if code:
            text += "\n授权码：`" + code + "`"
        return text + "\n\n请在官方页面确认，无需在聊天中提供密码或令牌。此卡片最多等待 5 分钟。"

    async def _invite(self, flow: _Flow) -> bool:
        if flow.manual:
            admins = [flow.message.sender_id]
        else:
            members = await self.store.members()
            admins = [m["open_id"] for m in members if m.get("enabled") and m.get("role") == "admin"]
            if flow.message.sender_id in admins:
                admins = [flow.message.sender_id]
            if not admins:
                await self.messenger.send(flow.message.chat_id,
                    View("info", "", "TLS 登录已失效，目前没有可处理授权的管理员。"), flow.message.id)
                return False
            await self.messenger.send(flow.message.chat_id,
                View("info", "", "TLS 登录已失效，已请求管理员在与机器人的私聊中授权；完成后继续本次查询。"),
                flow.message.id)
        for actor in admins:
            if not await self._valid(flow):
                return False
            if not await self._admin(actor):
                continue
            view = View("approval", "更新 TLS 查询身份" if flow.manual else "TLS 查询需要授权", _SCOPE + "\n\n"
                "点击“开始授权”获取官方登录入口。5 分钟内有效；服务重启后需重新发起。",
                [Button("开始授权", flow.ticket_id, "accept"), Button("取消", flow.ticket_id, "decline")])
            try:
                card = await self.messenger.send_user(actor, view)
                if isinstance(card, str) and card:
                    flow.cards[actor] = card
            except asyncio.CancelledError:
                raise
            except Exception:
                log.warning("TLS 授权请求卡片暂未送达")
        if flow.manual and not flow.cards and await self._valid(flow):
            await self.messenger.send(flow.message.chat_id,
                View("info", "", "TLS 授权卡片未能送达，请稍后重试。"), flow.message.id)
        return bool(flow.cards)

    async def _run(self, flow: _Flow) -> None:
        state = "failed"
        displayed = None
        try:
            async with asyncio.timeout(AUTHORIZATION_WAIT_SECONDS):
                if not await self._invite(flow):
                    return
                while not flow.decision:
                    if not await self._valid(flow):
                        state = self._invalid_state(flow)
                        return
                    await self._pause(flow)
                if flow.cancelled:
                    state = "cancelled"
                    return
                if not await self._valid(flow):
                    state = self._invalid_state(flow)
                    return
                await self._audit(flow, "accepted")
                if not await self._valid(flow):
                    state = self._invalid_state(flow)
                    return
                # This flag is set before the HTTP call. A lost response must
                # still attempt cancellation with the same opaque request ID.
                flow.requested = True
                result = await self._call(flow, "start")
                while True:
                    if not await self._valid(flow):
                        state = self._invalid_state(flow)
                        return
                    state = result.get("state", "failed")
                    if state in _TERMINAL:
                        return
                    if state not in {"starting", "pending"}:
                        state = "failed"
                        return
                    text = self._login_text(result)
                    if text and text != displayed:
                        # Only the exact card originally sent to the selecting
                        # administrator receives the login capability.
                        await self.messenger.update(flow.cards[flow.actor], View(
                            "approval", "TLS 官方授权", text,
                            [Button("取消授权", flow.ticket_id, "decline")]))
                        displayed = text
                    await self._pause(flow)
                    if not await self._valid(flow):
                        state = self._invalid_state(flow)
                        return
                    result = await self._call(flow, "status")
        except asyncio.CancelledError:
            state = "cancelled"
        except TimeoutError:
            state = "expired"
        except Exception:
            state = "failed"
            log.warning("TLS 对话授权未完成")
        finally:
            flow.finishing = True
            if state == "succeeded":
                try:
                    if not await self._valid(flow):
                        state = self._invalid_state(flow)
                    elif flow.manual and displayed is None:
                        state = "no_device_prompt"
                except Exception:
                    state = "forbidden"
            if flow.requested and state not in {"succeeded", "no_device_prompt"}:
                try:
                    await asyncio.wait_for(self.queries.auth_cancel("tls", flow.actor, flow.request_id), 5)
                except Exception:
                    log.warning("TLS 授权取消状态尚未确认")
            text = _TERMINAL.get(state, _TERMINAL["failed"])
            if not flow.manual:
                text += "仍有效的原任务将继续查询。" if state == "succeeded" else "本次查询没有重试。"
            # Remove URLs and codes even if an administrator was revoked.
            async def clear_card(card: str) -> None:
                try:
                    await asyncio.wait_for(self.messenger.update(card, View("info", "TLS 授权结果", text)), 5)
                except Exception:
                    log.warning("TLS 授权卡片清理暂未完成")
            await asyncio.gather(*(clear_card(card) for card in flow.cards.values()))
            try:
                await asyncio.wait_for(self._audit(flow, state if state in _TERMINAL else "failed"), 5)
            except Exception:
                log.warning("TLS 授权结果审计暂未完成")
            if self._flow is flow:
                self._flow = None
            if not flow.done.done():
                flow.done.set_result(state == "succeeded")

    async def close(self) -> None:
        self._closed = True
        flow = self._flow
        if flow and flow.task:
            flow.cancelled = True
            flow.wakeup.set()
            if not flow.finishing:
                flow.task.cancel()
            await asyncio.gather(flow.task, return_exceptions=True)
            # A newly created task can be cancelled before its finally runs.
            if not flow.done.done():
                flow.done.set_result(False)
            if self._flow is flow:
                self._flow = None
