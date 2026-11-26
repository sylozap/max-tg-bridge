import asyncio
import logging
import mimetypes
import re
import ssl
from collections.abc import AsyncIterator, Sequence
from dataclasses import replace
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from itertools import groupby
from pathlib import Path
from typing import Any
from urllib.parse import quote, unquote
from uuid import uuid4

import aiofiles
import httpx

from app.core.model import Attachment, Author, Batch, Event, LocalFile, Message, Part, Platform, Reference, RichText
from app.core.port import DeliveryError, MediaUnavailable
from app.formatting.text import from_max, max_html, split_text, utf16_length

log = logging.getLogger(__name__)
_MB = 1_000_000
_LIMITS = {"image": 50 * _MB, "video": 250 * _MB, "audio": 256 * _MB, "file": 4_000 * _MB}
_CDN_DOMAINS = ("max.ru", "oneme.ru", "okcdn.ru", "mycdn.me", "vkuser.net", "userapi.com", "vkvideo.ru", "vk-cdn.net")
_SERVICE = {
    "user_added": "Участник вступил в чат", "user_removed": "Участник вышел из чата",
    "chat_title_changed": "Название чата изменено", "bot_added": "Бот добавлен в чат",
    "bot_removed": "Бот удалён из чата", "bot_admin_permissions_changed": "Права бота изменены",
}
_LABELS = {"image": "Изображение", "video": "Видео", "audio": "Аудио", "file": "Файл"}


def _author(user: dict) -> Author:
    name = " ".join(value for value in (user.get("first_name"), user.get("last_name")) if value)
    return Author(user.get("user_id"), name or user.get("name") or "Неизвестный автор", bool(user.get("is_bot")))


def _vcard_field(vcard: str, key: str) -> str | None:
    text = re.sub(r"\r?\n[ \t]", "", vcard)
    found = re.search(rf"^{key}(?:;[^:\r\n]*)?:([^\r\n]+)", text, re.MULTILINE | re.IGNORECASE)
    return found.group(1).strip() if found else None


def _attachments(body: dict) -> tuple[Attachment, ...]:
    result = []
    for item in body.get("attachments") or ():
        kind, payload = item.get("type"), item.get("payload") or {}
        if kind in {"image", "video", "audio", "file", "sticker"}:
            name = payload.get("filename") or {
                "image": "photo.jpg", "video": "video.mp4", "audio": "audio.mp3",
                "file": "file", "sticker": "sticker.webp",
            }[kind]
            result.append(Attachment(
                "sticker_image" if kind == "sticker" else kind, payload.get("url"),
                name, payload.get("size"), payload.get("duration"), token=payload.get("token"),
            ))
        elif kind == "location":
            lat = item.get("latitude", payload.get("latitude"))
            lon = item.get("longitude", payload.get("longitude"))
            if isinstance(lat, (float, int)) and isinstance(lon, (float, int)) and -90 <= lat <= 90 and -180 <= lon <= 180:
                result.append(Attachment("location", coordinates=(lat, lon)))
            else:
                result.append(Attachment("note", detail="[Геолокация: координаты недоступны]"))
        elif kind == "contact":
            vcard = payload.get("vcf_info") or ""
            name = _vcard_field(vcard, "FN") or _author(payload.get("max_info") or {}).name
            phone = _vcard_field(vcard, "TEL")
            if phone:
                result.append(Attachment("contact", name=name, phone=phone.removeprefix("tel:"), detail=vcard))
            else:
                contact_id = (payload.get("max_info") or {}).get("user_id")
                identifier = f", ID MAX {contact_id}" if contact_id else ""
                result.append(Attachment("note", detail=f"[Контакт: {name}{identifier}, номер недоступен]"))
        elif kind == "share":
            url = payload.get("url")
            if url:
                result.append(Attachment("note", detail=f"[Ссылка] {url}"))
        elif kind == "inline_keyboard":
            result.append(Attachment("note", detail="[Кнопки MAX]"))
        else:
            result.append(Attachment("note", detail="[Вложение MAX: тип не поддерживается Bot API моста]"))
    return tuple(result)


def _rich(body: dict, utf16: bool) -> RichText:
    return from_max(body.get("text") or "", body.get("markup") or (), utf16=utf16)


def normalize(update: dict, *, utf16: bool = False) -> Message | None:
    kind = update.get("update_type")
    timestamp = str(update.get("timestamp") or 0)
    if kind == "message_removed":
        chat, mid = update.get("chat_id"), update.get("message_id")
        if chat is None or mid is None:
            return None
        # user_id identifies the moderator, not necessarily the author.
        return Message(Platform.MAX, chat, str(mid), Author(None, ""), event=Event.DELETE)
    if kind in _SERVICE or kind == "bot_started":
        chat = update.get("chat_id")
        if chat is None:
            return None
        author = _author(update.get("user") or {})
        text = "/start" if kind == "bot_started" else f"[{_SERVICE[kind]}]"
        return Message(Platform.MAX, chat, f"{kind}:{timestamp}:{author.id}", author, RichText(text),
                       event=Event.CREATE if kind == "bot_started" else Event.SERVICE)
    if kind not in {"message_created", "message_edited"}:
        return None
    message = update.get("message") or {}
    body = message.get("body") or {}
    chat = (message.get("recipient") or {}).get("chat_id")
    sender = _author(message.get("sender") or {})
    link = message.get("link") or {}
    linked = link.get("message") or {}
    mid = body.get("mid") or message.get("mid")
    if not mid and link.get("type") == "forward" and linked.get("mid") and timestamp != "0":
        mid = f"forward:{timestamp}:{sender.id}:{linked['mid']}"
    if chat is None or not mid:
        return None
    rich, media = _rich(body, utf16), _attachments(body)
    reference = None
    forward = None
    if link.get("type") == "reply":
        reference = Reference(str(linked.get("mid") or ""), linked.get("text") or "[вложение]")
    elif link.get("type") == "forward":
        forward = _author(link.get("sender") or {}).name if link.get("sender") else "скрытого автора"
        if linked.get("text"):
            rich += (RichText("\n") if rich.text else RichText()) + _rich(linked, utf16)
        media += _attachments(linked)
    event = Event.EDIT if kind == "message_edited" else Event.CREATE
    return Message(Platform.MAX, chat, str(mid), sender, rich, media, event,
                   timestamp if event == Event.EDIT else "", reference, forward)


def _retry_after(value: str | None) -> float:
    if not value:
        return 0
    try:
        if value.isascii() and value.isdigit() and len(value) <= 8:
            return float(value)
        return max(0, (parsedate_to_datetime(value) - datetime.now(timezone.utc)).total_seconds())
    except (ValueError, TypeError, OverflowError):
        return 0


def _status(response: httpx.Response, data: dict | None = None, *, media: bool = False) -> None:
    status = response.status_code
    code = (data or {}).get("code")
    if status == 429:
        raise DeliveryError("max_rate_limit", retry=True, delay=_retry_after(response.headers.get("Retry-After")))
    if status in {408, 425} or status >= 500:
        raise DeliveryError("max_network", retry=True)
    if code == "attachment.not.ready":
        raise DeliveryError("max_attachment_pending", retry=True, delay=1)
    if code in {"not.found", "message.not.found"} or status == 404:
        raise DeliveryError("max_not_found", missing=True)
    if status == 401:
        raise DeliveryError("max_unauthorized")
    if status == 403 or code == "chat.denied":
        raise DeliveryError("max_forbidden")
    if status in {413, 415} or (media and status in {400, 422}):
        raise DeliveryError("media_rejected")
    if not response.is_success or (data or {}).get("success") is False:
        raise DeliveryError(f"max_http_{status}")


def _cdn_url(value: str | httpx.URL) -> httpx.URL:
    url = httpx.URL(value)
    host = url.host.lower()
    if (url.scheme != "https" or url.port not in {None, 443} or url.userinfo
            or not any(host == domain or host.endswith("." + domain) for domain in _CDN_DOMAINS)):
        raise MediaUnavailable("внешний URL вложения запрещён")
    return url


def _upload_kind(attachment: Attachment) -> str:
    suffix = Path(attachment.name or "").suffix.lower()
    if attachment.kind == "image" or attachment.kind == "animation" and suffix == ".gif":
        return "image"
    if attachment.kind in {"video", "video_note", "sticker_video", "animation"}:
        return "video"
    if attachment.kind in {"audio", "voice"}:
        return "audio"
    return "file"


async def _file_stream(path: Path, prefix: bytes, suffix: bytes) -> AsyncIterator[bytes]:
    yield prefix
    async with aiofiles.open(path, "rb") as file:
        while chunk := await file.read(65536):
            yield chunk
    yield suffix


def _max_size(piece: RichText) -> int:
    return utf16_length(max_html(piece))


class MaxAdapter:
    platform = Platform.MAX

    def __init__(self, token: str, base_url: str, *, ca_file: str | None = None, utf16: bool = False):
        tls = ssl.create_default_context()
        if ca_file:
            tls.load_verify_locations(cafile=ca_file)
        self.api = httpx.AsyncClient(base_url=base_url, headers={"Authorization": token},
                                     verify=tls, trust_env=False, timeout=20)
        self.cdn = httpx.AsyncClient(verify=tls, trust_env=False, timeout=30)
        self.bot_id = 0
        self.username = ""
        self.utf16 = utf16
        self._rate_lock = asyncio.Lock()
        self._next_request = 0.0
        self._next_mutation: dict[tuple[str, int], float] = {}

    async def _call(
        self, method: str, path: str, *, params: dict | None = None, body: dict | None = None,
        timeout: float = 20, media: bool = False,
    ) -> dict:
        loop = asyncio.get_running_loop()
        async with self._rate_lock:
            await asyncio.sleep(max(0, self._next_request - loop.time()))
            self._next_request = loop.time() + 1 / 30
        try:
            response = await self.api.request(method, path, params=params, json=body, timeout=timeout)
        except httpx.RequestError:
            raise DeliveryError("max_network", retry=True) from None
        try:
            data = response.json()
        except (ValueError, UnicodeError):
            data = None
        if not isinstance(data, dict):
            data = None
        _status(response, data, media=media)
        if data is None:
            raise DeliveryError("max_invalid_response", retry=True)
        return data

    async def identify(self) -> None:
        profile = await self._call("GET", "/me")
        self.bot_id = profile["user_id"]
        self.username = profile.get("username") or ""

    async def check_chat(self, chat: int) -> None:
        info = await self._call("GET", f"/chats/{chat}")
        if info.get("type") != "chat":
            raise DeliveryError("max_group_required")
        member = await self._call("GET", f"/chats/{chat}/members/me")
        if not member.get("is_owner"):
            perms = set(member.get("permissions") or ())
            if (not member.get("is_admin") or "read_all_messages" not in perms
                    or not {"write", "post_edit_delete_message"} & perms):
                raise DeliveryError("max_admin_required")

    async def register(self, commands: dict[str, str]) -> None:
        await self._call("PATCH", "/me/commands", body={"commands": [
            {"name": name, "description": description} for name, description in commands.items()
        ]})

    async def poll(self, cursor: int | None) -> Batch:
        params: dict[str, Any] = {"limit": 100, "timeout": 25}
        if cursor is not None:
            params["marker"] = cursor
        data = await self._call("GET", "/updates", params=params, timeout=40)
        marker = data.get("marker")
        if not isinstance(data.get("updates"), list) or not isinstance(marker, int):
            raise DeliveryError("max_invalid_poll_response", retry=True)
        messages = []
        for update in data["updates"]:
            try:
                message = normalize(update, utf16=self.utf16)
            except (AttributeError, TypeError, ValueError):
                log.warning("invalid_max_update")
                continue
            if message is not None:
                messages.append(message)
        return Batch(tuple(messages), marker)

    async def _video_url(self, token: str) -> str | None:
        result = await self._call("GET", "/videos/" + quote(token, safe=""))
        urls = result.get("urls") or {}
        return next((urls[f"mp4_{size}"] for size in (1080, 720, 480, 360, 240, 144)
                     if urls.get(f"mp4_{size}")), None)

    async def download(self, attachment: Attachment, folder: Path, limit: int) -> LocalFile:
        if attachment.size is not None and attachment.size > limit:
            raise MediaUnavailable(f"файл превышает лимит {limit // _MB} МБ")
        candidates = [attachment.ref] if attachment.ref else []
        for source in candidates:
            try:
                return await self._download_url(attachment, folder, limit, source)
            except MediaUnavailable:
                if attachment.kind != "video" or not attachment.token:
                    raise
        if attachment.kind == "video" and attachment.token:
            try:
                if url := await self._video_url(attachment.token):
                    return await self._download_url(attachment, folder, limit, url)
            except DeliveryError as exc:
                if exc.retry:
                    raise
                raise MediaUnavailable("MAX не выдал прямую ссылку на видео") from None
        raise MediaUnavailable("MAX не выдал доступную ссылку на файл")

    async def _download_url(self, attachment: Attachment, folder: Path, limit: int, source: str) -> LocalFile:
        url = _cdn_url(source)
        path = folder / "content"
        try:
            async with asyncio.timeout(150):
                for _ in range(5):
                    async with self.cdn.stream("GET", url) as response:
                        if response.is_redirect and (location := response.headers.get("location")):
                            url = _cdn_url(url.join(location))
                            continue
                        _status(response)
                        length = response.headers.get("content-length", "")
                        if length.isdigit() and int(length) > limit:
                            raise MediaUnavailable(f"файл превышает лимит {limit // _MB} МБ")
                        mime = response.headers.get("content-type", "").split(";")[0]
                        if mime.startswith("text/html") or "mpegurl" in mime.lower():
                            raise MediaUnavailable("MAX выдал страницу просмотра вместо скачиваемого файла")
                        total = 0
                        async with aiofiles.open(path, "wb") as file:
                            async for chunk in response.aiter_bytes(65536):
                                total += len(chunk)
                                if total > limit:
                                    raise MediaUnavailable(f"файл превышает лимит {limit // _MB} МБ")
                                await file.write(chunk)
                        if not total:
                            raise MediaUnavailable("MAX вернул пустой файл")
                        return LocalFile(self._media_metadata(attachment, url, mime, total), path)
                raise MediaUnavailable("слишком много переадресаций при скачивании MAX")
        except (httpx.RequestError, TimeoutError):
            raise DeliveryError("max_download_network", retry=True) from None
        except DeliveryError as exc:
            if exc.retry:
                raise
            raise MediaUnavailable("ссылка MAX недоступна или истекла") from None

    @staticmethod
    def _media_metadata(attachment: Attachment, url: httpx.URL, mime: str, size: int) -> Attachment:
        kind = attachment.kind
        name = attachment.name or "file"
        suffixes = {
            "image/jpeg": ".jpg", "image/gif": ".gif", "image/png": ".png", "image/webp": ".webp",
            "video/mp4": ".mp4", "video/webm": ".webm", "audio/mpeg": ".mp3", "audio/mp4": ".m4a",
            "audio/ogg": ".ogg", "application/ogg": ".ogg",
        }
        suffix = suffixes.get(mime) or Path(unquote(url.path)).suffix.lower()
        if kind != "file" and re.fullmatch(r"\.[a-z0-9]{1,8}", suffix):
            name = Path(name).stem + suffix
        if kind in {"image", "sticker_image"} and suffix == ".gif":
            kind = "animation"
        if kind == "audio" and suffix in {".ogg", ".opus"}:
            kind = "voice"
        return replace(attachment, kind=kind, name=name, size=size, mime=mime)

    def media_limit(self, attachment: Attachment) -> int:
        return _LIMITS[_upload_kind(attachment)]

    async def prepare(self, attachment: Attachment, local: LocalFile | None) -> dict:
        if attachment.kind == "location" and attachment.coordinates:
            lat, lon = attachment.coordinates
            return {"type": "location", "latitude": lat, "longitude": lon}
        if attachment.kind == "contact" and attachment.phone:
            name = attachment.name or "Контакт"
            vcard = attachment.detail or self._vcard(name, attachment.phone)
            return {"type": "contact", "payload": {"name": name, "vcf_info": vcard}}
        if not local:
            raise MediaUnavailable("доступный для скачивания файл отсутствует")
        kind = _upload_kind(attachment)
        if kind == "audio" and (attachment.duration or 0) > 3600:
            raise MediaUnavailable("аудио длиннее 60 минут")
        try:
            upload = await self._call("POST", "/uploads", params={"type": kind}, media=True)
            return {"type": kind, "payload": {"token": await self._upload(upload, kind, local)}}
        except DeliveryError as exc:
            if exc.retry or exc.code in {"max_unauthorized", "max_forbidden"}:
                raise
            raise MediaUnavailable(f"MAX отклонил загрузку ({_LABELS[kind].lower()})") from None

    @staticmethod
    def _vcard(name: str, phone: str) -> str:
        def escaped(value: str) -> str:
            return value.replace("\\", "\\\\").replace("\r", "").replace("\n", "\\n").replace(",", "\\,").replace(";", "\\;")
        return f"BEGIN:VCARD\r\nVERSION:3.0\r\nFN:{escaped(name)}\r\nTEL:{escaped(phone)}\r\nEND:VCARD\r\n"

    async def _upload(self, upload: dict, kind: str, local: LocalFile) -> str:
        raw_url = upload.get("url")
        if not raw_url:
            raise MediaUnavailable("MAX не выдал адрес загрузки")
        url = _cdn_url(raw_url)
        name = Path(local.attachment.name or "file").name
        filename = re.sub(r'[\x00-\x1f"\\]', "_", name)[:240] or "file"
        mime = mimetypes.guess_type(filename)[0] or "application/octet-stream"
        boundary = uuid4().hex
        prefix = (
            f"--{boundary}\r\nContent-Disposition: form-data; name=\"data\"; filename=\"{filename}\"\r\n"
            f"Content-Type: {mime}\r\n\r\n"
        ).encode()
        suffix = f"\r\n--{boundary}--\r\n".encode()
        size = await asyncio.to_thread(lambda: local.path.stat().st_size)
        headers = {"Content-Type": f"multipart/form-data; boundary={boundary}",
                   "Content-Length": str(len(prefix) + size + len(suffix))}
        try:
            async with asyncio.timeout(180):
                for _ in range(4):
                    response = await self.cdn.post(url, headers=headers, content=_file_stream(local.path, prefix, suffix))
                    if response.status_code in {307, 308} and (location := response.headers.get("location")):
                        url = _cdn_url(url.join(location))
                        continue
                    _status(response, media=True)
                    break
                else:
                    raise MediaUnavailable("слишком много переадресаций при загрузке MAX")
        except (httpx.RequestError, TimeoutError):
            raise DeliveryError("max_upload_network", retry=True) from None
        try:
            data = response.json()
        except ValueError:
            data = {}
        token = None
        if isinstance(data, dict):
            token = data.get("token")
            photos = data.get("photos") or {}
            if isinstance(photos, dict):
                token = token or next((photo.get("token") for photo in photos.values()
                                       if isinstance(photo, dict) and photo.get("token")), None)
        if kind in {"video", "audio"}:
            accepted = re.search(r"<retval>\s*1\s*</retval>", response.text) or token
            if accepted:
                token = token or upload.get("token")
        if not isinstance(token, str) or not token:
            raise MediaUnavailable("MAX не подтвердил загрузку файла")
        return token

    def plan(self, content: RichText, media: Sequence[dict]) -> list[Part]:
        chunks = split_text(content, 4000, _max_size)
        if media and _max_size(chunks[0]) > 3800:
            first = split_text(content, 3800, _max_size)[0]
            rest = content.slice(len(first.text), len(content.text))
            chunks = [first, *split_text(rest, 4000, _max_size)]
        groups = []
        for visual, group in groupby(media, key=lambda item: item["type"] in {"image", "video"}):
            items = list(group)
            count = 12 if visual else 1
            groups.extend(tuple(items[index:index + count]) for index in range(0, len(items), count))
        if not groups:
            return [Part(piece) for piece in chunks]
        first, *rest = groups
        return [Part(chunks[0], first, "media"), *(Part(piece) for piece in chunks[1:]),
                *(Part(RichText(), group, "media") for group in rest)]

    @staticmethod
    def _body(part: Part) -> dict:
        return {"text": max_html(part.content), "format": "html", "attachments": list(part.media)}

    async def send(self, chat: int, part: Part, reply: str | None) -> str:
        body = self._body(part)
        if reply:
            body["link"] = {"type": "reply", "mid": reply}
        data = await self._call("POST", "/messages", params={"chat_id": chat, "disable_link_preview": "true"},
                                body=body, media=bool(part.media))
        mid = ((data.get("message") or data).get("body") or {}).get("mid")
        if not isinstance(mid, str) or not mid:
            raise DeliveryError("max_missing_sent_id", retry=True)
        return mid

    async def _limit_mutation(self, method: str, chat: int) -> None:
        key = (method, chat)
        loop = asyncio.get_running_loop()
        while self._next_mutation.get(key, 0) > loop.time():
            await asyncio.sleep(max(0, self._next_mutation[key] - loop.time()))
        self._next_mutation[key] = loop.time() + 0.51

    async def edit(self, chat: int, mid: str, part: Part, old_mode: str, *, replace_media: bool = True) -> None:
        await self._limit_mutation("PUT", chat)
        body = self._body(part)
        if not replace_media:
            body.pop("attachments")
        body["notify"] = False
        await self._call("PUT", "/messages", params={"message_id": mid}, body=body, media=bool(part.media))

    async def delete(self, chat: int, mid: str) -> None:
        await self._limit_mutation("DELETE", chat)
        try:
            await self._call("DELETE", "/messages", params={"message_id": mid})
        except DeliveryError as exc:
            if not exc.missing:
                raise

    async def close(self) -> None:
        await asyncio.gather(self.api.aclose(), self.cdn.aclose())
