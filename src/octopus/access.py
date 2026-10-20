"""Membership requests, kept separate from Codex tasks and execution approvals."""
from __future__ import annotations

import asyncio
import logging
import time
import uuid
from collections import OrderedDict, deque
from html import escape
from typing import Any

from .access_test import AccessSelfTest
from .contracts import Button, CardAction, IncomingMessage, Messenger, View
from .store import Store

log = logging.getLogger(__name__)

ACCESS_COMMANDS = frozenset({"/access-requests", "/access-approve", "/access-deny", "/access-test"})
APPLICATION_WORDS = frozenset({"/apply", "申请权限", "申请使用", "申请使用权限"})
APPLICATION_HELP = "你还没有此 bot 的对话权限。发送“申请权限”或 /apply，管理员确认后即可使用。"


def _request_id(value: str) -> str | None:
    try:
        return str(uuid.UUID(value))
    except (ValueError, AttributeError, TypeError):
        return None


def _applicant(request: dict[str, Any]) -> str:
    return f'<at id="{escape(request["requester_id"], quote=True)}"></at>'


class AccessRequests:
    def __init__(self, store: Store, messenger: Messenger):
        self.store, self.messenger = store, messenger
        self.self_test = AccessSelfTest(store, messenger)
        self.wakeup = asyncio.Event()
        self._feedback_times: OrderedDict[str, float] = OrderedDict()
        self._feedback_window: deque[float] = deque()

    def _feedback(self, sender: str, text: str) -> str | None:
        """Bound unsolicited hints; database request limits are independent."""
        now = time.monotonic()
        while self._feedback_window and self._feedback_window[0] <= now - 60:
            self._feedback_window.popleft()
        if now - self._feedback_times.get(sender, -60) < 30 or len(self._feedback_window) >= 60:
            return None
        self._feedback_window.append(now)
        self._feedback_times[sender] = now
        self._feedback_times.move_to_end(sender)
        while len(self._feedback_times) > 1024:
            self._feedback_times.popitem(last=False)
        return text

    async def receive(self, message: IncomingMessage, *, authorized: bool) -> tuple[bool, str | None]:
        """Return an optional fixed reply; never make an HTTP/model call here."""
        if message.chat_type != "p2p":
            return not authorized, None
        text = message.text.strip()
        application = text.lower() in APPLICATION_WORDS
        if authorized:
            if application:
                return True, self._feedback(message.sender_id, "你已有对话权限，可以直接发送业务或代码问题。")
            return False, None
        if not application:
            member = await self.store.member(message.sender_id)
            if member and not member["enabled"]:
                return True, self._feedback(message.sender_id, "你的账号已被停用，请联系管理员处理。")
            return True, self._feedback(message.sender_id, APPLICATION_HELP)
        outcome, _ = await self.store.submit_access_request(message)
        if outcome in {"created", "duplicate"}:
            self.wakeup.set()
            return True, None
        if outcome == "pending":
            self.wakeup.set()
        notices = {
            "pending": "你的申请正在等待管理员处理，无需重复提交。",
            "cooldown": "近期申请未通过，请在上次申请处理满24小时后再申请，或联系管理员。",
            "disabled": "你的账号已被停用，请联系管理员处理。",
            "member": "你已有对话权限，可以直接发送业务或代码问题。",
            "no_admin": "目前没有可处理申请的管理员，请联系应用维护人。",
            "busy": "待处理申请较多，请稍后再申请。",
        }
        return True, self._feedback(message.sender_id, notices.get(outcome, "暂时无法提交申请，请稍后重试。"))

    async def reply(self, message: IncomingMessage, text: str) -> None:
        # Only fixed application instructions/status reach this method. Normal
        # business output continues to use Bridge._notice and its member check.
        await self.messenger.send(message.chat_id, View("info", "", text), message.id)

    async def receive_action(self, action: CardAction) -> None:
        if not action.ticket_id.startswith("access:"):
            return
        identifier = _request_id(action.ticket_id.removeprefix("access:"))
        if not identifier or not action.message_id or not action.chat_id or action.decision not in {"accept", "decline"}:
            return
        # The transaction verifies the currently enabled admin and the exact
        # review card sent to them. Never trust an applicant ID in card values.
        await self.store.decide_access_request(
            identifier, action.sender_id, action.decision, card_id=action.message_id,
        )
        self.wakeup.set()

    async def command(self, message: IncomingMessage) -> str:
        actor = await self.store.member(message.sender_id)
        if message.chat_type != "p2p" or not actor or not actor["enabled"] or actor["role"] != "admin":
            return "权限申请只能由管理员在私聊中处理。"
        parts = message.text.strip().split()
        command = parts[0].lower()
        if command == "/access-test":
            if len(parts) != 1:
                return "用法：/access-test"
            return await self.self_test.create(message)
        if command == "/access-requests":
            if len(parts) != 1:
                return "用法：/access-requests"
            try:
                requests = await self.store.pending_access_requests(message.sender_id)
            except PermissionError:
                return "管理员权限已停用。"
            self.wakeup.set()
            if not requests:
                return "暂无待处理的权限申请。"
            rows = [f'{_applicant(request)}：`{request["id"]}`' for request in requests]
            return "\n".join(rows) + "\n\n使用 /access-approve 编号 通过，或 /access-deny 编号 拒绝。"
        identifier = _request_id(parts[1]) if len(parts) == 2 else None
        if not identifier:
            return f"用法：{command} 申请编号"
        outcome, request = await self.store.decide_access_request(
            identifier, message.sender_id, "accept" if command == "/access-approve" else "decline",
        )
        self.wakeup.set()
        if outcome == "decided":
            return "已通过此申请，结果将通知申请人。" if request["status"] == "accepted" else "已拒绝此申请，结果将通知申请人。"
        return {
            "already_decided": "此申请已处理，无需重复操作。",
            "expired": "此申请已过期，请让申请人重新提交。",
            "member_changed": "申请人的成员状态已变更，此申请已关闭。请通过成员管理查看当前状态。",
            "forbidden": "无权处理此申请。",
            "invalid": "申请编号或操作无效。",
            "missing": "没有找到此申请。",
        }.get(outcome, "此申请当前无法处理。")

    async def _view(self, delivery: dict[str, Any]) -> View:
        request = delivery["request"]
        status = request["status"]
        if delivery["kind"] == "review":
            applicant = _applicant(request)
            if status == "pending":
                return View("approval", "对话权限申请",
                    f'申请人：{applicant}\n申请编号：`{request["id"]}`\n\n'
                    "通过后可使用本 bot 的只读业务查询：代码、文档、日志、指标和受限数据排查。"
                    "授予普通成员权限，申请7天内有效。",
                    [Button("通过", "access:" + request["id"], "accept"),
                     Button("拒绝", "access:" + request["id"], "decline")])
            labels = {"accepted": "已通过", "declined": "已拒绝", "expired": "已过期",
                      "member_changed": "成员状态已变更，申请已关闭"}
            return View("info", "", f'申请人：{applicant}\n申请编号：`{request["id"]}`\n'
                        f'处理结果：{labels.get(status, "已关闭")}。')
        if delivery["kind"] == "requested" and status == "pending":
            return View("info", "", "权限申请已提交，等待管理员确认。通过后请重新发送要问的问题。")
        if status == "accepted":
            member = await self.store.member(request["requester_id"])
            text = ("权限申请已通过，可以直接发送业务或代码问题。\n请重新发送要问的问题，输入 /help 可查看用法。"
                    if member and member["enabled"] else "权限申请曾通过，但你的账号当前已停用，请联系管理员。")
        else:
            text = {
                "declined": "你的对话权限申请未通过。可联系管理员了解原因，24小时后可以重新申请。",
                "expired": "你的对话权限申请已过期，可以发送“申请权限”重新提交。",
                "member_changed": "你的成员状态已变更，本次申请已关闭。请重新发送消息确认权限，或联系管理员。",
            }.get(status, "本次对话权限申请已关闭，请联系管理员。")
        return View("info", "", text)

    async def deliver_once(self) -> None:
        for delivery in await self.store.access_deliveries(limit=20):
            try:
                view = await self._view(delivery)
                message_id = delivery["message_id"]
                if message_id:
                    await self.messenger.update(message_id, view)
                else:
                    message_id = await self.messenger.send_user(
                        delivery["recipient_id"], view, request_id=delivery["delivery_id"],
                    )
                    if delivery["kind"] == "review" and delivery["request"]["status"] != "pending":
                        # A successful pending send may have lost its database
                        # acknowledgement. Feishu can return that original
                        # card for the same UUID, without replacing its body.
                        await self.messenger.update(message_id, view)
                await self.store.complete_access_delivery(
                    delivery["delivery_id"], message_id,
                    terminal=delivery["request"]["status"] != "pending",
                )
            except asyncio.CancelledError:
                raise
            except Exception:
                # SDK errors can contain private request metadata. A failed
                # send stays in the database and never grants membership.
                log.warning("权限申请通知暂未送达，将重试")
                await self.store.retry_access_delivery(delivery["delivery_id"])

    async def run(self) -> None:
        while True:
            self.wakeup.clear()
            try:
                await self.deliver_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.warning("权限申请通知暂不可用，将重试")
            try:
                await asyncio.wait_for(self.wakeup.wait(), 5)
            except TimeoutError:
                pass
