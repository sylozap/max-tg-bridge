import asyncio
import sqlite3
from collections.abc import AsyncIterator, Iterable, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass
from importlib.resources import files
from pathlib import Path

import aiosqlite

from app.core.model import Message, Platform


@dataclass(frozen=True, slots=True)
class Pair:
    id: int
    telegram_chat: int
    max_chat: int
    paused: bool
    generation: int
    delivered: int
    failed: int
    last_delivery: str | None
    last_error: str | None

    def chat(self, platform: Platform) -> int:
        return self.telegram_chat if platform == Platform.TELEGRAM else self.max_chat


@dataclass(frozen=True, slots=True)
class Copy:
    part: int
    mid: str
    mode: str


def identity(message: Message) -> tuple[str, int, str, str, str]:
    return message.platform, message.chat, message.id, message.event, message.revision



@dataclass(frozen=True, slots=True)
class Original:
    mid: str
    root: str
    media_key: str | None
    has_caption: bool


def media_key(message: Message) -> str | None:
    return message.attachments[0].key if len(message.attachments) == 1 else None


class Store:
    def __init__(self, connection: aiosqlite.Connection):
        self.db = connection
        self.lock = asyncio.Lock()

    @classmethod
    @asynccontextmanager
    async def open(cls, path: Path) -> AsyncIterator["Store"]:
        path.parent.mkdir(parents=True, exist_ok=True)
        async with aiosqlite.connect(path, isolation_level=None) as db:
            db.row_factory = sqlite3.Row
            await db.execute("PRAGMA journal_mode = WAL")
            await db.execute("PRAGMA synchronous = FULL")
            await db.execute("PRAGMA busy_timeout = 5000")
            await db.execute("PRAGMA foreign_keys = ON")
            async with db.execute("PRAGMA user_version") as cursor:
                version = (await cursor.fetchone())[0]
            if version == 0:
                schema = files("app.storage").joinpath("schema.sql").read_text(encoding="utf-8")
                await db.executescript("BEGIN IMMEDIATE;\n" + schema + "\nCOMMIT;")
            elif version == 1:
                await db.executescript("""
                    BEGIN IMMEDIATE;
                    ALTER TABLE originals ADD COLUMN media_key TEXT;
                    ALTER TABLE originals ADD COLUMN has_caption INTEGER NOT NULL DEFAULT 1;
                    PRAGMA user_version = 2;
                    COMMIT;
                """)
            elif version != 2:
                raise RuntimeError("Unsupported SQLite schema version")
            yield cls(db)

    @asynccontextmanager
    async def _write(self) -> AsyncIterator[aiosqlite.Connection]:
        async with self.lock:
            try:
                await self.db.execute("BEGIN IMMEDIATE")
                yield self.db
                await self.db.commit()
            except BaseException:
                await self.db.rollback()
                raise

    async def _rows(self, sql: str, args: tuple = ()) -> list[sqlite3.Row]:
        async with self.lock:
            async with self.db.execute(sql, args) as cursor:
                return list(await cursor.fetchall())

    async def _one(self, sql: str, args: tuple = ()) -> sqlite3.Row | None:
        rows = await self._rows(sql, args)
        return rows[0] if rows else None

    async def pair_for(self, platform: Platform, chat: int) -> Pair | None:
        column = "telegram_chat" if platform == Platform.TELEGRAM else "max_chat"
        row = await self._one(f"SELECT * FROM pairs WHERE {column} = ?", (chat,))
        return Pair(**row) if row else None

    async def pair(self, pair_id: int) -> Pair | None:
        row = await self._one("SELECT * FROM pairs WHERE id = ?", (pair_id,))
        return Pair(**row) if row else None

    async def pairs(self) -> list[Pair]:
        return [Pair(**row) for row in await self._rows("SELECT * FROM pairs ORDER BY id")]

    async def link(self, telegram: int, max_chat: int) -> bool:
        async with self._write() as db:
            cur = await db.execute(
                "INSERT INTO pairs(telegram_chat, max_chat) VALUES (?, ?) ON CONFLICT DO NOTHING",
                (telegram, max_chat),
            )
            return cur.rowcount == 1

    async def unlink(self, pair_id: int) -> None:
        async with self._write() as db:
            await db.execute("DELETE FROM pairs WHERE id = ?", (pair_id,))

    async def pause(self, pair_id: int, paused: bool) -> None:
        async with self._write() as db:
            await db.execute(
                "UPDATE pairs SET paused = ?, generation = generation + 1 WHERE id = ? AND paused != ?",
                (paused, pair_id, paused),
            )

    async def cursor(self, platform: Platform, bot: int) -> int | None:
        row = await self._one(
            "SELECT cursor, saved_at FROM cursors WHERE platform = ? AND bot = ?", (platform, bot)
        )
        return row["cursor"] if row else None

    async def save_cursor(self, platform: Platform, bot: int, cursor: int) -> None:
        async with self._write() as db:
            await db.execute(
                """INSERT INTO cursors(platform, bot, cursor) VALUES (?, ?, ?)
                   ON CONFLICT(platform, bot) DO UPDATE SET cursor=excluded.cursor, saved_at=excluded.saved_at""",
                (platform, bot, cursor),
            )

    async def seen(self, message: Message) -> bool:
        return await self._one(
            "SELECT 1 FROM receipts WHERE platform=? AND chat=? AND mid=? AND event=? AND revision=?",
            identity(message),
        ) is not None

    async def finish(
        self, messages: Sequence[Message], pair_id: int | None = None, *,
        delivered: bool = False, error: str | None = None, warning: str | None = None,
    ) -> None:
        async with self._write() as db:
            cur = await db.executemany(
                "INSERT OR IGNORE INTO receipts VALUES (?, ?, ?, ?, ?)", map(identity, messages)
            )
            if not cur.rowcount or pair_id is None:
                return
            if error:
                await db.execute(
                    "UPDATE pairs SET failed=failed+?, last_error=? WHERE id=?", (cur.rowcount, error, pair_id)
                )
            elif delivered:
                await db.execute(
                    """UPDATE pairs SET delivered=delivered+?, last_delivery=strftime('%Y-%m-%dT%H:%M:%SZ','now'),
                       last_error=coalesce(?,last_error) WHERE id=?""",
                    (cur.rowcount, warning, pair_id),
                )

    async def is_outgoing(self, platform: Platform, chat: int, mid: str) -> bool:
        return await self._one("SELECT 1 FROM outgoing WHERE platform=? AND chat=? AND mid=?",
                               (platform, chat, mid)) is not None

    async def outgoing(self, platform: Platform, chat: int, mid: str) -> None:
        async with self._write() as db:
            await db.execute("INSERT OR IGNORE INTO outgoing VALUES (?, ?, ?)", (platform, chat, mid))

    async def record_copy(
        self, pair: Pair, source: Platform, root: str, originals: Iterable[Message], copy: Copy,
    ) -> None:
        async with self._write() as db:
            await db.execute("INSERT OR IGNORE INTO outgoing VALUES (?, ?, ?)",
                             (source.opposite, pair.chat(source.opposite), copy.mid))
            # A concurrent /unlink can remove the pair during the remote call.
            async with db.execute("SELECT 1 FROM pairs WHERE id=?", (pair.id,)) as cur:
                if not await cur.fetchone():
                    return
            await db.executemany(
                "INSERT OR IGNORE INTO originals(pair, source, mid, root, position, media_key, has_caption) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                ((pair.id, source, message.id, root, index, media_key(message), bool(message.content.text))
                 for index, message in enumerate(originals)),
            )
            await db.execute(
                """INSERT INTO copies VALUES (?, ?, ?, ?, ?, ?)
                   ON CONFLICT(pair,source,root,part) DO UPDATE SET target_mid=excluded.target_mid, mode=excluded.mode""",
                (pair.id, source, root, copy.part, copy.mid, copy.mode),
            )

    async def copies(self, pair: Pair, source: Platform, root: str) -> list[Copy]:
        rows = await self._rows(
            "SELECT part, target_mid, mode FROM copies WHERE pair=? AND source=? AND root=? ORDER BY part",
            (pair.id, source, root),
        )
        return [Copy(*row) for row in rows]

    async def forget_copy(self, pair: Pair, source: Platform, root: str, part: int) -> None:
        async with self._write() as db:
            await db.execute("DELETE FROM copies WHERE pair=? AND source=? AND root=? AND part=?",
                             (pair.id, source, root, part))

    async def original(self, pair: Pair, source: Platform, mid: str) -> Original | None:
        row = await self._one(
            "SELECT mid, root, media_key, has_caption FROM originals WHERE pair=? AND source=? AND mid=?",
            (pair.id, source, mid),
        )
        return Original(*row) if row else None

    async def has_other_captions(self, pair: Pair, source: Platform, root: str, mid: str) -> bool:
        return await self._one(
            "SELECT 1 FROM originals WHERE pair=? AND source=? AND root=? AND mid!=? AND has_caption=1 LIMIT 1",
            (pair.id, source, root, mid),
        ) is not None

    async def update_original(self, pair: Pair, message: Message) -> None:
        async with self._write() as db:
            await db.execute("UPDATE originals SET has_caption=?, media_key=? WHERE pair=? AND source=? AND mid=?",
                             (bool(message.content.text), media_key(message), pair.id, message.platform, message.id))

    async def reply_target(self, pair: Pair, source: Platform, mid: str) -> str | None:
        direct = await self._one(
            """SELECT c.target_mid FROM originals o JOIN copies c USING(pair, source, root)
               WHERE o.pair=? AND o.source=? AND o.mid=? ORDER BY c.part LIMIT 1""",
            (pair.id, source, mid),
        )
        if direct:
            return direct[0]
        reverse = await self._one(
            """SELECT o.mid FROM copies c JOIN originals o USING(pair, source, root)
               WHERE c.pair=? AND c.source=? AND c.target_mid=? ORDER BY o.position LIMIT 1""",
            (pair.id, source.opposite, mid),
        )
        return reverse[0] if reverse else None
