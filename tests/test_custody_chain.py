from datetime import datetime, timedelta, timezone

import pytest

from terminalweave.core.errors import ConflictError, StateTransitionError, ValidationError
from terminalweave.custody import (
    ChainStage,
    CustodyJournal,
    CustodyLedger,
    CustomsDisposition,
    HandoffRequest,
    replay_records,
)

UTC = timezone.utc
T0 = datetime(2026, 3, 1, 8, 0, tzinfo=UTC)
VESSEL = 'MV.ANTARES'
QUAY = 'QUAY-TEAM-A'
YARD = 'YARD-BLOCK-07'
CUSTOMS = 'CUSTOMS'
RAIL = 'RAIL-CARRIER'
TRUCK = 'TRUCK-LANE-3'
CONT = 'CSQU3054383'


def req(stage, frm, to, at, *, msg, disposition=None, container=CONT):
    return HandoffRequest(msg, container, stage, frm, to, at, disposition)


def rebuild_record(record, **changes):
    """Rebuild a sealed record with altered fields (tampering simulation)."""
    from terminalweave.custody.chain import HandoffRecord
    data = record.to_dict()
    data.update(changes)
    return HandoffRecord(
        sequence=data['sequence'],
        message_id=data['message_id'],
        container_id=data['container_id'],
        stage=ChainStage(data['stage']),
        from_party=data['from_party'],
        to_party=data['to_party'],
        at=datetime.fromisoformat(data['at']),
        disposition=(
            CustomsDisposition(data['disposition']) if data['disposition'] else None
        ),
        reference=data['reference'],
        prev_hash=data['prev_hash'],
        record_hash=data['record_hash'],
    )


def full_chain(ledger, t=T0, container=CONT, final=ChainStage.RAIL_DEPARTED, carrier=RAIL):
    ledger.submit(req(ChainStage.DISCHARGED, VESSEL, QUAY, t, msg='M1', container=container))
    ledger.submit(req(ChainStage.IN_YARD, QUAY, YARD, t + timedelta(minutes=20), msg='M2', container=container))
    ledger.submit(req(ChainStage.YARD_MOVED, YARD, 'YARD-DRIVER-2', t + timedelta(hours=2), msg='M3', container=container))
    ledger.submit(req(ChainStage.CUSTOMS_DISPOSED, 'YARD-DRIVER-2', 'YARD-DRIVER-2',
                      t + timedelta(hours=3), msg='M4',
                      disposition=CustomsDisposition.RELEASED, container=container))
    ledger.submit(req(final, 'YARD-DRIVER-2', carrier, t + timedelta(hours=6), msg='M5', container=container))


# -- discharge first -------------------------------------------------------

def test_chain_must_open_with_discharge():
    ledger = CustodyLedger()
    with pytest.raises(StateTransitionError):
        ledger.submit(req(ChainStage.IN_YARD, QUAY, YARD, T0, msg='X1'))


def test_complete_chain_is_traceable_to_rail_departure():
    ledger = CustodyLedger()
    full_chain(ledger)
    state = ledger.state(CONT)
    assert state.stage is ChainStage.RAIL_DEPARTED
    assert state.holder == RAIL
    assert ledger.is_traceable(CONT)
    assert [r.stage for r in ledger.history(CONT)] == [
        ChainStage.DISCHARGED,
        ChainStage.IN_YARD,
        ChainStage.YARD_MOVED,
        ChainStage.CUSTOMS_DISPOSED,
        ChainStage.RAIL_DEPARTED,
    ]


def test_gate_departure_is_also_a_final_state():
    ledger = CustodyLedger()
    full_chain(ledger, final=ChainStage.GATE_DEPARTED, carrier=TRUCK)
    assert ledger.state(CONT).stage is ChainStage.GATE_DEPARTED
    assert ledger.holder_of(CONT) == TRUCK


# -- previous holder -------------------------------------------------------

def test_each_handover_verifies_previous_holder():
    ledger = CustodyLedger()
    ledger.submit(req(ChainStage.DISCHARGED, VESSEL, QUAY, T0, msg='M1'))
    ledger.submit(req(ChainStage.IN_YARD, QUAY, YARD, T0 + timedelta(minutes=20), msg='M2'))
    # someone other than the yard tries to move the box
    with pytest.raises(ConflictError):
        ledger.submit(req(ChainStage.YARD_MOVED, QUAY, 'YARD-DRIVER-2',
                          T0 + timedelta(hours=1), msg='M3'))


def test_self_transfer_is_rejected_outside_customs():
    ledger = CustodyLedger()
    ledger.submit(req(ChainStage.DISCHARGED, VESSEL, QUAY, T0, msg='M1'))
    with pytest.raises(ValidationError):
        ledger.submit(req(ChainStage.IN_YARD, QUAY, QUAY, T0 + timedelta(minutes=5), msg='M2'))


# -- time ordering ---------------------------------------------------------

def test_handover_time_must_strictly_increase():
    ledger = CustodyLedger()
    ledger.submit(req(ChainStage.DISCHARGED, VESSEL, QUAY, T0, msg='M1'))
    with pytest.raises(ValidationError):
        ledger.submit(req(ChainStage.IN_YARD, QUAY, YARD, T0, msg='M2'))
    with pytest.raises(ValidationError):
        ledger.submit(req(ChainStage.IN_YARD, QUAY, YARD, T0 - timedelta(seconds=1), msg='M2'))


def test_naive_datetime_rejected():
    ledger = CustodyLedger()
    with pytest.raises(ValidationError):
        ledger.submit(HandoffRequest('M1', CONT, ChainStage.DISCHARGED, VESSEL, QUAY,
                                     datetime(2026, 3, 1, 8, 0)))


# -- customs ---------------------------------------------------------------

def test_customs_hold_then_release_returns_custody_to_yard():
    ledger = CustodyLedger()
    ledger.submit(req(ChainStage.DISCHARGED, VESSEL, QUAY, T0, msg='M1'))
    ledger.submit(req(ChainStage.IN_YARD, QUAY, YARD, T0 + timedelta(minutes=20), msg='M2'))
    ledger.submit(req(ChainStage.CUSTOMS_DISPOSED, YARD, CUSTOMS,
                      T0 + timedelta(hours=1), msg='M3', disposition=CustomsDisposition.HELD))
    assert ledger.holder_of(CONT) == CUSTOMS
    ledger.submit(req(ChainStage.CUSTOMS_DISPOSED, CUSTOMS, YARD,
                      T0 + timedelta(hours=4), msg='M4', disposition=CustomsDisposition.RELEASED))
    assert ledger.holder_of(CONT) == YARD
    ledger.submit(req(ChainStage.RAIL_DEPARTED, YARD, RAIL,
                      T0 + timedelta(hours=6), msg='M5'))
    assert ledger.state(CONT).stage is ChainStage.RAIL_DEPARTED


def test_customs_authorized_move_then_release_then_gate_departure():
    ledger = CustodyLedger()
    ledger.submit(req(ChainStage.DISCHARGED, VESSEL, QUAY, T0, msg='M1'))
    ledger.submit(req(ChainStage.IN_YARD, QUAY, YARD, T0 + timedelta(minutes=20), msg='M2'))
    ledger.submit(req(ChainStage.CUSTOMS_DISPOSED, YARD, CUSTOMS,
                      T0 + timedelta(hours=1), msg='M3', disposition=CustomsDisposition.HELD))
    # customs orders the box to the examination bay
    ledger.submit(req(ChainStage.YARD_MOVED, CUSTOMS, 'YARD-DRIVER-2',
                      T0 + timedelta(hours=2), msg='M4'))
    # release recorded in place by the yard party now holding the box
    ledger.submit(req(ChainStage.CUSTOMS_DISPOSED, 'YARD-DRIVER-2', 'YARD-DRIVER-2',
                      T0 + timedelta(hours=3), msg='M5', disposition=CustomsDisposition.RELEASED))
    ledger.submit(req(ChainStage.GATE_DEPARTED, 'YARD-DRIVER-2', TRUCK,
                      T0 + timedelta(hours=4), msg='M6'))
    assert ledger.holder_of(CONT) == TRUCK
    assert ledger.is_traceable(CONT)


def test_release_must_move_box_out_of_customs_custody():
    ledger = CustodyLedger()
    ledger.submit(req(ChainStage.DISCHARGED, VESSEL, QUAY, T0, msg='M1'))
    ledger.submit(req(ChainStage.IN_YARD, QUAY, YARD, T0 + timedelta(minutes=20), msg='M2'))
    ledger.submit(req(ChainStage.CUSTOMS_DISPOSED, YARD, CUSTOMS,
                      T0 + timedelta(hours=1), msg='M3', disposition=CustomsDisposition.HELD))
    with pytest.raises(ValidationError):
        ledger.submit(req(ChainStage.CUSTOMS_DISPOSED, CUSTOMS, CUSTOMS,
                          T0 + timedelta(hours=2), msg='M4',
                          disposition=CustomsDisposition.RELEASED))


def test_seizure_closes_chain():
    ledger = CustodyLedger()
    ledger.submit(req(ChainStage.DISCHARGED, VESSEL, QUAY, T0, msg='M1'))
    ledger.submit(req(ChainStage.IN_YARD, QUAY, YARD, T0 + timedelta(minutes=20), msg='M2'))
    ledger.submit(req(ChainStage.CUSTOMS_DISPOSED, YARD, CUSTOMS,
                      T0 + timedelta(hours=1), msg='M3', disposition=CustomsDisposition.HELD))
    ledger.submit(req(ChainStage.CUSTOMS_DISPOSED, CUSTOMS, CUSTOMS,
                      T0 + timedelta(hours=2), msg='M4', disposition=CustomsDisposition.SEIZED))
    assert ledger.state(CONT).closed
    with pytest.raises(StateTransitionError):
        ledger.submit(req(ChainStage.RAIL_DEPARTED, CUSTOMS, RAIL,
                          T0 + timedelta(hours=3), msg='M5'))


def test_customs_disposition_required_on_customs_stage():
    ledger = CustodyLedger()
    ledger.submit(req(ChainStage.DISCHARGED, VESSEL, QUAY, T0, msg='M1'))
    ledger.submit(req(ChainStage.IN_YARD, QUAY, YARD, T0 + timedelta(minutes=20), msg='M2'))
    with pytest.raises(ValidationError):
        ledger.submit(req(ChainStage.CUSTOMS_DISPOSED, YARD, YARD,
                          T0 + timedelta(hours=1), msg='M3'))


def test_disposition_not_allowed_on_other_stages():
    ledger = CustodyLedger()
    with pytest.raises(ValidationError):
        ledger.submit(req(ChainStage.DISCHARGED, VESSEL, QUAY, T0, msg='M1',
                          disposition=CustomsDisposition.HELD))


# -- missing intermediate links -------------------------------------------

def test_departure_blocked_without_yard_entry_and_customs():
    ledger = CustodyLedger()
    ledger.submit(req(ChainStage.DISCHARGED, VESSEL, QUAY, T0, msg='M1'))
    # jumping straight from the quay to departure is an illegal transition
    with pytest.raises(StateTransitionError):
        ledger.submit(req(ChainStage.RAIL_DEPARTED, QUAY, RAIL,
                          T0 + timedelta(hours=1), msg='M9'))


def test_departure_blocked_after_yard_without_customs_release():
    ledger = CustodyLedger()
    ledger.submit(req(ChainStage.DISCHARGED, VESSEL, QUAY, T0, msg='M1'))
    ledger.submit(req(ChainStage.IN_YARD, QUAY, YARD, T0 + timedelta(minutes=20), msg='M2'))
    ledger.submit(req(ChainStage.YARD_MOVED, YARD, 'YARD-DRIVER-2',
                      T0 + timedelta(hours=2), msg='M3'))
    # in the yard and moved, but no customs disposition: final state cannot be written
    with pytest.raises(StateTransitionError, match='customs release'):
        ledger.submit(req(ChainStage.RAIL_DEPARTED, 'YARD-DRIVER-2', RAIL,
                          T0 + timedelta(hours=3), msg='M9'))


def test_departure_blocked_when_only_hold_recorded():
    ledger = CustodyLedger()
    ledger.submit(req(ChainStage.DISCHARGED, VESSEL, QUAY, T0, msg='M1'))
    ledger.submit(req(ChainStage.IN_YARD, QUAY, YARD, T0 + timedelta(minutes=20), msg='M2'))
    ledger.submit(req(ChainStage.CUSTOMS_DISPOSED, YARD, CUSTOMS,
                      T0 + timedelta(hours=1), msg='M3', disposition=CustomsDisposition.HELD))
    # cannot backfill a final state while the box remains on hold
    with pytest.raises(StateTransitionError, match='customs release'):
        ledger.submit(req(ChainStage.GATE_DEPARTED, CUSTOMS, TRUCK,
                          T0 + timedelta(hours=2), msg='M9'))


def test_stage_order_cannot_be_jumped():
    ledger = CustodyLedger()
    ledger.submit(req(ChainStage.DISCHARGED, VESSEL, QUAY, T0, msg='M1'))
    with pytest.raises(StateTransitionError):
        ledger.submit(req(ChainStage.YARD_MOVED, QUAY, YARD,
                          T0 + timedelta(minutes=10), msg='M2'))


# -- offline idempotency ---------------------------------------------------

def test_duplicate_message_resubmission_does_not_transfer_twice():
    ledger = CustodyLedger()
    first = ledger.submit(req(ChainStage.DISCHARGED, VESSEL, QUAY, T0, msg='MSG-77'))
    # offline client comes back online and posts the same message again,
    # even with a later timestamp
    again = ledger.submit(req(ChainStage.DISCHARGED, VESSEL, QUAY,
                              T0 + timedelta(hours=5), msg='MSG-77'))
    assert first is again
    assert len(ledger.history(CONT)) == 1
    assert ledger.state(CONT).holder == QUAY


def test_duplicate_message_id_cannot_be_reused_for_another_container():
    ledger = CustodyLedger()
    ledger.submit(req(ChainStage.DISCHARGED, VESSEL, QUAY, T0, msg='MSG-77'))
    with pytest.raises(ValidationError):
        ledger.submit(req(ChainStage.DISCHARGED, VESSEL, QUAY, T0 + timedelta(minutes=1),
                          msg='MSG-77', container='CSQU9999999'))


def test_duplicate_message_with_mutated_content_is_rejected():
    ledger = CustodyLedger()
    ledger.submit(req(ChainStage.DISCHARGED, VESSEL, QUAY, T0, msg='MSG-77'))
    ledger.submit(req(ChainStage.IN_YARD, QUAY, YARD, T0 + timedelta(minutes=20), msg='M2'))
    records = list(ledger.history(CONT))
    # tamper: rewrite the stored discharge recipient, keep the old hash
    tampered = rebuild_record(records[0], to_party='IMPOSTOR')
    fresh = CustodyLedger()
    with pytest.raises(ConflictError):
        replay_records(fresh, [tampered, records[1]])


# -- replay ----------------------------------------------------------------

def test_replay_recovers_full_chains_and_deduplicates():
    source = CustodyLedger()
    full_chain(source)
    captured = list(source.history(CONT))

    ledger = CustodyLedger()
    report = replay_records(ledger, captured)
    assert report.accepted == 5 and report.duplicates == 0
    # recovering offline messages a second time is all duplicates
    report2 = replay_records(ledger, captured)
    assert report2.accepted == 0 and report2.duplicates == 5
    assert ledger.holder_of(CONT) == RAIL
    assert ledger.is_traceable(CONT)


def test_replay_fails_closed_on_sequence_gap():
    source = CustodyLedger()
    full_chain(source)
    captured = list(source.history(CONT))
    # intermediate link missing after recovery transport loss
    ledger = CustodyLedger()
    with pytest.raises(ConflictError):
        replay_records(ledger, [captured[0], captured[2], captured[4]])


# -- durable journal -------------------------------------------------------

def test_journal_persists_and_recovers_after_outage():
    journal = CustodyJournal()
    full_chain_via_journal(journal)
    assert journal.holder_of(CONT) == RAIL

    events = journal.recover()
    assert events == 4
    assert journal.holder_of(CONT) == RAIL
    assert [r.stage for r in journal.history(CONT)][-1] is ChainStage.RAIL_DEPARTED


def full_chain_via_journal(journal, t=T0):
    journal.submit(req(ChainStage.DISCHARGED, VESSEL, QUAY, t, msg='J1'))
    journal.submit(req(ChainStage.IN_YARD, QUAY, YARD, t + timedelta(minutes=20), msg='J2'))
    journal.submit(req(ChainStage.CUSTOMS_DISPOSED, YARD, YARD,
                       t + timedelta(hours=1), msg='J3',
                       disposition=CustomsDisposition.RELEASED))
    journal.submit(req(ChainStage.RAIL_DEPARTED, YARD, RAIL,
                       t + timedelta(hours=3), msg='J4'))


def test_journal_offline_resubmission_returns_same_event():
    journal = CustodyJournal()
    first = journal.submit(req(ChainStage.DISCHARGED, VESSEL, QUAY, T0, msg='J1'))
    again = journal.submit(req(ChainStage.DISCHARGED, VESSEL, QUAY,
                               T0 + timedelta(hours=8), msg='J1'))
    assert first.record_hash == again.record_hash
    assert len(journal.history(CONT)) == 1


def test_journal_recovery_blocks_final_state_when_intermediate_lost():
    journal = CustodyJournal()
    journal.submit(req(ChainStage.DISCHARGED, VESSEL, QUAY, T0, msg='J1'))
    journal.submit(req(ChainStage.IN_YARD, QUAY, YARD, T0 + timedelta(minutes=20), msg='J2'))
    journal.recover()
    # backfilling a departure without customs release is still impossible
    with pytest.raises(StateTransitionError):
        journal.submit(req(ChainStage.RAIL_DEPARTED, YARD, RAIL,
                           T0 + timedelta(hours=1), msg='J9'))
