import asyncio
import logging
import random
from collections.abc import Awaitable, Callable
from typing import TypeVar

from .port import DeliveryError

T = TypeVar("T")
log = logging.getLogger(__name__)


async def retry(
    operation: Callable[[], Awaitable[T]],
    *,
    attempts: int = 5,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> T:
    for attempt in range(attempts):
        try:
            return await operation()
        except DeliveryError as exc:
            if not exc.retry or attempt + 1 == attempts:
                raise
            delay = max(exc.delay, min(12, 0.5 * 2**attempt + random.uniform(0, 0.25)))
            log.info("retry_scheduled", extra={"code": exc.code, "attempt": attempt + 1, "delay": delay})
            await sleep(delay)
    raise ValueError("attempts must be positive")
