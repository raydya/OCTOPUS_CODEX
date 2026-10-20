from __future__ import annotations

import asyncio
import json
import logging
import secrets
import time
import re
from dataclasses import dataclass, field
from typing import Any

from .config import Config
from .contracts import Button, CardAction, CodexClient, CodexEvent, IncomingMessage, Messenger, View
from .store import Store
from .code_reader import redact
from .queries import QueryClient, TOOL_NAMES
from .privacy import dms_request_allowed, privacy_request_denied, redact_private_text
from .access import ACCESS_COMMANDS, AccessRequests

log = logging.getLogger(__name__)

READ_ONLY_POLICY = "readonly:v2"
POLICY_MIGRATION_REASON = "查询策略已更新，旧会话排队任务已取消；历史记录保留，请重新发送业务问题"
PRIVACY_REFUSAL = "无法查询或提供个人隐私数据。请改为不含个人标识的业务规则、状态或汇总问题。"
_BUSINESS_DATA_TOOLS = frozenset({"business_data_checks", "business_data_check"})
_CONTEXT_FIELDS = frozenset({"context", "actor", "job_id", "thread_id", "turn_id", "question", "policy_version"})

HELP = """直接发消息即可开始或继续当前对话。同一聊天中的每位用户有独立会话。

/new — 开启新会话（旧会话保存在 Codex）
/status — 查看执行和排队状态
/stop — 停止当前任务，并取消本会话排队任务
/resume — 检查上次中断的任务后，恢复暂停的会话队列
/last — 重新查看最近一次结果
/approve 编号 — 允许指定的本次操作
/deny 编号 — 拒绝指定的本次操作
/answer 编号 答案 — 回答 Codex 的问题
/help — 查看帮助

任务进行中收到的新消息会排队。操作确认也可直接点击卡片按钮。"""


def split_text(text: str, limit: int = 6000) -> list[str]:
    """Keep all text, preferring paragraph boundaries for Feishu card limits."""
    if not text:
        return ["（没有文字输出）"]
    parts = []
    while len(text) > limit:
        cut = text.rfind("\n", limit // 2, limit)
        if cut < 0:
            cut = limit
        else:
            cut += 1
        parts.append(text[:cut])
        text = text[cut:]
    if text:
        parts.append(text)
    return parts


@dataclass
class Active:
    job: Any
    thread_id: str
    done: asyncio.Future[CodexEvent]
    turn_id: str = ""
    text: str = ""
    activity: str = "正在启动任务"
    stopping: bool = False
    tool_calls: int = 0
    data_checks_allowed: bool = True
    turn_ready: asyncio.Event = field(default_factory=asyncio.Event)


@dataclass
class Ticket:
    id: str
    event: CodexEvent
    active: Active
    card_id: str = ""
    consumed: bool = False
    expiry: asyncio.Task[None] | None = None


class Bridge:
    def __init__(self, config: Config, store: Store, messenger: Messenger, codex: CodexClient,
                 queries: QueryClient | None = None):
        self.config, self.store, self.messenger, self.codex = config, store, messenger, codex
        self.allowed = set(config.allowed_users)
        self.queries = queries
        self.access = AccessRequests(store, messenger) if config.read_only else None
        self._control_times: dict[str, float] = {}
        if config.read_only:
            if queries is None:
                raise ValueError("Read-only mode requires a query broker")
            codex.tool_handler = self._query_tool
        self.active: dict[str, Active] = {}
        self.busy: set[str] = set()
        self.blocked: set[str] = set()
        self._stop_starting: set[str] = set()
        self.tickets: dict[str, Ticket] = {}
        self.tasks: set[asyncio.Task[Any]] = set()
        self.acknowledgements: dict[str, asyncio.Task[None]] = {}
        self._reaction_cleanup_wakeup = asyncio.Event()
        self.events: asyncio.Queue[CodexEvent] = asyncio.Queue()
        self.wakeup = asyncio.Event()
        self.control_wakeup = asyncio.Event()
        self._state_lock = asyncio.Lock()
        self.idle = asyncio.Event()
        self.idle.set()
        self.closed = False
        self.connected = False
        self.fatal = asyncio.Event()
        self._unregister = codex.on_event(self.events.put_nowait)

    def _background(self, coro: Any, *, critical: bool = False) -> asyncio.Task[Any]:
        task = asyncio.create_task(coro)
        self.tasks.add(task)
        def finished(done: asyncio.Task[Any]) -> None:
            self.tasks.discard(done)
            if not done.cancelled() and done.exception():
                # Exceptions can include request metadata; never log raw SDK errors.
                log.error("后台操作失败 (%s)", type(done.exception()).__name__)
                if critical:
                    self.fatal.set()
        task.add_done_callback(finished)
        return task

    async def start(self) -> None:
        self.allowed.update(await self.store.owners())
        if self.config.read_only:
            for owner in await self.store.owners():
                await self.store.ensure_member(owner, "admin")
            for member in self.config.allowed_users:
                await self.store.ensure_member(member)
            if not any(x["enabled"] and x["role"] == "admin" for x in await self.store.members()):
                raise RuntimeError("只读模式需先在本地创建管理员")
            await self.queries.check()
        elif not self.allowed:
            raise RuntimeError("尚未配对用户，请先运行 pair 命令")
        if not await self.store.acquire_service_lock():
            raise RuntimeError("已有桥接服务连接此数据库，请勿重复启动")
        recovered = await self.store.recover_interrupted()
        recovered_controls = await self.store.recover_controls()
        retired = (await self.store.retire_legacy_jobs(READ_ONLY_POLICY + ":", POLICY_MIGRATION_REASON)
                   if self.config.read_only else [])
        self.blocked = await self.store.blocked_session_keys()
        await self.codex.start()
        self.connected = True
        self._background(self._event_worker(), critical=True)
        await self.messenger.start(self.receive, self.receive_action)
        if self.access:
            self._background(self.access.run(), critical=True)
        self._background(self._reaction_cleanup_worker())
        self._background(self._restore_reactions(), critical=True)
        legacy_keys = await self._close_legacy_sessions(recovered, retired) if self.config.read_only else set()
        for job in recovered:
            if job.session_key in legacy_keys:
                continue
            self._background(self._notice(job.message, "上次任务被中断",
                "服务上次退出时任务尚未确认完成，未自动重跑。请检查项目状态，使用 /stop 停止仍在运行的任务，再 /resume 恢复排队。"))
        for message in recovered_controls:
            self._background(self._notice(message, "上次命令状态未确认",
                "服务在处理此命令时退出，未自动重放。请查看 /status，并按需要重新发送命令。"))
        self._background(self._scheduler(), critical=True)
        self._background(self._control_worker(), critical=True)
        if self.config.read_only:
            self._background(self._revocation_worker(), critical=True)
        self.wakeup.set()
        self.control_wakeup.set()

    async def _close_legacy_sessions(self, recovered: list, retired: list) -> set[str]:
        """Retain history while retiring old policy queues and active turns.

        Read/interrupt the existing thread only; never resume it with old tools.
        Combine running and queued recovery into one notice per legacy session.
        """
        affected = {job.session_key: job for job in [*recovered, *retired]
                    if job.session_key != self._key(job.message)}
        for key, job in affected.items():
            uncertain = False
            try:
                session = await self.store.get_session(key)
                if session and session.thread_id:
                    turn = await asyncio.wait_for(self.codex.active_turn(session.thread_id), 5)
                    if turn:
                        await asyncio.wait_for(self.codex.interrupt(session.thread_id, turn), 5)
            except Exception:
                uncertain = True
                log.warning("旧策略会话的远端状态未确认，需要管理员检查")
            text = POLICY_MIGRATION_REASON + "。"
            if uncertain:
                text += "无法确认宿主机上的旧任务状态，请联系管理员检查。"
            await self._audit(job.message.sender_id, "policy.migrate", decision="retired", job_id=job.id)
            self._background(self._notice(job.message, "查询策略已更新", text))
        return set(affected)

    def _key(self, message: IncomingMessage) -> str:
        prefix = READ_ONLY_POLICY + ":" if self.config.read_only else ""
        return f"{prefix}{message.chat_id}:{message.sender_id}"

    async def _authorized(self, sender: str) -> bool:
        if not self.config.read_only:
            return sender in self.allowed
        member = await self.store.member(sender)
        return bool(member and member["enabled"])

    async def _audit(self, sender: str, action: str, resource: str = "", decision: str = "allow",
                     job_id: str | None = None) -> None:
        if self.config.read_only:
            await self.store.audit(sender, action, resource, decision, job_id)

    async def receive(self, message: IncomingMessage) -> None:
        """Only persist/validate here: Feishu needs its event ACK in <3 seconds."""
        if self.closed:
            return
        authorized = await self._authorized(message.sender_id)
        if self.access:
            handled, reply = await self.access.receive(message, authorized=authorized)
            if handled:
                if reply:
                    self._background(self.access.reply(message, reply))
                return
        if not authorized:
            return
        if message.chat_type == "group" and (self.config.read_only or not self.config.allow_groups):
            return
        key = self._key(message)
        if self.config.read_only and len(message.text) > 16000:
            self._background(self._notice(message, "消息过长", "请将问题缩短至16000字以内。"))
            return
        if self.config.read_only and privacy_request_denied(message.text):
            await self._audit(message.sender_id, "privacy.request", decision="deny", job_id=message.id)
            self._background(self._notice(message, "无法提供此类数据", PRIVACY_REFUSAL))
            return
        await self.store.session(key, message.chat_id, message.sender_id)
        if message.text.strip().startswith("/"):
            if self.config.read_only:
                limit = 4000 if message.text.strip().split()[0].lower() == "/answer" else 512
                if len(message.text) > limit:
                    self._background(self._notice(message, "命令过长", f"此命令最多{limit}字。"))
                    return
                now = time.monotonic()
                if now - self._control_times.get(message.sender_id, -10) < 0.5:
                    return
                self._control_times[message.sender_id] = now
            queued = (await self.store.enqueue_control(message, max_pending=5) if self.config.read_only
                      else await self.store.enqueue_control(message))
            if queued:
                self.control_wakeup.set()
            return
        if self.config.read_only:
            admitted = await self.store.admission(key, message, max_pending=self.config.max_pending_per_user,
                daily_limit=self.config.daily_task_limit, min_interval_seconds=self.config.min_interval_seconds)
            if admitted == "duplicate":
                return
            await self._audit(message.sender_id, "task.admission", decision=admitted, job_id=message.id)
            if admitted != "accepted":
                reasons = {"queue_full": "正在处理或排队的任务过多，请等待现有任务完成。",
                           "daily_limit": "已达到过去24小时的任务上限。", "rate_limit": "消息过于频繁，请稍后再试。",
                           "disabled": "当前账户不可用。"}
                self._background(self._notice(message, "暂未接收任务", reasons.get(admitted, "暂时无法接收任务。")))
                return
            accepted = True
        else:
            accepted = await self.store.enqueue(key, message)
        if accepted:
            self._acknowledgement(message)
            self.wakeup.set()
            if key in self.blocked:
                self._background(self._notice(message, "会话已暂停", "消息已保存。请检查上次中断的任务，再 /resume 恢复队列。"))

    def _acknowledgement(self, message: IncomingMessage) -> asyncio.Task[None]:
        """Share the pending HTTP request between the inbox and the job worker."""
        if message.id not in self.acknowledgements:
            task = self._background(self._acknowledge(message))
            self.acknowledgements[message.id] = task
            task.add_done_callback(lambda _: self.acknowledgements.pop(message.id, None))
        return self.acknowledgements[message.id]

    async def _acknowledge(self, message: IncomingMessage) -> None:
        try:
            if not await self.store.get_reaction(message.id):
                reaction_id = await self.messenger.add_reaction(message.id, "OneSecond")
                try:
                    await self.store.save_reaction(message.id, reaction_id)
                except Exception:
                    # Compensate a failed persistence write while we still have the ID.
                    await self.messenger.remove_reaction(message.id, reaction_id)
                    raise
        except Exception:
            # A cosmetic acknowledgement must not lose or block accepted work.
            log.warning("稍等表情添加失败；请检查连接及 im:message.reactions:write_only 权限，任务将继续")
        # /stop may have cancelled a queued job during the HTTP request.
        self._reaction_cleanup_wakeup.set()

    async def _restore_reactions(self) -> None:
        for job in await self.store.next_jobs():
            self._acknowledgement(job.message)
        self._reaction_cleanup_wakeup.set()

    async def _reaction_cleanup_worker(self) -> None:
        while not self.closed:
            await self._reaction_cleanup_wakeup.wait()
            self._reaction_cleanup_wakeup.clear()
            if not self.closed:
                await self._clear_finished_reactions()

    async def _clear_finished_reactions(self) -> None:
        try:
            reactions = await self.store.finished_reactions()
            for message_id, reaction_id in reactions:
                try:
                    await self.messenger.remove_reaction(message_id, reaction_id)
                    await self.store.clear_reaction(message_id, reaction_id)
                except Exception:
                    # Keep the ID so another completion or restart can retry.
                    log.warning("稍等表情移除失败，将在后续任务结束或重启时重试")
        except Exception:
            log.warning("无法读取待清理表情，将在后续任务结束或重启时重试")

    async def receive_action(self, action: CardAction) -> None:
        if self.closed:
            return
        if self.access and action.ticket_id.startswith("access-test:"):
            decision = await self.access.self_test.decide(action)
            if decision:
                self._background(self.access.self_test.deliver_result(decision))
            return
        if self.access and action.ticket_id.startswith("access:"):
            await self.access.receive_action(action)
            return
        ticket = self.tickets.get(action.ticket_id)
        if not ticket or ticket.consumed or not await self._authorized(action.sender_id):
            return
        message = ticket.active.job.message
        if action.sender_id != message.sender_id:
            return
        if action.chat_id and action.chat_id != message.chat_id:
            return
        if action.message_id and action.message_id != ticket.card_id:
            return
        if ticket.event.type != "approval":
            return
        ticket.consumed = True  # one-time gate before asynchronous RPC
        self._background(self._answer_ticket(ticket, decision=action.decision))

    async def _notice(self, message: IncomingMessage, title: str, text: str, kind: str = "info") -> None:
        if self.config.read_only:
            if not await self._authorized(message.sender_id):
                return
            text = redact_private_text(redact(text))[:32000]
        await self.messenger.send(message.chat_id, View(kind, title, text), message.id)

    async def _command(self, message: IncomingMessage) -> None:
        parts = message.text.strip().split(maxsplit=2)
        command, key = parts[0].lower(), self._key(message)
        active = next((x for x in self.active.values() if x.job.session_key == key), None)
        if self.access and command in ACCESS_COMMANDS:
            await self._notice(message, "", await self.access.command(message))
        elif self.config.read_only and command in {"/members", "/member-add", "/member-disable", "/member-enable"}:
            await self._member_command(message, command, parts)
        elif command == "/help":
            help_text = HELP
            if self.config.read_only:
                help_text = "只读业务查询：日志、指标、飞书文档和共享代码。\n" + "\n".join(
                    line for line in HELP.splitlines() if "/approve" not in line and "/deny" not in line)
                help_text += "\n管理员：/members、/member-add ou_…、/member-disable ou_…、/member-enable ou_…"
                help_text += "\n权限申请：未授权用户发送“申请权限”或 /apply；管理员用 /access-requests 查看待办，或点击申请通知的通过/拒绝按钮。"
                help_text += "\n管理员自测：/access-test 给自己发送测试申请卡片，不改变任何成员权限。"
            await self._notice(message, "Codex 使用说明", help_text)
        elif command == "/status":
            session = await self.store.get_session(key)
            queued = await self.store.pending_count(key)
            status = active.activity if active else ("任务正在启动" if key in self.busy else "空闲")
            if key in self.blocked:
                status = "暂停：" + (session.blocked_reason or "上次执行状态待确认")
            await self._notice(message, "会话状态", f"状态：{status}\n排队：{queued}\n会话：{session.thread_id or '尚未创建'}")
        elif command == "/new":
            if key in self.busy or await self.store.pending_count(key):
                await self._notice(message, "当前仍有任务", "请先 /stop 并等待任务停止，再使用 /new。")
            else:
                previous = await self.store.get_session(key)
                if previous.thread_id:
                    try:
                        if await self.codex.active_turn(previous.thread_id):
                            await self._notice(message, "旧会话仍在执行", "请先 /stop 停止宿主机上的旧任务，再使用 /new。")
                            return
                    except Exception:
                        await self._notice(message, "无法确认旧会话状态", "已保留原会话。请恢复 Codex 连接并检查任务，之后再 /new。")
                        return
                await self.store.set_thread(key, None)
                await self.store.set_blocked(key, None)
                self.blocked.discard(key)
                await self._notice(message, "已开启新会话", "下一条消息将创建新的 Codex 会话。")
        elif command == "/resume":
            await self.store.set_blocked(key, None)
            self.blocked.discard(key)
            self.wakeup.set()
            await self._notice(message, "已恢复队列", "将继续原会话；若 Codex 仍有任务运行，会暂停并提示处理。")
        elif command == "/stop":
            cancelled = await self.store.cancel_queued(key)
            self._reaction_cleanup_wakeup.set()
            if active:
                active.stopping = True
                if active.turn_id:
                    await self.codex.interrupt(active.thread_id, active.turn_id)
                await self._notice(message, "已请求停止", f"已取消 {cancelled} 条排队消息，等待当前任务停止。")
            elif key in self.busy:
                # Startup is in flight. A sentinel prevents a later turn/start.
                self._stop_starting.add(key)
                await self._notice(message, "已请求停止", f"已取消 {cancelled} 条排队消息，启动中的任务将停止。")
            else:
                session = await self.store.get_session(key)
                previous_turn = None
                if key in self.blocked and session.thread_id and hasattr(self.codex, "active_turn"):
                    previous_turn = await self.codex.active_turn(session.thread_id)
                if previous_turn:
                    await self.codex.interrupt(session.thread_id, previous_turn)
                    await self._notice(message, "已请求停止上次任务", f"已取消 {cancelled} 条排队消息。停止后可 /resume 或 /new。")
                else:
                    await self._notice(message, "已停止", f"当前没有执行中的任务；已取消 {cancelled} 条排队消息。")
        elif command == "/last":
            result = await self.store.last_result(key)
            if result:
                await self._send_parts(message, "", result["text"])
            else:
                await self._notice(message, "暂无结果", "当前聊天尚未保存执行结果。")
        elif command in {"/approve", "/deny", "/answer"}:
            ticket = self.tickets.get(parts[1]) if len(parts) >= 2 else None
            if not ticket or ticket.consumed or ticket.active.job.session_key != key:
                await self._notice(message, "请求不可用", "编号不存在、已处理或已过期。请使用当前会话中最新的卡片。")
                return
            if command == "/answer" and ticket.event.type == "question":
                if len(parts) < 3:
                    await self._notice(message, "需要答案", f"用法：/answer {ticket.id} 你的答案")
                    return
                try:
                    answers = self._parse_answers(ticket, parts[2])
                except ValueError as exc:
                    await self._notice(message, "答案格式不正确", str(exc))
                    return
                ticket.consumed = True
                await self._answer_ticket(ticket, answers=answers)
            elif command != "/answer" and ticket.event.type == "approval":
                ticket.consumed = True
                await self._answer_ticket(ticket, decision="accept" if command == "/approve" else "decline")
            else:
                await self._notice(message, "命令类型不匹配", "操作确认请用 /approve 或 /deny；问题请用 /answer。")
        else:
            await self._notice(message, "未知命令", "输入 /help 查看可用命令。")

    async def _member_command(self, message: IncomingMessage, command: str, parts: list[str]) -> None:
        actor = await self.store.member(message.sender_id)
        if not actor or not actor["enabled"] or actor["role"] != "admin" or message.chat_type != "p2p":
            await self._audit(message.sender_id, "members.manage", decision="deny")
            await self._notice(message, "无管理权限", "成员管理仅限管理员私聊操作。")
            return
        if command == "/members":
            rows = await self.store.members()
            text = "\n".join(f"{x['open_id']}  {x['role']}  {'启用' if x['enabled'] else '停用'}" for x in rows)
            await self._notice(message, "共同查询成员", text or "暂无成员")
            return
        if len(parts) != 2 or not re.fullmatch(r"ou_[A-Za-z0-9_-]{3,128}", parts[1]):
            await self._notice(message, "需要成员 ID", f"用法：{command} ou_…（须为此机器人应用下的 open_id）")
            return
        target = parts[1]
        existing = await self.store.member(target)
        # Admin provisioning/revocation is intentionally a trusted local operation.
        if existing and existing["role"] == "admin":
            await self._notice(message, "需本地管理", "管理员账号须由宿主机管理员管理。")
            return
        await self._audit(message.sender_id, command[1:], target, "requested")
        if command == "/member-add":
            await self.store.ensure_member(target)
        elif existing:
            await self.store.set_member_enabled(target, command == "/member-enable")
        else:
            await self._notice(message, "成员不存在", "请先添加成员。")
            return
        await self._audit(message.sender_id, command[1:], target, "complete")
        await self._revoke_active()
        self._reaction_cleanup_wakeup.set()
        state = await self.store.member(target)
        await self._notice(message, "成员已更新", f"{target}：{'启用' if state['enabled'] else '停用'}")

    async def _revoke_active(self) -> None:
        for active in list(self.active.values()):
            if not await self._authorized(active.job.message.sender_id):
                active.stopping = True
                await self._clear_tickets(active)
                if active.turn_id and not active.done.done():
                    await self.codex.interrupt(active.thread_id, active.turn_id)

    async def _revocation_worker(self) -> None:
        while not self.closed:
            await self._revoke_active()
            await asyncio.sleep(2)

    async def _query_tool(self, thread_id: str, turn_id: str, tool: str, arguments: dict) -> dict:
        active = self.active.get(thread_id)
        if active and not active.turn_id and not active.done.done():
            try:
                await asyncio.wait_for(active.turn_ready.wait(), 5)
            except TimeoutError:
                return {"ok": False, "error": "任务尚未就绪。"}
        if (not self.config.read_only or not active or not turn_id or active.turn_id != turn_id
                or active.stopping or active.done.done() or self.closed
                or tool not in TOOL_NAMES or not isinstance(arguments, dict)):
            return {"ok": False, "error": "查询请求不属于当前有效任务。"}
        actor = active.job.message.sender_id
        if not await self._authorized(actor):
            return {"ok": False, "error": "成员权限已停用。"}
        if privacy_request_denied(active.job.message.text):
            await self._audit(actor, "privacy.query", tool, "deny", active.job.id)
            return {"ok": False, "error": PRIVACY_REFUSAL}
        if arguments.keys() & _CONTEXT_FIELDS:
            return {"ok": False, "error": "查询参数不能指定任务身份或授权上下文。"}
        if tool in _BUSINESS_DATA_TOOLS and (
                not active.data_checks_allowed or not dms_request_allowed(active.job.message.text)):
            await self._audit(actor, "data.request", tool, "deny", active.job.id)
            return {"ok": False, "error": "指定数据工具、提供SQL或跳过代码不会授权生产查询；请先描述具体业务问题。"}
        if active.tool_calls >= self.config.max_tool_calls:
            await self._audit(actor, "query.budget", tool, "deny", active.job.id)
            return {"ok": False, "error": "本任务的查询次数已用完，请根据已有证据回答或缩小范围重新提问。"}
        active.tool_calls += 1  # Reserve before yielding, including concurrent calls.
        await self._audit(actor, "query.start", tool, "allow", active.job.id)
        if (active.stopping or active.done.done() or not await self._authorized(actor)
                or (tool in _BUSINESS_DATA_TOOLS and not active.data_checks_allowed)):
            return {"ok": False, "error": "任务已停止或查询权限已撤销。"}
        context = {"actor": actor, "job_id": active.job.id, "thread_id": thread_id,
                   "turn_id": turn_id, "question": active.job.message.text,
                   "policy_version": READ_ONLY_POLICY}
        result = await self.queries.call(tool, arguments, context=context)
        if (active.stopping or active.done.done() or self.active.get(thread_id) is not active
                or not await self._authorized(actor)
                or (tool in _BUSINESS_DATA_TOOLS and not active.data_checks_allowed)):
            await self._audit(actor, "query.result", tool, "revoked", active.job.id)
            return {"ok": False, "error": "任务已停止或查询权限已撤销。"}
        await self._audit(actor, "query.result", tool, "allow" if result.get("ok") else "error", active.job.id)
        return result

    @staticmethod
    def _parse_answers(ticket: Ticket, text: str) -> dict[str, list[str]]:
        questions = ticket.event.questions
        if len(questions) == 1:
            return {questions[0].id: [text]}
        try:
            values = json.loads(text)
        except ValueError as exc:
            raise ValueError('多个问题请用 JSON 回答，例如 {"question_id":"答案"}。') from exc
        if not isinstance(values, dict) or set(values) != {q.id for q in questions}:
            raise ValueError("请使用每个问题的 ID 作为 JSON 字段，并回答所有问题。")
        answers = {}
        for key, value in values.items():
            if not isinstance(value, str) or not value.strip():
                raise ValueError("每个答案必须是非空字符串。")
            answers[key] = [value]
        return answers

    async def _scheduler(self) -> None:
        while not self.closed:
            await self.wakeup.wait()
            self.wakeup.clear()
            if self.closed:
                return
            if not self.connected:
                continue
            jobs = (await self.store.next_jobs(eligible_only=True) if self.config.read_only
                    else await self.store.next_jobs())
            for job in jobs:
                if self.config.read_only and (job.session_key != self._key(job.message)
                        or not await self._authorized(job.message.sender_id)):
                    await self.store.cancel_queued(job.session_key)
                    self._reaction_cleanup_wakeup.set()
                    continue
                if len(self.busy) >= self.config.max_concurrent:
                    break
                if job.session_key in self.busy or job.session_key in self.blocked:
                    continue
                async with self._state_lock:
                    if job.session_key in self.busy or job.session_key in self.blocked:
                        continue
                    if await self.store.start_job(job.id):
                        self.busy.add(job.session_key)
                        self.idle.clear()
                        self._background(self._run_job(job))

    async def _control_worker(self) -> None:
        while not self.closed:
            await self.control_wakeup.wait()
            self.control_wakeup.clear()
            if self.closed:
                return
            controls = await self.store.next_controls()
            for message in controls:
                if not await self.store.start_control(message.id):
                    continue
                try:
                    async with self._state_lock:
                        if await self._authorized(message.sender_id):
                            await self._command(message)
                            await self._audit(message.sender_id, "command", message.text.split()[0], job_id=message.id)
                except Exception as exc:
                    await self.store.finish_control(message.id, type(exc).__name__)
                    log.error("控制命令未完成 (%s)", type(exc).__name__)
                    try:
                        await self._notice(message, "命令未完成", "请检查连接状态后重新发送命令。")
                    except Exception:
                        pass
                else:
                    await self.store.finish_control(message.id)
            if len(controls) >= 100:
                self.control_wakeup.set()

    async def _run_job(self, job: Any) -> None:
        active: Active | None = None
        persisted_result = False
        try:
            # Recheck durable jobs accepted before a policy change, before any
            # model request or provider lookup. This also covers queue recovery.
            if self.config.read_only and privacy_request_denied(job.message.text):
                await self.store.finish_job(job.id, "privacy_denied")
                await self._audit(job.message.sender_id, "privacy.queued", decision="deny", job_id=job.id)
                await self._notice(job.message, "无法提供此类数据", PRIVACY_REFUSAL)
                return
            # Do not hold up Feishu's event ACK; wait here before starting Codex.
            await asyncio.shield(self._acknowledgement(job.message))
            if self.closed:
                return
            if not await self._authorized(job.message.sender_id):
                await self.store.finish_job(job.id, "member_disabled")
                return
            session = await self.store.get_session(job.session_key)
            if session.thread_id:
                thread_id = session.thread_id
                await self.codex.resume_thread(thread_id, self.config.workspace)
            else:
                thread_id = await self.codex.create_thread(self.config.workspace)
                await self.store.set_thread(job.session_key, thread_id)
            active = Active(job, thread_id, asyncio.get_running_loop().create_future())
            self.active[thread_id] = active
            if job.session_key in self._stop_starting or not await self._authorized(job.message.sender_id):
                active.done.set_result(CodexEvent("completed", thread_id=thread_id, status="interrupted"))
            else:
                active.turn_id = await self.codex.start_turn(thread_id, job.message.text)
                active.turn_ready.set()
                if active.stopping:
                    await self.codex.interrupt(thread_id, active.turn_id)
            result = await asyncio.wait_for(asyncio.shield(active.done), self.config.task_timeout)
            text = result.text or active.text or "（没有文字输出）"
            if result.status != "completed":
                text = f"任务{'已停止' if result.status == 'interrupted' else '未完成'}。\n{result.error}\n\n{text}"
            if self.config.read_only:
                text = redact_private_text(redact(text))[:32000]
            await self.store.save_result(job.session_key, text, result.status)
            persisted_result = True
            if result.status == "connection_lost":
                await self._block(job.session_key, "Codex 连接中断，检查项目状态后 /resume")
            await self.store.finish_job(job.id, None if result.status == "completed" else result.status)
            await self._audit(job.message.sender_id, "task.finish", decision=result.status, job_id=job.id)
            await self._clear_tickets(active)
            await self._send_parts(job.message, "" if result.status == "completed" else "任务已结束",
                                   text, kind="result" if result.status == "completed" else "error")
        except asyncio.CancelledError:
            # Leave durable running state for restart recovery; never auto-replay it.
            raise
        except Exception as exc:
            log.error("任务失败 (%s)", type(exc).__name__)
            if persisted_result:
                log.error("结果发送失败；完整结果保存在 PostgreSQL，可用 /last 获取")
                return
            reason = "连接或执行失败，任务结果尚未确认。不会自动重复执行；请检查项目状态后继续。"
            if isinstance(exc, asyncio.TimeoutError):
                reason = "任务超过配置的时限，已请求停止。请检查项目状态后继续。"
            if active:
                if active.turn_id and self.connected and not active.done.done():
                    try:
                        await self.codex.interrupt(active.thread_id, active.turn_id)
                    except Exception:
                        pass
                await self._clear_tickets(active)
            await self.store.finish_job(job.id, reason)
            await self._block(job.session_key, reason)
            failure = reason + ("\n\n" + active.text if active else "")
            await self.store.save_result(job.session_key,
                redact_private_text(redact(failure))[:32000] if self.config.read_only else failure, "failed")
            try:
                await self._notice(job.message, "任务未完成", reason, "error")
            except Exception:
                log.error("结果发送失败；结果保存在 PostgreSQL，可用 /last 获取")
        finally:
            self._reaction_cleanup_wakeup.set()
            if active:
                self.active.pop(active.thread_id, None)
            self.busy.discard(job.session_key)
            if not self.busy:
                self.idle.set()
            self._stop_starting.discard(job.session_key)
            self.wakeup.set()

    async def _send_parts(self, message: IncomingMessage, title: str, text: str,
                          kind: str = "result") -> None:
        parts = split_text(redact_private_text(redact(text))[:32000] if self.config.read_only else text)
        for index, part in enumerate(parts):
            if self.config.read_only and not await self._authorized(message.sender_id):
                return
            suffix = f"（{index + 1}/{len(parts)}）" if len(parts) > 1 else ""
            view = View(kind, title + suffix if title else "", part)
            await self.messenger.send(message.chat_id, view, message.id)

    async def _block(self, key: str, reason: str) -> None:
        self.blocked.add(key)
        await self.store.set_blocked(key, reason)

    async def _event_worker(self) -> None:
        while True:
            event = await self.events.get()
            try:
                await self._event(event)
            except Exception as exc:
                log.error("事件处理失败 (%s)", type(exc).__name__)
                active = self.active.get(event.thread_id)
                if active and not active.done.done():
                    try:
                        await self.codex.interrupt(active.thread_id, event.turn_id or active.turn_id)
                    except Exception:
                        pass
                    active.done.set_result(CodexEvent("completed", thread_id=event.thread_id,
                        turn_id=event.turn_id, status="failed", error="无法处理 Codex 的交互请求"))
            finally:
                self.events.task_done()

    async def _event(self, event: CodexEvent) -> None:
        if event.type == "disconnect":
            self.connected = False
            for active in list(self.active.values()):
                if not active.done.done():
                    active.done.set_result(CodexEvent("completed", thread_id=active.thread_id,
                        turn_id=active.turn_id, status="connection_lost", error="Codex 连接中断，执行结果待确认；未自动重跑。"))
            self.fatal.set()  # supervisor restarts a fresh connection, durable queue is retained
            return
        active = self.active.get(event.thread_id)
        if not active or (active.turn_id and event.turn_id and active.turn_id != event.turn_id):
            return
        if event.type == "text":
            active.text = (active.text + event.delta)[:64000] if self.config.read_only else active.text + event.delta
        elif event.type == "activity":
            active.activity = event.text
        elif event.type == "completed":
            if not active.done.done():
                active.done.set_result(event)
        elif event.type in {"approval", "question"}:
            await self._ask(active, event)
        elif event.type == "request_resolved":
            for ticket in list(self.tickets.values()):
                if ticket.event.request_id == event.request_id and ticket.active is active:
                    await self._remove_ticket(ticket, "此请求已由 Codex 关闭。")

    async def _ask(self, active: Active, event: CodexEvent) -> None:
        if event.request_id is None:
            raise ValueError("missing server request id")
        if self.config.read_only and event.type == "approval":
            await self.codex.respond_approval(event.request_id, "decline")
            await self._audit(active.job.message.sender_id, "approval", decision="deny", job_id=active.job.id)
            return
        if self.config.read_only and not await self._authorized(active.job.message.sender_id):
            if active.turn_id:
                await self.codex.interrupt(active.thread_id, active.turn_id)
            return
        if event.type == "question" and any(q.is_secret for q in event.questions):
            # Credentials should be configured locally, not copied into a chat transcript.
            await self._notice(active.job.message, "需要本地配置", "Codex 请求了敏感输入。请在宿主机配置相关凭证后继续。")
            await self.codex.interrupt(active.thread_id, event.turn_id or active.turn_id)
            return
        ticket = Ticket(secrets.token_hex(6), event, active)
        self.tickets[ticket.id] = ticket
        if event.type == "approval":
            details = split_text(event.detail)
            for index, part in enumerate(details[:-1]):
                await self._notice(active.job.message, f"操作详情（{index + 1}/{len(details)}）", part)
            text = details[-1] + f"\n\n编号：{ticket.id}\n也可回复 /approve {ticket.id} 或 /deny {ticket.id}"
            view = View("approval", "Codex 请求操作确认", text, [
                Button("允许本次", ticket.id, "accept"), Button("拒绝", ticket.id, "decline")])
        else:
            lines = []
            for question in event.questions:
                lines.append(f"{question.header} [{question.id}]\n{question.question}")
                lines.extend(f"• {x['label']}：{x.get('description', '')}" for x in question.options)
            if len(event.questions) == 1:
                lines.append(f"回复 /answer {ticket.id} 你的答案")
            else:
                example = json.dumps({q.id: "答案" for q in event.questions}, ensure_ascii=False)
                lines.append(f"回复 /answer {ticket.id} {example}")
            view = View("question", "Codex 需要补充信息", "\n\n".join(lines))
        if self.config.read_only:
            view = View(view.kind, view.title, redact_private_text(redact(view.text))[:32000], view.actions)
            if not await self._authorized(active.job.message.sender_id):
                self.tickets.pop(ticket.id, None)
                return
        ticket.card_id = await self.messenger.send(active.job.message.chat_id, view, active.job.message.id)
        active.activity = "等待你在飞书中回复"
        ticket.expiry = self._background(self._expire(ticket))

    async def _expire(self, ticket: Ticket) -> None:
        await asyncio.sleep(self.config.approval_timeout)
        if ticket.consumed or ticket.id not in self.tickets:
            return
        ticket.consumed = True
        if ticket.event.type == "approval":
            await self._answer_ticket(ticket, decision="decline", label="等待超时，已拒绝本次操作。")
        else:
            await self.codex.interrupt(ticket.active.thread_id, ticket.event.turn_id or ticket.active.turn_id)
            await self._remove_ticket(ticket, "等待超时，已请求停止任务。")

    async def _answer_ticket(self, ticket: Ticket, *, decision: str | None = None,
                              answers: dict[str, list[str]] | None = None, label: str = "") -> None:
        if ticket.id not in self.tickets or not self.connected:
            return
        if self.config.read_only and not await self._authorized(ticket.active.job.message.sender_id):
            await self._remove_ticket(ticket, "成员已停用，此请求不再有效。")
            return
        if self.config.read_only and answers:
            answer_text = "\n".join(value for values in answers.values() for value in values)
            if privacy_request_denied(answer_text):
                ticket.active.data_checks_allowed = False
                ticket.active.stopping = True
                await self._audit(ticket.active.job.message.sender_id, "privacy.answer", decision="deny",
                                  job_id=ticket.active.job.id)
                await self._remove_ticket(ticket, PRIVACY_REFUSAL)
                await self.codex.interrupt(ticket.active.thread_id,
                                           ticket.active.turn_id or ticket.event.turn_id)
                return
            # Clarification can narrow authority, never restore it within a turn.
            ticket.active.data_checks_allowed &= dms_request_allowed(answer_text)
        try:
            if decision:
                await self.codex.respond_approval(ticket.event.request_id, decision)
            else:
                await self.codex.respond_question(ticket.event.request_id, answers or {})
            await self._remove_ticket(ticket, label or ("已允许本次操作。" if decision == "accept" else
                "已拒绝本次操作。" if decision == "decline" else "已提交答案。"))
            ticket.active.activity = "Codex 正在继续"
        except Exception:
            await self._remove_ticket(ticket, "提交未成功，此请求已失效。请查看任务状态。")
            raise

    async def _remove_ticket(self, ticket: Ticket, text: str) -> None:
        self.tickets.pop(ticket.id, None)
        ticket.consumed = True
        if ticket.expiry and ticket.expiry is not asyncio.current_task():
            ticket.expiry.cancel()
        if ticket.card_id:
            try:
                await self.messenger.update(ticket.card_id, View("info", "交互请求已关闭", text))
            except Exception:
                log.warning("旧交互卡更新失败，编号已在服务端失效")

    async def _clear_tickets(self, active: Active) -> None:
        for ticket in list(self.tickets.values()):
            if ticket.active is active:
                await self._remove_ticket(ticket, "任务已结束，此请求不再有效。")

    async def close(self) -> None:
        self.closed = True
        self.wakeup.set()
        self.control_wakeup.set()
        for active in list(self.active.values()):
            if active.turn_id and self.connected:
                try:
                    await asyncio.wait_for(self.codex.interrupt(active.thread_id, active.turn_id), 3)
                except Exception:
                    pass
        # Let acknowledgements finish and persist their IDs before closing HTTP.
        # Cancelling an asyncio.to_thread request cannot cancel its network write.
        acknowledgements = set(self.acknowledgements.values())
        for task in self.tasks - acknowledgements:
            task.cancel()
        await asyncio.gather(*list(self.tasks), return_exceptions=True)
        await self.messenger.close()
        self._unregister()
        await self.codex.close()
