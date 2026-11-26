import json
from pathlib import Path

import httpx
import pytest
from aiogram.types import Update

from app.adapters.max import MaxAdapter, normalize as normalize_max
from app.adapters.telegram import TelegramAdapter, normalize as normalize_telegram
from app.core.model import Attachment, Event, LocalFile, RichText
from app.core.port import DeliveryError, MediaUnavailable


def telegram_update(mid: int, **fields) -> Update:
    return Update.model_validate({
        "update_id": mid + 900,
        "message": {
            "message_id": mid, "date": 1700000000, "chat": {"id": -10077, "type": "supergroup"},
            "from": {"id": 100, "is_bot": False, "first_name": "Иван"}, **fields,
        },
    })


def max_update(**message):
    return {"update_type": "message_created", "timestamp": 1700000000, "message": {
        "recipient": {"chat_id": -55, "chat_type": "chat"},
        "sender": {"user_id": 200, "first_name": "Алиса"}, **message,
    }}


def test_telegram_normalizes_poll_and_static_sticker():
    poll = telegram_update(1, poll={"id": "x", "question": "Можно?", "options": [
        {"text": "Да", "voter_count": 0, "persistent_id": "a"},
        {"text": "Нет", "voter_count": 0, "persistent_id": "b"}],
        "total_voter_count": 0, "is_closed": False, "is_anonymous": True,
        "type": "regular", "allows_multiple_answers": False, "allows_revoting": False, "members_only": False,
    })
    assert normalize_telegram(poll).attachments[0].detail == "[Опрос: Можно?]\n• Да\n• Нет"
    sticker = telegram_update(2, sticker={
        "file_id": "sticker-id", "file_unique_id": "unique", "type": "regular",
        "width": 64, "height": 64, "is_animated": False, "is_video": False, "emoji": "🐈",
    })
    assert normalize_telegram(sticker).attachments[0].kind == "sticker_image"


def test_max_normalizes_forward_without_outer_body_and_delete():
    update = max_update(link={
        "type": "forward", "sender": {"first_name": "Автор"},
        "message": {"mid": "old", "text": "🐈 текст", "markup": [{"type": "strong", "from": 2, "length": 5}],
                    "attachments": [{"type": "location", "latitude": 55, "longitude": 37}]},
    }, body=None)
    message = normalize_max(update)
    assert message.forwarded_from == "Автор"
    assert message.content.text == "🐈 текст"
    assert message.content.spans[0].start == 2
    assert message.attachments[0].coordinates == (55, 37)
    delete = normalize_max({"update_type": "message_removed", "chat_id": -55, "message_id": "mid", "user_id": 200})
    assert (delete.id, delete.event, delete.author.id) == ("mid", Event.DELETE, None)


async def test_telegram_album_refetch_does_not_confirm_first_page(monkeypatch):
    adapter = TelegramAdapter("123:placeholder")
    adapter._album_wait = 0
    photo = [{"file_id": "id", "file_unique_id": "unique", "width": 2, "height": 3}]
    updates = [
        telegram_update(101, photo=photo, media_group_id="album"),
        telegram_update(102, photo=photo, media_group_id="album"),
    ]
    requests = []

    async def call(request, **_):
        requests.append((request.offset, request.timeout))
        return updates if len(requests) > 1 else updates[:1]

    monkeypatch.setattr(adapter, "_call", call)
    try:
        batch = await adapter.poll(1001)
    finally:
        await adapter.close()
    assert requests == [(1001, 25), (1001, 0)]
    assert [item.album for item in batch.messages] == ["album", "album"]
    assert batch.cursor == 1003


async def test_max_album_upload_headers_tokens_and_marker(tmp_path: Path):
    token = "placeholder"
    adapter = MaxAdapter(token, "https://platform-api2.max.ru")
    exchanges = []

    def handler(request):
        path = request.url.path
        exchanges.append(path)
        if request.url.host == "fu.oneme.ru":
            assert "Authorization" not in request.headers
            assert b'name="data"; filename="test.jpg"' in request.content
            assert b"media bytes" in request.content
            return httpx.Response(200, json={"photos": {"999": {"token": "uploaded-photo"}}})
        assert request.headers["Authorization"] == token
        if path == "/updates":
            assert request.url.params["marker"] == "42"
            return httpx.Response(200, json={"marker": 43, "updates": []})
        if path == "/uploads":
            assert request.url.params["type"] == "image"
            return httpx.Response(200, json={"url": "https://fu.oneme.ru/media"})
        if path == "/messages":
            body = json.loads(request.content)
            assert request.url.params["chat_id"] == "-42"
            assert body["attachments"] == [{"type": "image", "payload": {"token": "uploaded-photo"}}] * 2
            assert body["link"] == {"type": "reply", "mid": "previous"}
            assert body["format"] == "html"
            return httpx.Response(200, json={"message": {"body": {"mid": "sent"}}})
        raise AssertionError(path)

    await adapter.close()
    transport = httpx.MockTransport(handler)
    adapter.api = httpx.AsyncClient(base_url="https://platform-api2.max.ru",
                                    headers={"Authorization": token}, transport=transport)
    adapter.cdn = httpx.AsyncClient(transport=transport)
    path = tmp_path / "upload"
    path.write_bytes(b"media bytes")
    media = Attachment("image", name="test.jpg")
    try:
        assert (await adapter.poll(42)).cursor == 43
        uploaded = await adapter.prepare(media, LocalFile(media, path))
        parts = adapter.plan(RichText("Подпись"), [uploaded, uploaded])
        assert len(parts) == 1
        assert await adapter.send(-42, parts[0], "previous") == "sent"
    finally:
        await adapter.close()
    assert exchanges == ["/updates", "/uploads", "/media", "/messages"]


async def test_max_retry_after_and_untrusted_media_url(tmp_path):
    adapter = MaxAdapter("placeholder", "https://platform-api2.max.ru")
    await adapter.api.aclose()
    adapter.api = httpx.AsyncClient(base_url="https://platform-api2.max.ru", transport=httpx.MockTransport(
        lambda _: httpx.Response(429, headers={"Retry-After": "11"}),
    ))
    try:
        with pytest.raises(DeliveryError) as caught:
            await adapter.poll(None)
        assert caught.value.retry and caught.value.delay == 11
        with pytest.raises(MediaUnavailable, match="запрещён"):
            await adapter.download(Attachment("image", ref="https://127.0.0.1/test"), tmp_path, 15)
    finally:
        await adapter.close()
