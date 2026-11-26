import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import TypeVar

from .bridge import Bridge
from .port import Adapter, DeliveryError
from .retry import retry

log = logging.getLogger(__name__)
T = TypeVar("T")


async def until_stop(operation: Callable[[], Awaitable[T]], stop: asyncio.Event) -> T | None:
    async def run() -> T:
        return await operation()

    task = asyncio.create_task(run())
    stopping = asyncio.create_task(stop.wait())
    try:
        await asyncio.wait((task, stopping), return_when=asyncio.FIRST_COMPLETED)
        return task.result() if task.done() else None
    finally:
        for future in (task, stopping):
            if not future.done():
                future.cancel()
        await asyncio.gather(task, stopping, return_exceptions=True)


async def poll(adapter: Adapter, bridge: Bridge, stop: asyncio.Event) -> None:
    while not stop.is_set():
        cursor = await bridge.store.cursor(adapter.platform, adapter.bot_id)
        try:
            batch = await until_stop(lambda: retry(lambda: adapter.poll(cursor)), stop)
        except DeliveryError as exc:
            bridge.poll_errors[adapter.platform] = exc.code
            log.warning("poll_failed", extra={"platform": adapter.platform, "code": exc.code})
            if exc.code in {"telegram_unauthorized", "max_unauthorized", "telegram_poll_conflict"}:
                raise
            await until_stop(lambda: asyncio.sleep(5), stop)
            continue
        if batch is None:
            return
        bridge.poll_errors.pop(adapter.platform, None)
        log.debug("poll_batch_received", extra={"platform": adapter.platform, "message_count": len(batch.messages)})
        await bridge.handle_batch(batch.messages)
        if batch.cursor is not None and batch.cursor != cursor:
            await bridge.store.save_cursor(adapter.platform, adapter.bot_id, batch.cursor)
