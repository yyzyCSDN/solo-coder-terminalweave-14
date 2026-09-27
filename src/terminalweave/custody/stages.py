from __future__ import annotations
from enum import Enum


class ChainStage(str, Enum):
    """Custody chain stages in terminal order.

    DISCHARGED        quay: vessel -> terminal
    IN_YARD           landed in the yard block
    YARD_MOVED        any intra-yard transfer (rehandle, inspection, rail staging)
    CUSTOMS_DISPOSED  customs decision recorded (held / released / seized)
    RAIL_DEPARTED     final state: left the terminal by train
    GATE_DEPARTED     final state: left the terminal by truck gate
    """

    DISCHARGED = 'discharged'
    IN_YARD = 'in_yard'
    YARD_MOVED = 'yard_moved'
    CUSTOMS_DISPOSED = 'customs_disposed'
    RAIL_DEPARTED = 'rail_departed'
    GATE_DEPARTED = 'gate_departed'


class CustomsDisposition(str, Enum):
    HELD = 'held'          # custody passes to customs while the box stays in yard
    RELEASED = 'released'  # customs clears the box for departure
    SEIZED = 'seized'      # custody passes to customs; the chain closes


FINAL_STAGES = frozenset({ChainStage.RAIL_DEPARTED, ChainStage.GATE_DEPARTED})

#: Stages from which each stage may be reached.
PREDECESSORS: dict[ChainStage, frozenset[ChainStage | None]] = {
    ChainStage.DISCHARGED: frozenset({None}),
    ChainStage.IN_YARD: frozenset({ChainStage.DISCHARGED}),
    ChainStage.YARD_MOVED: frozenset({
        ChainStage.IN_YARD,
        ChainStage.YARD_MOVED,
        ChainStage.CUSTOMS_DISPOSED,
    }),
    ChainStage.CUSTOMS_DISPOSED: frozenset({
        ChainStage.IN_YARD,
        ChainStage.YARD_MOVED,
        ChainStage.CUSTOMS_DISPOSED,
    }),
    ChainStage.RAIL_DEPARTED: frozenset({
        ChainStage.YARD_MOVED,
        ChainStage.CUSTOMS_DISPOSED,
    }),
    ChainStage.GATE_DEPARTED: frozenset({
        ChainStage.YARD_MOVED,
        ChainStage.CUSTOMS_DISPOSED,
    }),
}

DEPARTURE_STAGES = frozenset({ChainStage.RAIL_DEPARTED, ChainStage.GATE_DEPARTED})
