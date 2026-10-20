from __future__ import annotations

from dataclasses import dataclass, field
from typing import Awaitable, Callable, Literal, Protocol


@dataclass(frozen=True)
class IncomingMessage:
    id: str
    chat_id: str
    sender_id: str
    text: str
    chat_type: Literal["p2p", "group"]
    created_at: int


@dataclass(frozen=True)
class CardAction:
    ticket_id: str
    decision: Literal["accept", "decline"]
    sender_id: str
    chat_id: str | None = None
    message_id: str | None = None


@dataclass(frozen=True)
class Button:
    label: str
    ticket_id: str
    decision: Literal["accept", "decline"]


@dataclass(frozen=True)
class View:
    kind: str
    title: str
    text: str
    actions: list[Button] = field(default_factory=list)


@dataclass(frozen=True)
class Question:
    id: str
    header: str
    question: str
    options: list[dict[str, str]] = field(default_factory=list)
    is_other: bool = False
    is_secret: bool = False


@dataclass(frozen=True)
class CodexEvent:
    type: str
    thread_id: str = ""
    turn_id: str = ""
    delta: str = ""
    text: str = ""
    status: str = ""
    error: str = ""
    request_id: str | int | None = None
    kind: str = ""
    detail: str = ""
    questions: list[Question] = field(default_factory=list)


MessageHandler = Callable[[IncomingMessage], Awaitable[None]]
ActionHandler = Callable[[CardAction], Awaitable[None]]
EventHandler = Callable[[CodexEvent], None]


class Messenger(Protocol):
    async def start(self, on_message: MessageHandler, on_action: ActionHandler) -> None: ...
    async def send(self, chat_id: str, view: View, reply_to: str | None = None) -> str: ...
    async def send_user(self, open_id: str, view: View, *, request_id: str | None = None) -> str: ...
    async def update(self, message_id: str, view: View) -> None: ...
    async def add_reaction(self, message_id: str, emoji_type: str = "OneSecond") -> str: ...
    async def remove_reaction(self, message_id: str, reaction_id: str) -> None: ...
    async def close(self) -> None: ...


class CodexClient(Protocol):
    async def start(self) -> None: ...
    async def create_thread(self, cwd: str) -> str: ...
    async def resume_thread(self, thread_id: str, cwd: str) -> None: ...
    async def start_turn(self, thread_id: str, text: str) -> str: ...
    async def interrupt(self, thread_id: str, turn_id: str) -> None: ...
    async def active_turn(self, thread_id: str) -> str | None: ...
    async def respond_approval(self, request_id: str | int, decision: str) -> None: ...
    async def respond_question(self, request_id: str | int, answers: dict[str, list[str]]) -> None: ...
    def on_event(self, handler: EventHandler) -> Callable[[], None]: ...
    async def close(self) -> None: ...
