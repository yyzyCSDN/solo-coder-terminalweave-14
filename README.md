# TerminalWeave

TerminalWeave is a dependency-light Python engine for modelling day-to-day container-terminal operations. It covers marine access, berth use, quay cranes, yard placement, truck-gate appointments, customs holds, reefer monitoring, hazardous-goods segregation, equipment maintenance, rail blocks, demurrage billing and transshipment connections.

The package is intentionally usable without a database or web framework. Domain services expose ordinary Python APIs, while the persistence layer supplies an append-only event store and snapshots for embedding in larger systems.

## Quick start

```bash
python -m pip install -e ".[test]"
python -m terminalweave.cli demo
python -m pytest -q
```

Run the small HTTP adapter with `terminalweave serve`. It exposes `GET /health`,
`POST /tide/clearance`, and `POST /billing/demurrage`. The other packages are
library components intended to be composed by an embedding application.

## Design principles

- domain calculations are deterministic for identical inputs;
- time values are timezone-aware UTC datetimes;
- operational state transitions are explicit and validated;
- safety constraints fail closed when required information is absent;
- money uses `Decimal`;
- core domain modules depend only on the Python standard library.

## Main packages

`marine`, `cranes`, `yard`, `gate`, `customs`, `reefer`, `hazmat`, `equipment`, `rail`, `billing`, `transshipment`, `planning`, `persistence`, `reporting`, `api`, and `custody`.

## Container custody chain

The `custody` package models the end-to-end handover chain of a container —
discharge from vessel, yard entry, customs disposition, intra-yard moves, and
rail/gate departure — as an append-only, hash-linked ledger
(`terminalweave.custody.CustodyLedger`).

Every handover is validated before it is accepted:

- the chain must open with `discharged`, and stages follow a fixed topology;
- the submitting party must be the previous holder on the chain;
- timestamps must be strictly increasing (timezone-aware UTC);
- a final `rail_departed` / `gate_departed` state can only be written once the
  box entered the yard **and** customs released it — intermediate links cannot
  be backfilled; a seized box closes the chain.

Each record carries a client-generated `message_id`, so messages recovered
after an offline outage can be re-submitted freely: a duplicate returns the
stored handover and never transfers custody twice, while the same id carrying
different content is rejected. Records are SHA-256 chained (`prev_hash`), and
`replay_records` rebuilds chains from recovered messages, failing closed on a
gap or a rewritten link. `CustodyJournal` persists handovers to the
append-only event store and rebuilds ledger state via `recover()`.
