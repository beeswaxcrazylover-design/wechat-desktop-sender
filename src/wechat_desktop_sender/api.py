"""Manual-batch adapter for the unchanged durable queue and Engine."""
from dataclasses import dataclass
import json
from pathlib import Path

from .simple_core import Ledger, require


@dataclass(frozen=True)
class Cursor:
    next_start: float = 0


@dataclass(frozen=True)
class Batch:
    events: tuple
    next_cursor: Cursor
    status: str = "READY"
    held: tuple = ()


class ManualBatch:
    """Stable caller IDs and exact text. No network, business parser or receiver."""
    def __init__(self, route, messages):
        require(isinstance(messages, list), "MESSAGE_LIST_REQUIRED")
        events = []
        for index, row in enumerate(messages):
            require(isinstance(row, dict) and set(row) == {"id", "text"}, "MESSAGE_FIELDS_INVALID")
            require(isinstance(row["id"], str) and bool(row["id"]), "STABLE_MESSAGE_ID_REQUIRED")
            require(isinstance(row["text"], str), "TEXT_REQUIRED")
            events.append({"account_id":route.account_id, "source_id":route.source_id,
                "event_id":row["id"], "occurred_at":index+1, "message_type":"text", "text":row["text"]})
        self.events = tuple(events)

    def initial_cursor(self):
        return Cursor()

    def poll(self, cursor, now):
        return Batch(self.events if cursor.next_start == 0 else (), Cursor(1))


def load_messages(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def dry_run(configuration, messages):
    """In-memory queue validation only; never imports desktop/clipboard modules."""
    source = ManualBatch(configuration.route, messages)
    ledger = Ledger(":memory:")
    try:
        batch = source.poll(source.initial_cursor(), 0)
        counts = ledger.accept_batch(configuration.route, batch.events, not_before=0)
        return {"status":"DRY_RUN", "desktop_calls":0, "counts":counts,
                "jobs":ledger.stats(configuration.route)}
    finally:
        ledger.close()

