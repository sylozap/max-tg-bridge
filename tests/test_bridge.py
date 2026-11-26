import asyncio
from dataclasses import replace
from pathlib import Path

import pytest

from app.commands import Commands
from app.config import Settings
from app.core import bridge as routing
from app.core.bridge import Bridge
from app.core.model import Attachment, Author, Batch, Event, LocalFile, Message, Part, Platform, Reference, RichText
from app.core.polling import poll
from app.core.port import DeliveryError, MediaUnavailable
from app.core.retry import retry
from app.storage.sqlite import Store

TG, MAX = Platform.TELEGRAM, Platform.MAX


class Fake:
    def __init__(self, platform):
        self.platform = platform
        self.bot_id = 90 if platform == TG else 91
        self.username = "BridgeBot"
        self.sent = []
        self.edited = []
        self.deleted = []
        self.checked = []
        self.sequence = 10000
        self.send_hook = None
        self.paths = []

    def plan(self, content, media):
        return [Part(content, tuple(media), "media" if media else "text")]

    async def send(self, chat, part, reply):
        if self.send_hook:
            await self.send_hook(chat, part, reply)
        for media in part.media:
            if isinstance(media, LocalFile):
                assert media.path.read_bytes() == b"test binary"
        self.sequence += 1
        self.sent.append((chat, part, reply))
        return str(self.sequence)

    async def edit(self, chat, mid, part, old_mode, *, replace_media=True):
        self.edited.append((chat, mid, part, old_mode, replace_media))

    async def delete(self, chat, mid):
        self.deleted.append((chat, mid))

    async def check_chat(self, chat):
        self.checked.append(chat)

    def media_limit(self, attachment):
        return 50_000_000

    async def download(self, attachment, folder, limit):
        if attachment.size and attachment.size > limit:
            raise MediaUnavailable("файл слишком большой")
        path = folder / "content"
        path.write_bytes(b"test binary")
        self.paths.append(path)
        return LocalFile(attachment, path)

    async def prepare(self, attachment, local):
        return local or attachment


def msg(
    mid, text="", *, platform=TG, chat=None, author=None, event=Event.CREATE,
    reply=None, revision="", attachments=(), album=None,
):
    return Message(platform, chat if chat is not None else -1 if platform == TG else -2, str(mid),
                   author or Author(5, "Иван"), RichText(text), attachments, event, revision, reply, album=album)


@pytest.fixture
async def setup_bridge(tmp_path):
    async with Store.open(tmp_path / "bridge.db") as store:
        assert await store.link(-1, -2)
        adapters = {platform: Fake(platform) for platform in Platform}
        bridge = Bridge(Settings("placeholder", "placeholder", frozenset({5}), frozenset({7})), store, adapters)
        try:
            yield bridge, store, adapters
        finally:
            await bridge.close()


async def test_roundtrip_signatures_loops_and_replay(setup_bridge):
    bridge, store, adapters = setup_bridge
    a = msg(1, "<Привет>!")
    b = msg(2, "Ответ", platform=MAX)
    await bridge.handle_batch([a, b, a])
    await bridge.handle_batch([a, b])
    assert adapters[MAX].sent[0][1].content.text == "Иван (Telegram):\n<Привет>!"
    assert adapters[TG].sent[0][1].content.text == "Иван (Макс):\nОтвет"
    assert adapters[MAX].sent[0][1].content.spans[0].kind == "bold"
    await bridge.handle_batch([
        msg(10001, "Отражение даже с неизвестным автором", platform=MAX),
        msg(7, "свой бот", platform=MAX, author=Author(91, "Мост")),
        msg(8, "чужой бот", author=Author(80, "Другой", True)),
        msg(9, "Событие", event=Event.SERVICE),
    ])
    assert [len(adapters[platform].sent) for platform in Platform] == [1, 1]
    assert (await store.pair_for(TG, -1)).delivered == 2


async def test_flags_are_independent(setup_bridge):
    bridge, _, adapters = setup_bridge
    bridge.settings = replace(bridge.settings, ignore_other_bots=False, ignore_service_events=False)
    await bridge.handle_batch([
        msg(1, "другой бот", author=Author(12, "Другой", True)),
        msg(2, "событие", event=Event.SERVICE),
        msg(3, "наш бот", author=Author(90, "Мост", True)),
    ])
    assert len(adapters[MAX].sent) == 2


async def test_retry_in_same_fifo_slot_and_independent_lanes(setup_bridge, monkeypatch):
    bridge, store, adapters = setup_bridge
    assert await store.link(-3, -4)
    first_retry = asyncio.Event()
    unblock = asyncio.Event()
    other_lane = asyncio.Event()
    waits = []
    attempts = []

    async def sleep(delay):
        waits.append(delay)
        first_retry.set()
        await unblock.wait()

    async def quick_retry(operation):
        return await retry(operation, sleep=sleep)

    async def send(chat, part, _):
        if chat == -4:
            other_lane.set()
            return
        attempts.append(part.content.text.splitlines()[-1])
        if len(attempts) == 1:
            raise DeliveryError("max_rate_limit", retry=True, delay=9)

    monkeypatch.setattr(routing, "retry", quick_retry)
    adapters[MAX].send_hook = send
    task = asyncio.create_task(bridge.handle_batch([msg(1, "first"), msg(2, "second"), msg(3, "other", chat=-3)]))
    try:
        await asyncio.wait_for(first_retry.wait(), 5)
        await asyncio.wait_for(other_lane.wait(), 5)
        assert waits == [9]
        assert attempts == ["first"]
    finally:
        unblock.set()
        await task
    assert attempts == ["first", "first", "second"]


async def test_native_reply_both_directions_and_edit_delete(setup_bridge):
    bridge, _, adapters = setup_bridge
    original = msg("max-a", "оригинал", platform=MAX)
    await bridge.handle_batch([original])
    await bridge.handle_batch([msg(2, "ответ", reply=Reference("10001", "оригинал"))])
    assert adapters[MAX].sent[0][2] == "max-a"
    await bridge.handle_batch([msg("max-b", "ответ", platform=MAX, reply=Reference("max-a"))])
    assert adapters[TG].sent[1][2] == "10001"
    await bridge.handle_batch([msg(3, "цитата", reply=Reference("unmapped", "короткая цитата"))])
    assert "↪ короткая цитата" in adapters[MAX].sent[1][1].content.text
    assert adapters[MAX].sent[1][2] is None

    edit = replace(original, event=Event.EDIT, revision="400", content=RichText("новый текст"))
    await bridge.handle_batch([edit, edit])
    await bridge.handle_batch([edit])
    assert len(adapters[TG].edited) == 1
    assert adapters[TG].edited[0][1:3] == ("10001", Part(RichText(
        "Иван (Макс):\nновый текст", adapters[TG].sent[0][1].content.spans)))
    await bridge.handle_batch([replace(original, event=Event.DELETE)])
    assert adapters[TG].deleted == [(-1, "10001")]


async def test_album_files_live_through_send_and_late_items_are_not_lost(setup_bridge):
    bridge, store, adapters = setup_bridge
    photo = Attachment("image", ref="api-file-id", key="unique-id")
    group = [msg(1, "альбом", album="A", attachments=(photo,)), msg(2, album="A", attachments=(photo,))]
    await bridge.handle_batch(group)
    assert len(adapters[MAX].sent) == 1
    assert len(adapters[MAX].sent[0][1].media) == 2
    assert all(not path.exists() for path in adapters[TG].paths)
    pair = await store.pair_for(TG, -1)
    assert await store.reply_target(pair, TG, "2") == "10001"
    await bridge.handle_batch([replace(group[0], event=Event.EDIT, revision="2", content=RichText("новая подпись"))])
    assert adapters[MAX].edited[-1][2].content.text.endswith("новая подпись")
    assert adapters[MAX].edited[-1][-1] is False
    await bridge.handle_batch([msg(3, album="A", attachments=(photo,))])
    assert len(adapters[MAX].sent) == 2
    assert await store.reply_target(pair, TG, "3") == "10002"


async def test_media_failure_preserves_caption_and_records_warning(setup_bridge):
    bridge, store, adapters = setup_bridge
    await bridge.handle_batch([msg(1, "подпись", attachments=(Attachment("video", ref="file", size=51_000_000),))])
    content = adapters[MAX].sent[0][1].content.text
    assert "подпись" in content
    assert "[Видео не передано: файл слишком большой]" in content
    assert (await store.pair_for(TG, -1)).last_error == "media_fallback"


async def test_commands_pair_pause_resume_and_unlink(setup_bridge):
    bridge, store, adapters = setup_bridge
    bridge.command = Commands(bridge)
    await bridge.handle_batch([msg(10, "/id", chat=-3, author=Author(99, "User"))])
    assert "ID чата Telegram — -3" in adapters[TG].sent[-1][1].content.text
    await bridge.handle_batch([msg(11, "/link -4", chat=-3, author=Author(99, "User"))])
    assert await store.pair_for(TG, -3) is None
    await bridge.handle_batch([msg(12, "/link@BridgeBot -4", chat=-3)])
    assert (await store.pair_for(TG, -3)).max_chat == -4
    assert adapters[TG].checked == [-3]
    assert adapters[MAX].checked == [-4]
    await bridge.handle_batch([msg(13, "/pause", chat=-3), msg(14, "drop", chat=-3)])
    assert not adapters[MAX].sent
    await bridge.handle_batch([
        msg(15, "/resume", platform=MAX, chat=-4, author=Author(7, "Admin")),
        msg(16, "pass", platform=MAX, chat=-4),
    ])
    assert adapters[TG].sent[-1][1].content.text.endswith("pass")
    await bridge.handle_batch([msg(17, "/status", chat=-3), msg(18, "/pairs"), msg(19, "/unlink", chat=-3)])
    assert any("Доставлено событий — 1" in entry[1].content.text for entry in adapters[TG].sent)
    assert await store.pair_for(TG, -3) is None
    assert len(adapters[MAX].sent) == 1


async def test_cursor_saved_after_delivery_and_recovers_after_reopen(tmp_path: Path):
    db_path = tmp_path / "bridge.db"
    entered, release, stopping = asyncio.Event(), asyncio.Event(), asyncio.Event()
    content = "unique-plaintext-that-must-never-be-persisted"
    async with Store.open(db_path) as store:
        await store.link(-1, -2)
        adapters = {platform: Fake(platform) for platform in Platform}
        bridge = Bridge(Settings("placeholder", "placeholder"), store, adapters)

        async def fetch(cursor):
            assert cursor is None
            return Batch((msg(66, content),), 204)

        async def send(*_):
            entered.set()
            await release.wait()

        adapters[TG].poll = fetch
        adapters[MAX].send_hook = send
        task = asyncio.create_task(poll(adapters[TG], bridge, stopping))
        try:
            await asyncio.wait_for(entered.wait(), 5)
            assert await store.cursor(TG, adapters[TG].bot_id) is None
            stopping.set()
            release.set()
            await asyncio.wait_for(task, 5)
        finally:
            await bridge.close()
    assert content.encode() not in db_path.read_bytes()
    async with Store.open(db_path) as store:
        assert await store.cursor(TG, adapters[TG].bot_id) == 204
        assert await store.seen(msg(66, content))


async def test_idle_telegram_offset_is_reset_but_max_marker_is_kept(setup_bridge):
    _, store, adapters = setup_bridge
    for platform in Platform:
        await store.save_cursor(platform, adapters[platform].bot_id, 123)
    await store.db.execute("UPDATE cursors SET saved_at = saved_at - 604800")
    assert await store.cursor(TG, adapters[TG].bot_id) is None
    assert await store.cursor(MAX, adapters[MAX].bot_id) == 123
