import json
import logging
from datetime import datetime, timezone

_FIELDS = (
    "attempt", "code", "delay", "error_type", "event", "message_count", "pair_id", "platform",
)


class BridgeFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        data = {
            "time": datetime.fromtimestamp(record.created, timezone.utc).isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "logger": record.name,
            "action": record.getMessage(),
        }
        for key in _FIELDS:
            if hasattr(record, key):
                data[key] = getattr(record, key)
        return json.dumps(data, ensure_ascii=False)


def configure(level: str, debug: bool) -> None:
    handler = logging.StreamHandler()
    handler.setFormatter(BridgeFormatter())
    # HTTP client diagnostics can contain request URLs or bodies.
    handler.addFilter(logging.Filter("app"))
    logging.basicConfig(level="DEBUG" if debug else level, handlers=[handler], force=True)
