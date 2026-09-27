from __future__ import annotations
from dataclasses import dataclass, replace
from datetime import datetime
from enum import Enum
from threading import RLock
from terminalweave.core.clock import ensure_utc
from terminalweave.core.errors import (
    ValidationError,
    ConflictError,
    StateTransitionError,
    NotFoundError,
)

# Holder identifiers are always ``kind:reference`` tokens, for example
# ``vessel:COSCO-PRIDE`` or ``yard:A-01-03-02``. The prefix makes the custody
# direction of every handover explicit and machine-checkable.
HOLDER_VESSEL = 'vessel'
HOLDER_QUAY = 'quay'
HOLDER_YARD = 'yard'
HOLDER_CUSTOMS = 'customs'
HOLDER_RAIL = 'rail'
HOLDER_TRUCK = 'truck'

_HOLDER_KINDS = frozenset({
    HOLDER_VESSEL,
    HOLDER_QUAY,
    HOLDER_YARD,
    HOLDER_CUSTOMS,
    HOLDER_RAIL,
    HOLDER_TRUCK,
})


class HandoverStage(str, Enum):
    DISCHARGE = 'discharge'                       # 卸船 vessel -> quay
    YARD_ENTRY = 'yard_entry'                     # 入场 quay -> yard
    CUSTOMS_DISPOSITION = 'customs_disposition'   # 海关处置 yard <-> customs
    YARD_MOVE = 'yard_move'                       # 场内移动 yard -> yard
    RAIL_DEPARTURE = 'rail_departure'             # 铁路离场 yard -> rail
    GATE_DEPARTURE = 'gate_departure'             # 闸口离场 yard -> truck


class CustomsDisposition(str, Enum):
    HELD = 'held'           # customs takes physical custody (examination/hold)
    RELEASED = 'released'   # customs returns custody to the terminal yard
    SEIZED = 'seized'       # customs takes permanent custody, chain closes


DEPARTURE_STAGES = frozenset({HandoverStage.RAIL_DEPARTURE, HandoverStage.GATE_DEPARTURE})


def holder_kind(holder: str) -> str:
    kind, sep, _ = holder.partition(':')
    return kind if sep else ''


def _check_holder(value: str, field_name: str) -> str:
    normalized = value.strip()
    kind, sep, ref = normalized.partition(':')
    if not sep or kind not in _HOLDER_KINDS or not ref:
        raise ValidationError(f'invalid {field_name}: {value!r}')
    return normalized


@dataclass(frozen=True)
class CustodyHandover:
    container_id: str
    stage: HandoverStage
    from_holder: str
    to_holder: str
    at: datetime
    message_id: str
    location: str | None = None
    disposition: CustomsDisposition | None = None
    reference: str | None = None
    sequence: int = 0

    def __post_init__(self) -> None:
        object.__setattr__(self, 'container_id', self.container_id.strip())
        if not self.container_id:
            raise ValidationError('container id is required')
        if not isinstance(self.stage, HandoverStage):
            try:
                object.__setattr__(self, 'stage', HandoverStage(self.stage))
            except ValueError as exc:
                raise ValidationError(f'unknown handover stage: {self.stage!r}') from exc
        object.__setattr__(self, 'from_holder', _check_holder(self.from_holder, 'from_holder'))
        object.__setattr__(self, 'to_holder', _check_holder(self.to_holder, 'to_holder'))
        object.__setattr__(self, 'at', ensure_utc(self.at))
        object.__setattr__(self, 'message_id', self.message_id.strip())
        if not self.message_id:
            raise ValidationError('message id is required for idempotent delivery')
        if self.location is not None:
            location = self.location.strip() or None
            object.__setattr__(self, 'location', location)
        if self.disposition is not None and not isinstance(self.disposition, CustomsDisposition):
            try:
                object.__setattr__(self, 'disposition', CustomsDisposition(self.disposition))
            except ValueError as exc:
                raise ValidationError(f'unknown customs disposition: {self.disposition!r}') from exc
        if self.stage is HandoverStage.CUSTOMS_DISPOSITION and self.disposition is None:
            raise ValidationError('customs disposition is required')
        if self.stage is not HandoverStage.CUSTOMS_DISPOSITION and self.disposition is not None:
            raise ValidationError('customs disposition is only valid on a customs handover')

    def dedup_key(self) -> tuple:
        return (
            self.stage,
            self.from_holder,
            self.to_holder,
            self.at,
            self.location,
            self.disposition,
            self.reference,
        )


@dataclass(frozen=True)
class CustodyState:
    container_id: str
    current_holder: str
    location: str | None
    last_stage: HandoverStage
    last_at: datetime
    discharged: bool
    entered_yard: bool
    customs_held: bool
    customs_cleared: bool
    seized: bool
    departed: bool
    length: int

    @property
    def sealed(self) -> bool:
        return self.departed or self.seized


def _project(container_id: str, events: list[CustodyHandover]) -> CustodyState | None:
    if not events:
        return None
    current_holder = ''
    location: str | None = None
    last_stage = events[0].stage
    last_at = events[0].at
    discharged = entered_yard = customs_held = customs_cleared = seized = departed = False
    for h in events:
        last_stage, last_at = h.stage, h.at
        if h.stage is HandoverStage.DISCHARGE:
            discharged = True
            current_holder = h.to_holder
            location = h.location
        elif h.stage is HandoverStage.YARD_ENTRY:
            entered_yard = True
            current_holder = h.to_holder
            location = h.location
        elif h.stage is HandoverStage.CUSTOMS_DISPOSITION:
            if h.disposition is CustomsDisposition.RELEASED:
                customs_cleared = True
                customs_held = False
                current_holder = h.to_holder
                if h.location is not None:
                    location = h.location
            elif h.disposition is CustomsDisposition.SEIZED:
                seized = True
                customs_held = False
                current_holder = h.to_holder
            else:
                customs_held = True
                current_holder = h.to_holder
                if h.location is not None:
                    location = h.location
        elif h.stage is HandoverStage.YARD_MOVE:
            current_holder = h.to_holder
            location = h.location
        elif h.stage in DEPARTURE_STAGES:
            departed = True
            current_holder = h.to_holder
            location = None
    return CustodyState(
        container_id,
        current_holder,
        location,
        last_stage,
        last_at,
        discharged,
        entered_yard,
        customs_held,
        customs_cleared,
        seized,
        departed,
        len(events),
    )


class CustodyChainLedger:
    """Append-only, per-container custody chain.

    Every accepted handover is linked to the previous holder and must occur
    after it in time. Submissions carry a client-generated ``message_id`` so
    that messages replayed after an offline period are accepted but applied
    at most once. Final states (rail/gate departure) cannot be written while
    intermediate stages are missing.
    """

    def __init__(self):
        self._chains: dict[str, list[CustodyHandover]] = {}
        self._messages: dict[str, tuple[str, int]] = {}
        self._lock = RLock()

    @classmethod
    def replay(cls, handovers) -> 'CustodyChainLedger':
        ledger = cls()
        for handover in handovers:
            ledger.submit(handover)
        return ledger

    def submit(self, handover: CustodyHandover) -> CustodyHandover:
        h = replace(handover, sequence=0)
        with self._lock:
            chain = self._chains.get(h.container_id, [])
            seen = self._messages.get(h.message_id)
            if seen is not None:
                return self._replay_duplicate(seen, h, chain)
            state = _project(h.container_id, chain)
            self._validate(h, state)
            stored = replace(h, sequence=len(chain) + 1)
            chain.append(stored)
            self._chains[h.container_id] = chain
            self._messages[h.message_id] = (h.container_id, stored.sequence)
            return stored

    def _replay_duplicate(
        self,
        seen: tuple[str, int],
        h: CustodyHandover,
        chain: list[CustodyHandover],
    ) -> CustodyHandover:
        container_id, sequence = seen
        if container_id != h.container_id:
            raise ConflictError('message id already committed for another container')
        prior = chain[sequence - 1]
        if prior.dedup_key() != h.dedup_key():
            raise ConflictError('message id resubmitted with a different payload')
        return prior

    def _validate(self, h: CustodyHandover, state: CustodyState | None) -> None:
        if state is None:
            if h.stage is not HandoverStage.DISCHARGE:
                raise StateTransitionError(
                    f'chain must start with discharge; cannot backfill {h.stage.value}'
                )
            self._require_kinds(h, HOLDER_VESSEL, HOLDER_QUAY)
            return

        if state.departed:
            raise StateTransitionError('container has departed; custody chain is sealed')
        if state.seized:
            raise StateTransitionError('container is seized by customs; custody chain is closed')
        if h.at <= state.last_at:
            raise ValidationError('handover time must be strictly after the previous handover')
        if h.from_holder != state.current_holder:
            raise ConflictError(
                f'custody chain broken: previous holder is {state.current_holder}, '
                f'not {h.from_holder}'
            )

        if h.stage is HandoverStage.DISCHARGE:
            raise StateTransitionError('container has already been discharged')
        if h.stage is HandoverStage.YARD_ENTRY:
            if state.last_stage is not HandoverStage.DISCHARGE:
                raise StateTransitionError('yard entry must follow discharge directly')
            self._require_kinds(h, HOLDER_QUAY, HOLDER_YARD)
            if not h.location:
                raise ValidationError('yard entry requires a destination yard location')
        elif h.stage is HandoverStage.CUSTOMS_DISPOSITION:
            self._validate_customs(h, state)
        elif h.stage is HandoverStage.YARD_MOVE:
            if not state.entered_yard:
                raise StateTransitionError('yard move requires prior yard entry')
            self._require_kinds(h, HOLDER_YARD, HOLDER_YARD)
            if not h.location:
                raise ValidationError('yard move requires a destination location')
            if h.location == state.location:
                raise ValidationError('yard move requires a location different from the current one')
        elif h.stage in DEPARTURE_STAGES:
            self._validate_departure(h, state)
        else:  # pragma: no cover - every enum member is handled above
            raise StateTransitionError(f'unsupported stage: {h.stage.value}')

    def _validate_customs(self, h: CustodyHandover, state: CustodyState) -> None:
        disposition = h.disposition
        if not state.entered_yard:
            raise StateTransitionError('customs disposition requires prior yard entry')
        if disposition in (CustomsDisposition.HELD, CustomsDisposition.SEIZED):
            self._require_kinds(h, HOLDER_YARD, HOLDER_CUSTOMS)
        elif disposition is CustomsDisposition.RELEASED:
            if not state.customs_held:
                raise StateTransitionError('customs release requires a prior customs hold')
            self._require_kinds(h, HOLDER_CUSTOMS, HOLDER_YARD)

    def _validate_departure(self, h: CustodyHandover, state: CustodyState) -> None:
        missing: list[str] = []
        if not state.discharged:
            missing.append(HandoverStage.DISCHARGE.value)
        if not state.entered_yard:
            missing.append(HandoverStage.YARD_ENTRY.value)
        if not state.customs_cleared:
            missing.append(HandoverStage.CUSTOMS_DISPOSITION.value + ':released')
        if missing:
            raise StateTransitionError(
                f'cannot record {h.stage.value} without {", ".join(missing)}; '
                'final state cannot be backfilled'
            )
        destination = HOLDER_RAIL if h.stage is HandoverStage.RAIL_DEPARTURE else HOLDER_TRUCK
        self._require_kinds(h, HOLDER_YARD, destination)

    @staticmethod
    def _require_kinds(h: CustodyHandover, expected_from: str, expected_to: str) -> None:
        actual_from = holder_kind(h.from_holder)
        actual_to = holder_kind(h.to_holder)
        if actual_from != expected_from or actual_to != expected_to:
            raise ConflictError(
                f'{h.stage.value} requires {expected_from} -> {expected_to}, '
                f'got {actual_from} -> {actual_to}'
            )

    def state(self, container_id: str) -> CustodyState | None:
        with self._lock:
            return _project(container_id, list(self._chains.get(container_id, [])))

    def history(self, container_id: str) -> tuple[CustodyHandover, ...]:
        with self._lock:
            return tuple(self._chains.get(container_id, []))

    def current_sequence(self, container_id: str) -> int:
        with self._lock:
            return len(self._chains.get(container_id, []))

    def missing_links(self, container_id: str) -> tuple[str, ...]:
        state = self.state(container_id)
        if state is None:
            return (
                HandoverStage.DISCHARGE.value,
                HandoverStage.YARD_ENTRY.value,
                HandoverStage.CUSTOMS_DISPOSITION.value + ':released',
            )
        missing: list[str] = []
        if not state.discharged:
            missing.append(HandoverStage.DISCHARGE.value)
        if not state.entered_yard:
            missing.append(HandoverStage.YARD_ENTRY.value)
        if not state.customs_cleared:
            missing.append(HandoverStage.CUSTOMS_DISPOSITION.value + ':released')
        return tuple(missing)

    def verify(self, container_id: str) -> bool:
        """Replay the whole chain through every invariant."""
        with self._lock:
            chain = list(self._chains.get(container_id, []))
        if not chain:
            raise NotFoundError(f'no custody chain for container {container_id}')
        probe = CustodyChainLedger()
        for handover in chain:
            probe.submit(replace(handover, sequence=0))
        return True
