import asyncio
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, TypeVar

import aiofiles
from aiohttp import ClientError
from aiogram import Bot
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.exceptions import (
    TelegramAPIError, TelegramBadRequest, TelegramConflictError, TelegramForbiddenError,
    TelegramNetworkError, TelegramNotFound, TelegramRetryAfter, TelegramServerError, TelegramUnauthorizedError,
)
from aiogram.methods import GetUpdates, TelegramMethod
from aiogram.types import (
    BotCommand, FSInputFile, InputMediaAnimation, InputMediaAudio, InputMediaDocument,
    InputMediaPhoto, InputMediaVideo, Message as TelegramMessage, ReplyParameters, Update,
)

from app.core.model import Attachment, Author, Batch, Event, LocalFile, Message, Part, Platform, Reference, RichText
from app.core.port import DeliveryError, MediaUnavailable
from app.formatting.text import from_telegram, split_text, telegram_entities, utf16_length

T = TypeVar("T")
_MB = 1_000_000
_ALLOWED = ["message", "edited_message", "channel_post", "edited_channel_post"]
_CAPTIONED = {"photo", "video", "document", "audio", "voice", "animation"}
_SERVICE = {
    "new_chat_members", "left_chat_member", "new_chat_title", "new_chat_photo", "delete_chat_photo",
    "group_chat_created", "supergroup_chat_created", "channel_chat_created", "message_auto_delete_timer_changed",
    "migrate_to_chat_id", "migrate_from_chat_id", "pinned_message", "forum_topic_created", "forum_topic_edited",
    "forum_topic_closed", "forum_topic_reopened", "general_forum_topic_hidden", "general_forum_topic_unhidden",
    "video_chat_scheduled", "video_chat_started", "video_chat_ended", "video_chat_participants_invited",
    "chat_shared", "users_shared", "write_access_allowed", "successful_payment", "refunded_payment",
    "boost_added", "connected_website", "passport_data", "proximity_alert_triggered",
}
_SERVICE_LABELS = {
    "new_chat_members": "Участники вступили в чат", "left_chat_member": "Участник вышел из чата",
    "new_chat_title": "Название чата изменено", "new_chat_photo": "Изменено фото чата",
    "pinned_message": "Сообщение закреплено",
}
_EDIT_MEDIA = {
    "animation": InputMediaAnimation, "audio": InputMediaAudio, "document": InputMediaDocument,
    "photo": InputMediaPhoto, "video": InputMediaVideo,
}


def _text_size(content: RichText) -> int:
    return utf16_length(content.text)


def _message(update: Update) -> TelegramMessage | None:
    return update.message or update.edited_message or update.channel_post or update.edited_channel_post


def _media_name(kind: str) -> str:
    return {
        "photo": "photo.jpg", "animation": "animation.mp4", "video": "video.mp4",
        "video_note": "video-note.mp4", "voice": "voice.ogg", "audio": "audio.mp3", "document": "file",
    }[kind]


def _attachments(m: TelegramMessage) -> tuple[Attachment, ...]:
    if m.photo:
        photo = m.photo[-1]
        return (Attachment("image", photo.file_id, "photo.jpg", photo.file_size, mime="image/jpeg", key=photo.file_unique_id),)
    for kind in ("animation", "video", "video_note", "voice", "audio", "document"):
        media = getattr(m, kind)
        if media:
            return (Attachment(
                "file" if kind == "document" else kind,
                media.file_id, getattr(media, "file_name", None) or _media_name(kind),
                media.file_size, getattr(media, "duration", None), mime=getattr(media, "mime_type", None),
                key=media.file_unique_id,
            ),)
    if m.sticker:
        sticker = m.sticker
        if sticker.is_animated:
            return (Attachment("note", detail=f"[Анимированный стикер Telegram {sticker.emoji or ''}]"),)
        return (Attachment(
            "sticker_video" if sticker.is_video else "sticker_image", sticker.file_id,
            "sticker.webm" if sticker.is_video else "sticker.webp", sticker.file_size,
            detail=sticker.emoji, key=sticker.file_unique_id,
        ),)
    if m.venue or m.location:
        point = m.venue.location if m.venue else m.location
        label = f"[Место: {m.venue.title}, {m.venue.address}]" if m.venue else None
        return (Attachment("location", coordinates=(point.latitude, point.longitude), detail=label),)
    if m.contact:
        contact = m.contact
        return (Attachment("contact", name=" ".join(filter(None, (contact.first_name, contact.last_name))),
                           phone=contact.phone_number, detail=contact.vcard),)
    if m.poll:
        answer = "\n".join(f"• {option.text}" for option in m.poll.options)
        return (Attachment("note", detail=f"[Опрос: {m.poll.question}]\n{answer}"),)
    if m.dice:
        return (Attachment("note", detail=f"[Игра {m.dice.emoji}: {m.dice.value}]"),)
    if m.game:
        return (Attachment("note", detail=f"[Игра: {m.game.title}]\n{m.game.description}"),)
    if m.checklist:
        tasks = "\n".join(f"• {item.text}" for item in m.checklist.tasks)
        return (Attachment("note", detail=f"[Чек-лист: {m.checklist.title}]\n{tasks}"),)
    kind = str(m.content_type)
    if kind not in _SERVICE | {"text", "unknown"}:
        return (Attachment("note", detail=f"[Вложение Telegram: {kind}]"),)
    return ()


def normalize(update: Update) -> Message | None:
    m = _message(update)
    if m is None:
        return None
    user = m.from_user
    group = m.sender_chat
    author = Author(
        user.id if user and not group else None,
        group.title if group and group.title else user.full_name if user else "Неизвестный автор",
        bool(user and user.is_bot and not group),
    )
    event = Event.EDIT if update.edited_message or update.edited_channel_post else Event.CREATE
    content = from_telegram(m.text or m.caption or "", m.entities or m.caption_entities or ())
    if str(m.content_type) in _SERVICE:
        event = Event.SERVICE
        content += RichText(f"[{_SERVICE_LABELS.get(str(m.content_type), 'Служебное событие Telegram')}]")
    reference = None
    if isinstance(m.reply_to_message, TelegramMessage):
        original = m.reply_to_message
        quote = m.quote.text if m.quote else original.text or original.caption or "[вложение]"
        reference = Reference(str(original.message_id), quote)
    elif m.quote or m.external_reply:
        reference = Reference("", m.quote.text if m.quote else "[сообщение из другого чата]")
    forward = None
    if m.forward_origin:
        origin = m.forward_origin
        sender_chat = getattr(origin, "sender_chat", None) or getattr(origin, "chat", None)
        sender_user = getattr(origin, "sender_user", None)
        forward = (sender_user.full_name if sender_user else sender_chat.title if sender_chat
                   else getattr(origin, "sender_user_name", None)) or "скрытого автора"
    return Message(Platform.TELEGRAM, m.chat.id, str(m.message_id), author, content, _attachments(m), event,
                   str(update.update_id) if event == Event.EDIT else "", reference, forward, m.media_group_id)


@dataclass(frozen=True, slots=True)
class TelegramMedia:
    kind: str
    value: FSInputFile | tuple[float, float] | tuple[str, str, str | None]


class TelegramAdapter:
    platform = Platform.TELEGRAM
    _album_wait = 0.65

    def __init__(self, token: str):
        self.bot = Bot(token, session=AiohttpSession(timeout=60))
        self.bot_id = self.bot.id
        self.username = ""

    async def _call(self, method: TelegramMethod[T], *, timeout: int | None = None) -> T:
        try:
            return await self.bot(method, request_timeout=timeout)
        except TelegramRetryAfter as exc:
            raise DeliveryError("telegram_rate_limit", retry=True, delay=exc.retry_after) from None
        except (TelegramNetworkError, TelegramServerError):
            raise DeliveryError("telegram_network", retry=True) from None
        except TelegramUnauthorizedError:
            raise DeliveryError("telegram_unauthorized") from None
        except TelegramConflictError:
            raise DeliveryError("telegram_poll_conflict") from None
        except TelegramForbiddenError:
            raise DeliveryError("telegram_forbidden") from None
        except (TelegramNotFound, TelegramBadRequest) as exc:
            description = exc.message.lower()
            if "message is not modified" in description:
                return None
            if "not found" in description and ("message to" in description or "reply message" in description):
                raise DeliveryError("telegram_message_missing", missing=True) from None
            if not any(word in description for word in ("chat not found", "not enough rights", "can't parse entities")):
                raise DeliveryError("telegram_rejected") from None
            raise DeliveryError("telegram_bad_request") from None
        except TelegramAPIError:
            raise DeliveryError("telegram_api_error") from None

    async def identify(self) -> None:
        from aiogram.methods import GetMe
        profile = await self._call(GetMe())
        self.bot_id, self.username = profile.id, profile.username or ""

    async def check_chat(self, chat: int) -> None:
        from aiogram.methods import GetChat, GetChatMember
        info = await self._call(GetChat(chat_id=chat))
        if info.type not in {"group", "supergroup"}:
            raise DeliveryError("telegram_group_required")
        member = await self._call(GetChatMember(chat_id=chat, user_id=self.bot_id))
        if member.status not in {"administrator", "creator"}:
            raise DeliveryError("telegram_admin_required")

    async def register(self, commands: dict[str, str]) -> None:
        from aiogram.methods import SetMyCommands
        await self._call(SetMyCommands(commands=[
            BotCommand(command=name, description=description) for name, description in commands.items()
        ]))

    async def poll(self, cursor: int | None) -> Batch:
        request = GetUpdates(offset=cursor, limit=100, timeout=25, allowed_updates=_ALLOWED)
        updates = await self._call(request, timeout=35)
        if not updates:
            return Batch((), cursor)
        last = _message(updates[-1])
        if last and last.media_group_id and updates[-1].message:
            if len(updates) < 100:
                await asyncio.sleep(self._album_wait)
                # Asking with the same offset leaves the entire page unconfirmed.
                updates = await self._call(request.model_copy(update={"timeout": 0}), timeout=10)
                if not updates:
                    return Batch((), cursor)
            if len(updates) == 100:
                last = _message(updates[-1])
                tail = last.media_group_id if last and updates[-1].message else None
                if tail:
                    start = len(updates) - 1
                    for i, update in enumerate(updates):
                        item = update.message
                        if item and item.chat.id == last.chat.id and item.media_group_id == tail:
                            start = i
                            break
                    if start:
                        updates = updates[:start]
        messages = tuple(m for update in updates if (m := normalize(update)) is not None)
        return Batch(messages, updates[-1].update_id + 1)

    async def download(self, attachment: Attachment, folder: Path, limit: int) -> LocalFile:
        limit = min(limit, 20 * _MB)
        if not attachment.ref:
            raise MediaUnavailable("Telegram не передал идентификатор файла")
        if attachment.size is not None and attachment.size > limit:
            raise MediaUnavailable(f"файл превышает лимит {limit // _MB} МБ")
        try:
            from aiogram.methods import GetFile
            file = await self._call(GetFile(file_id=attachment.ref))
        except DeliveryError as exc:
            if exc.retry:
                raise
            raise MediaUnavailable("Telegram не выдал ссылку на файл") from None
        if not file.file_path:
            raise MediaUnavailable("Telegram не выдал ссылку на файл")
        url = self.bot.session.api.file_url(self.bot.token, file.file_path)
        chunks = self.bot.session.stream_content(url=url, timeout=60, chunk_size=65536)
        return await self._write_media(attachment, folder / "content", chunks, limit)

    async def _write_media(
        self, attachment: Attachment, path: Path, chunks: AsyncIterator[bytes], limit: int,
    ) -> LocalFile:
        from contextlib import aclosing
        try:
            async with aclosing(chunks), aiofiles.open(path, "wb") as file:
                total = 0
                async for chunk in chunks:
                    total += len(chunk)
                    if total > limit:
                        raise MediaUnavailable(f"файл превышает лимит {limit // _MB} МБ")
                    await file.write(chunk)
            if not total:
                raise MediaUnavailable("Telegram вернул пустой файл")
        except (ClientError, TimeoutError):
            raise DeliveryError("telegram_download_network", retry=True) from None
        return LocalFile(attachment, path)

    def media_limit(self, attachment: Attachment) -> int:
        return 50 * _MB

    async def prepare(self, attachment: Attachment, local: LocalFile | None) -> TelegramMedia:
        if attachment.kind == "location" and attachment.coordinates:
            return TelegramMedia("location", attachment.coordinates)
        if attachment.kind == "contact" and attachment.phone:
            return TelegramMedia("contact", (attachment.name or "Контакт", attachment.phone, attachment.detail))
        if not local:
            raise MediaUnavailable("доступный для скачивания файл отсутствует")
        name = attachment.name or "file"
        suffix = Path(name).suffix.lower()
        kind = {
            "image": "photo" if suffix in {".jpg", ".jpeg", ".png", ".webp", ""} else "document",
            "sticker_image": "photo", "sticker_video": "document", "video_note": "video_note",
            "video": "video" if suffix in {".mp4", ""} else "document",
            "audio": "audio" if suffix in {".mp3", ".m4a", ""} else "document",
            "voice": "voice" if suffix in {".ogg", ".opus", ".mp3", ".m4a", ""} else "document",
            "animation": "animation", "file": "document",
        }.get(attachment.kind, "document")
        if kind == "photo" and (attachment.size or 0) > 10 * _MB:
            kind = "document"
        return TelegramMedia(kind, FSInputFile(local.path, filename=name))

    def plan(self, content: RichText, media: Sequence[TelegramMedia]) -> list[Part]:
        result = []
        if media and media[0].kind in _CAPTIONED:
            caption = split_text(content, 1024, _text_size)[0]
            result.append(Part(caption, (media[0],), f"media:{media[0].kind}"))
            if len(caption.text) < len(content.text):
                rest = content.slice(len(caption.text), len(content.text))
                result.extend(Part(piece) for piece in split_text(rest, 4096, _text_size))
            media = media[1:]
        else:
            result.extend(Part(piece) for piece in split_text(content, 4096, _text_size))
        result.extend(Part(RichText(), (item,), f"media:{item.kind}") for item in media)
        return result

    @staticmethod
    def _reply(mid: str | None) -> ReplyParameters | None:
        return ReplyParameters(message_id=int(mid), allow_sending_without_reply=False) if mid else None

    async def send(self, chat: int, part: Part, reply: str | None) -> str:
        from aiogram import methods
        options: dict[str, Any] = {"chat_id": chat, "reply_parameters": self._reply(reply)}
        if not part.media:
            message = await self._call(methods.SendMessage(
                **options, text=part.content.text, entities=telegram_entities(part.content),
                link_preview_options={"is_disabled": True},
            ))
            return str(message.message_id)
        (media,) = part.media
        if media.kind in _CAPTIONED:
            options.update(caption=part.content.text or None, caption_entities=telegram_entities(part.content))
        if media.kind == "location":
            latitude, longitude = media.value
            method = methods.SendLocation(**options, latitude=latitude, longitude=longitude)
        elif media.kind == "contact":
            name, phone, vcard = media.value
            method = methods.SendContact(**options, first_name=name[:128], phone_number=phone,
                                         vcard=vcard if vcard and len(vcard.encode()) <= 2048 else None)
        else:
            methods_for = {
                "photo": methods.SendPhoto, "audio": methods.SendAudio, "document": methods.SendDocument,
                "animation": methods.SendAnimation, "video": methods.SendVideo,
                "voice": methods.SendVoice, "video_note": methods.SendVideoNote,
            }
            method = methods_for[media.kind](**options, **{media.kind: media.value})
        try:
            message = await self._call(method)
        except DeliveryError as exc:
            if exc.code != "telegram_rejected":
                raise
            if media.kind in _CAPTIONED | {"video_note"} and media.kind != "document":
                try:
                    message = await self._call(methods.SendDocument(
                        chat_id=chat, document=media.value, reply_parameters=self._reply(reply),
                        caption=part.content.text or None, caption_entities=telegram_entities(part.content),
                    ))
                except DeliveryError as fallback:
                    if fallback.code == "telegram_rejected":
                        raise DeliveryError("media_rejected") from None
                    raise
            else:
                raise DeliveryError("media_rejected") from None
        return str(message.message_id)

    async def edit(self, chat: int, mid: str, part: Part, old_mode: str, *, replace_media: bool = True) -> None:
        from aiogram import methods
        options = {"chat_id": chat, "message_id": int(mid)}
        kind = part.mode.removeprefix("media:")
        if old_mode == part.mode == "text":
            await self._call(methods.EditMessageText(
                **options, text=part.content.text, entities=telegram_entities(part.content),
                link_preview_options={"is_disabled": True},
            ))
        elif old_mode == part.mode and not replace_media:
            if kind in _CAPTIONED:
                await self._call(methods.EditMessageCaption(
                    **options, caption=part.content.text or None, caption_entities=telegram_entities(part.content),
                ))
        elif kind in _EDIT_MEDIA and (old_mode == "text" or old_mode.removeprefix("media:") in _EDIT_MEDIA):
            (media,) = part.media
            await self._call(methods.EditMessageMedia(
                **options, media=_EDIT_MEDIA[kind](media=media.value, caption=part.content.text or None,
                                                  caption_entities=telegram_entities(part.content)),
            ))
        else:
            raise DeliveryError("edit_requires_replacement")

    async def delete(self, chat: int, mid: str) -> None:
        from aiogram.methods import DeleteMessage
        try:
            await self._call(DeleteMessage(chat_id=chat, message_id=int(mid)))
        except DeliveryError as exc:
            if not exc.missing:
                raise

    async def close(self) -> None:
        await self.bot.session.close()
