import re
from bisect import bisect_left
from collections.abc import Callable, Iterable, Mapping
from dataclasses import replace
from html import escape
from urllib.parse import urlsplit

from app.core.model import RichText, Span

_STYLE = {"bold", "italic", "underline", "strikethrough", "spoiler"}
_CODE = {"code", "pre"}
_MAX_KINDS = {
    "strong": "bold", "emphasized": "italic", "underline": "underline",
    "strikethrough": "strikethrough", "monospaced": "code",
    "quote": "blockquote", "link": "text_link", "heading": "bold",
    "highlighted": "bold", "user_mention": "mention",
}
_MAX_TAGS = {
    "bold": "b", "italic": "i", "underline": "u", "strikethrough": "s",
    "code": "code", "pre": "pre", "blockquote": "blockquote",
    "expandable_blockquote": "blockquote",
}
_MENTION = re.compile(r"(?<![\w@])@(?=[\w])")
_LANGUAGE = re.compile(r"[A-Za-z0-9_+.-]{1,32}")


def utf16_length(text: str) -> int:
    return len(text.encode("utf-16-le")) // 2


def _offsets(text: str) -> list[int]:
    offsets = [0]
    for char in text:
        offsets.append(offsets[-1] + (2 if ord(char) > 0xFFFF else 1))
    return offsets


def _url(value: str | None) -> str | None:
    if not value or len(value) > 2048 or any(char.isspace() or ord(char) < 32 for char in value):
        return None
    try:
        parts = urlsplit(value)
        if parts.scheme in {"http", "https"} and parts.hostname and parts.username is None:
            return value
        if parts.scheme == "mailto" and parts.path:
            return value
    except ValueError:
        pass
    return None


def _read_spans(
    text: str, fields: Iterable[tuple[str, int, int, str | None]], *, utf16: bool,
) -> RichText:
    offsets = {value: index for index, value in enumerate(_offsets(text))} if utf16 else None
    spans = []
    for kind, start, length, value in fields:
        if not isinstance(start, int) or not isinstance(length, int):
            continue
        end = start + length
        if offsets is not None:
            start, end = offsets.get(start, -1), offsets.get(end, -1)
        if not 0 <= start < end <= len(text):
            continue
        if kind == "text_link":
            value = _url(value)
            if not value:
                continue
        if kind == "code" and "\n" in text[start:end]:
            kind = "pre"
        spans.append(Span(kind, start, end, value))
    return RichText(text, tuple(spans))


def from_telegram(text: str, entities: Iterable) -> RichText:
    allowed = _STYLE | _CODE | {"blockquote", "expandable_blockquote", "text_link", "mention", "text_mention"}
    return _read_spans(
        text,
        ((str(e.type), e.offset, e.length, e.url if e.type == "text_link" else e.language)
         for e in entities if e.type in allowed),
        utf16=True,
    )


def from_max(text: str, markup: Iterable[Mapping], *, utf16: bool = False) -> RichText:
    return _read_spans(
        text,
        ((_MAX_KINDS[m["type"]], m.get("from", -1), m.get("length", 0), m.get("url"))
         for m in markup if m.get("type") in _MAX_KINDS),
        utf16=utf16,
    )


def neutralize_mentions(content: RichText) -> RichText:
    positions = [match.end() for match in _MENTION.finditer(content.text)]
    if not positions:
        return content
    # The zero-width break preserves the visible spelling. Translate span endpoints with it.
    text = _MENTION.sub("@\u200b", content.text)
    spans = tuple(
        replace(span, start=span.start + bisect_left(positions, span.start),
                end=span.end + bisect_left(positions, span.end))
        for span in content.spans
    )
    return RichText(text, spans)


def _compatible(spans: Iterable[Span]) -> list[Span]:
    def priority(span: Span) -> tuple[int, int, int, str]:
        group = 0 if span.kind in _CODE else 1 if span.kind.endswith("blockquote") else 2
        return group, span.start, -span.end, span.kind

    accepted: list[Span] = []
    for span in sorted(set(spans), key=priority):
        valid = True
        for other in accepted:
            if span.end <= other.start or other.end <= span.start:
                continue
            nested = ((span.start <= other.start and other.end <= span.end)
                      or (other.start <= span.start and span.end <= other.end))
            if (not nested or span.kind in _CODE or other.kind in _CODE or span.kind == other.kind
                    or (span.kind not in _STYLE and other.kind not in _STYLE)):
                valid = False
                break
        if valid:
            accepted.append(span)
    return sorted(accepted, key=lambda span: (span.start, -span.end, span.kind))


def telegram_entities(content: RichText) -> list[dict]:
    offsets = _offsets(content.text)
    result = []
    for span in _compatible(s for s in content.spans if s.kind not in {"mention", "text_mention"}):
        entity = {"type": span.kind, "offset": offsets[span.start],
                  "length": offsets[span.end] - offsets[span.start]}
        if span.kind == "text_link":
            if not _url(span.value):
                continue
            entity["url"] = span.value
        if span.kind == "pre" and span.value and _LANGUAGE.fullmatch(span.value):
            entity["language"] = span.value
        result.append(entity)
    return result


def max_html(content: RichText) -> str:
    openings: dict[int, list[str]] = {}
    closings: dict[int, list[str]] = {}
    spans = _compatible(s for s in content.spans if s.kind in _MAX_TAGS or
                        (s.kind == "text_link" and _url(s.value)))
    for span in spans:
        tag = "a" if span.kind == "text_link" else _MAX_TAGS[span.kind]
        opening = f'<a href="{escape(span.value, quote=True)}">' if tag == "a" else f"<{tag}>"
        openings.setdefault(span.start, []).append(opening)
        closings.setdefault(span.end, []).append(f"</{tag}>")
    output = []
    start = 0
    for position in sorted(openings.keys() | closings.keys()):
        output.append(escape(content.text[start:position], quote=False))
        output.extend(reversed(closings.get(position, ())))
        output.extend(openings.get(position, ()))
        start = position
    output.append(escape(content.text[start:], quote=False))
    return "".join(output)


def split_text(
    content: RichText, limit: int, measure: Callable[[RichText], int],
) -> list[RichText]:
    parts = []
    while content.text and measure(content) > limit:
        low, high = 1, min(len(content.text), limit)
        end = 0
        while low <= high:
            middle = (low + high) // 2
            if measure(content.slice(0, middle)) <= limit:
                end = middle
                low = middle + 1
            else:
                high = middle - 1
        if not end:
            # A single character can have an oversized link attribute.
            parts.append(RichText(content.text[0]))
            content = content.slice(1, len(content.text))
            continue
        boundary = max(content.text.rfind("\n", end * 3 // 4, end),
                       content.text.rfind(" ", end * 3 // 4, end)) + 1
        end = boundary or end
        parts.append(content.slice(0, end))
        content = content.slice(end, len(content.text))
    if content.text or not parts:
        parts.append(content)
    return parts
