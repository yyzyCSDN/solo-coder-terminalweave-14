from __future__ import annotations
from dataclasses import dataclass, field
from datetime import datetime

from terminalweave.core.checksum import PayloadChecksum
from terminalweave.core.clock import ensure_utc, iso_z, parse_iso_z
from terminalweave.core.errors import (
    ConflictError,
    NotFoundError,
    StateTransitionError,
    ValidationError,
)
from .stages import (
    ChainStage,
    CustomsDisposition,
    DEPARTURE_STAGES,
    PREDECESSORS,
)


def _party(value: str, label: str) -> str:
    value = (value or '').strip()
    if not value:
        raise ValidationError(f'{label} required')
    return value.upper()


@dataclass(frozen=True)
class HandoffRequest:
    """A custody handover message, possibly produced by an offline client.

    ``message_id`` is the client-generated deduplication key; re-posting the
    same message after an offline gap must not transfer custody twice.
    """

    message_id: str
    container_id: str
    stage: ChainStage
    from_party: str
    to_party: str
    at: datetime
    disposition: CustomsDisposition | None = None
    reference: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, 'message_id', _party(self.message_id, 'message id'))
        object.__setattr__(self, 'container_id', _party(self.container_id, 'container id'))
        if not isinstance(self.stage, ChainStage):
            raise ValidationError(f'unknown chain stage: {self.stage!r}')
        object.__setattr__(self, 'from_party', _party(self.from_party, 'from party'))
        object.__setattr__(self, 'to_party', _party(self.to_party, 'to party'))
        object.__setattr__(self, 'at', ensure_utc(self.at))
        if self.disposition is not None and not isinstance(self.disposition, CustomsDisposition):
            raise ValidationError(f'unknown customs disposition: {self.disposition!r}')
        if self.stage != ChainStage.CUSTOMS_DISPOSED and self.disposition is not None:
            raise ValidationError('customs disposition only valid on customs_disposed stage')
        if self.reference is not None and not str(self.reference).strip():
            object.__setattr__(self, 'reference', None)


def _record_payload(
    sequence: int,
    req: HandoffRequest,
    prev_hash: str,
) -> dict[str, object]:
    return {
        'sequence': sequence,
        'message_id': req.message_id,
        'container_id': req.container_id,
        'stage': req.stage.value,
        'from_party': req.from_party,
        'to_party': req.to_party,
        'at': iso_z(req.at),
        'disposition': req.disposition.value if req.disposition else None,
        'reference': req.reference,
        'prev_hash': prev_hash,
    }


def _seal(payload: dict[str, object]) -> str:
    return PayloadChecksum.digest(payload)


@dataclass(frozen=True)
class HandoffRecord:
    sequence: int
    message_id: str
    container_id: str
    stage: ChainStage
    from_party: str
    to_party: str
    at: datetime
    disposition: CustomsDisposition | None
    reference: str | None
    prev_hash: str
    record_hash: str

    @classmethod
    def seal(cls, sequence: int, req: HandoffRequest, prev_hash: str) -> 'HandoffRecord':
        payload = _record_payload(sequence, req, prev_hash)
        return cls(
            sequence=sequence,
            message_id=req.message_id,
            container_id=req.container_id,
            stage=req.stage,
            from_party=req.from_party,
            to_party=req.to_party,
            at=req.at,
            disposition=req.disposition,
            reference=req.reference,
            prev_hash=prev_hash,
            record_hash=_seal(payload),
        )

    def payload(self) -> dict[str, object]:
        return {
            'sequence': self.sequence,
            'message_id': self.message_id,
            'container_id': self.container_id,
            'stage': self.stage.value,
            'from_party': self.from_party,
            'to_party': self.to_party,
            'at': iso_z(self.at),
            'disposition': self.disposition.value if self.disposition else None,
            'reference': self.reference,
            'prev_hash': self.prev_hash,
        }

    def verify(self) -> bool:
        """Recompute the chained hash; detects missing or rewritten links."""
        return _seal(self.payload()) == self.record_hash

    def to_dict(self) -> dict[str, object]:
        data = self.payload()
        data['record_hash'] = self.record_hash
        return data

    @classmethod
    def from_dict(cls, data: dict[str, object]) -> 'HandoffRecord':
        record = cls(
            sequence=int(data['sequence']),
            message_id=str(data['message_id']),
            container_id=str(data['container_id']),
            stage=ChainStage(str(data['stage'])),
            from_party=str(data['from_party']),
            to_party=str(data['to_party']),
            at=parse_iso_z(str(data['at'])),
            disposition=(
                CustomsDisposition(str(data['disposition']))
                if data.get('disposition') is not None
                else None
            ),
            reference=data.get('reference'),
            prev_hash=str(data['prev_hash']),
            record_hash=str(data['record_hash']),
        )
        if not record.verify():
            raise ConflictError(f'handover {record.sequence} hash mismatch; chain tampered')
        return record


GENESIS_HASH = '0' * 64


@dataclass
class ChainState:
    container_id: str
    stage: ChainStage | None = None
    holder: str | None = None
    customs_cleared: bool = False
    closed: bool = False
    last_at: datetime | None = None
    sequence: int = 0
    last_hash: str = GENESIS_HASH
    history: list[HandoffRecord] = field(default_factory=list)


class CustodyLedger:
    """Append-only custody chain per container.

    Every handover is admitted only when:

    * the previous holder on the chain matches ``from_party``;
    * the timestamp is strictly later than the previous handover;
    * the stage follows the last stage (discharge must be first);
    * a terminal departure is only recorded after the box entered the yard
      and customs released it; missing intermediate stages cannot be skipped.

    Re-submitting a previously accepted ``message_id`` returns the original
    record and never transfers custody again, which is what makes offline
    message replay safe.
    """

    GENESIS_HASH = GENESIS_HASH  # exposed for callers replaying streams

    def __init__(self) -> None:
        self._chains: dict[str, ChainState] = {}
        self._messages: dict[str, HandoffRecord] = {}

    # -- queries ----------------------------------------------------------

    def state(self, container_id: str) -> ChainState:
        state = self._chains.get(container_id.strip().upper())
        if state is None:
            raise NotFoundError(f'no custody chain for container {container_id}')
        return state

    def history(self, container_id: str) -> tuple[HandoffRecord, ...]:
        return tuple(self.state(container_id).history)

    def holder_of(self, container_id: str) -> str | None:
        return self.state(container_id).holder

    def is_traceable(self, container_id: str) -> bool:
        """True when every stored link still seals against its predecessor."""
        try:
            state = self.state(container_id)
        except NotFoundError:
            return False
        prev = self.GENESIS_HASH
        for record in state.history:
            if record.prev_hash != prev or not record.verify():
                return False
            prev = record.record_hash
        return True

    def containers(self) -> tuple[str, ...]:
        return tuple(sorted(self._chains))

    def known_message(self, message_id: str) -> HandoffRecord | None:
        """Return the record already accepted under this client message id."""
        return self._messages.get(message_id.strip().upper())

    # -- writes -----------------------------------------------------------

    def submit(self, req: HandoffRequest) -> HandoffRecord:
        state = self._chains.get(req.container_id)

        existing = self._messages.get(req.message_id)
        if existing is not None:
            if existing.container_id != req.container_id:
                raise ValidationError('message id already used for another container')
            # Offline retry: same message returns the stored handover and
            # never transfers custody again.
            return existing

        if state is None:
            state = ChainState(container_id=req.container_id)
            self._chains[req.container_id] = state

        self._validate(req, state)

        sequence = state.sequence + 1
        record = HandoffRecord.seal(sequence, req, state.last_hash)
        self._admit_record(record)
        self._messages[req.message_id] = record
        return record

    def _admit_record(self, record: HandoffRecord) -> None:
        state = self._chains.setdefault(record.container_id, ChainState(record.container_id))
        state.history.append(record)
        state.sequence = record.sequence
        state.last_hash = record.record_hash
        state.stage = record.stage
        state.holder = record.to_party
        state.last_at = record.at
        if record.disposition is CustomsDisposition.RELEASED:
            state.customs_cleared = True
        if record.disposition is CustomsDisposition.SEIZED:
            state.closed = True

    def _validate(self, req: HandoffRequest, state: ChainState) -> None:
        expected = PREDECESSORS[req.stage]

        if state.stage is None:
            if req.stage is not ChainStage.DISCHARGED:
                raise StateTransitionError(
                    f'custody chain must open with discharge, got {req.stage.value}'
                )
            if req.from_party == req.to_party:
                raise ValidationError('discharge must transfer custody from the vessel')
            return

        if state.closed:
            raise StateTransitionError(
                f'custody chain closed at {state.stage.value}; no further handovers'
            )

        if state.stage not in expected:
            raise StateTransitionError(
                f'{req.stage.value} cannot follow {state.stage.value}'
            )

        if req.at <= state.last_at:
            raise ValidationError('handover time must be strictly after the previous handover')

        if req.stage is ChainStage.CUSTOMS_DISPOSED:
            self._validate_customs(req, state)
        else:
            if req.from_party != state.holder:
                raise ConflictError(
                    f'previous holder is {state.holder}, handover claims {req.from_party}'
                )
            if req.from_party == req.to_party:
                raise ValidationError('handover must transfer custody to a new holder')

        if req.stage in DEPARTURE_STAGES:
            self._validate_departure(req, state)

    def _validate_customs(self, req: HandoffRequest, state: ChainState) -> None:
        if req.disposition is None:
            raise ValidationError('customs handover requires a disposition (held/released/seized)')
        prior_customs = next(
            (r for r in reversed(state.history) if r.stage is ChainStage.CUSTOMS_DISPOSED),
            None,
        )
        if prior_customs is None:
            # First customs action, taken while the box is physically in the yard.
            if req.disposition is CustomsDisposition.RELEASED:
                # Clearance recorded in place; the yard keeps custody.
                if req.from_party != state.holder:
                    raise ConflictError(
                        f'release must be acknowledged by current holder {state.holder}'
                    )
                if req.from_party != req.to_party:
                    raise ValidationError('customs release keeps the box with the yard holder')
                return
            # HELD / SEIZED: the box physically passes from the yard to customs.
            if req.from_party != state.holder:
                raise ConflictError(
                    f'previous holder is {state.holder}, handover claims {req.from_party}'
                )
            if req.from_party == req.to_party:
                raise ValidationError(f'{req.disposition.value} requires a customs receiving party')
            return

        # A further customs decision is only possible while the box is on hold.
        if prior_customs.disposition is not CustomsDisposition.HELD:
            raise StateTransitionError(
                f'customs already {prior_customs.disposition.value}; cannot record another decision'
            )
        if req.disposition is CustomsDisposition.RELEASED:
            # Release either returns custody from customs to the yard, or is
            # recorded in place by the yard party after a customs-authorized
            # inspection move; customs must not remain the recorded holder.
            if req.from_party != state.holder:
                raise ConflictError(
                    f'previous holder is {state.holder}, handover claims {req.from_party}'
                )
            if req.to_party == prior_customs.to_party:
                raise ValidationError('release must move the box out of customs custody')
        elif req.disposition is CustomsDisposition.SEIZED:
            # Customs already holds the box; seizure closes the chain in place.
            if req.from_party != state.holder or req.to_party != state.holder:
                raise ValidationError('seizure after hold closes custody with customs')
        else:
            raise ValidationError('a held box can only be released or seized')

    def _validate_departure(self, req: HandoffRequest, state: ChainState) -> None:
        missing: list[str] = []
        stages_seen = {r.stage for r in state.history}
        if ChainStage.DISCHARGED not in stages_seen:
            missing.append(ChainStage.DISCHARGED.value)
        if ChainStage.IN_YARD not in stages_seen:
            missing.append(ChainStage.IN_YARD.value)
        last_customs = next(
            (r for r in reversed(state.history) if r.stage is ChainStage.CUSTOMS_DISPOSED),
            None,
        )
        if last_customs is None or last_customs.disposition is not CustomsDisposition.RELEASED:
            missing.append('customs release')
        if missing:
            raise StateTransitionError(
                f'cannot write final state {req.stage.value}; missing: {", ".join(missing)}'
            )


@dataclass
class ReplayReport:
    accepted: int
    duplicates: int
    containers: tuple[str, ...]
    last_sequence: int | None


def replay_records(ledger: CustodyLedger, records: list[HandoffRecord] | tuple[HandoffRecord, ...]) -> ReplayReport:
    """Rebuild chains from records recovered after an offline outage.

    Records are applied in container/sequence order. Replaying an
    already-accepted message is counted as a duplicate and never moves
    custody; a broken hash link, a sequence gap, or a rewritten stage
    transition fails closed.
    """
    ordered = sorted(records, key=lambda r: (r.container_id, r.sequence))
    accepted = 0
    duplicates = 0
    for record in ordered:
        if not record.verify():
            raise ConflictError(f'handover {record.sequence} hash mismatch; chain tampered')
        state = ledger._chains.get(record.container_id)
        if state is not None:
            known = ledger._messages.get(record.message_id)
            if known is not None:
                if known.container_id != record.container_id:
                    raise ValidationError('message id already used for another container')
                if known.sequence != record.sequence or known.record_hash != record.record_hash:
                    raise ConflictError(
                        f'duplicate message {record.message_id} submitted with different content'
                    )
                duplicates += 1
                continue
            if record.sequence != state.sequence + 1 or record.prev_hash != state.last_hash:
                raise ConflictError(
                    f'replay gap for {record.container_id} at sequence {record.sequence}'
                )
        else:
            if record.sequence != 1 or record.prev_hash != CustodyLedger.GENESIS_HASH:
                raise ConflictError('replay must begin with the discharge handover')

        req = HandoffRequest(
            message_id=record.message_id,
            container_id=record.container_id,
            stage=record.stage,
            from_party=record.from_party,
            to_party=record.to_party,
            at=record.at,
            disposition=record.disposition,
            reference=record.reference,
        )
        ledger._validate(req, state if state is not None else ChainState(record.container_id))
        ledger._admit_record(record)
        ledger._messages[record.message_id] = record
        accepted += 1
    last = max((r.sequence for r in records), default=None)
    return ReplayReport(accepted, duplicates, ledger.containers(), last)
