import asyncio
import logging
import sqlite3
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass

from app.config import Settings
from app.formatting.text import neutralize_mentions
from app.storage.sqlite import Copy, Pair, Store

from .model import Event, Message, Platform, RichText, Span
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
    def __init__(self, settings: Settings, store: Store, adapters: dict[Platform, Adapter]):
        self.settings = settings
        self.store = store
        self.adapters = adapters
        self.command: Handler | None = None
        self.poll_errors: dict[Platform, str] = {}
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
            delivered = await self._deliver(pair, messages)
        except sqlite3.Error:
            raise
        except DeliveryError as exc:
            log.warning("delivery_failed", extra={**fields, "code": exc.code})
            await self.store.finish(messages, pair.id, error=exc.code)
        except Exception as exc:
            log.error("unexpected_delivery_error", extra={**fields, "error_type": type(exc).__name__})
            await self.store.finish(messages, pair.id, error="unexpected_delivery_error")
        else:
            await self.store.finish(messages, pair.id, delivered=delivered)
            if delivered:
                log.info("delivery_completed", extra={**fields, "message_count": len(messages)})

    def _content(self, messages: list[Message]) -> RichText:
        first = messages[0]
        name = " ".join(first.author.name.split())[:100] or "Неизвестный автор"
        title = self.settings.author_template.format(author=name, source=first.platform.title)
        result = RichText(title, (Span("bold", 0, len(title)),))
        if first.forwarded_from:
            sender = " ".join(first.forwarded_from.split())[:120]
            result += RichText(f"\nПереслано от {sender}")
        for message in messages:
            if message.content.text:
                result += RichText("\n") + message.content
        return neutralize_mentions(result)

    async def _deliver(self, pair: Pair, messages: list[Message]) -> bool:
        first = messages[0]
        if first.event not in {Event.CREATE, Event.SERVICE}:
            return False
        root = _root(first)
        adapter = self.adapters[first.platform.opposite]
        chat = pair.chat(adapter.platform)
        existing = {copy.part for copy in await self.store.copies(pair, first.platform, root)}
        originals = [item.id for item in messages]
        for i, part in enumerate(adapter.plan(self._content(messages), ())):
            if i in existing:
                continue
            mid = await retry(lambda: adapter.send(chat, part, None))
            await self.store.record_copy(pair, first.platform, root, originals, Copy(i, mid, part.mode))
        return True

    async def close(self) -> None:
        tasks = [lane.task for lane in self.lanes.values()]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
