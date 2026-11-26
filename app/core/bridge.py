import asyncio
import logging
import sqlite3
from collections.abc import Awaitable, Callable, Sequence
from contextlib import AsyncExitStack
from dataclasses import dataclass, replace
from pathlib import Path
from tempfile import TemporaryDirectory

from app.config import Settings
from app.formatting.text import neutralize_mentions
from app.storage.sqlite import Copy, Pair, Store, media_key

from .media import prepare
from .model import Event, Message, Part, Platform, RichText, Span
from .port import Adapter, DeliveryError
from .retry import retry

log = logging.getLogger(__name__)

Handler = Callable[[Message], Awaitable[bool]]


@dataclass(slots=True)
class _Job:
    pair: Pair
    messages: list[Message]
    done: asyncio.Future[None]


@dataclass(slots=True)
class _Lane:
    queue: asyncio.Queue[_Job]
    task: asyncio.Task[None]
    active: bool = False


def _root(message: Message) -> str:
    if message.album and message.event in {Event.CREATE, Event.EDIT}:
        return f"album:{message.album}"
    return f"message:{message.id}"


class Bridge:
    def __init__(
        self, settings: Settings, store: Store, adapters: dict[Platform, Adapter],
        media_directory: Path | None = None,
    ):
        self.settings = settings
        self.store = store
        self.adapters = adapters
        self.command: Handler | None = None
        self.poll_errors: dict[Platform, str] = {}
        self.media_directory = media_directory
        self.media_slots = asyncio.Semaphore(4)
        self.lanes: dict[tuple[int, Platform], _Lane] = {}

    def queue_size(self, pair_id: int, source: Platform) -> int:
        lane = self.lanes.get((pair_id, source))
        return lane.queue.qsize() + int(lane.active) if lane else 0

    async def handle_batch(self, messages: Sequence[Message]) -> None:
        # An album may be interleaved with updates from other chats in the page.
        groups: dict[tuple[Platform, int, str], list[Message]] = {}
        for message in messages:
            key = _root(message) if message.event == Event.CREATE else f"{message.event}:{message.id}:{message.revision}"
            group = groups.setdefault((message.platform, message.chat, key), [])
            if all(old.id != message.id for old in group):
                group.append(message)
        jobs = []
        for group in groups.values():
            messages_to_route = []
            for message in group:
                if await self.store.seen(message):
                    continue
                if await self._ignore(message):
                    await self.store.finish((message,))
                    continue
                if self.command and await self.command(message):
                    await self.store.finish((message,))
                    continue
                messages_to_route.append(message)
            if not messages_to_route:
                continue
            source = messages_to_route[0].platform
            pair = await self.store.pair_for(source, messages_to_route[0].chat)
            if not pair:
                continue
            if pair.paused:
                await self.store.finish(messages_to_route)
                continue
            jobs.append(self._submit(pair, source, messages_to_route))
        if jobs:
            await asyncio.gather(*jobs)

    async def _ignore(self, message: Message) -> bool:
        return (
            (message.author.id == self.adapters[message.platform].bot_id)
            or (message.author.is_bot and self.settings.ignore_other_bots)
            or (message.event == Event.SERVICE and self.settings.ignore_service_events)
            or await self.store.is_outgoing(message.platform, message.chat, message.id)
        )

    def _submit(self, pair: Pair, source: Platform, messages: list[Message]) -> asyncio.Future[None]:
        key = (pair.id, source)
        lane = self.lanes.get(key)
        if not lane:
            queue: asyncio.Queue[_Job] = asyncio.Queue()
            task = asyncio.create_task(self._worker(key, queue), name=f"bridge:{pair.id}:{source}")
            lane = _Lane(queue, task)
            self.lanes[key] = lane
        done = asyncio.get_running_loop().create_future()
        lane.queue.put_nowait(_Job(pair, messages, done))
        return done

    async def _worker(self, key: tuple[int, Platform], queue: asyncio.Queue[_Job]) -> None:
        try:
            while True:
                job = await queue.get()
                self.lanes[key].active = True
                try:
                    await self._process(job)
                except asyncio.CancelledError:
                    job.done.cancel()
                    raise
                except Exception as exc:
                    if not job.done.done():
                        job.done.set_exception(exc)
                else:
                    if not job.done.done():
                        job.done.set_result(None)
                finally:
                    self.lanes[key].active = False
                    queue.task_done()
        finally:
            self.lanes.pop(key, None)
            while not queue.empty():
                queue.get_nowait().done.cancel()
                queue.task_done()

    async def _process(self, job: _Job) -> None:
        pair = await self.store.pair(job.pair.id)
        messages = [message for message in job.messages if not await self.store.seen(message)]
        if not messages or not pair:
            return
        if pair.paused or pair.generation != job.pair.generation:
            await self.store.finish(messages)
            return
        first = messages[0]
        fields = {"pair_id": pair.id, "platform": first.platform, "event": first.event}
        try:
            delivered, warning = await self._deliver(pair, messages)
        except sqlite3.Error:
            raise
        except DeliveryError as exc:
            log.warning("delivery_failed", extra={**fields, "code": exc.code})
            await self.store.finish(messages, pair.id, error=exc.code)
        except Exception as exc:
            log.error("unexpected_delivery_error", extra={**fields, "error_type": type(exc).__name__})
            await self.store.finish(messages, pair.id, error="unexpected_delivery_error")
        else:
            await self.store.finish(messages, pair.id, delivered=delivered, warning=warning)
            if delivered:
                log.info("delivery_completed", extra={**fields, "message_count": len(messages), "code": warning})

    def _content(self, messages: list[Message], reply: str | None) -> RichText:
        first = messages[0]
        name = " ".join(first.author.name.split())[:100] or "Неизвестный автор"
        title = self.settings.author_template.format(author=name, source=first.platform.title)
        result = RichText(title, (Span("bold", 0, len(title)),))
        if first.reply and not reply:
            quote = " ".join(first.reply.quote.split()) or "[сообщение недоступно]"
            quote = "↪ " + (quote[:179] + "…" if len(quote) > 180 else quote)
            result += RichText("\n") + RichText(quote, (Span("blockquote", 0, len(quote)),))
        if first.forwarded_from:
            sender = " ".join(first.forwarded_from.split())[:120]
            result += RichText(f"\nПереслано от {sender}")
        for message in messages:
            if message.content.text:
                result += RichText("\n") + message.content
        return result

    async def _deliver(self, pair: Pair, messages: list[Message]) -> tuple[bool, str | None]:
        first = messages[0]
        source = first.platform
        default_root = _root(first)
        if first.album and first.event == Event.CREATE:
            groups: dict[str, list[Message]] = {}
            new = []
            for message in messages:
                original = await self.store.original(pair, source, message.id)
                if original:
                    groups.setdefault(original.root, []).append(message)
                else:
                    new.append(message)
            if new:
                has_album = await self.store.copies(pair, source, default_root)
                root = f"late-album:{first.album}:{new[0].id}" if has_album else default_root
                groups[root] = new
            delivered, warning = False, None
            for root, group in groups.items():
                result, code = await self._mirror(pair, group, root)
                delivered, warning = delivered or result, warning or code
            return delivered, warning
        original = await self.store.original(pair, source, first.id) if first.event == Event.EDIT else None
        return await self._mirror(pair, messages, original.root if original else default_root)

    async def _mirror(self, pair: Pair, messages: list[Message], root: str) -> tuple[bool, str | None]:
        first = messages[0]
        source = first.platform
        target = self.adapters[source.opposite]
        chat = pair.chat(target.platform)
        existing = {copy.part: copy for copy in await self.store.copies(pair, source, root)}
        if first.event == Event.DELETE:
            return await self._delete(pair, source, root, target, chat, existing), None
        editing = first.event == Event.EDIT
        if editing and not existing:
            return False, None
        reply = await self.store.reply_target(pair, source, first.reply.id) if first.reply and first.reply.id else None
        keep_media = False
        if editing and target.platform == Platform.MAX and first.attachments:
            original = await self.store.original(pair, source, first.id)
            has_other_caption = (
                first.album and await self.store.has_other_captions(pair, source, root, first.id)
            )
            keep_media = bool(
                original and original.media_key and original.media_key == media_key(first) and not has_other_caption
                and 0 in existing and existing[0].mode == "media"
                and all(copy.mode != "media" for index, copy in existing.items() if index != 0)
            )
        if editing and first.album and not keep_media:
            note = self._content(messages, reply) + RichText("\n[Изменение в альбоме Telegram]")
            correction = f"edit:{first.id}:{first.revision}"
            copies = {copy.part: copy for copy in await self.store.copies(pair, source, correction)}
            for pos, part in enumerate(target.plan(neutralize_mentions(note), ())):
                if pos not in copies:
                    parent = next(iter(existing.values())).mid if pos == 0 else None
                    try:
                        mid = await retry(lambda: target.send(chat, part, parent))
                    except DeliveryError as exc:
                        if not (exc.missing and parent):
                            raise
                        mid = await retry(lambda: target.send(chat, part, None))
                    await self.store.record_copy(pair, source, correction, (), Copy(pos, mid, part.mode))
            return True, "album_edit_notice"

        async with AsyncExitStack() as stack:
            media, notes, degraded = [], RichText(), False
            if not keep_media and any(message.attachments for message in messages):
                await stack.enter_async_context(self.media_slots)
                folder = Path(stack.enter_context(
                    TemporaryDirectory(prefix="max-tg-bridge-", dir=self.media_directory)
                ))
                media, notes, degraded = await prepare(messages, self.adapters[source], target, folder)

            def plan(reply_to: str | None) -> list[Part]:
                content = neutralize_mentions(self._content(messages, reply_to) + notes)
                parts = target.plan(content, media)
                if keep_media:
                    parts[0] = replace(parts[0], mode=existing[0].mode)
                return parts

            parts = plan(reply)
            pos = 0
            while pos < len(parts):
                if not await self._current(pair):
                    return False, None
                part = parts[pos]
                old = existing.get(pos)
                if old and editing:
                    try:
                        await retry(lambda: target.edit(chat, old.mid, part, old.mode,
                                                        replace_media=not (keep_media and pos == 0)))
                    except DeliveryError as exc:
                        if exc.code == "edit_requires_replacement":
                            await retry(lambda: target.delete(chat, old.mid))
                        elif not exc.missing or (keep_media and pos == 0):
                            raise
                        await self.store.forget_copy(pair, source, root, pos)
                        old = None
                    else:
                        if old.mode != part.mode:
                            await self.store.record_copy(pair, source, root, messages, Copy(pos, old.mid, part.mode))
                if not old:
                    try:
                        mid, mode = await self._send(target, chat, part, reply if pos == 0 else None)
                    except DeliveryError as exc:
                        if not (exc.missing and reply and pos == 0):
                            raise
                        reply = None
                        parts = plan(reply)
                        continue
                    degraded = degraded or mode != part.mode
                    await self.store.record_copy(pair, source, root, messages, Copy(pos, mid, mode))
                pos += 1
            if editing:
                surplus = {index: copy for index, copy in existing.items() if index >= len(parts)}
                await self._delete(pair, source, root, target, chat, surplus)
                await self.store.update_original(pair, first)
        return True, "media_fallback" if degraded else None

    async def _current(self, original: Pair) -> bool:
        pair = await self.store.pair(original.id)
        return pair is not None and not pair.paused and pair.generation == original.generation

    @staticmethod
    async def _send(target: Adapter, chat: int, part: Part, reply: str | None) -> tuple[str, str]:
        try:
            return await retry(lambda: target.send(chat, part, reply)), part.mode
        except DeliveryError as exc:
            if not part.media or exc.code not in {"media_rejected", "max_attachment_pending"}:
                raise
            note = "[Вложение не передано: мессенджер отклонил файл или не завершил его обработку]"
            fallback, = target.plan(part.content + RichText(("\n" if part.content.text else "") + note), ())
            return await retry(lambda: target.send(chat, fallback, reply)), fallback.mode

    async def _delete(
        self, pair: Pair, source: Platform, root: str, adapter: Adapter, chat: int, existing: dict[int, Copy],
    ) -> bool:
        error = None
        for copy in existing.values():
            try:
                await retry(lambda: adapter.delete(chat, copy.mid))
            except DeliveryError as exc:
                error = exc
            else:
                await self.store.forget_copy(pair, source, root, copy.part)
        if error:
            raise error
        return bool(existing)

    async def close(self) -> None:
        tasks = [lane.task for lane in self.lanes.values()]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
