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

`marine`, `cranes`, `yard`, `gate`, `customs`, `custody`, `reefer`, `hazmat`, `equipment`, `rail`, `billing`, `transshipment`, `planning`, `persistence`, `reporting`, and `api`.

## Custody handover chain

`custody` records the end-to-end chain of custody for a container as
append-only handovers: discharge (`vessel -> quay`), yard entry
(`quay -> yard`), customs disposition (`yard <-> customs`), yard moves
(`yard -> yard`), and rail or gate departure (`yard -> rail|truck`).

- every handover names the previous holder and must occur strictly later
  than the previous one, otherwise it is rejected;
- submissions carry a client `message_id`; a message replayed after an
  offline outage is accepted but applied at most once, and a replay whose
  payload changed is a conflict;
- a final departure state cannot be written while discharge, yard entry,
  or the customs release are missing; seizures and departures seal the
  chain.
