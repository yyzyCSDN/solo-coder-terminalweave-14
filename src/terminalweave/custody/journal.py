from __future__ import annotations

from terminalweave.core.errors import ConflictError
from terminalweave.persistence.event_store import InMemoryEventStore
from .chain import (
    CustodyLedger,
    HandoffRecord,
    HandoffRequest,
    replay_records,
)

_EVENT_TYPE = 'custody.handover'
_STREAM_PREFIX = 'custody:'


class CustodyJournal:
    """Durable custody service built on the append-only event store.

    Each accepted handover is appended to a per-container stream with
    optimistic concurrency. After an offline outage ``recover`` re-reads the
    streams and rebuilds ledger state, so messages can be re-submitted
    safely: duplicates collapse to the stored event and no custody is moved
    twice.
    """

    def __init__(self, store: InMemoryEventStore | None = None) -> None:
        self.store = store or InMemoryEventStore()
        self.ledger = CustodyLedger()

    def submit(self, req: HandoffRequest) -> HandoffRecord:
        # Offline retry: return the already-persisted handover unchanged.
        existing = self.ledger.known_message(req.message_id)
        if existing is not None:
            return existing
        stream = _STREAM_PREFIX + req.container_id
        expected = self.store.current_sequence(stream)
        record = self.ledger.submit(req)
        payload = record.to_dict()
        try:
            self.store.append(stream, expected, _EVENT_TYPE, payload, record.at)
        except ConflictError:
            # Another writer appended first; rebuild and re-evaluate the message.
            self.recover()
            existing = self.ledger.known_message(req.message_id)
            if existing is not None:
                return existing
            return self.submit(req)
        return record

    def recover(self) -> int:
        """Re-read every custody stream and rebuild the in-memory ledger."""
        records: list[HandoffRecord] = []
        for stream in self.store.all_streams():
            if not stream.startswith(_STREAM_PREFIX):
                continue
            for event in self.store.read(stream):
                records.append(HandoffRecord.from_dict(dict(event.payload)))
        self.ledger = CustodyLedger()
        replay_records(self.ledger, records)
        return len(records)

    def history(self, container_id: str) -> tuple[HandoffRecord, ...]:
        return self.ledger.history(container_id)

    def holder_of(self, container_id: str) -> str | None:
        return self.ledger.holder_of(container_id)
