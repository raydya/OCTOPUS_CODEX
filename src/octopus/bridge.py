from __future__ import annotations

import asyncio
import json
import logging
import secrets
from dataclasses import dataclass, field
from typing import Any

from .config import Config
from .contracts import Button, CardAction, CodexClient, CodexEvent, IncomingMessage, Messenger, View
from .store import Store

log = logging.getLogger(__name__)

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
    card_id: str
    done: asyncio.Future[CodexEvent]
    turn_id: str = ""
    text: str = ""
    activity: str = "正在启动任务"
    dirty: bool = True
    stopping: bool = False
    update_lock: asyncio.Lock = field(default_factory=asyncio.Lock)


@dataclass
class Ticket:
    id: str
    event: CodexEvent
    active: Active
    card_id: str = ""
    consumed: bool = False
    expiry: asyncio.Task[None] | None = None


class Bridge:
    def __init__(self, config: Config, store: Store, messenger: Messenger, codex: CodexClient):
        self.config, self.store, self.messenger, self.codex = config, store, messenger, codex
        self.allowed = set(config.allowed_users)
        self.active: dict[str, Active] = {}
        self.busy: set[str] = set()
        self.blocked: set[str] = set()
        self._stop_starting: set[str] = set()
        self.tickets: dict[str, Ticket] = {}
        self.tasks: set[asyncio.Task[Any]] = set()
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
        if not self.allowed:
            raise RuntimeError("尚未配对用户，请先运行 pair 命令")
        if not await self.store.acquire_service_lock():
            raise RuntimeError("已有桥接服务连接此数据库，请勿重复启动")
        recovered = await self.store.recover_interrupted()
        recovered_controls = await self.store.recover_controls()
        self.blocked = await self.store.blocked_session_keys()
        await self.codex.start()
        self.connected = True
        self._background(self._event_worker(), critical=True)
        await self.messenger.start(self.receive, self.receive_action)
        for job in recovered:
            self._background(self._notice(job.message, "上次任务被中断",
                "服务上次退出时任务尚未确认完成，未自动重跑。请检查项目状态，使用 /stop 停止仍在运行的任务，再 /resume 恢复排队。"))
        for message in recovered_controls:
            self._background(self._notice(message, "上次命令状态未确认",
                "服务在处理此命令时退出，未自动重放。请查看 /status，并按需要重新发送命令。"))
        self._background(self._scheduler(), critical=True)
        self._background(self._control_worker(), critical=True)
        self.wakeup.set()
        self.control_wakeup.set()

    def _key(self, message: IncomingMessage) -> str:
        return f"{message.chat_id}:{message.sender_id}"

    async def receive(self, message: IncomingMessage) -> None:
        """Only persist/validate here: Feishu needs its event ACK in <3 seconds."""
        if self.closed or message.sender_id not in self.allowed:
            return
        if message.chat_type == "group" and not self.config.allow_groups:
            return
        key = self._key(message)
        await self.store.session(key, message.chat_id, message.sender_id)
        if message.text.strip().startswith("/"):
            if await self.store.enqueue_control(message):
                self.control_wakeup.set()
            return
        if await self.store.enqueue(key, message):
            self.wakeup.set()
            if key in self.blocked:
                self._background(self._notice(message, "会话已暂停", "消息已保存。请检查上次中断的任务，再 /resume 恢复队列。"))
            elif key in self.busy:
                self._background(self._notice(message, "已排队", "当前任务结束后，将在同一 Codex 会话中继续。"))

    async def receive_action(self, action: CardAction) -> None:
        ticket = self.tickets.get(action.ticket_id)
        if not ticket or ticket.consumed or action.sender_id not in self.allowed:
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
        await self.messenger.send(message.chat_id, View(kind, title, text), message.id)

    async def _command(self, message: IncomingMessage) -> None:
        parts = message.text.strip().split(maxsplit=2)
        command, key = parts[0].lower(), self._key(message)
        active = next((x for x in self.active.values() if x.job.session_key == key), None)
        if command == "/help":
            await self._notice(message, "Codex 使用说明", HELP)
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
                await self._send_parts(message, "最近一次结果", result["text"])
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
            if not self.connected:
                continue
            for job in await self.store.next_jobs():
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
            for message in await self.store.next_controls():
                if not await self.store.start_control(message.id):
                    continue
                try:
                    async with self._state_lock:
                        await self._command(message)
                except Exception as exc:
                    await self.store.finish_control(message.id, type(exc).__name__)
                    log.error("控制命令未完成 (%s)", type(exc).__name__)
                    try:
                        await self._notice(message, "命令未完成", "请检查连接状态后重新发送命令。")
                    except Exception:
                        pass
                else:
                    await self.store.finish_control(message.id)

    async def _run_job(self, job: Any) -> None:
        active: Active | None = None
        progress: asyncio.Task[None] | None = None
        persisted_result = False
        try:
            card = await self.messenger.send(job.message.chat_id,
                View("progress", "Codex 正在处理", "正在连接会话…"), job.message.id)
            session = await self.store.get_session(job.session_key)
            if session.thread_id:
                thread_id = session.thread_id
                await self.codex.resume_thread(thread_id, self.config.workspace)
            else:
                thread_id = await self.codex.create_thread(self.config.workspace)
                await self.store.set_thread(job.session_key, thread_id)
            active = Active(job, thread_id, card, asyncio.get_running_loop().create_future())
            self.active[thread_id] = active
            if job.session_key in self._stop_starting:
                active.done.set_result(CodexEvent("completed", thread_id=thread_id, status="interrupted"))
            else:
                progress = asyncio.create_task(self._progress(active))
                active.turn_id = await self.codex.start_turn(thread_id, job.message.text)
                if active.stopping:
                    await self.codex.interrupt(thread_id, active.turn_id)
            result = await asyncio.wait_for(asyncio.shield(active.done), self.config.task_timeout)
            text = result.text or active.text or "（没有文字输出）"
            if result.status != "completed":
                text = f"任务{'已停止' if result.status == 'interrupted' else '未完成'}。\n{result.error}\n\n{text}"
            await self.store.save_result(job.session_key, text, result.status)
            persisted_result = True
            if result.status == "connection_lost":
                await self._block(job.session_key, "Codex 连接中断，检查项目状态后 /resume")
            await self.store.finish_job(job.id, None if result.status == "completed" else result.status)
            if progress:
                progress.cancel()
                await asyncio.gather(progress, return_exceptions=True)
            await self._clear_tickets(active)
            await self._send_parts(job.message, "Codex 已完成" if result.status == "completed" else "任务已结束",
                                   text, card_id=card, kind="result" if result.status == "completed" else "error")
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
            await self.store.save_result(job.session_key, reason + ("\n\n" + active.text if active else ""), "failed")
            try:
                await self._notice(job.message, "任务未完成", reason, "error")
            except Exception:
                log.error("结果发送失败；结果保存在 PostgreSQL，可用 /last 获取")
        finally:
            if progress:
                progress.cancel()
                await asyncio.gather(progress, return_exceptions=True)
            if active:
                self.active.pop(active.thread_id, None)
            self.busy.discard(job.session_key)
            if not self.busy:
                self.idle.set()
            self._stop_starting.discard(job.session_key)
            self.wakeup.set()

    async def _send_parts(self, message: IncomingMessage, title: str, text: str,
                          card_id: str | None = None, kind: str = "result") -> None:
        parts = split_text(text)
        for index, part in enumerate(parts):
            suffix = f"（{index + 1}/{len(parts)}）" if len(parts) > 1 else ""
            view = View(kind, title + suffix, part)
            if index == 0 and card_id:
                await self.messenger.update(card_id, view)
            else:
                await self.messenger.send(message.chat_id, view, message.id)

    async def _block(self, key: str, reason: str) -> None:
        self.blocked.add(key)
        await self.store.set_blocked(key, reason)

    async def _progress(self, active: Active) -> None:
        while True:
            await asyncio.sleep(self.config.progress_interval)
            if not active.dirty:
                continue
            active.dirty = False
            preview = active.text[-6000:]
            text = active.activity + ("\n\n" + preview if preview else "")
            try:
                async with active.update_lock:
                    update = asyncio.create_task(self.messenger.update(active.card_id, View("progress", "Codex 正在处理", text)))
                    try:
                        await asyncio.shield(update)
                    except asyncio.CancelledError:
                        # SDK HTTP runs in a thread: cancellation cannot stop it.
                        # Finish this PATCH before allowing the final result PATCH.
                        await asyncio.gather(update, return_exceptions=True)
                        raise
            except Exception:
                active.dirty = True
                log.warning("进度卡更新失败，下一次刷新将重试")

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
            active.text += event.delta
            active.dirty = True
        elif event.type == "activity":
            active.activity = event.text
            active.dirty = True
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
        ticket.card_id = await self.messenger.send(active.job.message.chat_id, view, active.job.message.id)
        active.activity = "等待你在飞书中回复"
        active.dirty = True
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
        try:
            if decision:
                await self.codex.respond_approval(ticket.event.request_id, decision)
            else:
                await self.codex.respond_question(ticket.event.request_id, answers or {})
            await self._remove_ticket(ticket, label or ("已允许本次操作。" if decision == "accept" else
                "已拒绝本次操作。" if decision == "decline" else "已提交答案。"))
            ticket.active.activity = "Codex 正在继续"
            ticket.active.dirty = True
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
        await self.messenger.close()
        for task in list(self.tasks):
            task.cancel()
        await asyncio.gather(*list(self.tasks), return_exceptions=True)
        self._unregister()
        await self.codex.close()
