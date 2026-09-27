from __future__ import annotations
from datetime import datetime, timedelta, timezone
import pytest

from terminalweave.custody import (
    CustodyChainLedger,
    CustodyHandover,
    CustomsDisposition,
    HandoverStage,
)
from terminalweave.core.errors import ConflictError, StateTransitionError, ValidationError, NotFoundError

UTC = timezone.utc
T0 = datetime(2026, 6, 1, 8, 0, tzinfo=UTC)
CID = 'MSCU1234567'
VESSEL = 'vessel:COSCO-PRIDE'
QUAY = 'quay:Q2'
YARD_A = 'yard:A-01-03-02'
YARD_B = 'yard:B-04-01-06'
CUSTOMS = 'customs:CBP'
RAIL = 'rail:BNSF-TRAIN-77'
TRUCK = 'truck:TRK-5521'


def handover(stage, frm, to, at, message_id, **kw):
    return CustodyHandover(CID, stage, frm, to, at, message_id, **kw)


def discharge(message_id='M1', at=T0):
    return handover(HandoverStage.DISCHARGE, VESSEL, QUAY, at, message_id, location='quay:Q2')


def yard_entry(message_id='M2', at=T0 + timedelta(hours=1), to=YARD_A):
    return handover(HandoverStage.YARD_ENTRY, QUAY, to, at, message_id, location=to)


def customs_hold(message_id='M3', at=T0 + timedelta(hours=5)):
    return handover(HandoverStage.CUSTOMS_DISPOSITION, YARD_A, CUSTOMS, at, message_id,
                    disposition=CustomsDisposition.HELD, location='customs:EXAM-BAY-2')


def customs_release(message_id='M4', at=T0 + timedelta(days=1)):
    return handover(HandoverStage.CUSTOMS_DISPOSITION, CUSTOMS, YARD_A, at, message_id,
                    disposition=CustomsDisposition.RELEASED, location=YARD_A)


def yard_move(message_id='M5', at=T0 + timedelta(days=1, hours=2), frm=YARD_A, to=YARD_B):
    return handover(HandoverStage.YARD_MOVE, frm, to, at, message_id, location=to)


def rail_departure(message_id='M6', at=T0 + timedelta(days=2), frm=YARD_B):
    return handover(HandoverStage.RAIL_DEPARTURE, frm, RAIL, at, message_id)


def gate_departure(message_id='M6', at=T0 + timedelta(days=2), frm=YARD_B):
    return handover(HandoverStage.GATE_DEPARTURE, frm, TRUCK, at, message_id)


def full_ledger(departure='rail'):
    ledger = CustodyChainLedger()
    ledger.submit(discharge())
    ledger.submit(yard_entry())
    ledger.submit(customs_hold())
    ledger.submit(customs_release())
    ledger.submit(yard_move())
    ledger.submit(rail_departure() if departure == 'rail' else gate_departure())
    return ledger


def test_full_chain_links_discharge_to_rail_departure():
    ledger = full_ledger()
    state = ledger.state(CID)
    assert state.current_holder == RAIL
    assert state.discharged and state.entered_yard
    assert state.customs_cleared and state.departed and state.sealed
    assert [e.sequence for e in ledger.history(CID)] == [1, 2, 3, 4, 5, 6]
    assert ledger.verify(CID)


def test_full_chain_links_discharge_to_gate_departure():
    ledger = full_ledger(departure='gate')
    assert ledger.state(CID).current_holder == TRUCK
    assert ledger.state(CID).departed


def test_handover_must_name_current_holder():
    ledger = CustodyChainLedger()
    ledger.submit(discharge())
    forged = handover(HandoverStage.YARD_ENTRY, YARD_A, YARD_B,
                      T0 + timedelta(hours=1), 'M2', location=YARD_B)
    with pytest.raises(ConflictError, match='custody chain broken'):
        ledger.submit(forged)


def test_handover_time_must_advance():
    ledger = CustodyChainLedger()
    ledger.submit(discharge())
    late = yard_entry(at=T0 - timedelta(minutes=1))
    with pytest.raises(ValidationError, match='strictly after'):
        ledger.submit(late)


def test_chain_must_start_with_discharge():
    ledger = CustodyChainLedger()
    with pytest.raises(StateTransitionError, match='start with discharge'):
        ledger.submit(yard_entry())


def test_holder_kinds_must_match_stage():
    ledger = CustodyChainLedger()
    bad = handover(HandoverStage.DISCHARGE, QUAY, VESSEL, T0, 'M1')
    with pytest.raises(ConflictError, match='vessel -> quay'):
        ledger.submit(bad)


def test_yard_entry_must_follow_discharge_directly():
    ledger = CustodyChainLedger()
    ledger.submit(discharge())
    ledger.submit(yard_entry())
    second_entry = handover(HandoverStage.YARD_ENTRY, YARD_A, YARD_B,
                            T0 + timedelta(hours=2), 'M2b', location=YARD_B)
    with pytest.raises(StateTransitionError, match='follow discharge directly'):
        ledger.submit(second_entry)


def test_customs_release_requires_prior_hold():
    ledger = CustodyChainLedger()
    ledger.submit(discharge())
    ledger.submit(yard_entry())
    release = handover(HandoverStage.CUSTOMS_DISPOSITION, YARD_A, YARD_B,
                       T0 + timedelta(hours=2), 'M3b',
                       disposition=CustomsDisposition.RELEASED, location=YARD_B)
    with pytest.raises(StateTransitionError, match='prior customs hold'):
        ledger.submit(release)


def test_yard_move_requires_entry_and_new_location():
    ledger = CustodyChainLedger()
    ledger.submit(discharge())
    with pytest.raises(StateTransitionError, match='prior yard entry'):
        ledger.submit(yard_move(frm=QUAY, to=YARD_A))
    ledger.submit(yard_entry())
    same_spot = handover(HandoverStage.YARD_MOVE, YARD_A, YARD_A,
                         T0 + timedelta(hours=2), 'M2c', location=YARD_A)
    with pytest.raises(ValidationError, match='different from the current'):
        ledger.submit(same_spot)


def test_missing_customs_release_blocks_final_departure():
    # discharge -> yard entry, then attempt to leave: intermediate customs
    # handling is missing, the final state must not be backfilled.
    ledger = CustodyChainLedger()
    ledger.submit(discharge())
    ledger.submit(yard_entry())
    with pytest.raises(StateTransitionError, match='final state cannot be backfilled'):
        ledger.submit(rail_departure(frm=YARD_A))
    assert not ledger.state(CID).departed
    assert 'customs_disposition:released' in ledger.missing_links(CID)


def test_held_without_release_blocks_departure():
    ledger = CustodyChainLedger()
    ledger.submit(discharge())
    ledger.submit(yard_entry())
    ledger.submit(customs_hold())
    with pytest.raises(StateTransitionError, match='final state cannot be backfilled'):
        ledger.submit(rail_departure(frm=CUSTOMS))


def test_idempotent_replay_does_not_transfer_twice():
    ledger = CustodyChainLedger()
    first = ledger.submit(discharge())
    # offline client retries after connectivity is restored, same message id
    retried = ledger.submit(discharge())
    assert retried is first
    assert ledger.current_sequence(CID) == 1
    assert len(ledger.history(CID)) == 1
    assert ledger.state(CID).current_holder == QUAY


def test_replayed_message_with_changed_payload_is_rejected():
    ledger = CustodyChainLedger()
    ledger.submit(discharge())
    altered = discharge(at=T0 + timedelta(minutes=5))
    with pytest.raises(ConflictError, match='different payload'):
        ledger.submit(altered)


def test_message_id_reuse_across_containers_is_rejected():
    ledger = CustodyChainLedger()
    ledger.submit(discharge())
    other = CustodyHandover('MSCU7654321', HandoverStage.DISCHARGE,
                            VESSEL, QUAY, T0, 'M1', location='quay:Q2')
    with pytest.raises(ConflictError, match='another container'):
        ledger.submit(other)


def test_seizure_closes_chain():
    ledger = CustodyChainLedger()
    ledger.submit(discharge())
    ledger.submit(yard_entry())
    seized = handover(HandoverStage.CUSTOMS_DISPOSITION, YARD_A, CUSTOMS,
                      T0 + timedelta(hours=4), 'M3', disposition=CustomsDisposition.SEIZED)
    ledger.submit(seized)
    assert ledger.state(CID).seized and ledger.state(CID).sealed
    with pytest.raises(StateTransitionError, match='seized'):
        ledger.submit(customs_release())


def test_departure_seals_chain():
    ledger = full_ledger()
    with pytest.raises(StateTransitionError, match='sealed'):
        ledger.submit(yard_move(message_id='M7', at=T0 + timedelta(days=3),
                                frm=RAIL, to=YARD_A))


def test_disposition_payload_must_be_consistent():
    with pytest.raises(ValidationError, match='customs disposition is required'):
        CustodyHandover(CID, HandoverStage.CUSTOMS_DISPOSITION, YARD_A, CUSTOMS, T0, 'M3')
    with pytest.raises(ValidationError, match='only valid on a customs handover'):
        CustodyHandover(CID, HandoverStage.YARD_MOVE, YARD_A, YARD_B, T0, 'M5',
                        location=YARD_B, disposition=CustomsDisposition.HELD)


def test_replay_rebuilds_chain_from_stored_handovers():
    source = full_ledger()
    restored = CustodyChainLedger.replay(source.history(CID))
    assert restored.state(CID).current_holder == RAIL
    assert restored.state(CID).length == 6
    assert restored.verify(CID)


def test_verify_raises_for_unknown_container():
    with pytest.raises(NotFoundError):
        CustodyChainLedger().verify(CID)
