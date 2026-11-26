import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from string import Formatter
from urllib.parse import urlsplit

from dotenv import load_dotenv


def _setting(key: str, default: str = "") -> str:
    return os.getenv(key, "").strip() or default


def _bool(key: str, default: bool) -> bool:
    value = _setting(key, str(default)).lower()
    if value in {"1", "true", "yes", "on"}:
        return True
    if value in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"Некорректный флаг {key}")


def _ids(key: str) -> frozenset[int]:
    try:
        ids = frozenset(int(item.strip()) for item in _setting(key).split(",") if item.strip())
        if any(not 0 < user_id < 2**63 for user_id in ids):
            raise ValueError
        return ids
    except ValueError:
        raise ValueError(f"Некорректный список ID в {key}") from None


@dataclass(frozen=True, slots=True)
class Settings:
    telegram_token: str = field(repr=False)
    max_token: str = field(repr=False)
    telegram_admins: frozenset[int] = frozenset()
    max_admins: frozenset[int] = frozenset()
    database: Path = Path("data/bridge.db")
    log_level: str = "INFO"
    debug: bool = False
    author_template: str = "{author} ({source}):"
    ignore_other_bots: bool = True
    ignore_service_events: bool = True
    max_base_url: str = "https://platform-api2.max.ru"
    max_ca_file: str | None = None
    max_markup_utf16: bool = False

    @classmethod
    def load(cls) -> "Settings":
        load_dotenv()
        telegram_token = _setting("TELEGRAM_BOT_TOKEN")
        max_token = _setting("MAX_BOT_TOKEN")
        if not telegram_token or not max_token:
            raise ValueError("Задайте TELEGRAM_BOT_TOKEN и MAX_BOT_TOKEN")
        log_level = _setting("LOG_LEVEL", "INFO").upper()
        if log_level not in logging.getLevelNamesMapping():
            raise ValueError("Некорректный LOG_LEVEL")
        template = _setting("AUTHOR_TEMPLATE", "{author} ({source}):")
        try:
            fields = set()
            for _, name, spec, conversion in Formatter().parse(template):
                if name is not None:
                    if name not in {"author", "source"} or spec or conversion:
                        raise ValueError
                    fields.add(name)
            if fields != {"author", "source"} or len(template) > 300:
                raise ValueError
        except ValueError:
            raise ValueError("AUTHOR_TEMPLATE требует {author} и {source}") from None
        url = _setting("MAX_API_BASE_URL", "https://platform-api2.max.ru")
        parts = urlsplit(url)
        if (parts.scheme != "https" or parts.netloc not in {"platform-api2.max.ru", "platform-api.max.ru"}
                or parts.path not in {"", "/"} or parts.query or parts.fragment):
            raise ValueError("Некорректный MAX_API_BASE_URL")
        default_database = Path(_setting("STATE_DIRECTORY", "data")) / "bridge.db"
        return cls(
            telegram_token, max_token, _ids("TELEGRAM_ADMIN_IDS"), _ids("MAX_ADMIN_IDS"),
            Path(_setting("DATABASE_PATH", str(default_database))), log_level, _bool("DEBUG", False),
            template, _bool("IGNORE_OTHER_BOTS", True), _bool("IGNORE_SERVICE_EVENTS", True),
            url.rstrip("/"), _setting("MAX_CA_FILE") or None, _bool("MAX_MARKUP_UTF16", False),
        )
