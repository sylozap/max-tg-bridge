from dataclasses import dataclass, field, replace
from enum import StrEnum
from pathlib import Path


class Platform(StrEnum):
    TELEGRAM = "telegram"
    MAX = "max"

    @property
    def opposite(self) -> "Platform":
        return Platform.MAX if self == Platform.TELEGRAM else Platform.TELEGRAM

    @property
    def title(self) -> str:
        return "Telegram" if self == Platform.TELEGRAM else "Макс"


class Event(StrEnum):
    CREATE = "create"
    EDIT = "edit"
    DELETE = "delete"
    SERVICE = "service"


@dataclass(frozen=True, slots=True)
class Span:
    kind: str
    start: int
    end: int
    value: str | None = None


@dataclass(frozen=True, slots=True)
class RichText:
    text: str = ""
    spans: tuple[Span, ...] = ()

    def __add__(self, other: "RichText") -> "RichText":
        offset = len(self.text)
        return RichText(self.text + other.text, self.spans + tuple(
            replace(span, start=span.start + offset, end=span.end + offset) for span in other.spans
        ))

    def slice(self, start: int, end: int) -> "RichText":
        return RichText(self.text[start:end], tuple(
            replace(span, start=max(span.start, start) - start, end=min(span.end, end) - start)
            for span in self.spans if span.start < end and start < span.end
        ))


@dataclass(frozen=True, slots=True)
class Author:
    id: int | None
    name: str
    is_bot: bool = False


@dataclass(frozen=True, slots=True)
class Reference:
    id: str
    quote: str = ""


@dataclass(frozen=True, slots=True)
class Attachment:
    kind: str
    # API-supplied references and contact data stay in memory only.
    ref: str | None = field(default=None, repr=False)
    name: str | None = None
    size: int | None = None
    duration: int | None = None
    detail: str | None = field(default=None, repr=False)
    token: str | None = field(default=None, repr=False)
    coordinates: tuple[float, float] | None = None
    phone: str | None = field(default=None, repr=False)
    mime: str | None = None
    key: str | None = field(default=None, repr=False)


@dataclass(frozen=True, slots=True)
class Message:
    platform: Platform
    chat: int
    id: str
    author: Author
    content: RichText = field(default_factory=RichText, repr=False)
    attachments: tuple[Attachment, ...] = field(default=(), repr=False)
    event: Event = Event.CREATE
    revision: str = ""
    reply: Reference | None = field(default=None, repr=False)
    forwarded_from: str | None = field(default=None, repr=False)
    album: str | None = None


@dataclass(frozen=True, slots=True)
class Batch:
    messages: tuple[Message, ...]
    cursor: int | None


@dataclass(frozen=True, slots=True)
class Part:
    content: RichText = field(repr=False)
    media: tuple[object, ...] = field(default=(), repr=False)
    mode: str = "text"


@dataclass(frozen=True, slots=True)
class LocalFile:
    attachment: Attachment
    path: Path
