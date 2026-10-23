"""In-memory persistence adapter for routing through the real Bridge interface."""
from __future__ import annotations

import time
import uuid
import re
from dataclasses import asdict, replace

from octopus.store import Session
from octopus.contracts import IncomingMessage
from octopus.topic_store import Route, Topic
from test_readonly_bridge import MembersStore


class TopicMemoryStore(MembersStore):
    def __init__(self):
        super().__init__()
        self.topics: dict[str, Topic] = {}
        self.routes: dict[str, Route] = {}
        self.topic_messages: dict[tuple[str, str], str] = {}
        self.topic_actors: dict[tuple[str, str], tuple[str, str | None]] = {}
        self.topic_pending: dict[tuple[str, str], tuple[float, dict]] = {}
        self.ingress_serial = 0
        self.job_order: dict[str, int] = {}
        self.control_order: dict[str, int] = {}

    async def enqueue(self, session_key, item):
        accepted = await super().enqueue(session_key, item)
        if accepted:
            self.ingress_serial += 1
            self.job_order[item.id] = self.ingress_serial
        return accepted

    async def enqueue_control(self, item, **kwargs):
        accepted = await super().enqueue_control(item, **kwargs)
        if accepted:
            self.ingress_serial += 1
            self.control_order[item.id] = self.ingress_serial
        return accepted

    @staticmethod
    def _same_ingress_scope(first, second):
        return ((first.chat_id, first.sender_id, first.chat_type) ==
                (second.chat_id, second.sender_id, second.chat_type)
                and (first.chat_type != "group" or first.topic_id == second.topic_id))

    @staticmethod
    def _selection_control(message):
        return bool(message.text.strip()) and message.text.strip().split()[0].lower() in {"/new", "/switch"}

    def _scope(self, key, actor=None, message=None):
        scope = self.sessions[key]
        if key in self.topics and self.topics[key].scope_key != key:
            raise ValueError("Child topic used as scope")
        if actor and scope.scope == "personal" and scope.sender_id != actor:
            raise ValueError("Wrong actor")
        if message and (scope.chat_id != message.chat_id or
                        (scope.scope == "topic" and (message.chat_type != "group" or scope.topic_id != message.topic_id)) or
                        (scope.scope == "personal" and (message.chat_type != "p2p" or scope.sender_id != message.sender_id))):
            raise ValueError("Wrong scope")
        return scope

    def _seed(self, key):
        scope = self._scope(key)
        if scope.thread_id and key not in self.topics:
            old = [j for j in self.jobs.values() if j.session_key == key and j.state in {"done", "failed"}]
            self.topics[key] = Topic(key, key, "此前的对话", old[-1].message.text[:1200] if old else "已有会话；主题详情未知")

    async def topic_candidates(self, scope_key, actor_id, limit=8):
        self._scope(scope_key, actor_id)
        self._seed(scope_key)
        current, previous = self.topic_actors.get((scope_key, actor_id), (None, None))
        topics = [replace(topic, data_context_restricted=self.sessions[topic.session_key].data_context_restricted,
                          is_current=topic.session_key == current, is_previous=topic.session_key == previous)
                  for topic in reversed(list(self.topics.values())) if topic.scope_key == scope_key]
        return sorted(topics, key=lambda t: (not t.is_current, not t.is_previous))[:limit]

    async def topic_by_key(self, scope_key, key):
        topic = self.topics.get(key)
        if topic and topic.scope_key == scope_key:
            return replace(topic, data_context_restricted=self.sessions[key].data_context_restricted)
        return None

    async def topic_for_message(self, scope_key, message_id):
        self._seed(scope_key)
        key = self.topic_messages.get((scope_key, message_id))
        if key:
            return key
        job = self.jobs.get(message_id)
        if job and job.state in {"running", "done", "failed"} and await self.topic_by_key(scope_key, job.session_key):
            return job.session_key
        return None

    async def current_topic(self, scope_key, actor_id):
        self._scope(scope_key, actor_id)
        return self.topic_actors.get((scope_key, actor_id), (None, None))[0]

    async def select_topic(self, scope_key, actor_id, key):
        self._scope(scope_key, actor_id)
        if not await self.topic_by_key(scope_key, key):
            raise ValueError("Wrong scope")
        old, previous = self.topic_actors.get((scope_key, actor_id), (None, None))
        self.topic_actors[(scope_key, actor_id)] = (key, old if old != key else previous)
        self.changed.set()

    async def new_topic(self, scope_key, actor_id, title="新问题", summary=""):
        scope = self._scope(scope_key, actor_id)
        key = scope_key + ":subject:" + uuid.uuid4().hex
        self.sessions[key] = Session(key, scope.chat_id, scope.sender_id, None,
                                     scope=scope.scope, topic_id=scope.topic_id)
        self.topics[key] = Topic(key, scope_key, title, summary)
        await self.select_topic(scope_key, actor_id, key)
        self.topic_pending.pop((scope_key, actor_id), None)
        return key

    async def pending_routes(self):
        return [job for job in self.jobs.values() if job.state == "queued" and job.id not in self.routes
                and not any(self.control_states.get(control.id) in {"queued", "running"}
                            and self._selection_control(control)
                            and self._same_ingress_scope(control, job.message)
                            and self.control_order.get(control.id, 0) < self.job_order.get(job.id, 0)
                            for control in self.controls.values())][:100]

    async def topic_control_ready(self, control_id):
        control = self.controls.get(control_id)
        if not control:
            return False
        if not self._selection_control(control):
            return True
        return not any(job.state == "queued" and job.id not in self.routes
                       and self._same_ingress_scope(control, job.message)
                       and self.job_order.get(job.id, 0) < self.control_order.get(control.id, 0)
                       for job in self.jobs.values())

    async def next_routed_jobs(self):
        jobs = [job for job in self.jobs.values() if job.state == "queued" and job.id in self.routes
                and not self.sessions[job.session_key].blocked_reason]
        first_by_topic = {}
        for job in jobs:
            first_by_topic.setdefault(job.session_key, job)
        ranks = {}
        ranked = []
        for job in first_by_topic.values():
            rank = ranks.get(job.message.sender_id, 0)
            ranks[job.message.sender_id] = rank + 1
            ranked.append((rank, job))
        return [job for _, job in sorted(ranked, key=lambda item: item[0])][:100]

    async def cancel_pending_routes(self, scope_key, actor_id):
        self._scope(scope_key, actor_id)
        matches = [job for job in self.jobs.values() if job.state == "queued" and job.session_key == scope_key
                   and job.message.sender_id == actor_id and job.id not in self.routes]
        for job in matches:
            self.jobs[job.id] = replace(job, state="failed", error="任务已取消")
        self.changed.set()
        return len(matches)

    async def cancel_routed_jobs(self, session_key):
        matches = [job for job in self.jobs.values() if job.state == "queued" and job.session_key == session_key
                   and job.id in self.routes]
        for job in matches:
            self.jobs[job.id] = replace(job, state="failed", error="任务已取消")
        self.changed.set()
        return len(matches)

    async def get_route(self, job_id):
        return self.routes.get(job_id)

    async def map_topic_message(self, scope_key, key, message_id):
        if not await self.topic_by_key(scope_key, key):
            raise ValueError("Wrong scope")
        old = self.topic_messages.get((scope_key, message_id))
        if old and old != key:
            raise ValueError("Message already mapped")
        self.topic_messages[(scope_key, message_id)] = key

    async def freeze_route(self, job_id, scope_key, target_key, *, title, summary, prompt, question,
                           data_allowed, action, reason, context_ids=(), image_sources=()):
        if job_id in self.routes:
            if self.routes[job_id].scope_key != scope_key:
                raise ValueError("Wrong scope")
            return self.routes[job_id]
        job = self.jobs.get(job_id)
        if not job or job.state != "queued":
            return None
        self._scope(scope_key, job.message.sender_id, job.message)
        if job.session_key != scope_key:
            raise ValueError("Wrong scope")
        if target_key and not await self.topic_by_key(scope_key, target_key):
            raise ValueError("Wrong scope")
        if not isinstance(image_sources, (tuple, list)) or len(image_sources) > 4:
            raise ValueError("Invalid image sources")
        count, seen = len(job.message.image_keys), {job.id}
        sources = []
        for raw in image_sources:
            source = IncomingMessage(**raw)
            self._scope(scope_key, source.sender_id, source)
            original = self.jobs.get(source.id)
            if (source.id in seen or source.sender_id != job.message.sender_id
                    or source.created_at > job.message.created_at or not source.image_keys
                    or source.attachment_error
                    or any(not re.fullmatch(r"[A-Za-z0-9_-]{1,512}", key) for key in source.image_keys)
                    or not original or original.message != source
                    or (original.session_key != scope_key and not await self.topic_by_key(scope_key, original.session_key))):
                raise ValueError("Invalid image source")
            seen.add(source.id)
            count += len(source.image_keys)
            sources.append(asdict(source))
        if count > 4:
            raise ValueError("Too many images")
        if sources or job.message.image_keys:
            data_allowed = False
        old = self.topic_messages.get((scope_key, job_id))
        if old and old != target_key:
            raise ValueError("Message already mapped")
        key = target_key or await self.new_topic(scope_key, job.message.sender_id, title, summary)
        if not data_allowed:
            await self.restrict_session_data(key)
        allowed = data_allowed and not self.sessions[key].data_context_restricted
        self.topics[key] = replace(self.topics[key], title=title, summary=summary)
        self.jobs[job_id] = replace(job, session_key=key)
        route = Route(job_id, scope_key, key, prompt, question, allowed, action, reason, tuple(sources))
        self.routes[job_id] = route
        await self.map_topic_message(scope_key, key, job_id)
        for identifier in context_ids:
            self.topic_messages.setdefault((scope_key, identifier), key)
        await self.select_topic(scope_key, job.message.sender_id, key)
        self.topic_pending.pop((scope_key, job.message.sender_id), None)
        self.changed.set()
        return route

    async def get_topic_pending(self, scope_key, actor_id):
        self._scope(scope_key, actor_id)
        value = self.topic_pending.get((scope_key, actor_id))
        return value[1] if value and time.time() - value[0] < 1800 else None

    async def defer_topic_route(self, job_id, scope_key, pending):
        job = self.jobs.get(job_id)
        if not job or job.state != "queued" or job_id in self.routes:
            return False
        self._scope(scope_key, job.message.sender_id, job.message)
        if job.session_key != scope_key:
            raise ValueError("Wrong scope")
        self.topic_pending[(scope_key, job.message.sender_id)] = (time.time(), pending)
        self.jobs[job_id] = replace(job, state="done")
        self.changed.set()
        return True

    async def reject_topic_route(self, job_id, error):
        job = self.jobs.get(job_id)
        if not job or job.state != "queued":
            return False
        self.jobs[job_id] = replace(job, state="failed", error=error)
        self.changed.set()
        return True

    async def topic_last_question(self, scope_key, key, actor_id=None):
        self._scope(scope_key, actor_id)
        for job in reversed(list(self.jobs.values())):
            route = self.routes.get(job.id)
            if (route and route.scope_key == scope_key and route.session_key == key
                    and (actor_id is None or job.message.sender_id == actor_id)):
                return {"question": (route.question or job.message.text)[:16000],
                        "data_allowed": route.data_allowed, "message_id": job.id,
                        "has_images": bool(job.message.image_keys or job.message.attachment_error or route.image_sources),
                        "image_sources": ((asdict(job.message),) if job.message.image_keys else ()) + route.image_sources}
        return None
