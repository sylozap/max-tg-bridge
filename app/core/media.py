from collections.abc import Sequence
from pathlib import Path

from .model import Message, RichText
from .port import Adapter, DeliveryError, MediaUnavailable
from .retry import retry

_LABELS = {
    "image": "Изображение", "video": "Видео", "audio": "Аудио", "file": "Документ",
    "voice": "Голосовое", "video_note": "Видеокружок", "location": "Геолокация",
    "contact": "Контакт", "sticker_image": "Стикер", "sticker_video": "Видеостикер",
    "animation": "Анимация",
}


async def prepare(
    messages: Sequence[Message], source: Adapter, target: Adapter,
    directory: Path,
) -> tuple[list[object], RichText, bool]:
    items = [item for message in messages for item in message.attachments]
    if not items:
        return [], RichText(), False
    media = []
    notes = RichText()
    degraded = False
    for index, item in enumerate(items):
        if item.kind == "note":
            notes += RichText("\n" + (item.detail or "[Вложение недоступно]"))
            continue
        label = _LABELS.get(item.kind, "Вложение")
        if item.kind == "location" and item.detail:
            notes += RichText("\n" + item.detail)
        elif item.kind in {"sticker_image", "sticker_video", "video_note", "voice", "animation"}:
            hint = item.detail if item.kind.startswith("sticker") else None
            notes += RichText(f"\n[{label}{' ' + hint if hint else ''}]")
        folder = directory / str(index)
        folder.mkdir()
        try:
            local = None
            if item.kind not in {"location", "contact"}:
                limit = target.media_limit(item)
                local = await retry(lambda: source.download(item, folder, limit))
            media.append(await retry(lambda: target.prepare(local.attachment if local else item, local)))
        except MediaUnavailable as exc:
            notes += RichText(f"\n[{label} не передано: {exc}]")
            degraded = True
        except DeliveryError as exc:
            if exc.code in {"telegram_unauthorized", "max_unauthorized", "telegram_forbidden", "max_forbidden"}:
                raise
            notes += RichText(f"\n[{label} не передано: ошибка передачи или обработки файла]")
            degraded = True
    return media, notes, degraded
