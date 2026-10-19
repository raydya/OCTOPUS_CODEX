from __future__ import annotations

import asyncio
import re
import unittest
from dataclasses import dataclass, replace
from typing import Any, Callable

from octopus.bridge import Bridge
from octopus.config import Config
from octopus.contracts import CardAction, CodexEvent, IncomingMessage, Question, View
from octopus.store import INTERRUPTED_ERROR, Job, Session


async def until(event: asyncio.Event, predicate: Callable[[], bool]) -> None:
    """Wait on observable fake I/O; never depend on task timing or long sleeps."""
    async def wait() -> None:
        while not predicate():
            event.clear()
            if not predicate():
                await event.wait()
    await asyncio.wait_for(wait(), 2)


def message(identifier: str, text: str = "do work", *, sender: str = "ou_alice",
            chat: str = "oc_shared", kind: str = "p2p") -> IncomingMessage:
    return IncomingMessage(identifier, chat, sender, text, kind, 1_792_368_000_000)


class MemoryStore:
    """In-memory implementation of the persistence boundary, with I/O signals."""
    def __init__(self, owners: tuple[str, ...] = ()):
        self.sessions: dict[str, Session] = {}
        self.jobs: dict[str, Job] = {}
        self.results: dict[str, dict[str, str]] = {}
        self.claimed: set[str] = set()
        self.controls: dict[str, IncomingMessage] = {}
        self.control_states: dict[str, str] = {}
        self.control_errors: dict[str, str | None] = {}
        self.next_jobs_error: Exception | None = None
        self.owner_ids = owners
        self.changed = asyncio.Event()
        self.scanned = asyncio.Event()
        self.scan_count = 0

    async def owners(self) -> list[str]:
        return list(self.owner_ids)

    async def acquire_service_lock(self) -> bool:
        return True

    async def session(self, key: str, chat_id: str, sender_id: str) -> Session:
        if key not in self.sessions:
            self.sessions[key] = Session(key, chat_id, sender_id, None)
        session = self.sessions[key]
        if (session.chat_id, session.sender_id) != (chat_id, sender_id):
            raise ValueError("Session ownership mismatch")
        return session

    async def get_session(self, key: str) -> Session | None:
        return self.sessions.get(key)

    async def set_thread(self, key: str, thread_id: str | None) -> None:
        self.sessions[key] = replace(self.sessions[key], thread_id=thread_id)
        self.changed.set()

    async def set_blocked(self, key: str, reason: str | None) -> None:
        self.sessions[key] = replace(self.sessions[key], blocked_reason=reason)
        self.changed.set()

    async def blocked_session_keys(self) -> set[str]:
        return {key for key, session in self.sessions.items() if session.blocked_reason is not None}

    async def enqueue(self, session_key: str, item: IncomingMessage) -> bool:
        if item.id in self.jobs:
            return False
        self.jobs[item.id] = Job(item.id, session_key, item, "queued", None)
        self.changed.set()
        return True

    async def next_jobs(self) -> list[Job]:
        self.scan_count += 1
        self.scanned.set()
        if self.next_jobs_error is not None:
            raise self.next_jobs_error
        return [job for job in self.jobs.values() if job.state == "queued"]

    async def get_reaction(self, identifier: str) -> str | None:
        return self.jobs[identifier].reaction_id

    async def save_reaction(self, identifier: str, reaction_id: str) -> None:
        self.jobs[identifier] = replace(self.jobs[identifier], reaction_id=reaction_id)
        self.changed.set()

    async def clear_reaction(self, identifier: str, reaction_id: str) -> None:
        if self.jobs[identifier].reaction_id == reaction_id:
            self.jobs[identifier] = replace(self.jobs[identifier], reaction_id=None)
            self.changed.set()

    async def finished_reactions(self) -> list[tuple[str, str]]:
        return [(job.id, job.reaction_id) for job in self.jobs.values()
                if job.state in {"done", "failed"} and job.reaction_id]

    async def start_job(self, identifier: str) -> bool:
        job = self.jobs[identifier]
        if job.state != "queued" or self.sessions[job.session_key].blocked_reason is not None:
            return False
        self.jobs[identifier] = replace(job, state="running")
        self.changed.set()
        return True

    async def finish_job(self, identifier: str, error: str | None = None) -> None:
        job = self.jobs[identifier]
        if job.state == "running":
            self.jobs[identifier] = replace(job, state="done" if error is None else "failed", error=error)
            self.changed.set()

    async def cancel_queued(self, key: str) -> int:
        pending = [job for job in self.jobs.values() if job.session_key == key and job.state == "queued"]
        for job in pending:
            self.jobs[job.id] = replace(job, state="failed", error="任务已取消")
        self.changed.set()
        return len(pending)

    async def recover_interrupted(self) -> list[Job]:
        recovered = []
        for identifier, job in self.jobs.items():
            if job.state == "running":
                updated = replace(job, state="failed", error=INTERRUPTED_ERROR)
                self.jobs[identifier] = updated
                self.sessions[job.session_key] = replace(self.sessions[job.session_key], blocked_reason=INTERRUPTED_ERROR)
                recovered.append(updated)
        self.changed.set()
        return recovered

    async def retire_legacy_jobs(self, current_prefix: str, reason: str) -> list[Job]:
        retired = []
        for identifier, job in self.jobs.items():
            if job.state == "queued" and not job.session_key.startswith(current_prefix):
                updated = replace(job, state="failed", error=reason)
                self.jobs[identifier] = updated
                retired.append(updated)
        self.changed.set()
        return retired

    async def pending_count(self, key: str) -> int:
        return sum(job.session_key == key and job.state == "queued" for job in self.jobs.values())

    async def save_result(self, key: str, text: str, status: str) -> None:
        self.results[key] = {"text": text, "status": status}
        self.changed.set()

    async def last_result(self, key: str) -> dict[str, str] | None:
        return self.results.get(key)

    async def claim_message(self, identifier: str) -> bool:
        if identifier in self.claimed:
            return False
        self.claimed.add(identifier)
        return True

    async def enqueue_control(self, item: IncomingMessage) -> bool:
        if item.id in self.controls:
            return False
        self.controls[item.id] = item
        self.control_states[item.id] = "queued"
        self.control_errors[item.id] = None
        self.changed.set()
        return True

    async def next_controls(self) -> list[IncomingMessage]:
        return [item for identifier, item in self.controls.items() if self.control_states[identifier] == "queued"]

    async def start_control(self, identifier: str) -> bool:
        if self.control_states.get(identifier) != "queued":
            return False
        self.control_states[identifier] = "running"
        self.changed.set()
        return True

    async def finish_control(self, identifier: str, error: str | None = None) -> None:
        if self.control_states.get(identifier) == "running":
            self.control_states[identifier] = "done" if error is None else "failed"
            self.control_errors[identifier] = error
            self.changed.set()

    async def recover_controls(self) -> list[IncomingMessage]:
        interrupted = []
        for identifier, state in self.control_states.items():
            if state == "running":
                self.control_states[identifier] = "failed"
                self.control_errors[identifier] = INTERRUPTED_ERROR
                interrupted.append(self.controls[identifier])
        self.changed.set()
        return interrupted

    async def wait_state(self, identifier: str, state: str) -> None:
        await until(self.changed, lambda: identifier in self.jobs and self.jobs[identifier].state == state)

    async def wait_control(self, identifier: str, state: str) -> None:
        await until(self.changed, lambda: self.control_states.get(identifier) == state)


@dataclass(frozen=True)
class Sent:
    id: str
    chat: str
    view: View
    reply_to: str | None
    operation: str


class FakeMessenger:
    def __init__(self):
        self.records: list[Sent] = []
        self.changed = asyncio.Event()
        self.failed = asyncio.Event()
        self.fail_results = False
        self.fail_reactions = False
        self.reactions: dict[str, str] = {}
        self.reaction_calls: list[tuple[str, str]] = []
        self.removed_reactions: list[tuple[str, str]] = []
        self.reaction_gates: dict[str, asyncio.Event] = {}
        self.reaction_entered: asyncio.Queue[str] = asyncio.Queue()
        self.send_gates: dict[str, asyncio.Event] = {}
        self.send_entered: asyncio.Queue[str] = asyncio.Queue()
        self.closed = False

    async def start(self, on_message: Any, on_action: Any) -> None:
        self.on_message, self.on_action = on_message, on_action

    async def add_reaction(self, message_id: str, emoji_type: str = "OneSecond") -> str:
        self.reaction_calls.append((message_id, emoji_type))
        self.reaction_entered.put_nowait(message_id)
        if message_id in self.reaction_gates:
            await self.reaction_gates[message_id].wait()
        if self.fail_reactions:
            raise RuntimeError("private API error metadata")
        identifier = self.reactions.setdefault(message_id, f"reaction-{message_id}")
        self.changed.set()
        return identifier

    async def remove_reaction(self, message_id: str, reaction_id: str) -> None:
        if self.reactions.get(message_id) == reaction_id:
            del self.reactions[message_id]
        self.removed_reactions.append((message_id, reaction_id))
        self.changed.set()

    async def send(self, chat: str, view: View, reply_to: str | None = None) -> str:
        if self.fail_results and view.kind == "result":
            self.failed.set()
            raise RuntimeError("Simulated outbound delivery failure")
        if reply_to in self.send_gates:
            self.send_entered.put_nowait(reply_to)
            await self.send_gates[reply_to].wait()
        identifier = f"om_out_{len(self.records) + 1}"
        self.records.append(Sent(identifier, chat, view, reply_to, "send"))
        self.changed.set()
        return identifier

    async def update(self, identifier: str, view: View) -> None:
        if self.fail_results and view.kind == "result":
            self.failed.set()
            raise RuntimeError("Simulated outbound delivery failure")
        original = next(record for record in self.records if record.id == identifier)
        self.records.append(Sent(identifier, original.chat, view, original.reply_to, "update"))
        self.changed.set()

    async def wait_record(self, predicate: Callable[[Sent], bool], *, after: int = 0) -> Sent:
        await until(self.changed, lambda: any(predicate(record) for record in self.records[after:]))
        return next(record for record in self.records[after:] if predicate(record))

    async def close(self) -> None:
        self.closed = True


@dataclass(frozen=True)
class Turn:
    thread: str
    id: str
    text: str


class FakeCodex:
    def __init__(self):
        self.turns: list[Turn] = []
        self.started: asyncio.Queue[Turn] = asyncio.Queue()
        self.interrupts: list[tuple[str, str]] = []
        self.interrupted: asyncio.Queue[tuple[str, str]] = asyncio.Queue()
        self.approvals: list[tuple[str | int, str]] = []
        self.approved: asyncio.Queue[tuple[str | int, str]] = asyncio.Queue()
        self.questions: list[tuple[str | int, dict[str, list[str]]]] = []
        self.answered: asyncio.Queue[tuple[str | int, dict[str, list[str]]]] = asyncio.Queue()
        self.resumes: list[str] = []
        self.remote_active_turns: dict[str, str] = {}
        self.active_turn_queries: list[str] = []
        self.active_turn_error: Exception | None = None
        self.thread_count = 0
        self.handler: Callable[[CodexEvent], None] | None = None
        self.closed = False

    def on_event(self, handler: Callable[[CodexEvent], None]) -> Callable[[], None]:
        self.handler = handler
        def unregister() -> None:
            self.handler = None
        return unregister

    async def start(self) -> None:
        pass

    async def create_thread(self, cwd: str) -> str:
        self.thread_count += 1
        return f"thread-{self.thread_count}"

    async def resume_thread(self, thread_id: str, cwd: str) -> None:
        self.resumes.append(thread_id)

    async def active_turn(self, thread_id: str) -> str | None:
        self.active_turn_queries.append(thread_id)
        if self.active_turn_error is not None:
            raise self.active_turn_error
        return self.remote_active_turns.get(thread_id)

    async def start_turn(self, thread_id: str, text: str) -> str:
        turn = Turn(thread_id, f"turn-{len(self.turns) + 1}", text)
        self.turns.append(turn)
        self.remote_active_turns[thread_id] = turn.id
        self.started.put_nowait(turn)
        return turn.id

    async def interrupt(self, thread_id: str, turn_id: str) -> None:
        self.interrupts.append((thread_id, turn_id))
        self.interrupted.put_nowait((thread_id, turn_id))
        if self.remote_active_turns.get(thread_id) == turn_id:
            del self.remote_active_turns[thread_id]
        self.emit(CodexEvent("completed", thread_id=thread_id, turn_id=turn_id, status="interrupted"))

    async def respond_approval(self, request_id: str | int, decision: str) -> None:
        value = (request_id, decision)
        self.approvals.append(value)
        self.approved.put_nowait(value)

    async def respond_question(self, request_id: str | int, answers: dict[str, list[str]]) -> None:
        value = (request_id, answers)
        self.questions.append(value)
        self.answered.put_nowait(value)

    def emit(self, event: CodexEvent) -> None:
        if self.handler is not None:
            self.handler(event)

    def complete(self, turn: Turn, text: str = "done") -> None:
        if self.remote_active_turns.get(turn.thread) == turn.id:
            del self.remote_active_turns[turn.thread]
        self.emit(CodexEvent("completed", thread_id=turn.thread, turn_id=turn.id, status="completed", text=text))

    async def next_turn(self) -> Turn:
        return await asyncio.wait_for(self.started.get(), 2)

    async def close(self) -> None:
        self.closed = True


class BridgeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.store = MemoryStore()
        self.messenger = FakeMessenger()
        self.codex = FakeCodex()
        self.config = Config("cli_test", "test-secret", "unused", "/project", None, None,
                             allowed_users=("ou_alice", "ou_bob"), max_concurrent=2,
                             task_timeout=60, approval_timeout=60)
        self.bridge: Bridge | None = None

    async def start(self) -> Bridge:
        self.bridge = Bridge(self.config, self.store, self.messenger, self.codex)
        await self.bridge.start()
        return self.bridge

    async def asyncTearDown(self):
        if self.bridge is not None:
            await self.bridge.close()

    async def begin(self, identifier: str = "input", text: str = "work") -> Turn:
        await self.bridge.receive(message(identifier, text))
        return await self.codex.next_turn()

    async def ask(self, turn: Turn, event: CodexEvent) -> Sent:
        after = len(self.messenger.records)
        self.codex.emit(event)
        return await self.messenger.wait_record(lambda record: record.view.kind == event.type, after=after)

    async def test_unauthorized_users_and_disabled_group_messages_do_not_enter_queue(self):
        bridge = await self.start()
        await bridge.receive(message("outsider", sender="ou_eve"))
        await bridge.receive(message("group", sender="ou_alice", kind="group"))
        await bridge.receive(message("command", "/last", sender="ou_eve"))
        self.assertEqual(self.store.sessions, {})
        self.assertEqual(self.store.jobs, {})
        self.assertEqual(self.codex.turns, [])
        self.assertEqual(self.messenger.records, [])
        self.assertEqual(self.messenger.reaction_calls, [])

    async def test_owners_in_same_chat_have_independent_threads_and_last_results(self):
        bridge = await self.start()
        await bridge.receive(message("alice", "alice work"))
        await bridge.receive(message("bob", "bob work", sender="ou_bob"))
        turns = [await self.codex.next_turn(), await self.codex.next_turn()]
        by_text = {turn.text: turn for turn in turns}
        self.assertNotEqual(by_text["alice work"].thread, by_text["bob work"].thread)
        self.codex.complete(by_text["alice work"], "private alice result")
        self.codex.complete(by_text["bob work"], "private bob result")
        await self.store.wait_state("alice", "done")
        await self.store.wait_state("bob", "done")
        await bridge.receive(message("last-bob", "/last", sender="ou_bob"))
        reply = await self.messenger.wait_record(lambda record: record.reply_to == "last-bob")
        self.assertEqual(reply.view.text, "private bob result")
        self.assertEqual(len(self.store.sessions), 2)

    async def test_duplicate_delivery_executes_once_even_after_completion(self):
        bridge = await self.start()
        item = message("same-id")
        await bridge.receive(item)
        await bridge.receive(item)
        turn = await self.codex.next_turn()
        self.codex.complete(turn)
        await self.store.wait_state(item.id, "done")
        await bridge.receive(item)
        await bridge.receive(message("barrier", "/status"))
        await self.messenger.wait_record(lambda record: record.reply_to == "barrier")
        self.assertEqual(len(self.codex.turns), 1)
        self.assertEqual(list(self.store.jobs), ["same-id"])
        self.assertEqual(self.messenger.reaction_calls, [("same-id", "OneSecond")])

    async def test_jobs_queue_per_session_while_other_owner_can_run(self):
        bridge = await self.start()
        first = await self.begin("a1", "alice first")
        await bridge.receive(message("a2", "alice second"))
        await bridge.receive(message("b1", "bob first", sender="ou_bob"))
        other = await self.codex.next_turn()
        self.assertEqual(other.text, "bob first")
        self.assertEqual(self.store.jobs["a2"].state, "queued")
        self.codex.complete(first)
        second = await self.codex.next_turn()
        self.assertEqual(second.text, "alice second")
        self.assertEqual(second.thread, first.thread)
        self.assertNotEqual(second.thread, other.thread)
        self.assertIn(first.thread, self.codex.resumes)

    async def test_stop_cancels_queued_work_and_interrupts_active_turn(self):
        bridge = await self.start()
        first = await self.begin("a1")
        await bridge.receive(message("a2", "queued"))
        await bridge.receive(message("stop", "/stop"))
        interrupted = await asyncio.wait_for(self.codex.interrupted.get(), 2)
        self.assertEqual(interrupted, (first.thread, first.id))
        await self.store.wait_state("a1", "failed")
        await self.messenger.wait_record(lambda record: record.reply_to == "stop")
        self.assertEqual(self.store.jobs["a2"].state, "failed")
        self.assertEqual(self.store.jobs["a2"].error, "任务已取消")
        self.assertEqual(len(self.codex.turns), 1)

    async def test_approval_is_bound_to_owner_chat_card_and_can_only_be_used_once(self):
        bridge = await self.start()
        turn = await self.begin()
        card = await self.ask(turn, CodexEvent("approval", thread_id=turn.thread, turn_id=turn.id,
                                              request_id=17, detail="Run the requested command"))
        ticket = card.view.actions[0].ticket_id
        for sender, chat, identifier in [("ou_eve", "oc_shared", card.id),
                                          ("ou_bob", "oc_shared", card.id),
                                          ("ou_alice", "oc_wrong", card.id),
                                          ("ou_alice", "oc_shared", "om_wrong")]:
            await bridge.receive_action(CardAction(ticket, "accept", sender, chat, identifier))
        self.assertEqual(self.codex.approvals, [])
        valid = CardAction(ticket, "accept", "ou_alice", "oc_shared", card.id)
        await asyncio.gather(bridge.receive_action(valid), bridge.receive_action(valid))
        self.assertEqual(await asyncio.wait_for(self.codex.approved.get(), 2), (17, "accept"))
        await self.messenger.wait_record(lambda record: record.id == card.id and record.operation == "update")
        await bridge.receive_action(valid)
        self.assertEqual(self.codex.approvals, [(17, "accept")])

    async def test_question_answers_support_plain_single_and_complete_json_multiple(self):
        bridge = await self.start()
        turn = await self.begin()
        single = await self.ask(turn, CodexEvent("question", thread_id=turn.thread, turn_id=turn.id,
                                                request_id="q-single", questions=[Question("city", "City", "Which city?")]))
        ticket = re.search(r"/answer ([0-9a-f]+)", single.view.text).group(1)
        await bridge.receive(message("wrong-owner", f"/answer {ticket} hidden", sender="ou_bob"))
        await self.messenger.wait_record(lambda record: record.reply_to == "wrong-owner")
        self.assertEqual(self.codex.questions, [])
        await bridge.receive(message("single-answer", f"/answer {ticket} Shanghai"))
        self.assertEqual(await asyncio.wait_for(self.codex.answered.get(), 2), ("q-single", {"city": ["Shanghai"]}))

        multiple = await self.ask(turn, CodexEvent("question", thread_id=turn.thread, turn_id=turn.id,
            request_id="q-multiple", questions=[Question("a", "A", "First?"), Question("b", "B", "Second?")]))
        ticket = re.search(r"/answer ([0-9a-f]+)", multiple.view.text).group(1)
        await bridge.receive(message("incomplete", f'/answer {ticket} {{"a":"one"}}'))
        invalid = await self.messenger.wait_record(lambda record: record.reply_to == "incomplete")
        self.assertEqual(invalid.view.title, "答案格式不正确")
        self.assertEqual(len(self.codex.questions), 1)
        await bridge.receive(message("complete", f'/answer {ticket} {{"a":"one","b":"two"}}'))
        self.assertEqual(await asyncio.wait_for(self.codex.answered.get(), 2),
                         ("q-multiple", {"a": ["one"], "b": ["two"]}))

    async def test_blocked_session_saves_new_messages_but_waits_for_resume(self):
        key = "oc_shared:ou_alice"
        await self.store.session(key, "oc_shared", "ou_alice")
        await self.store.set_thread(key, "existing-thread")
        await self.store.set_blocked(key, "Check the previous task")
        await self.store.enqueue(key, message("pending", "old pending"))
        bridge = await self.start()
        await until(self.store.scanned, lambda: self.store.scan_count > 0)
        self.assertEqual(self.codex.turns, [])
        await bridge.receive(message("new-pending", "new pending"))
        notice = await self.messenger.wait_record(lambda record: record.reply_to == "new-pending")
        self.assertEqual(notice.view.title, "会话已暂停")
        self.assertEqual(await self.store.pending_count(key), 2)
        await bridge.receive(message("resume", "/resume"))
        first = await self.codex.next_turn()
        self.assertEqual(first.text, "old pending")
        self.assertEqual(first.thread, "existing-thread")
        self.codex.complete(first)
        second = await self.codex.next_turn()
        self.assertEqual(second.text, "new pending")

    async def test_failed_result_delivery_does_not_erase_success_and_last_can_retry(self):
        bridge = await self.start()
        turn = await self.begin()
        self.messenger.fail_results = True
        with self.assertLogs("octopus.bridge", level="ERROR"):
            self.codex.complete(turn, "successful complete result")
            await asyncio.wait_for(self.messenger.failed.wait(), 2)
            await self.store.wait_state("input", "done")
        self.assertEqual(self.store.results["oc_shared:ou_alice"],
                         {"text": "successful complete result", "status": "completed"})
        self.messenger.fail_results = False
        await bridge.receive(message("last", "/last"))
        recovered = await self.messenger.wait_record(lambda record: record.reply_to == "last")
        self.assertEqual(recovered.view.text, "successful complete result")
        self.assertEqual(len(self.codex.turns), 1)

    async def test_acknowledgement_precedes_codex_without_delaying_inbox_acceptance(self):
        release = asyncio.Event()
        self.messenger.reaction_gates["input"] = release
        bridge = await self.start()
        await bridge.receive(message("input"))
        self.assertEqual(await asyncio.wait_for(self.messenger.reaction_entered.get(), 2), "input")
        self.assertEqual(self.codex.thread_count, 0)
        self.assertEqual(self.codex.turns, [])
        self.assertEqual(self.messenger.records, [])
        release.set()
        turn = await self.codex.next_turn()
        self.assertEqual(self.messenger.reactions, {"input": "reaction-input"})
        self.assertEqual(self.messenger.reaction_calls, [("input", "OneSecond")])
        self.codex.emit(CodexEvent("activity", thread_id=turn.thread, turn_id=turn.id, text="Working"))
        self.codex.emit(CodexEvent("text", thread_id=turn.thread, turn_id=turn.id, delta="partial answer"))
        await asyncio.wait_for(bridge.events.join(), 2)
        self.assertEqual(self.messenger.records, [])
        await bridge.receive(message("status", "/status"))
        status = await self.messenger.wait_record(lambda record: record.reply_to == "status")
        self.assertIn("Working", status.view.text)
        self.codex.complete(turn, "complete answer")
        result = await self.messenger.wait_record(lambda record: record.view.kind == "result")
        self.assertEqual(result.reply_to, "input")
        self.assertEqual(result.operation, "send")
        self.assertEqual(result.view.text, "complete answer")
        await until(self.messenger.changed, lambda: "input" not in self.messenger.reactions)
        self.assertIsNone(await self.store.get_reaction("input"))

    async def test_queued_messages_react_immediately_and_late_ack_is_removed_after_stop(self):
        release = asyncio.Event()
        self.messenger.reaction_gates["queued"] = release
        bridge = await self.start()
        await self.begin("running")
        self.assertEqual(await self.messenger.reaction_entered.get(), "running")
        await bridge.receive(message("queued"))
        self.assertEqual(await asyncio.wait_for(self.messenger.reaction_entered.get(), 2), "queued")
        self.assertEqual(len(self.codex.turns), 1)
        self.assertEqual(self.messenger.records, [])
        await bridge.receive(message("stop", "/stop"))
        await self.store.wait_control("stop", "done")
        self.assertEqual(self.store.jobs["queued"].state, "failed")
        release.set()
        await until(self.messenger.changed, lambda: ("queued", "reaction-queued") in self.messenger.removed_reactions)
        self.assertIsNone(await self.store.get_reaction("queued"))
        self.assertEqual(len(self.codex.turns), 1)

    async def test_reaction_failure_does_not_fail_task_or_restore_progress_card(self):
        self.messenger.fail_reactions = True
        bridge = await self.start()
        with self.assertLogs("octopus.bridge", level="WARNING") as captured:
            turn = await self.begin()
        self.assertNotIn("private API error", "\n".join(captured.output))
        self.assertEqual(self.messenger.records, [])
        self.codex.complete(turn, "success despite reaction error")
        result = await self.messenger.wait_record(lambda record: record.view.kind == "result")
        self.assertEqual(result.view.text, "success despite reaction error")
        self.assertEqual(self.store.jobs["input"].state, "done")
        self.assertNotIn("oc_shared:ou_alice", bridge.blocked)

    async def test_failed_reaction_persistence_removes_the_untracked_reaction(self):
        original_save = self.store.save_reaction
        async def fail_save(identifier: str, reaction_id: str) -> None:
            raise RuntimeError("database unavailable")
        self.store.save_reaction = fail_save
        await self.start()
        with self.assertLogs("octopus.bridge", level="WARNING"):
            turn = await self.begin()
        self.assertEqual(self.messenger.reactions, {})
        self.assertIn(("input", "reaction-input"), self.messenger.removed_reactions)
        self.assertIsNone(await self.store.get_reaction("input"))
        self.store.save_reaction = original_save
        self.codex.complete(turn)
        await self.messenger.wait_record(lambda record: record.view.kind == "result")

    async def test_slow_cleanup_does_not_delay_new_work_and_can_retry_failed_removal(self):
        key = "oc_shared:ou_alice"
        await self.store.session(key, "oc_shared", "ou_alice")
        await self.store.enqueue(key, message("old"))
        await self.store.start_job("old")
        await self.store.finish_job("old")
        await self.store.save_reaction("old", "reaction-old")
        self.messenger.reactions["old"] = "reaction-old"
        entered, release = asyncio.Event(), asyncio.Event()
        original_remove = self.messenger.remove_reaction
        attempts = 0
        async def remove(identifier: str, reaction_id: str) -> None:
            nonlocal attempts
            if identifier == "old":
                attempts += 1
                if attempts == 1:
                    entered.set()
                    await release.wait()
                    raise RuntimeError("temporary network failure")
            await original_remove(identifier, reaction_id)
        self.messenger.remove_reaction = remove
        bridge = await self.start()
        try:
            await asyncio.wait_for(entered.wait(), 2)
            turn = await self.begin()
            self.assertEqual(self.store.jobs["input"].state, "running")
            self.codex.complete(turn)
            await self.messenger.wait_record(lambda record: record.view.kind == "result")
            await asyncio.wait_for(bridge.idle.wait(), 2)
            with self.assertLogs("octopus.bridge", level="WARNING"):
                release.set()
                await until(self.messenger.changed, lambda: not self.messenger.reactions)
            self.assertGreaterEqual(attempts, 2)
            self.assertIsNone(await self.store.get_reaction("old"))
        finally:
            release.set()

    async def test_restart_cleans_old_reaction_and_reuses_pending_reaction(self):
        key = "oc_shared:ou_alice"
        await self.store.session(key, "oc_shared", "ou_alice")
        for identifier in ("interrupted", "queued"):
            await self.store.enqueue(key, message(identifier))
            await self.store.save_reaction(identifier, f"reaction-{identifier}")
            self.messenger.reactions[identifier] = f"reaction-{identifier}"
        await self.store.start_job("interrupted")
        bridge = await self.start()
        await until(self.messenger.changed, lambda: "interrupted" not in self.messenger.reactions)
        self.assertEqual(self.messenger.reactions, {"queued": "reaction-queued"})
        await bridge.receive(message("resume", "/resume"))
        turn = await self.codex.next_turn()
        self.assertEqual(self.messenger.reaction_calls, [])
        self.codex.complete(turn)
        await until(self.messenger.changed, lambda: not self.messenger.reactions)

    async def test_shutdown_preserves_pending_acknowledgement_for_restart_cleanup(self):
        release = asyncio.Event()
        self.messenger.reaction_gates["input"] = release
        bridge = await self.start()
        await bridge.receive(message("input"))
        await asyncio.wait_for(self.messenger.reaction_entered.get(), 2)
        closing = asyncio.create_task(bridge.close())
        try:
            await asyncio.sleep(0)
            self.assertFalse(closing.done())
            release.set()
            await asyncio.wait_for(closing, 2)
            self.assertEqual(await self.store.get_reaction("input"), "reaction-input")
            self.assertEqual(self.codex.turns, [])
        finally:
            release.set()
            await closing
            self.bridge = None

    async def test_restart_notifies_interrupted_owner_and_does_not_replay_work(self):
        key = "oc_shared:ou_alice"
        await self.store.session(key, "oc_shared", "ou_alice")
        await self.store.set_thread(key, "old-thread")
        await self.store.enqueue(key, message("interrupted", "started before crash"))
        await self.store.start_job("interrupted")
        await self.store.enqueue(key, message("queued", "waiting before crash"))
        await self.start()
        notice = await self.messenger.wait_record(lambda record: record.reply_to == "interrupted")
        self.assertEqual(notice.view.title, "上次任务被中断")
        self.assertIn("/resume", notice.view.text)
        await until(self.store.scanned, lambda: self.store.scan_count > 0)
        self.assertEqual(self.store.jobs["interrupted"].state, "failed")
        self.assertEqual(self.store.jobs["queued"].state, "queued")
        self.assertIsNotNone(self.store.sessions[key].blocked_reason)
        self.assertEqual(self.codex.turns, [])

    async def test_commands_are_durable_before_ack_and_duplicate_commands_execute_once(self):
        bridge = await self.start()
        command = message("help-command", "/help")
        await bridge.receive(command)
        self.assertEqual(self.store.controls[command.id], command)
        self.assertEqual(self.store.control_states[command.id], "queued")
        await bridge.receive(command)
        await self.store.wait_control(command.id, "done")
        await bridge.receive(command)
        self.assertEqual(len([record for record in self.messenger.records if record.reply_to == command.id]), 1)
        self.assertEqual(self.store.jobs, {})

    async def test_control_worker_executes_commands_in_order(self):
        bridge = await self.start()
        release = asyncio.Event()
        self.messenger.send_gates["first-command"] = release
        await bridge.receive(message("first-command", "/help"))
        self.assertEqual(await asyncio.wait_for(self.messenger.send_entered.get(), 2), "first-command")
        await bridge.receive(message("second-command", "/status"))
        self.assertEqual(self.store.control_states["first-command"], "running")
        self.assertEqual(self.store.control_states["second-command"], "queued")
        release.set()
        await self.store.wait_control("second-command", "done")
        self.assertEqual([record.reply_to for record in self.messenger.records], ["first-command", "second-command"])

    async def test_queued_control_survives_restart_and_executes(self):
        key = "oc_shared:ou_alice"
        await self.store.session(key, "oc_shared", "ou_alice")
        await self.store.enqueue_control(message("saved-help", "/help"))
        await self.start()
        await self.store.wait_control("saved-help", "done")
        sent = await self.messenger.wait_record(lambda record: record.reply_to == "saved-help")
        self.assertIn("/stop", sent.view.text)

    async def test_running_control_on_restart_is_not_replayed_and_requests_resend(self):
        key = "oc_shared:ou_alice"
        await self.store.session(key, "oc_shared", "ou_alice")
        await self.store.set_thread(key, "preserve-existing-thread")
        await self.store.enqueue_control(message("uncertain-new", "/new"))
        await self.store.start_control("uncertain-new")
        await self.start()
        notice = await self.messenger.wait_record(lambda record: record.reply_to == "uncertain-new")
        self.assertRegex(notice.view.text, "重发|重新发送")
        self.assertEqual(self.store.control_states["uncertain-new"], "failed")
        self.assertEqual(self.store.sessions[key].thread_id, "preserve-existing-thread")
        self.assertEqual(self.codex.active_turn_queries, [])
        self.assertEqual(self.codex.turns, [])

    async def test_scheduler_database_failure_sets_fatal_instead_of_silently_stalling(self):
        self.store.next_jobs_error = RuntimeError("simulated database failure with private metadata")
        with self.assertLogs("octopus.bridge", level="ERROR") as captured:
            bridge = await self.start()
            await asyncio.wait_for(bridge.fatal.wait(), 2)
        self.assertTrue(bridge.fatal.is_set())
        self.assertEqual(self.codex.turns, [])
        self.assertNotIn("private metadata", "\n".join(captured.output))

    async def test_new_keeps_old_thread_until_remote_active_turn_has_stopped(self):
        key = "oc_shared:ou_alice"
        await self.store.session(key, "oc_shared", "ou_alice")
        await self.store.set_thread(key, "old-thread")
        await self.store.set_blocked(key, "Previous operation needs checking")
        self.codex.remote_active_turns["old-thread"] = "remote-turn"
        bridge = await self.start()
        await bridge.receive(message("new-refused", "/new"))
        notice = await self.messenger.wait_record(lambda record: record.reply_to == "new-refused")
        self.assertIn("仍在执行", notice.view.title)
        self.assertEqual(self.store.sessions[key].thread_id, "old-thread")
        self.assertIsNotNone(self.store.sessions[key].blocked_reason)
        self.assertIn("old-thread", self.codex.active_turn_queries)
        await bridge.receive(message("stop-remote", "/stop"))
        self.assertEqual(await asyncio.wait_for(self.codex.interrupted.get(), 2), ("old-thread", "remote-turn"))
        await bridge.receive(message("new-allowed", "/new"))
        await self.messenger.wait_record(lambda record: record.reply_to == "new-allowed")
        self.assertIsNone(self.store.sessions[key].thread_id)
        self.assertIsNone(self.store.sessions[key].blocked_reason)

    async def test_new_preserves_session_when_remote_state_cannot_be_verified(self):
        key = "oc_shared:ou_alice"
        await self.store.session(key, "oc_shared", "ou_alice")
        await self.store.set_thread(key, "old-thread")
        self.codex.active_turn_error = RuntimeError("remote state unavailable")
        bridge = await self.start()
        await bridge.receive(message("new-unverified", "/new"))
        notice = await self.messenger.wait_record(lambda record: record.reply_to == "new-unverified")
        self.assertIn("无法确认", notice.view.title)
        self.assertEqual(self.store.sessions[key].thread_id, "old-thread")


if __name__ == "__main__":
    unittest.main()
