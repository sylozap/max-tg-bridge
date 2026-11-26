from aiogram.types import MessageEntity

from app.core.model import RichText, Span
from app.formatting.text import (
    from_max, from_telegram, max_html, neutralize_mentions, split_text, telegram_entities, utf16_length,
)


def test_telegram_emoji_nested_format_and_escaping():
    text = "😀 <привет> & мир"
    entities = [
        MessageEntity(type="bold", offset=3, length=8),
        MessageEntity(type="italic", offset=4, length=6),
        MessageEntity(type="text_link", offset=14, length=3, url='https://example.org/?a=1&b="2"'),
    ]
    content = from_telegram(text, entities)
    assert content.spans[0] == Span("bold", 2, 10)
    assert max_html(content) == (
        '😀 <b>&lt;<i>привет</i>&gt;</b> &amp; <a href="https://example.org/?a=1&amp;b=&quot;2&quot;">мир</a>'
    )
    assert telegram_entities(content)[0] == {"type": "bold", "offset": 3, "length": 8}


def test_max_emoji_and_supported_styles():
    content = from_max("🐈 слово", [
        {"type": "quote", "from": 2, "length": 5},
        {"type": "underline", "from": 2, "length": 5},
        {"type": "emphasized", "from": 2, "length": 5},
    ])
    assert {(e["type"], e["offset"], e["length"]) for e in telegram_entities(content)} == {
        ("blockquote", 3, 5), ("underline", 3, 5), ("italic", 3, 5),
    }
    assert from_max("🐈 ok", [{"type": "strong", "from": 3, "length": 2}], utf16=True).spans == (
        Span("bold", 2, 4),
    )
    assert not from_max("🐈", [{"type": "strong", "from": 1, "length": 1}], utf16=True).spans


def test_plain_mentions_unsafe_links_and_spoiler_fallback():
    content = from_max("😀 @someone & name", [
        {"type": "strong", "from": 2, "length": 8},
        {"type": "user_mention", "from": 2, "length": 8, "user_id": 123},
        {"type": "link", "from": 13, "length": 4, "url": "tg://user?id=123"},
    ])
    safe = neutralize_mentions(content)
    assert safe.text == "😀 @\u200bsomeone & name"
    assert telegram_entities(safe) == [{"type": "bold", "offset": 3, "length": 9}]
    assert max_html(safe) == "😀 <b>@\u200bsomeone</b> &amp; name"
    spoiler = RichText("secret", (Span("spoiler", 0, 6),))
    assert max_html(spoiler) == "secret"
    assert telegram_entities(spoiler)[0]["type"] == "spoiler"


def test_split_preserves_text_and_reopens_code_blocks():
    text = ("<&🐈\n" * 14).rstrip()
    source = RichText(text, (Span("pre", 0, len(text), "python"),))
    measure = lambda part: utf16_length(max_html(part))
    parts = split_text(source, 42, measure)
    assert "".join(part.text for part in parts) == text
    assert all(measure(part) <= 42 for part in parts)
    assert all(max_html(part).startswith("<pre>") and max_html(part).endswith("</pre>") for part in parts)
    assert all(telegram_entities(part)[0]["language"] == "python" for part in parts)
