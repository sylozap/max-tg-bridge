import argparse
import asyncio
import fcntl
import logging
import os
import signal
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from aiogram.utils.token import TokenValidationError

from .adapters.max import MaxAdapter
from .adapters.telegram import TelegramAdapter
from .config import Settings
from .core.bridge import Bridge
from .core.model import Platform
from .core.polling import poll, until_stop
from .core.port import Adapter, DeliveryError
from .core.retry import retry
from .logging import configure
from .storage.sqlite import Store

log = logging.getLogger(__name__)


@contextmanager
def instance_lock(database: Path) -> Iterator[None]:
    database = database.resolve()
    database.parent.mkdir(parents=True, exist_ok=True)
    lock_path = database.with_name(database.name + "-lock")
    with lock_path.open("a") as file:
        try:
            fcntl.flock(file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise DeliveryError("bridge_already_running") from None
        yield


async def _run(settings: Settings, stop: asyncio.Event) -> None:
    telegram = TelegramAdapter(settings.telegram_token)
    adapters: dict[Platform, Adapter] = {Platform.TELEGRAM: telegram}
    try:
        adapters[Platform.MAX] = MaxAdapter(
            settings.max_token, settings.max_base_url, ca_file=settings.max_ca_file,
            utf16=settings.max_markup_utf16,
        )
        async with Store.open(settings.database) as store:
            for adapter in adapters.values():
                await until_stop(lambda: retry(adapter.identify), stop)
                if stop.is_set():
                    return
            bridge = Bridge(settings, store, adapters)
            try:
                async with asyncio.TaskGroup() as tasks:
                    listeners = [
                        tasks.create_task(poll(adapter, bridge, stop), name=f"poll:{adapter.platform}")
                        for adapter in adapters.values()
                    ]
                    log.info("bridge_started")
                    await stop.wait()
                    log.info("shutdown_requested")
                    try:
                        async with asyncio.timeout(30):
                            await asyncio.gather(*listeners)
                    except TimeoutError:
                        log.warning("shutdown_drain_timeout")
            finally:
                await bridge.close()
    finally:
        await asyncio.gather(*(adapter.close() for adapter in adapters.values()), return_exceptions=True)
    log.info("bridge_stopped")


async def run(settings: Settings) -> None:
    loop = asyncio.get_running_loop()
    stop = asyncio.Event()
    signals = (signal.SIGINT, signal.SIGTERM)
    for sig in signals:
        loop.add_signal_handler(sig, stop.set)
    try:
        with instance_lock(settings.database):
            await _run(settings, stop)
    finally:
        for sig in signals:
            loop.remove_signal_handler(sig)


def _log_failure(exc: BaseException) -> None:
    if isinstance(exc, BaseExceptionGroup):
        for error in exc.exceptions:
            _log_failure(error)
    elif isinstance(exc, DeliveryError):
        log.error("bridge_failed", extra={"code": exc.code})
    else:
        log.error("bridge_failed", extra={"error_type": type(exc).__name__})


def main() -> None:
    parser = argparse.ArgumentParser(description="Двусторонний мост MAX ↔ Telegram")
    parser.add_argument("--check-config", action="store_true", help="Проверить настройки без сетевых запросов")
    options = parser.parse_args()
    try:
        settings = Settings.load()
        from aiogram.utils.token import validate_token
        validate_token(settings.telegram_token)
    except TokenValidationError:
        parser.exit(2, "Некорректный формат TELEGRAM_BOT_TOKEN\n")
    except ValueError as exc:
        parser.exit(2, f"{exc}\n")
    if options.check_config:
        print("Настройки корректны")
        return
    os.umask(0o077)
    configure(settings.log_level, settings.debug)
    log.debug("debug_logging_enabled")
    try:
        asyncio.run(run(settings))
    except KeyboardInterrupt:
        return
    except Exception as exc:
        _log_failure(exc)
        raise SystemExit(1) from None
