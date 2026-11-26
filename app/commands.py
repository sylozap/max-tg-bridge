import logging
import re

from .core.bridge import Bridge
from .core.model import Event, Message, Platform, RichText
from .core.port import DeliveryError
from .core.retry import retry

log = logging.getLogger(__name__)
COMMANDS = {
    "start": "Запустить справку",
    "help": "Показать команды моста",
    "id": "Показать ID текущего чата",
    "status": "Показать состояние текущей пары",
    "pause": "Приостановить пересылку",
    "resume": "Возобновить пересылку",
    "link": "Связать текущий чат с другим",
    "unlink": "Разорвать связь текущего чата",
    "pairs": "Показать связанные чаты",
}
_COMMAND = re.compile(r"^/([A-Za-z][A-Za-z0-9_]{0,63})(?:@([A-Za-z0-9_]+))?(?=\s|$)")
_CHAT_ID = re.compile(r"-?[0-9]{1,19}")
_HELP = (
    "Мост MAX ↔ Telegram\n"
    "/id — ID текущего чата. /id me — также ваш ID.\n"
    "Для администраторов моста\n"
    "/link <chat_id> — связать чаты\n"
    "/unlink — разорвать связь\n"
    "/status — состояние и статистика текущей пары\n"
    "/pause и /resume — приостановить или возобновить пересылку\n"
    "/pairs — список всех пар"
)
_LINK_ERRORS = {
    "max_admin_required": "В MAX назначьте боту права «Читать все сообщения» и «Редактировать и удалять сообщения».",
    "telegram_admin_required": "В Telegram назначьте бота администратором чата.",
    "max_group_required": "Укажите групповой чат MAX.",
    "telegram_group_required": "Укажите группу или супергруппу Telegram.",
}


class Commands:
    def __init__(self, bridge: Bridge):
        self.bridge = bridge
        self.store = bridge.store
        self.adapters = bridge.adapters
        self.admins = {
            Platform.TELEGRAM: bridge.settings.telegram_admins,
            Platform.MAX: bridge.settings.max_admins,
        }

    async def __call__(self, message: Message) -> bool:
        if message.event not in {Event.CREATE, Event.EDIT} or message.forwarded_from or message.attachments:
            return False
        text = message.content.text.lstrip()
        match = _COMMAND.match(text)
        if not match:
            return False
        name, username = match.groups()
        adapter = self.adapters[message.platform]
        if username and username.casefold() != adapter.username.casefold():
            return True
        name = name.casefold()
        if message.event == Event.EDIT or message.author.is_bot or name not in COMMANDS:
            return True
        arguments = text[match.end():].strip()
        response = await self._respond(message, name, arguments)
        for index, part in enumerate(adapter.plan(RichText(response), ())):
            reply = message.id if index == 0 and not message.id.startswith("bot_started:") else None
            try:
                try:
                    mid = await retry(lambda: adapter.send(message.chat, part, reply), attempts=3)
                except DeliveryError as exc:
                    if not (exc.missing and reply):
                        raise
                    mid = await retry(lambda: adapter.send(message.chat, part, None), attempts=3)
            except DeliveryError as exc:
                log.warning("command_response_failed", extra={"platform": message.platform, "code": exc.code})
                break
            await self.store.outgoing(message.platform, message.chat, mid)
        return True

    async def _respond(self, message: Message, name: str, arguments: str) -> str:
        if name in {"start", "help"}:
            return _HELP
        if name == "id":
            extra = f"\nВаш ID — {message.author.id}" if arguments == "me" and message.author.id is not None else ""
            return f"ID чата {message.platform.title} — {message.chat}{extra}"
        if message.author.id not in self.admins[message.platform]:
            return "Команда доступна только администраторам моста."
        if name == "pairs":
            pairs = await self.store.pairs()
            lines = [
                f"• Telegram {pair.telegram_chat} ↔ MAX {pair.max_chat} ({'пауза' if pair.paused else 'активна'})"
                for pair in pairs
            ]
            return "\n".join(lines) if lines else "Связанных чатов пока нет."
        if name == "link":
            return await self._link(message, arguments)
        pair = await self.store.pair_for(message.platform, message.chat)
        if pair is None:
            return "Этот чат ещё не связан. Используйте /link <chat_id>."
        if name == "unlink":
            await self.store.unlink(pair.id)
            return "Связь разорвана."
        if name in {"pause", "resume"}:
            paused = name == "pause"
            await self.store.pause(pair.id, paused)
            return "Пересылка приостановлена. Новые сообщения пропускаются." if paused else "Пересылка возобновлена."

        bridge = self.bridge
        polls = ", ".join(f"{platform.title} — {code}" for platform, code in bridge.poll_errors.items())
        return (
            f"Пересылка {'приостановлена' if pair.paused else 'включена'}\n"
            f"Telegram {pair.telegram_chat} ↔ MAX {pair.max_chat}\n"
            f"Очереди с обработкой — Telegram→MAX {bridge.queue_size(pair.id, Platform.TELEGRAM)}, "
            f"MAX→Telegram {bridge.queue_size(pair.id, Platform.MAX)}\n"
            f"Доставлено событий — {pair.delivered}. Ошибок доставки — {pair.failed}.\n"
            f"Последняя пересылка (UTC) — {pair.last_delivery or 'пока нет'}\n"
            f"Последняя ошибка или замена вложения — {pair.last_error or 'пока нет'}\n"
            f"Ошибки опроса — {polls or 'нет'}"
        )

    async def _link(self, message: Message, arguments: str) -> str:
        if not _CHAT_ID.fullmatch(arguments):
            return "Используйте /link <числовой chat_id другого мессенджера>."
        remote = int(arguments)
        if not -(2**63) <= remote < 2**63 or remote == 0:
            return "Некорректный chat_id."
        source = message.platform
        if await self.store.pair_for(source, message.chat) or await self.store.pair_for(source.opposite, remote):
            return "Один из чатов уже связан. Сначала используйте /unlink."
        try:
            await retry(lambda: self.adapters[source].check_chat(message.chat), attempts=3)
            await retry(lambda: self.adapters[source.opposite].check_chat(remote), attempts=3)
        except DeliveryError as exc:
            return _LINK_ERRORS.get(exc.code, f"Не удалось проверить доступ ботов к чатам ({exc.code}).")
        telegram, maximum = (message.chat, remote) if source == Platform.TELEGRAM else (remote, message.chat)
        if await self.store.link(telegram, maximum):
            return f"Чаты связаны. Telegram {telegram} ↔ MAX {maximum}."
        return "Один из чатов уже связан. Сначала используйте /unlink."
