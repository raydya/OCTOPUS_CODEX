from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Literal, Protocol


@dataclass(frozen=True)
class IncomingMessage:
    id: str
    chat_id: str
    sender_id: str
    text: str
    chat_type: Literal["p2p", "group"]
    created_at: int
    image_keys: tuple[str, ...] = ()
    attachment_error: str = ""
    root_id: str = ""
    parent_id: str = ""
    thread_id: str = ""
    mentioned_bot: bool = False

    def __post_init__(self) -> None:
        # JSONB and listener IPC restore tuples as lists. Old records omit both
        # attachment fields and continue to use their text-only defaults.
        if not isinstance(self.image_keys, (tuple, list)):
            raise ValueError("Image references must be a list or tuple")
        object.__setattr__(self, "image_keys", tuple(self.image_keys))

    @property
    def topic_id(self) -> str:
        # The first message may not have a thread_id until the first reply.
        # Reply events retain the root message ID across that transition.
        return self.root_id or self.id


@dataclass(frozen=True)
class QuotedContext:
    message_id: str
    chat_id: str
    text: str
    created_at: int
    has_unread_images: bool = False


class ReferenceContextError(RuntimeError):
    """The explicitly referenced message cannot be used safely as context."""


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
    async def add_reaction(self, message_id: str, emoji_type: str = "Typing") -> str: ...
    async def remove_reaction(self, message_id: str, reaction_id: str) -> None: ...
    async def download_image(self, message_id: str, image_key: str) -> bytes: ...
    async def fetch_thread_context(self, message: IncomingMessage) -> tuple[IncomingMessage, ...]: ...
    async def fetch_quoted_context(self, message: IncomingMessage) -> QuotedContext | None: ...
    async def close(self) -> None: ...


class CodexClient(Protocol):
    async def start(self) -> None: ...
    async def create_thread(self, cwd: str) -> str: ...
    async def classify_topic(self, cwd: str, payload: dict[str, Any], *, images: tuple[str, ...] = ()) -> dict[str, Any]: ...
    async def resume_thread(self, thread_id: str, cwd: str) -> None: ...
    async def start_turn(self, thread_id: str, text: str, images: tuple[str, ...] = ()) -> str: ...
    async def interrupt(self, thread_id: str, turn_id: str) -> None: ...
    async def active_turn(self, thread_id: str) -> str | None: ...
    async def respond_approval(self, request_id: str | int, decision: str) -> None: ...
    async def respond_question(self, request_id: str | int, answers: dict[str, list[str]]) -> None: ...
    def on_event(self, handler: EventHandler) -> Callable[[], None]: ...
    async def close(self) -> None: ...
