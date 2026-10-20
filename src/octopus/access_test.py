"""Ephemeral, admin-only rehearsal of access cards with no membership writes."""
from __future__ import annotations

import asyncio
import logging
import uuid
from collections import OrderedDict
from dataclasses import dataclass
from time import monotonic
from typing import Literal

from .contracts import Button, CardAction, IncomingMessage, Messenger, View
from .store import Store

log = logging.getLogger(__name__)

TEST_PREFIX = "access-test:"
TEST_TTL = 600
TEST_COOLDOWN = 30
MAX_PENDING = 128
MAX_RECENT = 1024
_SENT = "权限申请测试卡片已发给你，可点击“通过”或“拒绝”。测试10分钟内有效；服务重启后请重新发送 /access-test。不会改变任何人的权限。"
_FAILED = "权限申请测试卡片暂未送达，请稍后重新发送 /access-test。不会改变任何人的权限。"


@dataclass(frozen=True)
class TestDecision:
    recipient_id: str
    card_id: str
    result: Literal["accept", "decline"]
    request_id: str


@dataclass
class _Ticket:
    recipient_id: str
    expires_at: float
    card_id: str | None = None


class AccessSelfTest:
    def __init__(self, store: Store, messenger: Messenger):
        self.store, self.messenger = store, messenger
        self._tickets: dict[str, _Ticket] = {}
        self._recent: OrderedDict[tuple[str, str], tuple[float, str]] = OrderedDict()
        self._attempts: OrderedDict[str, float] = OrderedDict()

    def _cleanup(self, now: float) -> None:
        for key, ticket in list(self._tickets.items()):
            if ticket.expires_at <= now:
                self._tickets.pop(key, None)
        for collection, age in ((self._recent, TEST_TTL), (self._attempts, TEST_COOLDOWN)):
            while collection:
                first = next(iter(collection))
                value = collection[first]
                created_at = value[0] if isinstance(value, tuple) else value
                if created_at + age > now:
                    break
                collection.popitem(last=False)

    def _remember(self, key: tuple[str, str], now: float, text: str) -> None:
        self._recent[key] = (now, text)
        self._recent.move_to_end(key)
        while len(self._recent) > MAX_RECENT:
            self._recent.popitem(last=False)

    async def create(self, message: IncomingMessage) -> str:
        if message.chat_type != "p2p" or not message.chat_id or not message.id or not message.sender_id:
            return "权限申请测试只能由管理员在与 bot 的私聊中使用。"
        actor = await self.store.member(message.sender_id)
        if not actor or not actor["enabled"] or actor["role"] != "admin":
            return "权限申请测试只能由当前启用的管理员使用。"
        now = monotonic()
        self._cleanup(now)
        key = (message.sender_id, message.id)
        if key in self._recent:
            return self._recent[key][1]
        if any(ticket.recipient_id == message.sender_id for ticket in self._tickets.values()):
            return "你已有待处理的权限申请测试，请先点击卡片；10分钟后失效，服务重启后需重新发送 /access-test。"
        if now - self._attempts.get(message.sender_id, -TEST_COOLDOWN) < TEST_COOLDOWN:
            return "测试操作较频繁，请在上次测试发起满30秒后重新发送 /access-test。"
        if len(self._tickets) >= MAX_PENDING:
            return "当前权限申请测试较多，请稍后再试。"

        identifier = uuid.uuid4()
        ticket_id = TEST_PREFIX + str(identifier)
        ticket = _Ticket(message.sender_id, now + TEST_TTL)
        # Reserve before the HTTP call so simultaneous commands cannot create
        # several live cards for one admin. All dictionaries have fixed bounds.
        self._tickets[ticket_id] = ticket
        self._attempts[message.sender_id] = now
        self._attempts.move_to_end(message.sender_id)
        while len(self._attempts) > MAX_RECENT:
            self._attempts.popitem(last=False)
        self._remember(key, now, _SENT)
        view = View(
            "approval", "权限申请测试",
            "这是你本人发起的权限申请测试，只发送给你。\n"
            "点击按钮可模拟通过或拒绝；不会提交真实申请，也不会改变任何人的权限。\n"
            "10分钟内有效；服务重启后请重新发送 /access-test。",
            [Button("通过（测试）", ticket_id, "accept"), Button("拒绝（测试）", ticket_id, "decline")],
        )
        try:
            ticket.card_id = await self.messenger.send_user(
                message.sender_id, view, request_id=str(identifier),
            )
            if not ticket.card_id:
                raise RuntimeError("Missing self-test card ID")
        except asyncio.CancelledError:
            self._tickets.pop(ticket_id, None)
            self._remember(key, now, _FAILED)
            raise
        except Exception:
            self._tickets.pop(ticket_id, None)
            self._remember(key, now, _FAILED)
            # Transport exceptions may contain credentials or private metadata.
            log.warning("权限申请测试卡片暂未送达")
            return _FAILED
        return _SENT

    async def decide(self, action: CardAction) -> TestDecision | None:
        """Validate and consume once without any messaging or database writes."""
        now = monotonic()
        self._cleanup(now)
        ticket = self._tickets.get(action.ticket_id)
        if (not ticket or not action.chat_id or not action.message_id
                or action.message_id != ticket.card_id or action.sender_id != ticket.recipient_id
                or action.decision not in {"accept", "decline"}):
            return None
        actor = await self.store.member(action.sender_id)
        if not actor or not actor["enabled"] or actor["role"] != "admin":
            return None
        # Membership lookup yields. Recheck ownership of this ticket and its
        # deadline afterwards so concurrent clicks can consume it only once.
        if (self._tickets.get(action.ticket_id) is not ticket
                or ticket.expires_at <= monotonic()):
            self._cleanup(monotonic())
            return None
        del self._tickets[action.ticket_id]
        return TestDecision(
            ticket.recipient_id, action.message_id, action.decision,
            str(uuid.uuid5(uuid.UUID(action.ticket_id.removeprefix(TEST_PREFIX)), "result")),
        )

    async def deliver_result(self, decision: TestDecision) -> None:
        actor = await self.store.member(decision.recipient_id)
        if not actor or not actor["enabled"] or actor["role"] != "admin":
            return
        result = "已通过" if decision.result == "accept" else "已拒绝"
        view = View("info", "", f"权限申请测试：{result}。\n测试已结束，没有提交真实申请，也没有改变任何人的权限。")
        try:
            await self.messenger.update(decision.card_id, view)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.warning("权限申请测试卡片暂未更新")
        # This is intentionally separate from decide: Feishu's callback can
        # return promptly even when the outbound messaging API is slow.
        await self.messenger.send_user(decision.recipient_id, view, request_id=decision.request_id)
